# USDC ↔ USDD Bidirectional Swap Service

User-facing guide for performing swaps between USDC (Solana) and USDD (Nexus).

Operator / setup documentation: see **`SETUP.md`**.  
Security hardening: **`SECURITY.md`**.  
Configuration reference: **`CONFIG.md`**.

---

## Quick Overview
The service lets you swap:
- USDC → USDD: Send USDC to the service vault with a memo that specifies a Nexus (USDD) account.
- USDD → USDC: Send USDD to the treasury and publish an asset mapping your USDD transaction `txid` to a Solana receival account.

Thresholds & Fees (defaults – operator may change):
- Minimum swap amount both directions: `0.100101` of the source token (smaller = treated as fees / ignored).
- Flat fee (USDC path) & dynamic fee (bps) may apply as configured.

---

## How to swap USDD for USDC

### USDC->USDD

Send USDC from a solana wallet which allow memos in the following format:

- Send to: `Bg1MUQDMjAuXSAFr8izhGCUUhsrta1EjHcTvvgFnJEzZ`
- Memo/note: `nexus:<USDD receival account>`
- Amount: minimum `0.100101 USDC`

Fees: 0.1 USDC + 0.1% of amount.

Optionally use the local solana CLI:

`spl-token transfer \EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \<amount> \Bg1MUQDMjAuXSAFr8izhGCUUhsrta1EjHcTvvgFnJEzZ \--with-memo "nexus:<USDD receival account>" \--url https://api.mainnet-beta.solana.com`

### USDD->USDC (Asset‑Mapped Receival Account)

> 📖 **Full specification:** See [ASSET_STANDARD.md](ASSET_STANDARD.md) for complete asset format details and examples.

Publish (or update) a Nexus Asset **you own** that maps the USDD transfer txid to your Solana receival address. The service matches on two fields: `txid_toService` (the USDD credit transaction hash) AND `owner` (the signature chain that sent the USDD). When it finds an asset row containing a `receival_account`, it sends USDC there.

**Quick Start (3 commands):**

1) **Create asset** (one-time setup):
```bash
nexus assets/create/asset name=distordiaBridge format=basic \
    txid_toService="" \
    receival_account=<YOUR_SOLANA_USDC_ATA> \
    pin=<PIN>
```

2) **Send USDD** to treasury and capture txid:
```bash
nexus finance/debit/token from=USDD to=<TREASURY_ACCOUNT> amount=10.5 pin=<PIN>
# Response includes "txid": "01b88ff8..."
```

3) **Update asset** with txid:
```bash
nexus assets/update/asset name=distordiaBridge format=basic \
    txid_toService=01b88ff8... \
    pin=<PIN>
```

Done! The service will detect your credit, verify the asset owner matches, and send USDC to your `receival_account`.

**Key Points:**
- Asset owner must match the sender's `owner` field of the USDD credit (security check)
- The same asset is reused for multiple swaps—just update `txid_toService` each time
- Your Solana wallet must already have a USDC ATA (most wallets auto-create on first receive)
- Minimum: `0.100101 USDD`. Smaller amounts are treated as fees
- If no asset mapping within `REFUND_TIMEOUT_SEC` (default 1 hour) → USDD is refunded

High‑level flow:
1. You send USDD to the service treasury.
2. You obtain the resulting transaction `txid` (returned by the CLI / wallet).
3. You create (or update) an asset you own adding fields:
  - `txid_toService` : the txid from step 2
  - `receival_account` : either your Solana USDC token account (ATA) OR your Solana wallet address (the service will derive the ATA if it already exists). 
4. Service detects the credit, queries assets filtering by `txid_toService=<txid>` AND `owner=<sender_owner_hash>`, validates the receival account, then sends net USDC.
5. If no matching asset appears before the refund timeout, the credit is moved into a refund / trade-balance check flow and ultimately refunded.

Detailed steps:

1) Ensure your Solana wallet already has (or will auto-create) a USDC ATA.
  - Most consumer wallets (Phantom, Solflare, Glow) auto-create it on first receive.
  - Power users can pre-create it: `spl-token create-account <USDC_MINT>`.

