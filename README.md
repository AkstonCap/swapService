# USDC ↔ USDD Bidirectional Swap Service

A Python service that enables automatic swapping between USDC (Solana) and USDD (Nexus) in both directions, with strict validation, automatic refunds on invalid input, loop-safety, and an optional on-chain heartbeat for public status checking.

## How It Works

### USDC → USDD (Solana to Nexus)
1. User sends USDC to your vault USDC token account (`VAULT_USDC_ACCOUNT`).
2. The same transaction must include a Memo: `nexus:<NEXUS_ADDRESS>`.
3. Service validates the Nexus address exists and is for the expected token (`NEXUS_TOKEN_NAME`, e.g., USDD).
4. If valid, the service mints/sends USDD on Nexus to that address (amount normalized by decimals).
5. If invalid/missing memo or wrong token, the service refunds the USDC back to the source SPL token account with a memo explaining the reason. A flat fee (`FLAT_FEE_USDC`) is always retained on this path. On successful swaps, a dynamic fee in bps (`FEE_BPS_USDC_TO_USDD`) is also retained. Tiny deposits ≤ `FLAT_FEE_USDC` are treated as fees and not processed further.

Notes:
- Amounts are handled in base units and normalized between `USDC_DECIMALS` and `USDD_DECIMALS`.
- The refund is sent to the original SPL token account the deposit came from (not a wallet owner).

### USDD → USDC (Nexus to Solana)
1. User sends USDD to your Nexus USDD Treasury account (`NEXUS_USDD_TREASURY_ACCOUNT`).
2. The transaction’s reference must be: `solana:<SOLANA_ADDRESS>`.
3. Service validates the Solana address format.
4. If valid, the service sends USDC from the vault to that address. The recipient must already have a USDC ATA (we do not create it).
5. If invalid address or send fails, the service refunds USDD back to the sender on Nexus with a reason in `reference`. On successful sends, an optional dynamic fee (`FEE_BPS_USDD_TO_USDC`, default 0) may be retained; no fee is taken on refunds.

Policy notes on USDD → USDC:
- Tiny USDD credits ≤ `FLAT_FEE_USDD` are routed to your `NEXUS_USDD_LOCAL_ACCOUNT` (no USDC is sent) and the item is marked processed.

### Loop-Safety and Reliability
- Actions that can incur fees (mint, send, refunds) are guarded by attempt limits and cooldowns:
  - `MAX_ACTION_ATTEMPTS` attempts per unique item (tx/signature).
  - `ACTION_RETRY_COOLDOWN_SEC` between attempts.
- Processed state is persisted; items are only marked processed after a successful outcome.
- Solana transfers include confirmation attempts.

## Optional Public Heartbeat (Free, On-Chain)
The service can update a Nexus Asset’s mutable field `last_poll_timestamp` after each poll cycle. Anyone can read this on-chain to determine whether the service is online.

