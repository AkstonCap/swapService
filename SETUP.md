# swapService Operator and Setup Guide

This guide covers installation and operation of the currently implemented bridge: **one configured Solana token / Nexus token pair**. User swap instructions are in [README.md](README.md); every operator-facing setting is listed in [CONFIG.md](CONFIG.md).

## Implemented scope

A process bridges one configured pair in two directions:

- **Solana → Nexus:** a user transfers the configured Solana-side token to the configured vault token account and includes `<DEPOSIT_MEMO_PREFIX><Nexus destination>`.
- **Nexus → Solana:** a user credits the configured Nexus treasury and publishes the existing `txid_toService` + `receival_account` mapping described in [ASSET_STANDARD.md](ASSET_STANDARD.md).

Missing mapping -> hold for operator review (no automatic Nexus refund).

The gross conversion is **1:1 in whole token units before fees**. Base units are rescaled when the two tokens use different decimal precision. The immutable `config.SWAP_PAIR` object supplies both token identities, custody accounts, decimals, fee policy and memo prefix to current money paths.

The Solana implementation is limited to mints and accounts owned by the classic SPL Token Program (`Tokenkeg...`). Configurability does **not** imply native SOL, Token-2022 extensions, arbitrary token programs, arbitrary chains, or simultaneous pairs. Deploying another classic SPL/Nexus pair still requires target-token and target-node acceptance testing; a configurable identity is not a blanket asset-safety guarantee.

### Compatibility names are not product branding

