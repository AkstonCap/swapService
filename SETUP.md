# swapService Operator & Setup Guide

This document contains the full installation, configuration, architecture, security, and troubleshooting details for the USDC ↔ USDD bidirectional swap service. User-facing swap instructions now live in `README.md`.

## Contents
- Overview
- Architecture & Flow
- Prerequisites & API Access Requirements
- Installation
- Environment Configuration
- Solana Setup
- Nexus Setup & Asset Mapping
- Running & Operational Loops
- Fees & Economics
- Idempotency & State
- Performance & Polling Strategy
- Troubleshooting
- Pointers (Security / Config)

---
## Overview
A Python service that automates swaps between USDC (Solana) and USDD (Nexus). It enforces:
- Strict memo / asset mapping validation
- Automatic refunds on invalid input
- Idempotent sends (memo signatures & processed markers)
- Micro-amount DoS resistance (thresholds & fee-only treatment)
- Heartbeat asset updates (optional)

## Architecture & Flow
### USDC → USDD
1. User sends USDC to vault token account with memo `nexus:<NEXUS_USDD_ACCOUNT>`.
2. Service parses signature, validates memo & Nexus account.
3. Computes fees, mints / debits USDD to recipient.
4. Writes processed markers; refunds on invalid cases.

### USDD → USDC
1. User sends USDD to treasury.
2. User publishes or updates Nexus Asset containing `txid_toService` and `receival_account`.
3. Service polls treasury transactions; for each credit above threshold it queries assets by `txid_toService` & owner.
4. Valid mapping -> send USDC to receival account (ATA required). Missing mapping -> pending until timeout -> refund.
5. Micro credits below `MIN_CREDIT_USDD` treated as fees (aggregated fee-only entries).

### State & Database
- SQLite database (`swap_service.db`) for all state persistence.
- Tables: `processed_sigs`, `unprocessed_sigs`, `refunded_sigs`, `quarantined_sigs`, `processed_txids`, `unprocessed_txids`, `refunded_txids`, `quarantined_txids`, `fee_entries`, `fee_summary`, `attempts`, `reservations`, `counters`, `payouts`, `waterline_proposals`, `heartbeat`, `accounts`. Created and migrated automatically by `init_db()` at startup (WAL mode).
- Heartbeat asset optionally stores `last_poll_timestamp` and per-chain waterlines.

---

## Prerequisites & API Access Requirements

### System Requirements
- **Python**: 3.10+ (tested with 3.12 on Ubuntu 24.04.1)
- **pip**: Package manager for Python
- **Disk**: ~100MB for SQLite database and dependencies
- **Network**: Outbound HTTPS access to Solana RPC and (optionally) Helius API

### Solana RPC Access

The service requires a Solana RPC endpoint to poll for deposits, send USDC, and confirm transactions.

| Option | Rate Limits | Cost | Notes |
|--------|------------|------|-------|
| **Public RPC** (`api.mainnet-beta.solana.com`) | Heavily rate-limited (~40 req/10s per IP) | Free | Not recommended for production; may cause timeouts under load |
| **Helius** (`rpc.helius.xyz`) | Varies by plan (Free: 10 req/s) | Free tier available | Recommended — enriched RPC reduces API calls by 50-100x |
| **QuickNode / Alchemy / Triton** | Varies by plan | Paid | Alternative dedicated RPC providers |
| **Self-hosted** (Solana validator or RPC node) | No limits | Infrastructure cost | Best reliability; requires significant disk/RAM |

**Helius API (Recommended):** The service uses `getTransactionsForAddress` (a Helius-specific enriched RPC method) to fetch deposits with memos in 1-2 API calls instead of N+1 calls with core RPC. To enable:
1. Sign up at https://helius.dev and get an API key
2. Set `HELIUS_RPC_URL=https://rpc.helius.xyz/?api-key=YOUR_KEY` in `.env`
   - Or set `HELIUS_API_KEY=YOUR_KEY` and the URL is built automatically
3. If not configured, the service falls back to core Solana RPC (slower, more rate-limit sensitive)

**RPC Timeout Tuning:** If your RPC provider is slow or rate-limited, adjust these `.env` variables:
```env
SOLANA_RPC_TIMEOUT_SEC=8        # Per-call timeout (default 8s)
SOLANA_TX_FETCH_TIMEOUT_SEC=12  # Per getTransaction timeout (default 12s)
SOLANA_POLL_TIME_BUDGET_SEC=15  # Total time budget per poll cycle (default 15s)
```