- One-time cost: create an Asset (1 NXS fee for asset creation, + optionally 1 NXS for adding a local name). Updates are free as long as they are not more frequent than every 10 seconds (there's a congestion fee of 0.01 NXS for more frequent transactions).
- The service enforces a minimum update interval: `HEARTBEAT_MIN_INTERVAL_SEC` (defaults to `max(10, POLL_INTERVAL)`).

Setup steps:
1. Create an asset with a mutable attribute named `last_poll_timestamp` (unix seconds):
   - Use Nexus API/CLI: `assets/create/asset` (only once).
   - Or use the helper script as described below ("create_heartbeat_asset.py") to create it quickly.
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
- Read the asset throught the `register` api: `register/get/assets:asset address=<ASSET_ADDRESS>`
  - Or by name: `register/get/assets:asset name=<ASSET_NAME>`
- Extract `results.last_poll_timestamp` (unix seconds).
- Consider the service online if `now - last_poll_timestamp <= grace`, where `grace ≈ 2–3 × POLL_INTERVAL`.

Waterline (optional):
- The service can also honor per-chain “waterline” timestamps stored on the same asset to bound how far back it scans:
  - `last_safe_timestamp_solana` and `last_safe_timestamp_usdd` (field names configurable via env)
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
NEXUS_TOKEN_NAME=USDD
NEXUS_RPC_HOST=http://127.0.0.1:8399

# Polling & State
POLL_INTERVAL=10
PROCESSED_SIG_FILE=processed_sigs.json
PROCESSED_NEXUS_FILE=processed_nexus_txs.json
ATTEMPT_STATE_FILE=attempt_state.json
MAX_ACTION_ATTEMPTS=3
ACTION_RETRY_COOLDOWN_SEC=300

# Fees & policy
# Flat fee for USDC→USDD (taken even if refund is required)
FLAT_FEE_USDC=0.1
# Threshold for tiny USDD deposits; tiny USDD is treated as dust on USDD→USDC
FLAT_FEE_USDD=0.1
# Dynamic fee (bps) on successful USDC→USDD swaps (0.1% = 10 bps)
FEE_BPS_USDC_TO_USDD=10
# No dynamic fee on USDD→USDC by default
FEE_BPS_USDD_TO_USDC=0

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

4) Optional: set up a separate USDC fee token account

You can keep fees in the same vault ATA, but we recommend a separate account.

Option A — Use the vault owner (simpler):

Windows (PowerShell):
```powershell
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```
Linux/macOS:
```bash
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```
Record the printed token account address and set it in `.env` as `USDC_FEES_ACCOUNT`.

Option B — Use a distinct fees owner:

