# USDC ↔ USDD Bidirectional Swap Service

A Python service that enables automatic swapping between USDC (Solana) and USDD (Nexus) in both directions, with strict validation, automatic refunds on invalid input, loop-safety, and an optional on-chain heartbeat for public status checking.

## How It Works

### USDC → USDD (Solana to Nexus)
1. User sends USDC to your vault USDC token account (`VAULT_USDC_ACCOUNT`).
2. The same transaction must include a Memo: `nexus:<NEXUS_ADDRESS>`.
3. Service validates the Nexus address exists and is for the expected token (`NEXUS_TOKEN_NAME`, e.g., USDD).
4. If valid, the service mints/sends USDD on Nexus to that address (amount normalized by decimals).
5. If invalid/missing memo or wrong token, the service refunds the USDC back to the source SPL token account with a memo explaining the reason. Optional fee may be deducted: `REFUND_USDC_FEE_BASE_UNITS`.

Notes:
- Amounts are handled in base units and normalized between `USDC_DECIMALS` and `USDD_DECIMALS`.
- The refund is sent to the original SPL token account the deposit came from (not a wallet owner).

### USDD → USDC (Nexus to Solana)
1. User sends USDD to your Nexus USDD account (`NEXUS_USDD_ACCOUNT`).
2. The transaction’s reference must be: `solana:<SOLANA_ADDRESS>`.
3. Service validates the Solana address format.
4. If valid, the service sends USDC from the vault to that address. If the recipient’s USDC ATA doesn’t exist, it is created automatically.
5. If invalid address or send fails, the service refunds USDD back to the sender on Nexus with a reason in `reference`. Optional fee may be deducted: `REFUND_USDD_FEE_BASE_UNITS`.

### Loop-Safety and Reliability
- Actions that can incur fees (mint, send, refunds) are guarded by attempt limits and cooldowns:
  - `MAX_ACTION_ATTEMPTS` attempts per unique item (tx/signature).
  - `ACTION_RETRY_COOLDOWN_SEC` between attempts.
- Processed state is persisted; items are only marked processed after a successful outcome.
- Solana transfers include confirmation attempts.

## Optional Public Heartbeat (Free, On-Chain)
The service can update a Nexus Asset’s mutable field `last_poll_timestamp` after each poll cycle. Anyone can read this on-chain to determine whether the service is online.

- One-time cost: create an Asset (about 1 NXS once). Updates are free as long as they are not more frequent than every 10 seconds.
- The service enforces a minimum update interval: `HEARTBEAT_MIN_INTERVAL_SEC` (defaults to `max(10, POLL_INTERVAL)`).

Setup steps:
1. Create an asset with a mutable attribute named `last_poll_timestamp` (unix seconds):
   - Use Nexus API/CLI: `assets/create/asset` (only once).
   - Or use the helper script below to create it quickly.
2. Put the asset’s address in `.env` as `NEXUS_HEARTBEAT_ASSET_ADDRESS`.
3. Ensure `HEARTBEAT_ENABLED=true`.

Create the heartbeat asset via helper script:
```powershell
python .\create_heartbeat_asset.py --name local:swapServiceHeartbeat
# If you omit --name, an unnamed asset is created; read it by address
```
Linux/macOS:
```bash
python3 ./create_heartbeat_asset.py --name local:swapServiceHeartbeat
# If you omit --name, an unnamed asset is created; read it by address
```
The script initializes a mutable `last_poll_timestamp` field and prints the asset address to set in `.env`.

How clients check status:
- Read the asset: `assets/get/asset address=<ASSET_ADDRESS>`
  - Or by name: `assets/get/asset name=<ASSET_NAME>`
- Extract `results.last_poll_timestamp` (unix seconds).
- Consider the service online if `now - last_poll_timestamp <= grace`, where `grace ≈ 2–3 × POLL_INTERVAL`.

## Prerequisites
- Python 3.8+
- Solana wallet and USDC vault token account (ATA)
- Nexus node/CLI available locally
- Sufficient balances: SOL for fees, USDC in vault for payouts, USDD for payouts

## Install Dependencies

Using pinned versions:
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
# Devnet USDC: 4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU

# Decimals (base units)
USDC_DECIMALS=6
USDD_DECIMALS=6

# Nexus
NEXUS_CLI_PATH=./nexus
NEXUS_PIN=<YOUR_NEXUS_PIN>
NEXUS_USDD_ACCOUNT=<YOUR_USDD_ACCOUNT_ADDRESS>
NEXUS_TOKEN_NAME=USDD
NEXUS_RPC_HOST=http://127.0.0.1:8399

# Polling & State
POLL_INTERVAL=10
PROCESSED_SIG_FILE=processed_sigs.json
PROCESSED_NEXUS_FILE=processed_nexus_txs.json
ATTEMPT_STATE_FILE=attempt_state.json
MAX_ACTION_ATTEMPTS=3
ACTION_RETRY_COOLDOWN_SEC=300

# Optional refund fees (base units)
REFUND_USDC_FEE_BASE_UNITS=0
REFUND_USDD_FEE_BASE_UNITS=0