2) Send USDD to the treasury
  - To: `NEXUS_USDD_TREASURY_ACCOUNT`
  - Amount: ≥ `MIN_CREDIT_USDD` (default 0.100101). Amounts < threshold are treated as micro credits (100% fee) and no USDC will be sent.
  - Command example (token debit):
    ```bash
    nexus finance/debit/token from=USDD to=<TREASURY_ACCOUNT> amount=<AMOUNT_IN_BASE_UNITS> pin=<PIN>
    ```
    or (account debit if you hold a USDD account object):
    ```bash
    nexus finance/debit/account from=<YOUR_USDD_ACCOUNT> to=<TREASURY_ACCOUNT> amount=<AMOUNT_UNITS> pin=<PIN>
    ```
  - Capture the `txid` from the CLI output.

3) Create or update the mapping asset (owned by the same signature chain that performed the debit):
  - If you do not already have an asset container for swaps, you can create one with mutable fields:
    ```bash
    nexus register/create/asset name=swapRecv mutable=txid_toService,receival_account
    ```
  - Then set (or update) the fields for this specific txid:
    ```bash
    nexus register/write/asset name=swapRecv data='{"txid_toService":"<TXID>","receival_account":"<SOLANA_OR_USDC_TOKEN_ACCOUNT>"}'
    ```
    (If your CLI supports partial field updates you can just write those two fields.)
  - Alternative: create one asset per swap (simpler, higher on‑chain object count):
    ```bash
    nexus register/create/asset name=swapRecv_<SHORT_TXID> data='{"txid_toService":"<TXID>","receival_account":"<SOLANA_OR_TOKEN_ACCOUNT>"}'
    ```

4) Wait for service processing
  - Service polls, resolves your Solana address via `find_asset_receival_account_by_txid_and_owner`. If the supplied value is a wallet address (not a USDC token account), it attempts to locate an existing USDC ATA; it will NOT create a missing one.
  - On success it sends net USDC (after flat + dynamic fees, if configured) with a memo referencing the originating Nexus txid.

5) Refund / fallback cases
  - No asset found within `REFUND_TIMEOUT_SEC`: credit moves to refund logic (may quarantine after repeated failures).
  - Invalid/malformed `receival_account`: refunded.
  - Solana send fails repeatedly: refunded.
  - Micro credit (< threshold): recorded as fees instantly; no asset lookup needed.

Notes
- Asset owner must match the sender’s `owner` field of the USDD credit; otherwise it is ignored.
- You can batch multiple swaps by using multiple assets or updating the same asset sequentially (only the row with matching `txid_toService` is considered).
- Tiny USDD credits below `MIN_CREDIT_USDD` (default 0.100101) are treated as fees (100% micro fee policy) and skipped.
- Keep the asset published before the refund timeout to avoid unnecessary refunds.

---

## Summary of Both Directions

### USDC → USDD (Solana to Nexus)
1. Send USDC to the vault token account (`VAULT_USDC_ACCOUNT`) with memo: `nexus:<NEXUS_USDD_ACCOUNT>`.
2. Service validates the Nexus account & token, mints/sends USDD minus fees.
3. Invalid or missing memo → refund (flat fee may apply). Tiny deposits ≤ flat fee are treated as fees.

### USDD → USDC (Nexus to Solana)
1. Send USDD to treasury (`NEXUS_USDD_TREASURY_ACCOUNT`).
2. Publish asset with `txid_toService` + `receival_account` (Solana wallet or USDC ATA).
3. Service finds mapping, validates address / existing ATA, sends net USDC. If mapping missing past timeout → refund.

---

## Common Questions
**Q: How fast are swaps?**  Depends on polling and chain confirmation. Typical: a few Solana blocks (USDC→USDD) or one Nexus credit + mapping publish cycle (USDD→USDC).  
**Q: Can I reuse the same asset?** Yes—just update its fields for each new `txid_toService`, or create per‑swap assets.  
**Q: What if I forget to publish the asset?** After the refund timeout the USDD is refunded (minus any congestion or micro handling fees).  
**Q: Do you create my USDC ATA?** No. Ensure it already exists (most wallets auto‑create on first receive).  
**Q: Are sub‑threshold amounts lost?** They are treated as fees/donations per policy; do not send below the published minimum.  

---

## Minimal Cheat Sheet

USDC→USDD:
```
Send USDC to <VAULT_USDC_ACCOUNT>
Memo: nexus:<YOUR_USDD_ACCOUNT>
Amount ≥ 0.100101 USDC
```

USDD→USDC:
```
Send USDD to <NEXUS_USDD_TREASURY_ACCOUNT>
Grab txid from CLI output
Publish or update asset with fields: {"txid_toService":"<TXID>","receival_account":"<SOL_OR_USDC_TOKEN_ACCOUNT>"}
Amount ≥ 0.100101 USDD
```

