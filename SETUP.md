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
- Tables: `processed_sigs`, `unprocessed_sigs`, `refunded_sigs`, `quarantined_sigs`, `processed_txids`, `unprocessed_txids`, `fee_entries`, `attempts`, `waterline_proposals`.
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

Optional but recommended:
- `HELIUS_RPC_URL` or `HELIUS_API_KEY` — For optimized Solana deposit polling
- `NEXUS_USDD_QUARANTINE_ACCOUNT` — For isolating failed refunds
- `USDC_QUARANTINE_ACCOUNT` — Self-owned USDC token account for quarantine

Optional chain-specific intervals: `SOLANA_POLL_INTERVAL`, `NEXUS_POLL_INTERVAL`.

## Solana Setup
1. Create vault keypair.
2. Create USDC ATA for vault.
3. Fund SOL for fees + initial USDC liquidity.
4. (Optional) Quarantine account for failed refunds.

See `README.md` "Set up Solana accounts" section for step-by-step commands.

## Nexus Setup & Asset Mapping
- Start Nexus daemon with API enabled (`apiauth=0` or credentials configured).
- Create a session: `./nexus sessions/create/local username=<USER> password=<PASS> pin=<PIN>`
- Treasury account (USDD) configured as `NEXUS_USDD_TREASURY_ACCOUNT`.
- (Optional) Create heartbeat asset — see `ASSET_STANDARD.md` for the provider heartbeat specification, or use `create_heartbeat_asset.py`.
- For USDD→USDC swaps, users publish assets with mapping fields; service uses `register/list/assets` queries.

## Running
```bash
python3 swapService.py
```
Startup prints vault / treasury balances, recovery results, and begins polling.

## Fees & Economics

### Fee Direction Map

| Fee Variable | Direction Applied | Default | Description |
|-------------|-------------------|---------|-------------|
| `FLAT_FEE_USDC` | USDD→USDC (deducted from USDC output) | 0.5 | Flat fee when user receives USDC |
| `FLAT_FEE_USDD` | USDC→USDD (deducted from swap amount) AND USDC refunds | 0.1 | Flat fee when user receives USDD, also applied to USDC refunds |
| `DYNAMIC_FEE_BPS` | Both directions | 10 (0.1%) | Percentage-based fee on swap amount |
| `MIN_DEPOSIT_USDC` | USDC→USDD | 0.100101 | Minimum USDC to process (below = 100% fee) |
| `MIN_CREDIT_USDD` | USDD→USDC | 0.500501 | Minimum USDD to process (below = 100% fee) |

> **Note on naming:** `FLAT_FEE_USDC` is the fee applied when the *output* is USDC (USDD→USDC path), not when the *input* is USDC. Similarly, `FLAT_FEE_USDD` is applied when the output is USDD (USDC→USDD path).

## Idempotency & State
- Solana: memo uniqueness + processed_sigs cache; pre-send crash recovery scans for memo.
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
- Audit findings: `AUDIT_FINDINGS.md`

## Appendix: Configuration Variables
See `.env.example` for the exhaustive, annotated list. Highlights:
- Heartbeat: HEARTBEAT_ENABLED, *_WATERLINE_* fields
- Backing management: BACKING_* vars
- Poll time budgets: *_POLL_TIME_BUDGET_SEC
- Adaptive (future extension): USE_NEXUS_WHERE_FILTER_USDD, SKIP_OWNER_LOOKUP_FOR_MICRO_USDD

---
LICENSE: Provided as-is; no warranty.