# Optional on-chain heartbeat
HEARTBEAT_ENABLED=true
NEXUS_HEARTBEAT_ASSET_ADDRESS=<OPTIONAL_HEARTBEAT_ASSET_ADDRESS>
# Updates free if >= 10 seconds apart
HEARTBEAT_MIN_INTERVAL_SEC=10
```

OS-specific notes:
- Linux/macOS: ensure the Nexus CLI is executable. If you keep it in the repo root, run:
  - `chmod +x ./nexus`
  - Set `NEXUS_CLI_PATH=./nexus` (or an absolute path) in `.env`.
- Windows (PowerShell): if the CLI is not in PATH, keep `NEXUS_CLI_PATH=./nexus` and run the service from the repo root so the relative path resolves.

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
   USDD Account: <NEXUS_USDD_ACCOUNT>
   Monitoring:
   - USDC → USDD: Solana deposits with Nexus address in memo
   - USDD → USDC: USDD deposits with Solana address in reference
```

## User Instructions

### Swap USDC → USDD (on Solana)
- Send USDC to `VAULT_USDC_ACCOUNT` with a Memo in the same transaction:
  - `nexus:<YOUR_NEXUS_ADDRESS>`
- If the memo is missing/invalid or the Nexus address is not a valid `NEXUS_TOKEN_NAME` account, your USDC is refunded to the source SPL token account with a reason memo. A fee may be deducted if configured.

### Swap USDD → USDC (on Nexus)
- Send USDD to `NEXUS_USDD_ACCOUNT` with reference:
  - `solana:<YOUR_SOLANA_ADDRESS>`
- The service creates your USDC ATA automatically if needed and sends USDC. If the address is invalid or a send fails, your USDD is refunded with a reason in the reference. A fee may be deducted if configured.

## Configuration Reference

| Variable | Description | Required | Default |
|---|---|---|---|
| SOLANA_RPC_URL | Solana RPC endpoint | ✅ | - |
| VAULT_KEYPAIR | Path to vault keypair JSON (array of ints) | ✅ | - |
| VAULT_USDC_ACCOUNT | Vault’s USDC token account (ATA) | ✅ | - |
| USDC_MINT | USDC mint address | ✅ | - |
| USDC_DECIMALS | USDC token decimals (base units) | ❌ | 6 |
| USDD_DECIMALS | USDD token decimals (base units) | ❌ | 6 |
| NEXUS_PIN | Nexus account PIN | ✅ | - |
| NEXUS_USDD_ACCOUNT | Your USDD account address | ✅ | - |
| NEXUS_CLI_PATH | Path to Nexus CLI | ❌ | ./nexus |
| NEXUS_TOKEN_NAME | Token ticker used for validation | ❌ | USDD |
| NEXUS_RPC_HOST | Nexus RPC host (if applicable) | ❌ | http://127.0.0.1:8399 |
| POLL_INTERVAL | Poll interval (seconds) | ❌ | 10 |
| PROCESSED_SIG_FILE | File for processed Solana signatures | ❌ | processed_sigs.json |
| PROCESSED_NEXUS_FILE | File for processed Nexus txids | ❌ | processed_nexus_txs.json |
| ATTEMPT_STATE_FILE | File for attempt/cooldown state | ❌ | attempt_state.json |
| MAX_ACTION_ATTEMPTS | Max attempts per action (mint/send/refund) | ❌ | 3 |
| ACTION_RETRY_COOLDOWN_SEC | Cooldown between attempts | ❌ | 300 |
| REFUND_USDC_FEE_BASE_UNITS | Fee deducted from USDC refunds (base units) | ❌ | 0 |
| REFUND_USDD_FEE_BASE_UNITS | Fee deducted from USDD refunds (base units) | ❌ | 0 |
| HEARTBEAT_ENABLED | Enable on-chain heartbeat | ❌ | true |
| NEXUS_HEARTBEAT_ASSET_ADDRESS | Asset to update with last_poll_timestamp | ❌ | - |
| HEARTBEAT_MIN_INTERVAL_SEC | Min seconds between heartbeat updates | ❌ | max(10, POLL_INTERVAL) |

## Security Notes
- Keep `vault-keypair.json` and `.env` secure. Never commit them.
- Ensure the vault has enough SOL to cover fees and ATA creation.
- PIN is masked in logs for Nexus CLI calls.

## Troubleshooting
- Unresolved imports: run `python -m pip install -r requirements.txt`.
- ImportError: No module named spl.token: The `spl.token` module is bundled with the `solana` Python package (we no longer install a separate `spl-token`). Ensure `solana>=0.30,<0.31` is installed in the same environment.
- No memo found (USDC → USDD): Wallet must include a Memo in the same transaction.
- Wrong token or invalid Nexus address: USDC is refunded with a reason memo.
- Invalid Solana address (USDD → USDC): USDD is refunded to sender with a reason.
- Heartbeat not updating: Check `HEARTBEAT_ENABLED`, asset address, and that updates are not more frequent than 10s.
- Repeated attempts skipped: You may be within cooldown or max attempts; adjust `MAX_ACTION_ATTEMPTS` / `ACTION_RETRY_COOLDOWN_SEC`.

## Nexus API Docs
Official Nexus API docs are included in the `Nexus API docs/` folder for reference.

## License
This project is provided as-is. Use at your own risk.