---

## Need Full Setup / Configuration Details?
See:
- `SETUP.md` (installation, architecture, operations)
- `SECURITY.md` (hardening & threat model)
- `CONFIG.md` (environment variable reference)

---

## License
Provided as‑is. Use at your own risk. See `SETUP.md` for extended security notes.


## How It Works

### USDC → USDD (Solana to Nexus)
1. User sends USDC to your vault USDC token account (`VAULT_USDC_ACCOUNT`).
2. The same transaction must include a Memo: `nexus:<NEXUS_ADDRESS>`.
3. Service validates the Nexus address exists and is for the expected token (`NEXUS_TOKEN_NAME`, e.g., USDD).
4. If valid, the service mints/sends USDD on Nexus to that address (amount normalized by decimals).
5. If invalid/missing memo or wrong token, the service refunds the USDC back to the source SPL token account with a memo explaining the reason. A flat fee (`FLAT_FEE_USDC`) is charged per refund attempt on this path. On successful swaps, a dynamic fee in bps (`DYNAMIC_FEE_BPS`) is also retained. Tiny deposits ≤ `FLAT_FEE_USDC` are treated as fees and not processed further.

Notes:
- Amounts are handled in base units and normalized between `USDC_DECIMALS` and `USDD_DECIMALS`.
- The refund is sent to the original SPL token account the deposit came from (not a wallet owner).

### USDD → USDC (Nexus to Solana)
1. User sends USDD to your Nexus USDD Treasury account (`NEXUS_USDD_TREASURY_ACCOUNT`).
2. The transaction’s reference must be: `solana:<SOLANA_ADDRESS>`.
3. Service validates the Solana address format.
4. If valid, the service sends USDC from the vault to that address. The recipient must already have a USDC ATA (we do not create it).
5. If invalid address or send fails, the service refunds USDD back to the sender on Nexus with a reason in `reference`. On successful sends, an optional dynamic fee (`DYNAMIC_FEE_BPS`, set to 0 if you want no fee on this path) may be retained; no fee is taken on refunds.

Policy notes on USDD → USDC:
- Tiny USDD credits ≤ `FLAT_FEE_USDD` are routed to your `NEXUS_USDD_LOCAL_ACCOUNT` (no USDC is sent) and the item is marked processed.

### Loop-Safety and Reliability
- Actions that can incur fees (mint, send, refunds) are guarded by attempt limits and cooldowns:
  - `MAX_ACTION_ATTEMPTS` attempts per unique item (tx/signature).
  - `ACTION_RETRY_COOLDOWN_SEC` between attempts.