Windows (PowerShell):
```powershell
solana-keygen new -o .\fees-keypair.json
setx SOLANA_KEYPAIR "%CD%\fees-keypair.json"
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```
Linux/macOS:
```bash
solana-keygen new -o ./fees-keypair.json
export SOLANA_KEYPAIR="$PWD/fees-keypair.json"
spl-token create-account EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```
Use the printed token account address as `USDC_FEES_ACCOUNT` in `.env`. Fund the fees owner with a tiny amount of SOL if you plan to move funds from this account.

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
```

## User Instructions

### Swap USDC → USDD (on Solana)
- Send USDC to `VAULT_USDC_ACCOUNT` with a Memo in the same transaction:
  - `nexus:<YOUR_NEXUS_ADDRESS>`
- If the memo is missing/invalid or the Nexus address is not a valid `NEXUS_TOKEN_NAME` account, your USDC is refunded to the source SPL token account with a reason memo. The flat fee is always retained on this path.
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
| USDC_DECIMALS | USDC token decimals (base units) | ❌ | 6 |
| USDD_DECIMALS | USDD token decimals (base units) | ❌ | 6 |
| NEXUS_PIN | Nexus account PIN | ✅ | - |
| NEXUS_USDD_TREASURY_ACCOUNT | Your USDD treasury account address | ✅ | - |
| NEXUS_USDD_LOCAL_ACCOUNT | Your local USDD account address (optional) | ❌ | - |
| USDC_FEES_ACCOUNT | USDC fee token account (SPL) | ❌ | - |
| FLAT_FEE_USDC | Flat fee (USDC) for Solana→Nexus | ❌ | 0.1 |
| FLAT_FEE_USDD | Tiny threshold (USDD) for Nexus→Solana | ❌ | 0.1 |
| FEE_BPS_USDC_TO_USDD | Dynamic fee bps on successful Solana→Nexus | ❌ | 10 |
| FEE_BPS_USDD_TO_USDC | Dynamic fee bps on Nexus→Solana | ❌ | 0 |
| NEXUS_CLI_PATH | Path to Nexus CLI | ❌ | ./nexus |
| NEXUS_TOKEN_NAME | Token ticker used for validation | ❌ | USDD |
| NEXUS_RPC_HOST | Nexus RPC host (if applicable) | ❌ | http://127.0.0.1:8399 |
| POLL_INTERVAL | Poll interval (seconds) | ❌ | 10 |
| PROCESSED_SIG_FILE | File for processed Solana signatures | ❌ | processed_sigs.json |
| PROCESSED_NEXUS_FILE | File for processed Nexus txids | ❌ | processed_nexus_txs.json |
| ATTEMPT_STATE_FILE | File for attempt/cooldown state | ❌ | attempt_state.json |
| MAX_ACTION_ATTEMPTS | Max attempts per action (mint/send/refund) | ❌ | 3 |
| ACTION_RETRY_COOLDOWN_SEC | Cooldown between attempts | ❌ | 300 |
| HEARTBEAT_ENABLED | Enable on-chain heartbeat | ❌ | true |
| NEXUS_HEARTBEAT_ASSET_ADDRESS | Asset to update with last_poll_timestamp | ❌ | - |
| HEARTBEAT_MIN_INTERVAL_SEC | Min seconds between heartbeat updates | ❌ | max(10, POLL_INTERVAL) |
| HEARTBEAT_WATERLINE_ENABLED | Enable waterline-based scan limits | ❌ | true |
| HEARTBEAT_WATERLINE_SOLANA_FIELD | Asset field name for Solana waterline | ❌ | last_safe_timestamp_solana |
| HEARTBEAT_WATERLINE_NEXUS_FIELD | Asset field name for Nexus waterline | ❌ | last_safe_timestamp_usdd |
| HEARTBEAT_WATERLINE_SAFETY_SEC | Seconds subtracted from waterline when filtering | ❌ | 120 |

Idempotency:
- USDC → USDD: Nexus mints include reference `USDC_TX:<solana_signature>`. Before minting, the service checks Nexus for a prior confirmed CREDIT with that reference.
- USDD → USDC: Solana sends include memo `NEXUS_TX:<nexus_txid>`. Before sending, the service checks recent vault transfers for that memo to the recipient ATA.

## Security Notes
- Keep secrets out of git: ensure `.env` and `vault-keypair.json` are ignored and stored securely.
- Least-privilege vault key: use a dedicated Solana keypair for this service and fund it only with what’s needed (SOL for tx fees; USDC for payouts). Avoid reusing personal keys.
- RPC integrity: use trusted Solana RPC endpoints (self-hosted or reputable providers). Consider rate limits and lags when setting `POLL_INTERVAL`.
- Nexus CLI integrity: pin a specific CLI build and verify its checksum when updating. Restrict execute permissions to the service user.
- State file permissions: `processed_sigs.json`, `processed_nexus_txs.json`, and `attempt_state.json` should be writable only by the service user (e.g., `chmod 600` on Linux/macOS).
- Logging hygiene: the service masks the PIN in CLI logs; avoid shell tracing that could echo command arguments.
- Refund loops: attempts/cooldowns help prevent fee-draining loops. If you reduce cooldowns, monitor logs for repeating failures.
- Test safely: try on Solana Devnet or a Nexus test environment first; verify both swap directions and refund paths.
- Backups and rotation: back up the vault keypair securely; rotate immediately if compromised.

## Troubleshooting
- Unresolved imports: run `python -m pip install -r requirements.txt`.
- ImportError: No module named spl.token: The `spl.token` module ships inside the `solana` package. Ensure the pinned versions from `requirements.txt` (e.g. `solana==0.36.9` and `solders==0.26.0`) are installed in the active virtual environment.
- No memo found (USDC → USDD): Wallet must include a Memo in the same transaction.
- Wrong token or invalid Nexus address: USDC is refunded with a reason memo.
- Invalid Solana address (USDD → USDC): USDD is refunded to sender with a reason.
- Heartbeat not updating: Check `HEARTBEAT_ENABLED`, asset address, and that updates are not more frequent than 10s.
- Repeated attempts skipped: You may be within cooldown or max attempts; adjust `MAX_ACTION_ATTEMPTS` / `ACTION_RETRY_COOLDOWN_SEC`.

## Nexus API Docs
Official Nexus API docs are included in the `Nexus API docs/` folder for reference.

## License
This project is provided as-is. Use at your own risk.