Some active environment variables, database columns, retry keys, reservation kinds and persisted statuses retain `USDC`/`USDD` in their literal names. They are compatibility interfaces from the original deployment, not assertions that every deployment bridges those assets. New environment configuration should use the generic canonical spellings where they exist. [CONFIG.md](CONFIG.md#canonical-keys-and-legacy-aliases) lists both spellings and the conflict rules.

### Planned, not implemented

The following remain planned work:

- provider asset v2 and address-based provider identity;
- general-chain or multi-pair routing;
- automatic pair discovery or per-request asset selection;
- automatic Nexus refunds/quarantine transfers from the service loop;
- automatic DEX conversion or backing-surplus mint/rebalance.

See [the planned provider-v2 standard](ASSET_STANDARD.md#provider-swapservice-asset-standard-v2-planned) and [Batch 7 in the evaluation](docs/EVALUATION.md#batch-7--complete-configurability-and-provider-asset-v2-in-progress-provider-v2-remains-documentation-only). The current runtime still uses one **name-addressed v1 heartbeat/service record**.

## Prerequisites

- Python 3.10+
- a Solana RPC endpoint
- Solana CLI and SPL Token CLI for deployment setup only
- a synced Nexus node and profile
- SQLite storage writable by one service instance

For production, use a dedicated Solana RPC provider. The runtime can use Helius enriched transaction retrieval when `HELIUS_RPC_URL` or `HELIUS_API_KEY` is set; otherwise it falls back to core Solana RPC.

Install the Solana tools from the maintained Anza documentation:

- [Install the Solana CLI](https://solana.com/docs/intro/installation)
- [SPL Token CLI documentation](https://spl.solana.com/token)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Do not commit `.env`, the vault keypair, Nexus PIN/session, API credentials, or private RPC URLs.

## Configure one pair

Use canonical generic keys for new deployments:

```env
# Solana custody and token identity
SOLANA_RPC_URL=<HTTPS_SOLANA_RPC_URL>
VAULT_KEYPAIR=<PATH_TO_VAULT_KEYPAIR_JSON>
SOL_MAIN_ACCOUNT=<SOLANA_VAULT_OWNER_PUBKEY>
SOLANA_VAULT_ACCOUNT=<CLASSIC_SPL_TOKEN_ACCOUNT_FOR_CONFIGURED_MINT>
SOLANA_TOKEN_MINT=<CLASSIC_SPL_MINT_PUBKEY>
SOLANA_TOKEN_SYMBOL=<DISPLAY_SYMBOL>
SOLANA_TOKEN_DECIMALS=<DECIMALS>

# Nexus custody and immutable token identity
NEXUS_PIN=<PROFILE_PIN>
NEXUS_TREASURY_ACCOUNT=<NEXUS_TOKEN_TREASURY_ACCOUNT>
NEXUS_TOKEN_NAME=<NEXUS_TOKEN_NAME>
NEXUS_TOKEN_REGISTER_ADDRESS=<IMMUTABLE_NEXUS_TOKEN_REGISTER_ADDRESS>
NEXUS_TOKEN_DECIMALS=<DECIMALS>

# Direction-named fee terms
FEE_FLAT_TO_NEXUS=<NEXUS_OUTPUT_TOKEN_UNITS>
FEE_FLAT_TO_SOLANA=<SOLANA_OUTPUT_TOKEN_UNITS>
FEE_REFUND_SOLANA=<SOLANA_TOKEN_UNITS>
FEE_BPS=<0_TO_4999>
DEPOSIT_MEMO_PREFIX=nexus:
```

All decimal fee and threshold values must be exactly representable at the relevant configured precision. Canonical and legacy aliases may both be present only when their strings are identical; conflicting identity, custody, precision or fee aliases fail startup.

If minimums and the Nexus dust threshold are omitted, the code derives them from the flat fee. An explicit minimum below the safety floor is raised to twice the corresponding flat fee and reported at startup. See [CONFIG.md § Fees, minimums and limits](CONFIG.md#fees-minimums-and-limits).

## Solana setup

Create a dedicated vault signer and protect it:

```bash
solana-keygen new -o <VAULT_KEYPAIR_PATH>
chmod 600 <VAULT_KEYPAIR_PATH>
solana config set -k <VAULT_KEYPAIR_PATH> -u <SOLANA_RPC_URL>
solana address
```

Fund the owner with enough SOL for transaction fees. Create classic SPL Token Program accounts for the configured mint:

```bash
spl-token create-account <SOLANA_TOKEN_MINT>
spl-token accounts --owner <SOL_MAIN_ACCOUNT>
spl-token balance <SOLANA_VAULT_ACCOUNT>
```

Use the resulting token-account address as `SOLANA_VAULT_ACCOUNT`; it is not the wallet owner address. For production, also create a self-owned account for the same mint and configure `SOLANA_QUARANTINE_ACCOUNT`. Optionally configure `SOLANA_FEE_ACCOUNT`; otherwise Solana-side fees remain in the vault.

Before deployment, independently verify that the configured mint, vault, quarantine and fee accounts all belong to the intended classic SPL mint and operator. Do not substitute a native SOL account or Token-2022 account.

## Nexus setup

The operator needs:

1. a synced Nexus node and active profile/session;
2. the configured Nexus token name and immutable register address;
3. a treasury account for that exact token;
4. a dedicated quarantine account in production;
5. authority required by the service's `finance/debit/token from=<NEXUS_TOKEN_NAME>` path.

Set the canonical custody keys:

```env
NEXUS_TREASURY_ACCOUNT=<TREASURY_ACCOUNT>
NEXUS_QUARANTINE_ACCOUNT=<QUARANTINE_ACCOUNT>
NEXUS_FEE_ACCOUNT=<OPTIONAL_FEE_ACCOUNT>
```

`NEXUS_USDD_LOCAL_ACCOUNT` remains a literal legacy-named compatibility setting for micro-credit handling; no generic alias exists yet. It does not enable automatic Nexus refunds.

### Nexus transport

Local development may use `NEXUS_CLI_PATH`. Production requires the authenticated HTTPS transport:

```env
NEXUS_API_URL=https://127.0.0.1:<TLS_PORT>
NEXUS_API_USER=<API_USER>
NEXUS_API_PASSWORD=<API_PASSWORD>
```

The production URL must be HTTPS and contain no embedded credentials, query or fragment. Configure `apiauth=1`, `apissl=1` and `apisslrequired=1` on the Nexus node. Restrict non-loopback access with a firewall or VPN and validate the certificate.

For a multiuser node:

```env
NEXUS_MULTIUSER=true
NEXUS_SESSION=<SESSION_ID>
```

`NEXUS_SESSION` is required when `NEXUS_MULTIUSER=true`. In single-user mode leave `NEXUS_MULTIUSER=false`; the service deliberately omits a session because single-user Nexus calls reject it. Treat the session and PIN as spending credentials.

## Required heartbeat and recovery checkpoints

`HEARTBEAT_ENABLED` is consulted by v1 service-record publication and heartbeat validation, but it does not disable the main recovery and polling dependencies. Startup recovery always reads the configured name-addressed heartbeat record. A live process therefore needs `NEXUS_HEARTBEAT_ASSET_NAME` resolving to a readable v1 asset containing:

- `last_poll_timestamp`;
- the configured Solana waterline field (default `last_safe_timestamp_solana`);
- the configured Nexus waterline field (default `last_safe_timestamp_nexus`).

The field names must be non-empty, distinct and match the top-level fields already present on the Nexus `format=basic` asset. `NEXUS_HEARTBEAT_ASSET_ADDRESS` is currently not used as the live lookup identity; provider-v2 address-based selection is planned.

Preview the current v1 service record without writing anything:

```bash
python3 register_service.py --show
python3 register_service.py --show --json
python3 register_service.py --create --name <HEARTBEAT_ASSET_NAME> --dry-run
python3 register_service.py --inspect <HEARTBEAT_ASSET_NAME>
```

`register_service.py --create` is a live, permanent Nexus operation when `--dry-run` is removed. It creates a complete current v1 field set, but its initial waterlines are zero. **Zero or missing waterlines cannot pass startup recovery.** Before first start, an operator must establish reviewed, positive checkpoints that cover all relevant custody history and write both top-level fields to the asset. Never set them to the current time merely to skip history. There is currently no automated helper that can choose a safe bootstrap checkpoint; preserve the evidence and review used to select each value.

`create_heartbeat_asset.py` is a legacy v1 helper. Its defaults include zero waterlines and pair-specific example labels, so do not rely on defaults for production. If retained for compatibility, supply explicit generic pair fields and reviewed positive `--solana-waterline-initial` / `--nexus-waterline-initial` values, preview with `--dry-run`, and verify the resulting asset before use.

Startup recovery is an admission gate, not a best-effort diagnostic. Polling begins only if both checkpoints are positive and both chain scans return complete, authoritative evidence. Missing/incompatible heartbeat data, incomplete pagination, malformed or legacy payout evidence, sparse refund/quarantine markers, reference lookup failure, or any recovery exception produces a nonzero service exit. A bounded recent scan is not accepted as recovery.

Source: [`src/startup_recovery.py`](src/startup_recovery.py) and [`src/main.py`](src/main.py).

## Production admission controls

> **Payout-budget control:** all automated Solana token sends reserve rolling-cap capacity in
> SQLite before RPC, retain it across submitted or unknown outcomes, and settle it only from the
> exact confirmed signature. The positive-cap startup requirement is therefore enforced locally;
> target-chain timeout/crash/finality acceptance remains required. See [SECURITY.md](docs/SECURITY.md).

Set `SWAP_PRODUCTION_MODE=true` only after configuring and testing all controls. Production startup requires:

- positive `MAX_SWAP_USDC`, `MAX_SWAP_USDD` and `DAILY_PAYOUT_CAP_USDC` values (literal legacy-named active keys; no generic env aliases exist yet);
- `SOLANA_QUARANTINE_ACCOUNT` and `NEXUS_QUARANTINE_ACCOUNT`;
- `NEXUS_TOKEN_REGISTER_ADDRESS`;
- `ALERT_WEBHOOK_URL` or `ALERT_COMMAND`;
- valid Nexus HTTPS API URL, user and password;
- `NEXUS_SESSION` when multiuser mode is enabled.
- `NEXUS_SWAP_RECEIPTS_ENABLED=false`; named receipt assets spend NXS. A local pre-create budget ledger exists, but registration migration and target-node acceptance are still unproven.

The production switch accepts only `1/true/yes/on` and `0/false/no/off`, case-insensitively. A typo fails closed. A configured alert route is not proof of delivery; test it separately before live operation.

## Run and verify

Run local, non-chain tests first:

```bash
python -m pytest -q
python3 scripts/check_markdown_links.py
```

Then start under a supervisor:

```bash
python3 swapService.py
```

The entrypoint exits nonzero when admission or recovery refuses startup. Only one process may use a state database; an exclusive lock at `SWAP_LOCK_PATH` (default `<STATE_DB_PATH>.lock`) rejects a second instance.

Example systemd service:

```ini
[Unit]
Description=Configured Solana/Nexus token swap service
After=network-online.target

[Service]
Type=simple
User=swapsvc
WorkingDirectory=/opt/swapService
EnvironmentFile=/opt/swapService/.env
ExecStart=/opt/swapService/.venv/bin/python3 swapService.py
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
```

On a test deployment, verify both directions with the configured token symbols and exact published terms. Confirm on-chain results and durable database transitions; do not infer success from process output alone. Also test invalid Solana memos, oversized inputs, failed target validation, alert delivery, restart recovery and paused-mode handling.

## Held Nexus credits: no automatic refund

A missing/invalid Nexus mapping, an oversized Nexus credit, or another refund condition is held for operator review. The service loop does **not** automatically debit the Nexus treasury for a refund or quarantine movement.

After independent source- and target-chain review, use the audited intent CLI. Each mutating step requires `--operator` and `--reason`; authorization and finalization also require exact copied evidence:

```bash
python3 nexus_transfer_operator.py prepare --kind refund \
  --txid <NEXUS_CREDIT_TXID> --contract-id <CREDIT_CONTRACT_ID> \
  --operator <OPERATOR> --reason "<REVIEWED_RATIONALE>"

python3 nexus_transfer_operator.py show --intent <INTENT_ID>

python3 nexus_transfer_operator.py authorize --intent <INTENT_ID> \
  --confirm-reference <DISPLAYED_REFERENCE> \
  --operator <OPERATOR> --reason "<REVIEWED_RATIONALE>"

python3 nexus_transfer_operator.py execute --intent <INTENT_ID> \
  --operator <OPERATOR> --reason "<ONE_TIME_EXECUTION_RATIONALE>"

python3 nexus_transfer_operator.py resolve

python3 nexus_transfer_operator.py finalize --intent <INTENT_ID> \
  --confirm-remote-txid <CONFIRMED_REMOTE_TXID> \
  --operator <OPERATOR> --reason "<CONFIRMED_EVIDENCE>"
```

Do not rerun `execute` after a timeout, nonzero result or unparsable response. Those outcomes may follow an accepted debit and remain `outcome_unknown` until positive reference resolution. `prepare --kind quarantine` targets `NEXUS_QUARANTINE_ACCOUNT` through its legacy internal attribute. `list`, `show` and `resolve` do not invoke a debit; `resolve` only recognizes positive chain-reference matches.

Source: [`nexus_transfer_operator.py`](nexus_transfer_operator.py) and the durable intent implementation in [`src/nexus_client.py`](src/nexus_client.py).

## State, monitoring and troubleshooting

- `STATE_DB_PATH` is the authoritative SQLite state store. Some schema and status names remain legacy-frozen for upgrade safety.
- `FEES_STATE_FILE` is a legacy JSON fee journal reconciled against the database; the database wins on drift.
- The read-only dashboard is a separate process: `python3 dashboard.py`. It opens SQLite in read-only mode and has no retry/refund/release endpoint. Non-loopback binding requires `DASHBOARD_TOKEN`; use a TLS reverse proxy or SSH tunnel.
- A backing deficit or incomplete mint reconciliation pauses new exposure but keeps existing Solana refund/quarantine and confirmation work running.
- Backing surplus is alert-only. No automatic DEX trade or Nexus surplus mint runs.
- A heartbeat update failure can freeze liveness and waterlines because Nexus `format=basic` updates are atomic and cannot add missing fields.
- An unreadable heartbeat during live Solana polling uses the last locally persisted waterline only if one exists; otherwise Solana ingestion halts. This fallback does not weaken the mandatory complete startup-recovery gate.

Useful read-only tools:

```bash
python3 quarantine_viewer.py --solana
python3 quarantine_viewer.py --nexus
python3 nexus_transfer_operator.py list
python3 register_service.py --inspect <HEARTBEAT_ASSET_NAME>
```

The hidden `quarantine_viewer.py --usdc` / `--usdd` options are legacy CLI aliases; prefer `--solana` / `--nexus`.

## References

- [Configuration reference](CONFIG.md)
- [Asset mapping and current/planned provider records](ASSET_STANDARD.md)
- [docs/SECURITY.md](docs/SECURITY.md)
- [docs/STATE_MACHINES.md](docs/STATE_MACHINES.md)
- [docs/SWAP_INITIATOR_STATE_MACHINES.md](docs/SWAP_INITIATOR_STATE_MACHINES.md)
- [docs/AUDIT_FINDINGS.md](docs/AUDIT_FINDINGS.md)
- [Current evaluation and live acceptance gaps](docs/EVALUATION.md)

LICENSE: Provided as-is; no warranty.