- Processed state is persisted; items are only marked processed after a successful outcome.
- Solana transfers include confirmation attempts.
 - If all refund attempts fail:
   - USDC→USDD path: the remaining refundable amount (after the last attempt's flat fee) is moved from the vault USDC token account to a self-owned quarantine USDC token account.
   - USDD→USDC path: the remaining refundable USDD is moved from the treasury to a self-owned Nexus USDD quarantine account.
   - In both cases, the event is recorded in the `quarantined_sigs` or `quarantined_txids` database table for manual inspection.

## Optional Public Heartbeat (Free, On-Chain)
The service can update a Nexus Asset’s mutable field `last_poll_timestamp` after each poll cycle. Anyone can read this on-chain to determine whether the service is online.

- One-time cost: create an Asset (1 NXS fee for asset creation, + optionally 1 NXS for adding a local name). Updates are free as long as they are not more frequent than every 10 seconds (there's a congestion fee of 0.01 NXS for more frequent transactions).
- The service enforces a minimum update interval: `HEARTBEAT_MIN_INTERVAL_SEC` (defaults to `max(10, POLL_INTERVAL)`).

Setup steps:
1. Create an asset with a mutable attribute named `last_poll_timestamp` (unix seconds). You can also add optional per-chain waterline fields. Use Nexus CLI (example):
  - `assets/create/asset name=swapServiceHeartbeat mutable=last_poll_timestamp`
  - Optionally add fields `last_safe_timestamp_solana` and `last_safe_timestamp_usdd` for waterlines.
2. Put the asset’s address in `.env` as `NEXUS_HEARTBEAT_ASSET_ADDRESS` and ensure `HEARTBEAT_ENABLED=true`.

How clients check status:
- Read the asset throught the `register` api: `register/get/assets:asset address=<ASSET_ADDRESS>`
  - Or by name: `register/get/assets:asset name=<ASSET_NAME>`
- Extract `results.last_poll_timestamp` (unix seconds).
- Consider the service online if `now - last_poll_timestamp <= grace`, where `grace ≈ 2–3 × POLL_INTERVAL`.

Waterline (optional):
- The service can also honor per-chain “waterline” timestamps stored on the same asset to bound how far back it scans:
  - Default field names: `last_safe_timestamp_solana` and `last_safe_timestamp_usdd` (configurable via env `HEARTBEAT_WATERLINE_SOLANA_FIELD` / `HEARTBEAT_WATERLINE_NEXUS_FIELD` or helper flags)
  - Pollers skip on-chain items strictly older than their respective waterline (with a small safety margin). Idempotency still prevents double-processing if you later move the waterline.

## Prerequisites
- Python 3.10+ (tested with 3.12 on Ubuntu 24.04.1)
- Solana wallet and USDC vault token account (ATA)
- Nexus node/CLI available locally
- Sufficient balances: SOL for fees, USDC in vault for payouts, USDD for payouts

## Install Dependencies

Using pinned versions (see `requirements.txt` for exact tested versions):
```powershell
python -m pip install -r requirements.txt
```
Or explicitly:
```powershell
python -m pip install python-dotenv solana solders
```

Linux/macOS:
```bash
python3 -m pip install -r requirements.txt
```
Or explicitly:
```bash
python3 -m pip install python-dotenv solana solders
```

Optional: create and use a virtual environment

Ubuntu 24.04.1 build prerequisites (if native wheels unavailable):
```bash
sudo apt update
sudo apt install -y build-essential pkg-config libssl-dev
```

Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux/macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Environment Configuration
Create a `.env` file in the project directory:

```env
# Solana
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
VAULT_KEYPAIR=./vault-keypair.json
VAULT_USDC_ACCOUNT=<YOUR_VAULT_USDC_TOKEN_ACCOUNT_ADDRESS>
USDC_MINT=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
# Native SOL (used by fee conversions/Jupiter path). Keep default unless you know otherwise.
SOL_MINT=So11111111111111111111111111111111111111112
# Devnet USDC: 4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU

# Decimals (base units)
USDC_DECIMALS=6
USDD_DECIMALS=6

# Nexus
NEXUS_CLI_PATH=./nexus
NEXUS_SESSION=<YOUR_NEXUS_SESSION>
NEXUS_PIN=<YOUR_NEXUS_PIN>
NEXUS_USDD_TREASURY_ACCOUNT=<YOUR_USDD_TREASURY_ACCOUNT_ADDRESS>
NEXUS_USDD_LOCAL_ACCOUNT=<YOUR_LOCAL_USDD_ACCOUNT_ADDRESS>
NEXUS_USDD_FEES_ACCOUNT=<YOUR_USDD_FEES_ACCOUNT_ADDRESS>
NEXUS_TOKEN_NAME=USDD
NEXUS_RPC_HOST=http://127.0.0.1:8399
NEXUS_USDD_QUARANTINE_ACCOUNT=<YOUR_USDD_QUARANTINE_ACCOUNT_ADDRESS>

# Quarantine and failed refunds
# Self-owned USDC token account used to quarantine amounts from failed refunds so they don't affect backing ratio
USDC_QUARANTINE_ACCOUNT=<YOUR_USDC_TOKEN_ACCOUNT_FOR_QUARANTINE>

# Polling & State
POLL_INTERVAL=10
# All state is stored in SQLite database (swap_service.db)
STATE_DB_PATH=swap_service.db
MAX_ACTION_ATTEMPTS=3
ACTION_RETRY_COOLDOWN_SEC=300

# Fees & policy
# Flat fee for USDC→USDD (charged per refund attempt if refunding)
FLAT_FEE_USDC=0.1
# Threshold for tiny USDD deposits; tiny USDD is treated as dust on USDD→USDC
FLAT_FEE_USDD=0.1
# Single dynamic fee (bps) applied on successful swaps (both directions)
# Set to 0 if you want no dynamic fee on USDD→USDC.
DYNAMIC_FEE_BPS=10

# Optional on-chain heartbeat
HEARTBEAT_ENABLED=true
NEXUS_HEARTBEAT_ASSET_ADDRESS=<OPTIONAL_HEARTBEAT_ASSET_ADDRESS>
# Updates free if >= 10 seconds apart
HEARTBEAT_MIN_INTERVAL_SEC=10
# Optional heartbeat waterline configuration
HEARTBEAT_WATERLINE_ENABLED=true
# These control which field names on the asset are used for waterlines
HEARTBEAT_WATERLINE_SOLANA_FIELD=last_safe_timestamp_solana
HEARTBEAT_WATERLINE_NEXUS_FIELD=last_safe_timestamp_usdd
# Safety margin (seconds) subtracted from waterline when filtering
HEARTBEAT_WATERLINE_SAFETY_SEC=120
```

Quick start from template:
- Windows (PowerShell):
  ```powershell
  Copy-Item .env.example .env
  ```
- Linux/macOS:
  ```bash
  cp .env.example .env
  ```
Then open `.env` and fill in the required values.

### Create/modify the .env file on Linux/macOS

Option A — use an editor (nano):
```bash
nano .env
# Paste the template above, edit values, then save (Ctrl+O) and exit (Ctrl+X)
```

Option B — create a minimal .env via heredoc (only required vars), then edit:
```bash
cat > .env << 'EOF'
# Required
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
VAULT_KEYPAIR=./vault-keypair.json
VAULT_USDC_ACCOUNT=<YOUR_VAULT_USDC_TOKEN_ACCOUNT_ADDRESS>
USDC_MINT=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
NEXUS_PIN=<YOUR_NEXUS_PIN>
NEXUS_USDD_TREASURY_ACCOUNT=<YOUR_USDD_TREASURY_ACCOUNT_ADDRESS>
NEXUS_USDD_LOCAL_ACCOUNT=<YOUR_LOCAL_USDD_ACCOUNT_ADDRESS>

# Common optional
NEXUS_CLI_PATH=./nexus
POLL_INTERVAL=10
EOF

# Review and complete the rest of the optional settings as needed
sed -n '1,200p' .env
```

Note on NEXUS_SESSION:
- `NEXUS_SESSION` is optional. The current service does not read it, but if your Nexus node/CLI is configured to require a session token, you can set it in `.env` and configure your CLI wrapper accordingly.

OS-specific notes:
- Linux/macOS: ensure the Nexus CLI is executable. If you keep it in the repo root, run:
  - `chmod +x ./nexus`
  - Set `NEXUS_CLI_PATH=./nexus` (or an absolute path) in `.env`.
- Windows (PowerShell): if the CLI is not in PATH, keep `NEXUS_CLI_PATH=./nexus` and run the service from the repo root so the relative path resolves.

## Set up Solana accounts (vault and fees)

These steps create the dedicated Solana keypair for the service, its USDC token account (ATA) to hold funds, and an optional separate USDC fee account.

Prereqs:
- Install Solana CLI and SPL Token CLI
  - Windows: https://docs.solana.com/cli/install-solana-cli-tools#windows
  - Linux/macOS: https://docs.solana.com/cli/install-solana-cli-tools
- Have some SOL to pay for transactions (devnet: `solana airdrop 1`)

1) Create a dedicated keypair for the service (vault)

Windows (PowerShell):
```powershell
solana-keygen new -o .\vault-keypair.json
solana config set -k .\vault-keypair.json -u https://api.mainnet-beta.solana.com
solana address
```
Linux/macOS:
```bash
solana-keygen new -o ./vault-keypair.json
solana config set -k ./vault-keypair.json -u https://api.mainnet-beta.solana.com
solana address
```
Copy the printed address into `.env` as the owner of your vault (this is implied by the keypair). Fund it with some SOL.

2) Create the vault USDC token account (ATA)

