# Copilot Instructions: swapService

## Project Overview
Bidirectional USDC ↔ USDD swap bridge between Solana (USDC) and Nexus blockchain (USDD). Service monitors deposits on both chains to treasury accounts, validates mapping metadata, applies fees, executes cross-chain transfers, and handles refunds with idempotency guarantees.

## Architecture

### Core Components
- **[src/main.py](../src/main.py)**: Main polling loop, orchestrates both swap directions with separate intervals
- **[src/swap_solana.py](../src/swap_solana.py)**: USDC→USDD pipeline (vault deposits with memo parsing)
- **[src/swap_nexus.py](../src/swap_nexus.py)**: USDD→USDC pipeline (asset-mapped receival accounts)
- **[src/state_db.py](../src/state_db.py)**: SQLite state management (processed/unprocessed/refunded sets)
- **[src/solana_client.py](../src/solana_client.py)**: Solana RPC wrapper with Helius enrichment support
- **[src/nexus_client.py](../src/nexus_client.py)**: Nexus CLI subprocess wrapper
- **[src/startup_recovery.py](../src/startup_recovery.py)**: Waterline-based disaster recovery from heartbeat asset

### Data Flow
**USDC → USDD**: User sends USDC to vault with memo `nexus:<USDD_ACCOUNT>` → Service validates memo, checks account exists → Applies fees → Debits USDD to recipient via Nexus CLI → Marks processed  
**USDD → USDC**: User sends USDD to treasury → Publishes Nexus asset with `txid_toService` + `receival_account` → Service queries assets by (txid, owner) → Validates Solana receival account → Sends USDC → Marks processed  
**Refunds**: Invalid memo/mapping, missing ATA, or timeout triggers refund to original sender (minus fees)

### State Management Pattern
- **Append-only DB tables**: `processed_sigs`, `processed_txids`, `unprocessed_sigs`, `unprocessed_txids`, `refunded_sigs`, `quarantined_sigs`
- **Idempotency**: Transaction signatures/txids are primary keys; duplicate processing prevented by DB constraints
- **Waterlines**: Heartbeat asset stores `last_safe_timestamp_solana` and `last_safe_timestamp_usdd` to bound historical reprocessing after restart
- **Attempt tracking**: Retry counter with exponential cooldown in `attempt_state` table (see `MAX_ACTION_ATTEMPTS`, `ACTION_RETRY_COOLDOWN_SEC`)

## Critical Conventions

### Configuration System
All config lives in [src/config.py](../src/config.py) loading from `.env`. Use `getattr(config, "VAR_NAME", default)` for optional vars. Required vars validated at startup (see `REQUIRED_ENV` list). Chain-specific poll intervals: `SOLANA_POLL_INTERVAL` (fast, 12-20s) vs `NEXUS_POLL_INTERVAL` (aligned to block time, 50-60s).

### Logging Convention
Structured, single-line prints with prefix: `[SWAP_USDC]`, `[USDD_PROCESS_START]`, `[WATERLINE_ADVANCED]`. Use `_log(kind, **fields)` helpers (not traditional logger). Critical: Never log `NEXUS_PIN` or private keys.

### Fee Calculation
Applies to both directions: `FLAT_FEE_USDC_UNITS` + `DYNAMIC_FEE_BPS` (basis points of amount). Micro deposits below thresholds (`MIN_DEPOSIT_USDC`, `MIN_CREDIT_USDD`) treated as 100% fee (`MICRO_DEPOSIT_FEE_PCT`). See [src/fees.py](../src/fees.py) for aggregation.

### Nexus CLI Interaction Pattern
Subprocess calls with timeout protection (see `_run()` in [nexus_client.py](../src/nexus_client.py)). Use `_parse_json_lenient()` for resilient CLI output parsing. Always inject PIN via CLI args, never environment. Asset queries use `register/list/assets` with `where` filters for `txid_toService` + owner validation.

