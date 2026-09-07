# swapService Configuration Reference

This reference follows the current code in [`src/config.py`](src/config.py), with runtime-only settings called out separately. `.env.example` is a template; this file defines semantics and validation.

## Runtime scope

One process configures exactly one pair:

- one Solana mint/account pair using the classic SPL Token Program;
- one Nexus token/treasury pair identified by token name and, for production identity checks, immutable register address.

Gross conversion is 1:1 in whole token units before fees. `config.rescale_units()` converts base units when token decimals differ. `config.SWAP_PAIR` is an immutable startup object containing the selected identities, custody accounts, precisions, fee policy and memo prefix.

This does not implement native SOL, Token-2022, arbitrary token programs, general-chain routing, automatic discovery or simultaneous pairs. Provider-v2 and multi-pair/general-chain support remain planned; see [ASSET_STANDARD.md](ASSET_STANDARD.md#provider-swapservice-asset-standard-v2-planned).

## Required startup keys

New configurations should use the canonical spelling in the first column.

| Canonical key | Legacy alias | Type | Required | Meaning |
|---|---|---:|---:|---|
| `SOLANA_RPC_URL` | — | URL | yes | Solana RPC endpoint. |
| `VAULT_KEYPAIR` | — | path | yes | JSON keypair for the Solana vault owner/signer. |
| `SOLANA_VAULT_ACCOUNT` | `VAULT_USDC_ACCOUNT` | pubkey | yes | Classic SPL token account holding the configured Solana token. |
| `SOLANA_TOKEN_MINT` | `USDC_MINT` | pubkey | yes | Configured classic SPL mint. |
| `SOL_MAIN_ACCOUNT` | — | pubkey | yes | Solana vault owner/base account. |
| `NEXUS_PIN` | — | secret | yes | Nexus profile PIN. |
| `NEXUS_TREASURY_ACCOUNT` | `NEXUS_USDD_TREASURY_ACCOUNT` | string | yes | Treasury account for the configured Nexus token. |

“Required” here means import of `src.config` fails when no accepted spelling has a non-empty value. Solana pubkeys are parsed during import. Production adds the controls in [Production admission](#production-admission).

## Canonical keys and legacy aliases

`_compat_env()` accepts either spelling below. If both are explicitly non-empty, their strings must be identical; otherwise startup raises instead of choosing precedence.

| Canonical key | Legacy alias | Default | Purpose |
|---|---|---:|---|
| `SOLANA_VAULT_ACCOUNT` | `VAULT_USDC_ACCOUNT` | required | Solana custody token account. |
| `SOLANA_TOKEN_MINT` | `USDC_MINT` | required | Solana mint identity. |
| `SOLANA_TOKEN_DECIMALS` | `USDC_DECIMALS` | `6` | Solana token precision. |
| `NEXUS_TOKEN_DECIMALS` | `USDD_DECIMALS` | `6` | Nexus token precision. |
| `NEXUS_TREASURY_ACCOUNT` | `NEXUS_USDD_TREASURY_ACCOUNT` | required | Nexus treasury custody account. |
| `SOLANA_QUARANTINE_ACCOUNT` | `USDC_QUARANTINE_ACCOUNT` | empty | Self-owned Solana quarantine token account. |
| `SOLANA_FEE_ACCOUNT` | `USDC_FEES_ACCOUNT` | empty | Optional Solana fee account; empty leaves fees in vault. |
| `NEXUS_QUARANTINE_ACCOUNT` | `NEXUS_USDD_QUARANTINE_ACCOUNT` | empty | Nexus destination for an explicitly authorized disposition. |
| `NEXUS_FEE_ACCOUNT` | `NEXUS_USDD_FEES_ACCOUNT` | empty | Optional Nexus fee-account setting. |
| `FEE_FLAT_TO_SOLANA` | `FLAT_FEE_USDC` | `0.5` | Fee deducted from Nexus→Solana output, in Solana token units. |
| `FEE_FLAT_TO_NEXUS` | `FLAT_FEE_USDD` | `0.1` | Fee deducted from Solana→Nexus output, in Nexus token units. |
| `FEE_REFUND_SOLANA` | `FLAT_FEE_USDD` | value of `FEE_FLAT_TO_NEXUS` | Fee deducted from an automated Solana-side refund. |
| `FEE_NEXUS_DISPOSITION` | `NEXUS_CONGESTION_FEE_USDD` | `0` | Fee term for a separately authorized Nexus disposition; not applied automatically. |
| `FEE_BPS` | `DYNAMIC_FEE_BPS` | `10` | Proportional swap fee in basis points. |

The old `FLAT_FEE_USDD` feeds two compatibility surfaces: Nexus output fee and Solana refund fee. To configure those independently, set `FEE_FLAT_TO_NEXUS` and `FEE_REFUND_SOLANA` and omit `FLAT_FEE_USDD`.

The following threshold/timeout pairs use first-non-empty fallback rather than `_compat_env()`. Canonical wins if both are set; unlike the table above, a disagreement is **not** rejected:

| Canonical key | Legacy fallback | Default |
|---|---|---|
| `MIN_DEPOSIT_SOLANA_TOKEN` | `MIN_DEPOSIT_USDC` | derived |
| `MIN_CREDIT_NEXUS_TOKEN` | `MIN_CREDIT_USDD` | derived |
| `DUST_CREDIT_NEXUS_TOKEN` | `DUST_CREDIT_USDD` | derived |
| `SOLANA_CONFIRM_TIMEOUT_SEC` | `USDC_CONFIRM_TIMEOUT_SEC` | `600` |
| `UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC` | `UNPROCESSED_PROCESS_BUDGET_SEC` | `30` |

Prefer only the canonical spelling in new files.

## Pair identity and custody

| Key | Type | Default | Notes |
|---|---:|---:|---|
| `SOLANA_TOKEN_SYMBOL` | string | `USDC` | Display metadata only; mint identity controls money paths. The default is historical, not universal support branding. |
| `SOLANA_TOKEN_DECIMALS` | int | `6` | Precision used for Solana base-unit parsing and formatting. |
| `NEXUS_TOKEN_NAME` | string | `USDD` | Nexus token name/symbol passed to token debit operations. The default is historical. |
| `NEXUS_TOKEN_REGISTER_ADDRESS` | string | empty | Immutable Nexus token register identity used for debit evidence and account validation; mandatory in production. |
| `NEXUS_TOKEN_DECIMALS` | int | `6` | Precision used for Nexus base-unit parsing and formatting. |
| `DEPOSIT_MEMO_PREFIX` | string | `nexus:` | Prefix on Solana deposits before the Nexus destination. |
| `SOLANA_QUARANTINE_ACCOUNT` | pubkey string | empty | Must be a self-owned classic SPL account for the configured mint. Required in production. |
| `SOLANA_FEE_ACCOUNT` | pubkey string | empty | Optional configured-mint fee account. |
| `NEXUS_QUARANTINE_ACCOUNT` | string | empty | Required in production, but movement requires the separate operator-intent workflow. |
| `NEXUS_FEE_ACCOUNT` | string | empty | Optional Nexus fee-account setting. Automatic surplus mint is disabled. |
| `NEXUS_USDD_LOCAL_ACCOUNT` | string | empty | Literal legacy-named compatibility setting used by Nexus local-balance helpers; no generic alias exists yet. It does not enable automatic refunds. |

### `SWAP_PAIR` mapping

The immutable object is assembled as follows:

- `SWAP_PAIR.solana`: mint, display symbol, decimals, vault, quarantine and fee account;
- `SWAP_PAIR.nexus`: immutable register address, token name/symbol, decimals, treasury, quarantine and fee account;
- `SWAP_PAIR.fees`: destination flat fees, Solana refund fee, Nexus disposition fee and basis points;
- `SWAP_PAIR.deposit_memo_prefix`: configured memo prefix.

Legacy Python attributes and persisted database names remain for compatibility. They do not change the configured values.

## Fees, minimums and limits

All decimal settings below are whole-token values. They are converted to integer base units during configuration. A value not exactly representable at the relevant configured precision raises `ValueError`.

| Key | Default | Unit/domain | Runtime behavior |
|---|---:|---|---|
| `FEE_FLAT_TO_NEXUS` | `0.1` | Nexus output token | Deducted from Solana→Nexus output. |
| `FEE_FLAT_TO_SOLANA` | `0.5` | Solana output token | Deducted from Nexus→Solana output. |
| `FEE_REFUND_SOLANA` | Nexus-output flat fee value | Solana token | Deducted from automated Solana deposit refunds/quarantine moves. |
| `FEE_NEXUS_DISPOSITION` | `0` | Nexus token | Stored in canonical fee policy; no automatic Nexus refund/quarantine path applies it. |
| `FEE_BPS` | `10` | basis points | Applied to successful swaps; valid range is `0..4999`. |
| `MIN_DEPOSIT_SOLANA_TOKEN` | derived | Solana input token | At least twice the Solana-scale equivalent of `FEE_FLAT_TO_NEXUS`; smaller configured values are raised. |
| `MIN_CREDIT_NEXUS_TOKEN` | derived | Nexus input token | At least twice the Nexus-scale equivalent of `FEE_FLAT_TO_SOLANA`; smaller configured values are raised. |
| `DUST_CREDIT_NEXUS_TOKEN` | derived | Nexus input token | Default is max(one Nexus base unit, one tenth of the Nexus-scale Solana-output flat fee). Credits below it are ignored; credits below the minimum but at/above dust are durably booked as fees. |
| `MAX_SWAP_USDC` | `0` | Solana token | Literal legacy-named active key; `0` disables outside production. Oversized Solana deposits follow the Solana refund path. |
| `MAX_SWAP_USDD` | `0` | Nexus token | Literal legacy-named active key; `0` disables outside production. Oversized Nexus credits are held for operator disposition, not automatically refunded. |
| `DAILY_PAYOUT_CAP_USDC` | `0` | Solana token | Legacy-named rolling 24-hour check in `send_solana_token()` (refund/quarantine paths). **The main Nexus→Solana payout helper bypasses this check.** `0` disables outside production; a positive production value does not close the bypass. |

There are currently no generic environment aliases for the three cap keys; preserve their literal spelling until code adds and validates a migration path.

`MICRO_DEPOSIT_FEE_PCT` and `MICRO_CREDIT_FEE_PCT` are parsed with default `100`, but current processing does not consume them as configurable percentages. Do not publish a non-100 policy based on those variables. `MAX_DEPOSITS_PER_LOOP` is also parsed (default `100`) but the current Solana poll path uses explicit processing bounds instead; do not rely on it as an enforced tuning knob.

## Nexus transport and identity

| Key | Default | Notes |
|---|---:|---|
| `NEXUS_CLI_PATH` | `./nexus` | Local/development compatibility transport when no API URL is configured. |
| `NEXUS_API_URL` | empty | When set, operations use HTTPS POST. Production requires HTTPS with hostname and no userinfo, query or fragment. |
| `NEXUS_API_USER` | empty | HTTP Basic-auth user; required in production. |
| `NEXUS_API_PASSWORD` | empty | HTTP Basic-auth password; required in production. |
| `NEXUS_MULTIUSER` | `false` | If true, session-scoped operations include `NEXUS_SESSION`. |
| `NEXUS_SESSION` | empty | Required in production when multiuser mode is true; deliberately omitted in single-user mode. |
| `NEXUS_CLI_TIMEOUT_SEC` | `20` | Nexus operation timeout. |
| `NEXUS_TRANSFER_MIN_CONFIRMATIONS` | `10` | Must be a positive integer; zero, negative and non-integer values fail configuration. |
| `NEXUS_RPC_HOST` | `http://127.0.0.1:8399` | Parsed compatibility setting; current main transport is selected by `NEXUS_API_URL` or `NEXUS_CLI_PATH`. |

The operator-intent CLI in [`nexus_transfer_operator.py`](nexus_transfer_operator.py) is separate from the service loop. Automatic Nexus refund/quarantine debits remain disabled. A timeout, nonzero result or unparseable execution response is outcome-unknown and must not be blindly retried.

## Polling, timeouts and state

| Key | Default | Notes |
|---|---:|---|
| `POLL_INTERVAL` | `10` | Legacy/global seconds fallback. |
| `SOLANA_POLL_INTERVAL` | `POLL_INTERVAL` | Solana loop cadence. |
| `NEXUS_POLL_INTERVAL` | `POLL_INTERVAL` | Nexus loop cadence. |
| `SOLANA_RPC_TIMEOUT_SEC` | `8` | Solana RPC timeout. |
| `SOLANA_TX_FETCH_TIMEOUT_SEC` | `12` | Individual transaction fetch timeout. |
| `SOLANA_POLL_TIME_BUDGET_SEC` | `15` | Solana watchdog join budget. Over-budget work is not killed; its next run is skipped until completion. |
| `NEXUS_POLL_TIME_BUDGET_SEC` | `15` | Nexus enumeration watchdog join budget. |
| `UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC` | `30` | Nexus queued-processing watchdog join budget. |
| `SOLANA_MAX_TX_FETCH_PER_POLL` | `120` | Core Solana transaction-fetch bound. |
| `MAX_CREDITS_PER_LOOP` | `100` | Checked between Nexus transactions; sibling CREDIT contracts within one transaction can exceed the value. This is not a strict per-contract ceiling. |
| `MAX_ACTION_ATTEMPTS` | `3` | Persistent action retry budget. Nexus transfer intents still prohibit blind debit retry. |
| `ACTION_RETRY_COOLDOWN_SEC` | `300` | Minimum retry spacing. |
| `REFUND_TIMEOUT_SEC` | `3600` | Mapping wait before Nexus credit moves to refund hold/review. It does not trigger an automatic Nexus debit. |
| `STALE_DEPOSIT_QUARANTINE_SEC` | `86400` | Age before eligible stale Solana rows are marked for quarantine handling. |
| `SOLANA_CONFIRM_TIMEOUT_SEC` | `600` | Outbound Solana confirmation timeout. |
| `STALE_ROW_SEC` | `86400` | General stale-row age threshold. |
| `METRICS_BUDGET_SEC` | `5` | Parsed setting; the current main metrics block uses explicit per-call timeouts rather than this value. |
| `METRICS_INTERVAL_SEC` | `30` | Metrics emission cadence setting. |
| `STATE_DB_PATH` | `swap_service.db` | Authoritative SQLite database; read directly by `src/state_db.py`. |
| `FEES_STATE_FILE` | `fees_state.json` | Legacy JSON fee accumulator. SQLite fee entries are authoritative on drift. |

`HELIUS_RPC_URL` and `HELIUS_API_KEY` are read directly by `src/solana_client.py`. Full URL wins; otherwise the key is used to construct the Helius endpoint. If neither is set, core RPC is used. `POLL_HELIUS_LIMIT`, `NEXUS_MAX_PAGES` and `FEE_EVENTS_FILE` appear as Python `getattr()` compatibility hooks but are not loaded from environment by `src/config.py`; documenting them as `.env` options would be incorrect.

### Solana finality

| Key | Default | Notes |
|---|---:|---|
| `SOLANA_DEPOSIT_COMMITMENT` | `finalized` | Used for deposit admission and outbound settlement. `confirmed` is not rooted and can be reorged. |
| `SOLANA_FINALIZED_ABOVE_UNITS` | `0` | Raw Solana base-unit threshold that always requires finalization if the general commitment is relaxed. `0` disables this carve-out. |

## Heartbeat and recovery

| Key | Default | Notes |
|---|---:|---|
| `HEARTBEAT_ENABLED` | `true` | Consulted by service-record publication and heartbeat validation. Main startup recovery and polling still depend on the name-addressed asset when false. |
| `NEXUS_HEARTBEAT_ASSET_NAME` | empty | Current live lookup identity. In practice required for complete startup recovery. |
| `NEXUS_HEARTBEAT_ASSET_ADDRESS` | empty | Recorded configuration only; current runtime does not use it to select the asset. Address-based provider-v2 is planned. |
| `HEARTBEAT_MIN_INTERVAL_SEC` | `max(10, POLL_INTERVAL)` | Parsed and clamped to at least 10 seconds; current live update functions do not consult it. |
| `HEARTBEAT_WATERLINE_ENABLED` | `true` | Controls Nexus live-poller cutoff use. Startup recovery still requires both waterlines. |
| `HEARTBEAT_WATERLINE_SOLANA_FIELD` | `last_safe_timestamp_solana` | Top-level field name on the current v1 asset. |
| `HEARTBEAT_WATERLINE_NEXUS_FIELD` | `last_safe_timestamp_nexus` | Top-level field name on the current v1 asset. |
| `HEARTBEAT_WATERLINE_SAFETY_SEC` | `120` | Safety margin used when advancing/filtering checkpoints. |

The two field names must be non-empty and distinct at runtime and must already exist on the `format=basic` asset. Values parse as non-negative integers, but **startup admission requires both to be strictly positive**. Missing/zero checkpoints or incomplete chain enumeration makes recovery incomplete and the entrypoint exits nonzero. Do not initialize to “now” to bypass custody history.

## Backing and alerting

| Key | Default | Notes |
|---|---:|---|
| `BACKING_DEFICIT_BPS_ALERT` | `10` | Deficit alert threshold. |
| `BACKING_DEFICIT_PAUSE_PCT` | `90` | New-exposure pause floor. Existing refund/quarantine/confirmation work continues. |
| `BACKING_RECONCILE_INTERVAL_SEC` | `3600` | Minimum spacing for read-only backing/surplus checks. |
| `BACKING_SURPLUS_MINT_THRESHOLD_USDC` | `20` | Literal legacy-named active setting in Solana token units. Surplus at/above this threshold is alert-only; automatic mint/rebalance is disabled. |
| `ALERT_WEBHOOK_URL` | empty | JSON webhook route. |
| `ALERT_COMMAND` | empty | Executable receiving alert JSON on stdin. |
| `ALERT_MIN_INTERVAL_SEC` | `300` | Per-event deduplication interval. |

An unavailable, malformed, incomplete or discrepant balance reconciliation latches new exposure paused until a later explicitly healthy result.

## Production admission

`SWAP_PRODUCTION_MODE` defaults to `false`. It is the only boolean parsed strictly: accepted values are `1/true/yes/on` and `0/false/no/off`, case-insensitively with surrounding whitespace ignored. Any other present value raises.

When true, startup refuses before polling unless all of these are present:

- `MAX_SWAP_USDC > 0`;
- `MAX_SWAP_USDD > 0`;
- `DAILY_PAYOUT_CAP_USDC > 0`;
- `SOLANA_QUARANTINE_ACCOUNT`;
- `NEXUS_QUARANTINE_ACCOUNT`;
- `NEXUS_TOKEN_REGISTER_ADDRESS`;
- either `ALERT_WEBHOOK_URL` or `ALERT_COMMAND`;
- valid `NEXUS_API_URL`, plus `NEXUS_API_USER` and `NEXUS_API_PASSWORD`;
- `NEXUS_SESSION` when `NEXUS_MULTIUSER=true`.

This validates configuration presence, not endpoint reachability or alert delivery. Live operation remains gated on the target-node and both-chain acceptance work tracked in [docs/EVALUATION.md](docs/EVALUATION.md).

Other booleans (`NEXUS_MULTIUSER`, `HEARTBEAT_ENABLED`, `HEARTBEAT_WATERLINE_ENABLED`, `SKIP_OWNER_LOOKUP_FOR_MICRO_USDD`, `MICRO_CREDIT_COUNT_AGAINST_LIMIT`) use permissive parsing: only `1/true/yes/on` means true; any other value becomes false. Avoid typos even where code does not reject them.

## Service identity and dashboard

| Key | Default | Notes |
|---|---:|---|
| `SERVICE_PROVIDER` | empty | Published operator identity; current record displays `unnamed-operator` when empty. |
| `SERVICE_VERSION` | `1.0.0` | Published v1 service version. |
| `SERVICE_CONTACT` | empty | Published contact; current record substitutes `-` when empty. |
| `DASHBOARD_HOST` | `127.0.0.1` | Read directly by dashboard runtime. Non-loopback requires a token. |
| `DASHBOARD_PORT` | `8787` | Dashboard listen port. |
| `DASHBOARD_TOKEN` | empty | Bearer token; required for non-loopback binding. Send in `Authorization`, not a query string. |
| `SWAP_LOCK_PATH` | `<STATE_DB_PATH>.lock` | Read directly by `src/main.py`; singleton process lock path. |

The dashboard is read-only and can start without chain credentials. It uses configured symbols/decimals for display; legacy database columns remain unchanged.

## Literal legacy interfaces

These names must be written literally when interacting with current compatibility surfaces:

- active env keys with no generic alias: `MAX_SWAP_USDC`, `MAX_SWAP_USDD`, `DAILY_PAYOUT_CAP_USDC`, `BACKING_SURPLUS_MINT_THRESHOLD_USDC`, `NEXUS_USDD_LOCAL_ACCOUNT`;
- parsed legacy settings `SKIP_OWNER_LOOKUP_FOR_MICRO_USDD` and `MICRO_CREDIT_COUNT_AGAINST_LIMIT` are not consumed by current credit admission: accepted below-minimum credits still perform owner lookup and count toward processing; there is no micro aggregation flush;
- hidden CLI aliases: `quarantine_viewer.py --usdc` / `--usdd` (prefer `--solana` / `--nexus`);
- database columns, retry-budget keys, reservation kinds and status strings documented in source as frozen upgrade interfaces.

Do not mass-rename these in an operator migration. They are compatibility identifiers, not universal token branding.

## Validation summary

At configuration import/startup, current code enforces:

1. required keys have at least one non-empty accepted spelling;
2. canonical/legacy `_compat_env()` values do not conflict;
3. Solana address strings parse as pubkeys;
4. numeric values parse in their declared integer/decimal domain;
5. fee values are exactly representable and non-negative;
6. `FEE_BPS` is from 0 through 4999;
7. `NEXUS_TRANSFER_MIN_CONFIRMATIONS` is a positive integer;
8. minimums are floored at twice the relevant flat fee;
9. strict production-mode syntax and production controls pass;
10. startup recovery returns explicit complete authoritative evidence before polling.

Some integer settings do not have dedicated positivity/range validation. Configuration acceptance alone is not a production safety claim.

## Source references

- Pair construction, aliases, defaults and validation: [`src/config.py`](src/config.py)
- Production gate and runtime order: [`src/main.py`](src/main.py)
- Mandatory recovery and positive checkpoints: [`src/startup_recovery.py`](src/startup_recovery.py)
- Classic SPL Token Program enforcement: [`src/solana_client.py`](src/solana_client.py)
- Nexus transport and held transfer intents: [`src/nexus_client.py`](src/nexus_client.py), [`nexus_transfer_operator.py`](nexus_transfer_operator.py)
- Setup procedure: [SETUP.md](SETUP.md)
- [docs/SECURITY.md](docs/SECURITY.md)