With the vault keypair selected in `solana config`:

Windows (PowerShell):
```powershell
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```
Linux/macOS:
```bash
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```
This prints a token account address (the ATA). Put that into `.env` as `VAULT_USDC_ACCOUNT`.

3) Fund the vault with USDC

- Mainnet: transfer USDC to `VAULT_USDC_ACCOUNT` from your exchange/custody.
- Devnet: use the devnet USDC mint `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU` and a test issuer; faucets are not officially provided.

4) Fees policy: single USDC vault account + USDD fees account

- All USDC fees remain in the vault USDC ATA (no separate USDC fee account).
- An equivalent amount of USDD is minted to your designated USDD fees account on Nexus for accounting.
- Configure your USDD fees account in `.env`:
  ```env
  NEXUS_USDD_FEES_ACCOUNT=<YOUR_USDD_FEES_ACCOUNT_ADDRESS>
  ```

5) Verify

Windows (PowerShell):
```powershell
spl-token accounts --owner (solana address)
```
Linux/macOS:
```bash
spl-token accounts --owner "$(solana address)"
```
You should see an account for the USDC mint. Optionally check balances:

```bash
spl-token balance <TOKEN_ACCOUNT_ADDRESS>
```

## Running the Service
```powershell
python .\swapService.py
```
Linux/macOS:
```bash
python3 ./swapService.py
```
Note: `swapService.py` is a thin entrypoint that delegates to the modular runner in `src/main.py`.
Expected startup output:
```
🌐 Starting bidirectional swap service
   Solana RPC: <RPC_URL>
   USDC Vault: <VAULT_USDC_ACCOUNT>
  USDD Treasury: <NEXUS_USDD_TREASURY_ACCOUNT>
   Monitoring:
   - USDC → USDD: Solana deposits with Nexus address in memo
   - USDD → USDC: USDD deposits with Solana address in reference
  USDC Vault Balance: <amount> USDC (<base> base) — <VAULT_USDC_ACCOUNT>
  USDD Circulating Supply: <amount> USDD (<base> base) — Treasury: <NEXUS_USDD_TREASURY_ACCOUNT>
```