### Solana Signature Scanning
Prefer Helius enhanced RPC (`fetch_incoming_usdc_deposits_via_helius`) over raw `getSignaturesForAddress` to batch fetch memos efficiently. **Performance**: Helius uses 1-2 API calls vs N+1 for core RPC (50-100x faster for 100 deposits). Automatic fallback to core RPC when Helius unavailable. See [solana_client.py](../src/solana_client.py#L179-L350). Memos format: `nexus:<NEXUS_ADDRESS>` or `refundSig:<ORIGINAL_SIG>`.

### Startup Recovery Flow
On every service start: fetch waterlines from heartbeat asset → rebuild missing unprocessed entries from on-chain history → scan sent transaction memos for `processedTxid:` and `refundSig:` patterns → seed reference counter if missing. Fully idempotent; safe to run multiple times. See [startup_recovery.py](../src/startup_recovery.py).

### USDD credit transaction json format (on Nexus)
Example of an incoming token transaction (credit) json element on Nexus, picked from `finance/transactions/account name=<account/token>` API output:
```json
{
  "txid": "0158ff8567753c244555bba8083f3285fa5150745879a7634f85f16178959ef825b1ab33328c1bc319cd18f2effbaa57981d27xxaf2bfa99a9988dd24da9d1e1",
  "type": "tritium user",
  "version": 4,
  "sequence": 5,
  "timestamp": 1764967240,
  "blockhash": "c80095c84743cabee3353e4e9590eaxx177c2aca67cb2ee0bc565479bb72e65b6eeca67e62b0d82969ab321eaadf8bd4e80aa448fb95c79ab7729e1b3e3cfc9c0a6b74ea3ec82eb52804df059bf020cf4a1359aed9104f0f2f083e57a3ef5735883f44916d86c1e312adb88fcfe3d17efa698538eb7ccba6f43b55d788cb857a",
  "confirmations": 41277,
  "contracts": [
    {
      "id": 0,
      "OP": "CREDIT",
      "for": "DEBIT",
      "txid": "01b88ff8707638acff63e05ca48dec9c79d5b9d754b065ae8f35e0b6cb8b90c694b54ddfeee934e87b257e028c81cdf1dxx328ca881cbd185bddd12dd9097c46",
      "contract": 0,
      "from": {
        "address": "8BsvE6DAsRD1j1DpHfRNW3xxKB1srkAMQABVJd39MQeguoVBK2U",
        "name": "DISTxx",
        "local": true,
        "mine": false,
        "type": "ACCOUNT"
      },
      "to": {
        "address": "8CuyRASoeBCRgcuA56Awyixxf34vRad5kB9b9H88bUVSJGfB5B7",
        "name": "distxx",
        "user": "xxxxx",
        "local": true,
        "mine": true,
        "type": "ACCOUNT"
      },
      "amount": 1.0,
      "token": "8DgWXw9dV9BgVNQpKwNZ3zJRbU8SKxjW4j1v9fn8Yma7HihMjeA",
      "ticker": "DIST"
    }
  ]
}
```

## Developer Workflows

### Running Service
```bash
python swapService.py  # Delegates to src.main.run()
```
Startup prints: vault/treasury balances, recovery stats, then enters dual polling loop.

### Testing Locally
Use `sol_testclient.py` for Solana deposit simulation. No automated test suite; testing is manual + mainnet observation. Before deploying changes, test with minimal poll intervals and watch logs for new prefixes.

### Adding New Config Variable
1. Add to `.env.example` with comment
2. Load in [src/config.py](../src/config.py) with `os.getenv()` and type conversion
3. Document in [CONFIG.md](../CONFIG.md) table
4. Add validation in `REQUIRED_ENV` if mandatory

### Modifying State Schema
1. Update table schema in `state_db.init_db()` with `CREATE TABLE IF NOT EXISTS`
2. SQLite auto-migration via IF NOT EXISTS; no explicit migration files
3. Add corresponding query/insert functions in [state_db.py](../src/state_db.py)
4. Update [STATE_DB_MIGRATION.md](../STATE_DB_MIGRATION.md) if breaking existing state

### Debugging Stalled Swaps
1. Check heartbeat asset for waterline staleness: `nexus register/get/asset address=<HEARTBEAT_ASSET_ADDRESS>`
2. Query unprocessed tables: `sqlite3 swap_service.db "SELECT * FROM unprocessed_sigs WHERE status='pending';"`
3. Check refund/quarantine tables for failed attempts
4. Inspect JSONL append logs: `failed_refunds.jsonl`, `fee_events.jsonl`

## Security & Safety

### Idempotency Guarantees
- **Solana sends**: Memo contains unique reference (`processedTxid:<txid>` or `refundSig:<sig>`); startup recovery scans memos to rebuild processed sets
- **Nexus debits**: Transaction inclusion checked via `finance/history` with txid confirmation tracking
- **Asset mapping**: Owner validation (sender's signature chain must match asset owner) prevents txid hijacking

### DoS Mitigation
Micro deposits (< `MIN_DEPOSIT_USDC`) aggregated as fees without full processing (`MICRO_DEPOSIT_FEE_PCT=100`). Skip expensive owner lookups for micro USDD credits (`SKIP_OWNER_LOOKUP_FOR_MICRO_USDD=true`). Per-loop caps: `MAX_DEPOSITS_PER_LOOP`, `MAX_CREDITS_PER_LOOP`.

### Backing Management
Optional backing ratio alerts: `BACKING_DEFICIT_BPS_ALERT`, `BACKING_DEFICIT_PAUSE_PCT`. Service pauses swaps if USDC vault < issued USDD by threshold. See [balance_reconciler.py](../src/balance_reconciler.py) for account-level reconciliation.

### File Permissions
Vault keypair must be mode 600. State DB and `.env` should be readable only by service user. Never commit `.env` or `*.json` state files.

## Common Patterns

### Timeout Protection
All external operations wrapped with time budgets: `SOLANA_POLL_TIME_BUDGET_SEC`, `NEXUS_CLI_TIMEOUT_SEC`. Use `_safe_call(fn, timeout_sec=5)` or thread-based watchdog (see [main.py](../src/main.py#L12-L28)).

### Decimal Handling
Use `Decimal` for token amounts, never floats. Convert to base units (multiply by `10**DECIMALS`) before storing. Format with `ROUND_DOWN` to prevent dust overflows. See `_parse_decimal_amount()` and `_format_token_amount()` in [swap_nexus.py](../src/swap_nexus.py).

### Status Lifecycle Transitions
USDC→USDD: `pending` → `validated` → `debit_sent` → `debit_confirmed` → `processed`  
USDD→USDC: `pending_receival` → `ready for processing` → `sending` → `sig created, awaiting confirmations` → `processed`  
Failures: `refund pending` → `refunded` or `quarantined` after max attempts

### Reading Documentation Files
- User-facing swap guide: [README.md](../README.md)
- Operator setup: [SETUP.md](../SETUP.md)
- Full config reference: [CONFIG.md](../CONFIG.md)
- Security hardening: [SECURITY.md](../SECURITY.md)
- Nexus API patterns: `Nexus API docs/` directory

## Integration Points

### Solana Dependencies
- **solana-py** (v0.36.9) + **solders** (v0.26.0): RPC client + transaction building
- **Helius RPC**: Optional enhanced API for batch transaction enrichment with memos (`parseTransactions` endpoint)
- **SPL Token Program**: ATA derivation and USDC transfers

### Nexus Dependencies
- **Nexus CLI**: External binary (`./nexus`) invoked via subprocess; expects `--pin=<PIN>` on privileged operations
- **Asset mapping**: Uses `register/list/assets` with WHERE filters, `register/get/asset` for heartbeat
- **Finance operations**: `finance/debit/token` for USDD transfers, `finance/history` for confirmation checks

### External RPC Requirements
- Solana: Stable mainnet RPC with `getSignaturesForAddress`, `getTransaction`, `getAccountInfo` support. Rate limits respected via `SOLANA_RPC_TIMEOUT_SEC`.
- Nexus: Local node or gateway at `NEXUS_RPC_HOST` (default `http://127.0.0.1:8399`)