### Solana CLI and SPL Token CLI

Required for initial setup (keypair creation, token account creation). Not required at runtime.

**Installation:**
- Linux/macOS: `sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"`
- Windows: See https://docs.solana.com/cli/install-solana-cli-tools#windows
- SPL Token CLI: `cargo install spl-token-cli` or install via the Solana tool suite

**Verify:**
```bash
solana --version      # Should be 1.16+ or 2.x
spl-token --version   # Should be 3.x+
```

### Nexus Node & CLI Access

The service invokes the Nexus CLI binary as a subprocess for all Nexus operations (debits, asset queries, heartbeat updates).

**Requirements:**
1. **Nexus daemon running** — The CLI connects to a local Nexus daemon. Ensure the daemon is started and synced.
2. **API server enabled** — The daemon must have its API server active. Configure in `nexus.conf`:
   ```
   # Option A: Disable authentication (local/trusted networks only)
   apiauth=0

   # Option B: Set API credentials (recommended for remote access)
   apiuser=your_api_user
   apipassword=your_api_password
   ```
   Without either setting, the API server will not start and the CLI will not work.
3. **Active session** — The service uses `pin=<PIN>` in CLI commands, which requires an active session. Create one before starting the service:
   ```bash
   ./nexus sessions/create/local username=<YOUR_USER> password=<YOUR_PASS> pin=<YOUR_PIN>
   ```
   The session must remain active while the service runs. If the daemon restarts, re-create the session.
4. **CLI binary accessible** — Set `NEXUS_CLI_PATH` in `.env` to the path of the CLI binary:
   ```env
   NEXUS_CLI_PATH=./nexus       # Relative (from repo root)
   NEXUS_CLI_PATH=/usr/bin/nexus # Absolute
   ```
   On Linux/macOS, ensure it's executable: `chmod +x ./nexus`

**Nexus CLI Timeout:** If the CLI is slow (e.g., large transaction history), increase:
```env
NEXUS_CLI_TIMEOUT_SEC=20  # Default 20s; increase for slow nodes
```

### Nexus Account Setup

The service operator must have:
1. **A Nexus signature chain** (profile) with the USDD token created or available
2. **A USDD treasury account** — receives user USDD deposits
3. **A USDD local account** (optional) — for micro credit handling
4. **A USDD quarantine account** (optional) — for failed refund isolation
5. **A USDD fees account** (optional) — for fee accounting

The service performs `finance/debit/token from=USDD` to mint USDD from the token supply to recipients. This requires the service's signature chain to be the USDD token creator/owner.

---

## Installation
Requirements: Python 3.10+, pip, Solana CLI (for initial setup), Nexus CLI.

```bash
# Create virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
python3 -m pip install -r requirements.txt
```

Ubuntu 24.04.1 build prerequisites (if native wheels unavailable):
```bash
sudo apt update
sudo apt install -y build-essential pkg-config libssl-dev python3-venv
```

## Environment Configuration
Copy `.env.example` to `.env` then fill required variables.

```bash
cp .env.example .env
nano .env  # Edit and fill in values
```