## User Instructions

### Swap USDC → USDD (on Solana)
- Send USDC to `VAULT_USDC_ACCOUNT` with a Memo in the same transaction:
  - `nexus:<YOUR_NEXUS_ADDRESS>`
- If the memo is missing/invalid or the Nexus address is not a valid `NEXUS_TOKEN_NAME` account, your USDC is refunded to the source SPL token account with a reason memo. The flat fee is charged per refund attempt on this path. If all attempts fail, the remaining refundable amount is quarantined and the incident is logged for manual review.
- Tiny USDC deposits ≤ `FLAT_FEE_USDC` are treated as fees and not processed further.

### Swap USDD → USDC (on Nexus)
- Send USDD to `NEXUS_USDD_TREASURY_ACCOUNT` with reference:
  - `solana:<YOUR_SOLANA_ADDRESS>`
- You must already have a USDC ATA for your wallet. The service will send USDC to your USDC ATA; it will not create it for you. If the address is invalid or a send fails, your USDD is refunded with a reason in the reference. A fee may be deducted if configured.
- Tiny USDD deposits ≤ `FLAT_FEE_USDD` are routed to the service's local USDD account (no USDC is sent).

How to create your USDC ATA (user-side):
- Most wallets (Phantom, Solflare) auto-create an ATA when you first receive the token.
- Dev tools users can initialize it via Solana CLI or spl-token CLI:
  - Solana-CLI example (creates token account for USDC mint, owned by your wallet):
    - Linux/macOS: `solana transfer --allow-unfunded-recipient --from <YOUR_KEYPAIR> <USDC_MINT> 0 <YOUR_WALLET_ADDRESS>`
    - Or use spl-token: `spl-token create-account <USDC_MINT>`

## Configuration Reference

| Variable | Description | Required | Default |
|---|---|---|---|
| SOLANA_RPC_URL | Solana RPC endpoint | ✅ | - |
| VAULT_KEYPAIR | Path to vault keypair JSON (array of ints) | ✅ | - |
| VAULT_USDC_ACCOUNT | Vault’s USDC token account (ATA) | ✅ | - |
| USDC_MINT | USDC mint address | ✅ | - |
| SOL_MINT | Native SOL mint (keep default) | ✅ | So1111...112 |
| SOL_MAIN_ACCOUNT | Main SOL account (owner of vault keypair) | ✅ | - |
| USDC_DECIMALS | USDC token decimals (base units) | ❌ | 6 |
| USDD_DECIMALS | USDD token decimals (base units) | ❌ | 6 |
| NEXUS_PIN | Nexus account PIN | ✅ | - |
| NEXUS_USDD_TREASURY_ACCOUNT | USDD treasury (receives deposits) | ✅ | - |
| NEXUS_USDD_LOCAL_ACCOUNT | Local USDD account (tiny deposits & congestion fees) | ❌ | - |
| NEXUS_USDD_QUARANTINE_ACCOUNT | USDD quarantine (failed refunds) | ❌ | - |
| NEXUS_USDD_FEES_ACCOUNT | USDD fees account (optional) | ❌ | - |
| NEXUS_TOKEN_NAME | Token ticker expected (validation) | ❌ | USDD |
| NEXUS_CLI_PATH | Path to Nexus CLI | ❌ | ./nexus |
| NEXUS_RPC_HOST | Nexus RPC host (if applicable) | ❌ | http://127.0.0.1:8399 |
| POLL_INTERVAL | Poll interval (seconds) | ❌ | 10 |
| STATE_DB_PATH | SQLite database path | ❌ | swap_service.db |
| MAX_ACTION_ATTEMPTS | Max attempts per action | ❌ | 3 |
| ACTION_RETRY_COOLDOWN_SEC | Cooldown between attempts | ❌ | 300 |
| REFUND_TIMEOUT_SEC | Age before attempting forced refund | ❌ | 3600 |
| STALE_DEPOSIT_QUARANTINE_SEC | Age to quarantine unresolved deposits | ❌ | 86400 |
| USDC_CONFIRM_TIMEOUT_SEC | Max seconds to await USDC send confirmation | ❌ | 600 |
| FLAT_FEE_USDC | Flat fee (USDC) Solana→Nexus | ❌ | 0.1 |
| FLAT_FEE_USDD | Tiny threshold (USDD) Nexus→Solana | ❌ | 0.1 |
| DYNAMIC_FEE_BPS | Dynamic fee bps (both directions) | ❌ | 10 |
| NEXUS_CONGESTION_FEE_USDD | Fee deducted on invalid USDD→USDC refunds | ❌ | 0.01 |
| FEE_CONVERSION_ENABLED | Enable optional DEX fee conversions | ❌ | false |
| FEE_CONVERSION_MIN_USDC | Min USDC base units before converting | ❌ | 0 |
| SOL_TOPUP_MIN_LAMPORTS | Min SOL lamports before top-up | ❌ | 0 |
| SOL_TOPUP_TARGET_LAMPORTS | Target SOL lamports | ❌ | 0 |
| NEXUS_NXS_TOPUP_MIN | Min NXS units before top-up (placeholder) | ❌ | 0 |
| BACKING_DEFICIT_BPS_ALERT | Bps deficit triggers alert/mint fees | ❌ | 10 |
| BACKING_DEFICIT_PAUSE_PCT | Pause if vault < pct of circulating | ❌ | 90 |
| BACKING_RECONCILE_INTERVAL_SEC | Interval to reconcile backing | ❌ | 3600 |
| BACKING_SURPLUS_MINT_THRESHOLD_USDC | Vault surplus threshold to mint fees | ❌ | 20 |
| HEARTBEAT_ENABLED | Enable on-chain heartbeat | ❌ | true |
| NEXUS_HEARTBEAT_ASSET_ADDRESS | Asset for last_poll_timestamp | ❌ | - |
| NEXUS_HEARTBEAT_ASSET_NAME | Heartbeat asset name (display) | ❌ | - |
| HEARTBEAT_MIN_INTERVAL_SEC | Min seconds between updates | ❌ | max(10,POLL_INTERVAL) |
| HEARTBEAT_WATERLINE_ENABLED | Enable waterline scanning bounds | ❌ | true |
| HEARTBEAT_WATERLINE_SOLANA_FIELD | Field name for Solana waterline | ❌ | last_safe_timestamp_solana |
| HEARTBEAT_WATERLINE_NEXUS_FIELD | Field name for Nexus waterline | ❌ | last_safe_timestamp_usdd |
| HEARTBEAT_WATERLINE_SAFETY_SEC | Safety subtraction from waterline | ❌ | 120 |
| SOLANA_RPC_TIMEOUT_SEC | Per Solana RPC call timeout | ❌ | 8 |
| SOLANA_TX_FETCH_TIMEOUT_SEC | Per get_transaction timeout | ❌ | 12 |
| SOLANA_POLL_TIME_BUDGET_SEC | Time slice per Solana poll loop | ❌ | 15 |
| SOLANA_MAX_TX_FETCH_PER_POLL | Cap tx fetch count per poll | ❌ | 120 |
| NEXUS_CLI_TIMEOUT_SEC | Nexus CLI call timeout | ❌ | 20 |
| NEXUS_POLL_TIME_BUDGET_SEC | Time slice per Nexus poll loop | ❌ | 15 |
| METRICS_BUDGET_SEC | Max seconds collecting metrics | ❌ | 5 |
| METRICS_INTERVAL_SEC | Interval for metrics print | ❌ | 30 |
| STALE_ROW_SEC | Age to treat state rows stale | ❌ | 86400 |
| USDC_QUARANTINE_ACCOUNT | Self-owned USDC quarantine ATA | ❌ | - |
| HEARTBEAT_MIN_INTERVAL_SEC | Min seconds between heartbeat updates | ❌ | max(10, POLL_INTERVAL) |
| HEARTBEAT_WATERLINE_ENABLED | Enable waterline-based scan limits | ❌ | true |
| HEARTBEAT_WATERLINE_SOLANA_FIELD | Asset field name for Solana waterline | ❌ | last_safe_timestamp_solana |
| HEARTBEAT_WATERLINE_NEXUS_FIELD | Asset field name for Nexus waterline | ❌ | last_safe_timestamp_usdd |
| HEARTBEAT_WATERLINE_SAFETY_SEC | Seconds subtracted from waterline when filtering | ❌ | 120 |