Key required:
- `SOLANA_RPC_URL` — Solana RPC endpoint (or Helius RPC URL)
- `VAULT_KEYPAIR` — Path to vault keypair JSON file
- `VAULT_USDC_ACCOUNT` — Vault's USDC token account (ATA) address
- `USDC_MINT` — USDC mint address (mainnet: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`)
- `SOL_MINT` — Native SOL mint (default okay: `So11111111111111111111111111111111111111112`)
- `SOL_MAIN_ACCOUNT` — Vault wallet address (base account, not token account)
- `NEXUS_PIN` — PIN for the Nexus signature chain
- `NEXUS_USDD_TREASURY_ACCOUNT` — Nexus USDD treasury account address

Also required in practice:
- `NEXUS_HEARTBEAT_ASSET_NAME` — without the heartbeat asset the Solana poller cannot start

Optional but recommended:
- `HELIUS_RPC_URL` or `HELIUS_API_KEY` — optimized Solana deposit polling (1-2 calls vs N+1)
- `ALERT_WEBHOOK_URL` or `ALERT_COMMAND` — **without one, safety alerts only reach stdout**
- `NEXUS_USDD_QUARANTINE_ACCOUNT` / `USDC_QUARANTINE_ACCOUNT` — isolating failed refunds
- `MAX_SWAP_USDC` / `MAX_SWAP_USDD` / `DAILY_PAYOUT_CAP_USDC` — exposure caps (0 = disabled)
- `SOLANA_DEPOSIT_COMMITMENT` — leave at `finalized`; `confirmed` can be reorged after you have minted

Optional chain-specific intervals: `SOLANA_POLL_INTERVAL`, `NEXUS_POLL_INTERVAL`.

## Choosing the token pair

This is a general Nexus↔Solana bridge; the USDC/USDD pairing is just the default. Pick any
Solana SPL token and any Nexus token:

```env
SOLANA_TOKEN_MINT=<mint address of the Solana-side token>
SOLANA_VAULT_ACCOUNT=<your SPL token account (ATA) for that mint>
SOLANA_TOKEN_SYMBOL=USDC          # display only
SOLANA_TOKEN_DECIMALS=6

NEXUS_TOKEN_NAME=USDD             # the Nexus token this service mints/debits
NEXUS_TOKEN_DECIMALS=6
NEXUS_USDD_TREASURY_ACCOUNT=<your Nexus treasury account for that token>

DEPOSIT_MEMO_PREFIX=nexus:        # memo users put on their Solana transfer
```

Leave `MIN_DEPOSIT_USDC` / `MIN_CREDIT_USDD` / `DUST_CREDIT_USDD` **blank** unless you have
a reason: they then derive from your flat fee, which is correct in any denomination. A
hardcoded `0.2` would mean 0.2 BTC on a wBTC bridge.

The legacy names (`VAULT_USDC_ACCOUNT`, `USDC_MINT`, `USDC_DECIMALS`) still work, so
existing `.env` files need no changes.

> **Note on internal naming.** Source identifiers say `solana` and `nexus`, not `usdc`
> and `usdd` — `send_solana_token()`, `poll_nexus_deposits()`, `MIN_DEPOSIT_SOLANA_UNITS`.
> Three things deliberately keep the original spelling, because in each case the name is
> not a code identifier but a value that already exists outside the process:
>
> | Kept as-is | Example | Why |
> |---|---|---|
> | Environment variables and the config attributes mirroring them | `VAULT_USDC_ACCOUNT` | Your `.env` already sets them; generic aliases exist alongside |
> | State-database column names | `amount_usdc_units` | Renaming needs an `ALTER TABLE` over live fund records |
> | Persisted row values with a safety property | the retry-budget keys, the debit reservation kind, the `USDD_STATUS_*` strings | A rename makes an in-flight swap written by the previous build invisible to the new one, which could re-debit it |
>
> The rationale is recorded in the header block of `src/state_db.py`, and
> `tests/legacy_frozen_names.py` is enforced through the isolated pytest suite and fails the build if any of them drifts.

## Solana Setup

Creates the service's Solana keypair and the token account (ATA) that holds vault liquidity.

**1. Create the vault keypair**
```bash
solana-keygen new -o ./vault-keypair.json
chmod 600 ./vault-keypair.json          # this key controls the entire vault
solana config set -k ./vault-keypair.json -u https://api.mainnet-beta.solana.com
solana address                          # -> set as SOL_MAIN_ACCOUNT in .env
```
Fund this address with SOL for transaction fees.

**2. Create the vault USDC token account (ATA)**
```bash
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
# Devnet mint instead: 4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU
```
The printed token account address goes in `.env` as `VAULT_USDC_ACCOUNT`.
**It is a token account, not a wallet address** — a common misconfiguration.

**3. Create the USDC quarantine account** (strongly recommended)
```bash
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v --owner $(solana address)
```
Set as `USDC_QUARANTINE_ACCOUNT`. Without it, funds from failed refunds have nowhere to go.

**4. Fund the vault with USDC** — transfer to `VAULT_USDC_ACCOUNT`. This is the liquidity
that backs outgoing USDD→USDC swaps; the service pauses new swaps if it falls below
`BACKING_DEFICIT_PAUSE_PCT`% of circulating USDD.

**5. Verify**
```bash
spl-token accounts --owner "$(solana address)"
spl-token balance <VAULT_USDC_ACCOUNT>
```

## Nexus Setup & Asset Mapping

**1. Daemon + session**
```bash
# nexus.conf must have apiauth=0 (local only) or apiuser/apipassword set
./nexus sessions/create/local username=<USER> password=<PASS> pin=<PIN>
```
The session must stay active while the service runs; re-create it if the daemon restarts.

**Single-user vs multiuser nodes.** If `nexus.conf` has `multiuser=1`, the login response
returns a **session id** that must accompany every user-scoped API call. Set both:

```env
NEXUS_MULTIUSER=true
NEXUS_SESSION=<session id from sessions/create/local>
```

If your node is single-user (`multiuser=0`, the default), leave `NEXUS_MULTIUSER=false`.
The session must **not** be sent on a single-user node — the API rejects it — so the
service adds or omits it automatically from this one flag; you never pass `session=`
by hand.

Which calls are affected, per the bundled API docs:

| API family | Session in multiuser mode | Used by |
|------------|---------------------------|---------|
| `finance/*` | **Required** | USDD debits, refunds, supply and balance reads |
| `assets/*` | **Required** | Heartbeat create/read/update |
| `market/*` | **Required** | Optional DEX fee conversions |
| `register/*` | Not used | Deposit scanning, asset mapping lookups, account validation |

The service validates this at startup and refuses to proceed quietly if
`NEXUS_MULTIUSER=true` with an empty `NEXUS_SESSION` — otherwise every debit, refund and
heartbeat update would fail and it would look like a total Nexus outage.

> The session id is a credential: combined with the PIN it authorises spending. It is
> redacted from logs and alerts, but like the PIN it is passed as a CLI argument and is
> therefore visible to local users via `ps`. See [SECURITY.md](docs/SECURITY.md).

**2. Accounts**

| Account | `.env` variable | Required | Purpose |
|---------|-----------------|----------|---------|
| USDD treasury | `NEXUS_USDD_TREASURY_ACCOUNT` | **Yes** | Receives user USDD; pays USDD refunds |
| USDD quarantine | `NEXUS_USDD_QUARANTINE_ACCOUNT` | Strongly recommended | Receives USDD from exhausted refunds. **If unset the USDD stays in the treasury and keeps counting toward the backing ratio**, overstating your reserves |
| USDD local | `NEXUS_USDD_LOCAL_ACCOUNT` | Optional | Micro-credit handling |
| USDD fees | `NEXUS_USDD_FEES_ACCOUNT` | Optional | Fee accrual target for the backing reconcile |

```bash
./nexus finance/create/account name=usddTreasury token=USDD pin=<PIN>
./nexus finance/create/account name=usddQuarantine token=USDD pin=<PIN>
```
The service mints with `finance/debit/token from=USDD`, so **its signature chain must own
the USDD token**.

**3. Register the bridge on-chain — REQUIRED, not optional**

The registration asset is both the service's **public description** and its
**proof of life**. One asset declares the token pair, the vault and treasury that back
it, the current fees and minimums, the deposit memo format, and a `last_poll_timestamp`
the service refreshes every cycle. A user or auditor can read it and know what the bridge
does, what it will charge, and whether it is online right now.

```bash
python3 register_service.py --show                    # what will be published, from .env
python3 register_service.py --create --dry-run        # preview, spends nothing
python3 register_service.py --create --name myBridgeHeartbeat
python3 register_service.py --inspect myBridgeHeartbeat   # verify, or read someone else's
```

Set `SERVICE_PROVIDER`, `SERVICE_CONTACT` and the token-pair variables before creating —
`format=basic` fixes the field set permanently, so an incomplete record means creating a
new asset (another ~1 NXS). `--show` prints the record and its size against the register
budget; `--create` refuses if the name already exists or the record is oversized.

Published fields: `distordiaType`, `provider`, `contact`, `version`, `memo_prefix`,
`nexus_token`, `nexus_treasury_address`, `solana_token`, `solana_vault_address`,
`solana_vault_mint`, `fee_flat_to_nexus`, `fee_flat_to_solana`, `fee_bps`, `min_to_nexus`,
`min_to_solana`, `status`, `last_poll_timestamp` and both waterlines. The service rewrites
the mutable subset (status, terms, liveness) each cycle, so the on-chain terms stay
truthful if you change your fees.

**3b. Legacy heartbeat-only asset**

The Solana poller reads its waterline from this asset. On a fresh install there is no
asset and no local fallback, so **`poll_solana_deposits()` returns immediately and no USDC
deposit is ever ingested.** Create it before first start:

```bash
python3 create_heartbeat_asset.py --name distordiaBridgeHeartbeat --dry-run   # preview, spends nothing
python3 create_heartbeat_asset.py --name distordiaBridgeHeartbeat            # ~1 NXS, asks to confirm
```
Then set `NEXUS_HEARTBEAT_ASSET_NAME` in `.env` — **the service resolves the asset by NAME**,
so an asset created without `--name` is unreachable.

`format=basic` fixes the field set at creation: an asset missing a field the service writes
makes **every** heartbeat update fail atomically, freezing both waterlines. The service
validates this at startup and prints the field names the asset actually has. Keep
`HEARTBEAT_WATERLINE_NEXUS_FIELD` / `_SOLANA_FIELD` matching the asset.

**4. User-side asset mapping** — for USDD→USDC, users publish an asset with
`txid_toService` + `receival_account`; the service matches on (txid, owner). See
[`ASSET_STANDARD.md`](ASSET_STANDARD.md) and the user guide in [`README.md`](README.md).

## Running

**Pre-flight check** — run before first start and after any config change:
```bash
python -m pytest -q                 # complete composable suite
# Individual isolated legacy checks:
python -m pytest -q tests/test_legacy_scripts.py
```
`test_smoke` catches configuration and schema faults that a syntax check cannot.
`test_frozen_names` is the one to run after **any** refactor that touches naming: it fails
if a database column, a retry-budget key, a reservation kind or a lifecycle status string
has drifted, all of which would break an upgrade over a database with swaps in flight.

**Start**
```bash
python3 swapService.py
```
Startup prints, in order: the singleton lock, heartbeat validation, any minimums that were
raised above their fee, a warning if no alert channel is configured, vault/treasury
balances, recovery results — then begins polling. **Read these lines**; each is a
pre-flight result.

Only one instance may run per database — an exclusive `flock` (override with
`SWAP_LOCK_PATH`) refuses a second start, because two instances can double-spend.

**Production supervision (systemd)**
```ini
[Unit]
Description=USDC/USDD swap service
After=network-online.target

[Service]
Type=simple
User=swapsvc
WorkingDirectory=/opt/swapService
EnvironmentFile=/opt/swapService/.env
ExecStart=/opt/swapService/.venv/bin/python3 swapService.py
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```
Keep `.env` at mode `600` owned by the service user; it holds the Nexus PIN.

**Verify the first swap end-to-end** (do this on devnet before mainnet):
1. Send the minimum USDC with memo `nexus:<YOUR_USDD_ACCOUNT>`.
2. Watch for `[metrics]` and a row in `unprocessed_sigs` moving
   `ready for processing` → `debit in flight` → `debited, awaiting confirmation`.
3. Confirm the USDD arrives, and that `processed_sigs` shows `debit_confirmed`.
4. Repeat the other direction using the asset mapping.
5. Deliberately send an invalid memo and confirm the refund path completes.

**Set up alerting before going live.** Without `ALERT_WEBHOOK_URL` or `ALERT_COMMAND`,
backing-deficit pauses, unbacked-mint discrepancies and halted pollers only ever reach
stdout. See [CONFIG.md § Exposure Caps & Alerting](CONFIG.md).

## Operator Dashboard

A read-only web UI for tracking transactions, issues, vault balances and the backing ratio.
Runs as a **separate process** from the swap service.

```bash
python3 dashboard.py            # http://127.0.0.1:8787
```

Remote access — keep the localhost bind and tunnel:
```bash
ssh -L 8787:127.0.0.1:8787 operator@your-host
```

| Variable | Default | Notes |
|----------|---------|-------|
| `DASHBOARD_HOST` | `127.0.0.1` | Binding anything else **requires** `DASHBOARD_TOKEN`; the service refuses otherwise |
| `DASHBOARD_PORT` | `8787` | |
| `DASHBOARD_TOKEN` | unset | Bearer token (`Authorization: Bearer …`, or `?token=`) |

**What it shows**
- Backing ratio, vault USDC, circulating USDD, fees collected, 24h payouts against the cap
- A paused banner during a backing deficit, and a stale banner if the service stops writing metrics
- **Issues** — everything needing a human: unverified debits, pending refunds, quarantined
  items, USDD quarantined but *not moved*, and actions that exhausted their retry budget
- Transaction tabs per direction: pending, completed, refunded, quarantined, payouts, fees

**Security properties** (enforced, and covered by the isolated dashboard test case in `tests/test_legacy_scripts.py`)
- Opens the database with SQLite `mode=ro`, so it *cannot* write state or hold a write lock.
- No mutating endpoints. There is deliberately no retry/refund/release button — a web
  endpoint that can move funds is a much larger attack surface than one that can only look.
  Manual intervention stays on the CLI.
- Needs **no** vault keypair, Nexus PIN or RPC key: balances come from the `metrics_snapshot`
  row the service writes each cycle.
- Deposit memos are attacker-controlled and are rendered in the operator's browser, so all
  values are delivered as JSON and inserted with `textContent`; the page never uses
  `innerHTML`. A strict CSP, `nosniff` and `no-referrer` are sent on every response.
- Stdlib `http.server` only — no new dependencies on a custodial service.

Run the tests before exposing it:
```bash
python -m pytest -q tests/test_legacy_scripts.py
```

## Fees & Economics

### Fee Direction Map

| Fee Variable | Direction Applied | Default | Description |
|-------------|-------------------|---------|-------------|
| `FLAT_FEE_USDC` | USDD→USDC (deducted from USDC output) | 0.5 | Flat fee when user receives USDC |
| `FLAT_FEE_USDD` | USDC→USDD (deducted from swap amount) AND USDC refunds | 0.1 | Flat fee when user receives USDD, also applied to USDC refunds |
| `DYNAMIC_FEE_BPS` | Both directions | 10 (0.1%) | Percentage-based fee on swap amount |
| `MIN_DEPOSIT_USDC` | USDC→USDD | 0.2 | Minimum USDC to process (= 2x flat fee; below = 100% fee, no refund) |
| `MIN_CREDIT_USDD` | USDD→USDC | 1.0 | Minimum USDD to process (= 2x flat fee; below = 100% fee, recorded) |
| `DUST_CREDIT_USDD` | USDD→USDC | 0.01 | Below this a credit is ignored entirely (no record) |

> **Note on naming:** `FLAT_FEE_USDC` is the fee applied when the *output* is USDC (USDD→USDC path), not when the *input* is USDC. Similarly, `FLAT_FEE_USDD` is applied when the output is USDD (USDC→USDD path).

## Idempotency & State
- Solana: memo uniqueness + processed_sigs cache; pre-send crash recovery scans for memo.
- USDC→USDD debits persist a unique `reference` BEFORE the debit; an unclear CLI response is resolved against the chain (`resolve_unverified_debits`), never assumed to be a failure.
- Nexus: asset mapping search by txid + owner, processed markers, refund attempt state.
- References: integer counters used internally (not user-facing) for audit.

## Performance & Polling Strategy
- Separate intervals: fast Solana (12–20s), Nexus aligned to ~block time (50–60s) to reduce empty polls.
- Per-loop caps: `SOLANA_MAX_TX_FETCH_PER_POLL`, `MAX_DEPOSITS_PER_LOOP`, `MAX_CREDITS_PER_LOOP`.
- Micro aggregation reduces write amplification.
- Future: optional WebSocket subscription to cut signature polls.

## Troubleshooting (Highlights)
See also `SECURITY.md` for security incidents & hardening.

Missing asset mapping: ensure asset includes both `txid_toService` and `receival_account` before timeout.
High RPC usage: increase `SOLANA_POLL_INTERVAL`, reduce max fetch caps, or enable delta skip. Consider using Helius for enriched RPC.
Stalled waterline: investigate unprocessed rows with old timestamps; they may be quarantined or awaiting mapping.
Refund loop failures: query `quarantined_sigs` and `quarantined_txids` tables; cross-check on-chain balances.
Nexus CLI errors: verify the daemon is running, a session is active, and `apiauth=0` is set.
RPC timeouts: increase `SOLANA_RPC_TIMEOUT_SEC` or switch to a dedicated RPC provider.

## Pointers
- Full security guidance: `SECURITY.md`
- Exhaustive configuration reference: `CONFIG.md`
- User swap instructions: `README.md`
- Initiator state machines: `SWAP_INITIATOR_STATE_MACHINES.md`
- Server-side state machines: `STATE_MACHINES.md`
- Operator dashboard: `python3 dashboard.py` (see above)
- Audit findings: `AUDIT_FINDINGS.md`

## Appendix: Configuration Variables
See `.env.example` for the exhaustive, annotated list. Highlights:
- Heartbeat: HEARTBEAT_ENABLED, *_WATERLINE_* fields
- Backing management: BACKING_* vars
- Poll time budgets: *_POLL_TIME_BUDGET_SEC
- Adaptive (future extension): SKIP_OWNER_LOOKUP_FOR_MICRO_USDD

---
LICENSE: Provided as-is; no warranty.