Idempotency:
- USDC → USDD: The service parses the memo `nexus:<NEXUS_ADDRESS>` from Solana deposits and validates the Nexus USDD account exists. It guards against duplicates using processed signature markers and Nexus debit reference numbers.
- USDD → USDC: The service queries user-owned `distordiaBridge` assets by `txid_toService` + `owner` to find the `receival_account`. See [ASSET_STANDARD.md](ASSET_STANDARD.md) for the full asset specification. Duplicates are prevented via processed txid markers and on-chain memo scanning.

## Security Notes
- Keep secrets out of git: ensure `.env` and `vault-keypair.json` are ignored and stored securely.
- Least-privilege vault key: use a dedicated Solana keypair for this service and fund it only with what’s needed (SOL for tx fees; USDC for payouts). Avoid reusing personal keys.
- RPC integrity: use trusted Solana RPC endpoints (self-hosted or reputable providers). Consider rate limits and lags when setting `POLL_INTERVAL`.
- Nexus CLI integrity: pin a specific CLI build and verify its checksum when updating. Restrict execute permissions to the service user.
- State database permissions: `swap_service.db` should be writable only by the service user (e.g., `chmod 600` on Linux/macOS).
- Logging hygiene: the service masks the PIN in CLI logs; avoid shell tracing that could echo command arguments.
- Refund loops: attempts/cooldowns help prevent fee-draining loops. If you reduce cooldowns, monitor logs for repeating failures.
- Test safely: try on Solana Devnet or a Nexus test environment first; verify both swap directions and refund paths.
- Backups and rotation: back up the vault keypair securely; rotate immediately if compromised.

## Troubleshooting
- Unresolved imports: run `python -m pip install -r requirements.txt`.
- Transactions are built/signed with solders; ensure `solana` and `solders` versions from `requirements.txt` are installed in your active environment.
- Nexus CLI outputs may include a trailing footer like `[Completed in … ms]`. The service parses JSON leniently and ignores this footer by default.
- Use Ctrl+C to stop the service gracefully; it will save state before exiting.
- error: externally-managed-environment (PEP 668) on Ubuntu/Debian: use a virtual environment instead of system Python.
  ```bash
  sudo apt update
  sudo apt install -y python3-venv
  # optional build tools if wheels are unavailable
  sudo apt install -y build-essential pkg-config libssl-dev
  # create and activate venv in repo root
  python3 -m venv .venv
  source .venv/bin/activate
  python3 -m pip install --upgrade pip
  python3 -m pip install -r requirements.txt
  ```
  If you must use system Python, append `--break-system-packages` to pip (not recommended).
- No recipient mapping yet (USDD → USDC): Ensure your `distordiaBridge` asset is published with `txid_toService` set to your Nexus debit txid and `receival_account` set to your Solana USDC ATA. See [ASSET_STANDARD.md](ASSET_STANDARD.md) for details.
- Wrong token or invalid Nexus address: USDC is refunded with a reason memo.
- Invalid Solana address (USDD → USDC): USDD is refunded to sender with a reason.
- Heartbeat not updating: Check `HEARTBEAT_ENABLED`, asset address, and that updates are not more frequent than 10s.
- Repeated attempts skipped: You may be within cooldown or max attempts; adjust `MAX_ACTION_ATTEMPTS` / `ACTION_RETRY_COOLDOWN_SEC`.

## Nexus API Docs
Official Nexus API docs are included in the `Nexus API docs/` folder for reference.

## License
This project is provided as-is. Use at your own risk.
