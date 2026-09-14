# Nexus Bridge Asset Standard

This document covers the Nexus assets used by the current swap service. The runtime bridges one
operator-configured pair in both directions:

- **Solana token → Nexus token**: send the configured classic SPL token to
  `SOLANA_VAULT_ACCOUNT` with memo `DEPOSIT_MEMO_PREFIX<NEXUS_TOKEN_ACCOUNT>`.
- **Nexus token → Solana token**: send the configured Nexus token to
  `NEXUS_TREASURY_ACCOUNT`, then publish the Nexus transaction id and destination SPL token
  account in the user mapping asset below.

`SOLANA_TOKEN_MINT`, `SOLANA_VAULT_ACCOUNT`, `SOLANA_TOKEN_SYMBOL`,
`SOLANA_TOKEN_DECIMALS`, `NEXUS_TOKEN_NAME`, `NEXUS_TOKEN_REGISTER_ADDRESS`,
`NEXUS_TREASURY_ACCOUNT`, and `NEXUS_TOKEN_DECIMALS` are the canonical pair settings. Historical
USDC/USDD environment aliases, database columns, retry keys, status values, and v1 asset fields are
retained for compatibility; their names do not restrict the configured token values.

> **Current scope:** one classic SPL Token Program mint and one Nexus token register per service
> process. Multi-pair routing, additional destination chains, the provider-v2 record below, and
> Token-2022 are not implemented. `src/solana_client.py` hard-codes the classic Token Program id
> and rejects token accounts owned by another program.

## User mapping asset (current runtime contract)

The Nexus→Solana path looks up an asset by the exact persisted fields `txid_toService` and `owner`,
then reads `receival_account`. These field names are an existing wire contract and are not renamed.

| Field | Runtime meaning | Required |
|---|---|---|
| `txid_toService` | Nexus transaction id containing the user's treasury CREDIT | Yes |
| `receival_account` | Existing classic SPL token account for the configured `SOLANA_TOKEN_MINT` | Yes |
| `owner` | Built-in Nexus asset owner; must match the sender's genesis owner | Returned by Nexus |

The current runtime does **not** route on `toChain`, `fromToken`, `toToken`, or `distordiaType`.
Those legacy informational fields may exist, but they do not authorize another chain or pair. The
only supported destination is the configured Solana mint, and `receival_account` must itself be a
token account for that mint; a wallet address is not converted to an ATA and the service does not
create an ATA.

### Create once

```bash
nexus assets/create/asset name=<USER_MAPPING_ASSET_NAME> format=basic \
    txid_toService="" \
    receival_account=<EXISTING_SOLANA_TOKEN_ACCOUNT> \
    pin=<YOUR_PIN>
```

Optional compatibility metadata can be included at creation if desired:

```text
distordiaType=nexusBridge toChain=solana \
fromToken=<NEXUS_TOKEN_SYMBOL> toToken=<SOLANA_TOKEN_SYMBOL>
```

Because `format=basic` fixes the field set, include every field you intend to update when creating
the asset.

### Update for each Nexus→Solana swap

1. Debit the configured Nexus token account to the published `NEXUS_TREASURY_ACCOUNT`:

   ```bash
   nexus finance/debit/account from=<YOUR_NEXUS_TOKEN_ACCOUNT> \
       to=<NEXUS_TREASURY_ACCOUNT> amount=<AMOUNT> pin=<YOUR_PIN>
   ```

2. Save the returned transaction id and update the mapping:

   ```bash
   nexus assets/update/asset name=<USER_MAPPING_ASSET_NAME> format=basic \
       txid_toService=<NEXUS_DEBIT_TXID> \
       receival_account=<EXISTING_SOLANA_TOKEN_ACCOUNT> \
       pin=<YOUR_PIN>
   ```

3. The service enumerates CREDIT contracts to the configured treasury, preserves each source as
   `(txid, contract_id)`, confirms the asset owner, and validates the destination account's classic
   Token Program owner and configured mint.

### Lookup and failure behavior

The service performs a bounded owner-and-transaction mapping lookup equivalent to:

```text
register/list/assets:asset/owner,txid_toService,receival_account
results.txid_toService=<NEXUS_DEBIT_TXID>
results.owner=<SENDER_OWNER_HASH>
```

A failed, malformed, incomplete, ambiguous, or owner-mismatched lookup remains held. Complete
absence after `REFUND_TIMEOUT_SEC`, an invalid destination, an over-cap credit, or exhausted payout
attempts also enters `"refund held for operator review"` (possibly through legacy statuses such as
`"refund pending"`). The runtime does **not** automatically debit the Nexus treasury for a refund.
Any Nexus refund/quarantine disposition uses the durable operator-intent workflow and positive
on-chain evidence.

Before the Solana payout RPC, the service atomically freezes `payout_solana_units` and
`payout_fee_nexus_units` on the exact `(txid, contract_id)` row. A signature or confirmation status
alone is not settlement: finalization requires a successful finalized transaction whose source,
signer, mint, recipient, amount, memo, transaction id, and contract id match the frozen evidence.
Unknown or mismatched outcomes hold rather than resubmit or refund.

## Solana→Nexus payout receipt asset (opt-in current extension)

When `NEXUS_SWAP_RECEIPTS_ENABLED=true`, confirmed Solana→Nexus payouts enqueue one durable
`nexus-swap-receipt-v1` publication obligation. Publication is separate from money movement and is
disabled by default. The asset is created with `format=JSON`; its `json=` parameter contains the
following 11 definitions in this order, each with `type: "string"` and `mutable: false`:

| Field | Exact meaning |
|---|---|
| `distordiaType` | Exact discriminator `nexusSwapReceipt` |
| `schema` | Exact schema `nexus-swap-receipt-v1` |
| `source_signature` | Full finalized Solana deposit signature |
| `solana_mint` | Canonical configured Solana mint |
| `solana_vault` | Canonical configured Solana vault token account |
| `nexus_token` | Canonical configured Nexus token register address |
| `nexus_account` | Exact Nexus payout account parsed from the source memo |
| `output_txid` | Confirmed Nexus DEBIT transaction id |
| `output_contract_id` | Canonical unsigned decimal string identifying that DEBIT contract |
| `output_units` | Positive canonical unsigned decimal string in Nexus base units |
| `reference` | Canonical unsigned decimal string copied from the exact DEBIT evidence |

The Nexus built-in `owner` is not a custom schema field and is never sent as an `owner=` creation
argument. Nexus derives it from the authenticated publisher profile. Before creation, the service
reads its registration owner and requires it to equal the owner frozen with the receipt obligation.

Readers retain the snake-case field contract above and query by the explicit Nexus query parameter:

```text
register/list/assets:asset/owner,address,distordiaType,schema,source_signature,solana_mint,solana_vault,nexus_token,nexus_account,output_txid,output_contract_id,output_units,reference
where=results.source_signature=<FULL_SOLANA_SIGNATURE>
```

A receipt is evidence only when exactly one result matches every frozen field and the provider owner.
The full Solana signature and exact Nexus `(output_txid, output_contract_id)` bind source to output;
the sequential `reference` is supplemental evidence and is never sufficient by itself. Clients must
also verify the referenced Nexus DEBIT and its spendable CREDIT/finality. Failed, malformed,
incomplete, owner-mismatched, or duplicate receipt readback remains unresolved. A timeout or crash
after crossing the durable create boundary never causes a blind second create.

> **Receipt release gate:** current upstream Nexus API documentation pinned at core commit
> [`1185145534a20ed4d2288e4513c505f271be536d`](https://github.com/Nexusoft/LLL-TAO/blob/1185145534a20ed4d2288e4513c505f271be536d/docs/API/COMMANDS/ASSETS.MD)
> states that asset creation costs 1 NXS and the deterministic optional name another 1 NXS on
> mainnet. Receipt publication is therefore a financial side effect even though it never moves bridged tokens. The current runtime
> has no separate NXS receipt budget/accounting control, and the target node has not verified JSON
> creation, global Query DSL filtering, projection shape, indexing delay or duplicate readback.
> Keep `NEXUS_SWAP_RECEIPTS_ENABLED=false` for production until those controls and live tests pass.

An existing v1 `format=basic` provider asset cannot gain the immutable `receipt_schema` field through
heartbeat updates. Enabling this extension requires a reviewed recreation/migration to a provider
record created with that field; otherwise receipts may be produced without being advertised by the
registration. See the [2026-09-08 development review](docs/DEVELOPMENT_REVIEW_2026-09-08.md).

## Current registration / heartbeat assets (v1)

The service currently addresses a heartbeat by `NEXUS_HEARTBEAT_ASSET_NAME`; the configured
`NEXUS_HEARTBEAT_ASSET_ADDRESS` is recorded but is not the runtime selector. Canonical waterline
field defaults are `last_safe_timestamp_solana` and `last_safe_timestamp_nexus`; custom names must
match the fields created on-chain.

Two v1 creation tools exist and their fixed field sets are not interchangeable:

- `register_service.py` creates the recommended current registration from
  `src.nexus_client.SERVICE_RECORD_FIELDS`. It describes the one configured pair with
  `nexus_token`, `nexus_token_register_address`, `solana_token`, and `solana_vault_mint`, plus
  custody, memo, fee, minimum, status, contact, and liveness fields.
- `create_heartbeat_asset.py` creates the older compatibility heartbeat. Its literal metadata
  fields include `supported_chains`, `supported_tokens`, `nexus_treasury_token`,
  `solana_vault_token`, and `solana_vault_mint`. Defaults such as `USDD:USDC` are historical tool
  defaults, not evidence of multiple runtime pairs and not canonical configuration names; pass the
  configured symbols and addresses explicitly if this helper is retained.

In both schemas, waterlines are custody checkpoints, not general progress counters. Startup requires
a readable heartbeat with positive checkpoints and **complete** Solana and Nexus reconstruction
before any chain activity. Missing/zero checkpoints, malformed evidence, legacy payout memos, or
incomplete scans abort startup. Live Nexus polling does not advance from an empty enumeration, and
any mutable nonzero-offset page request holds the checkpoint.

## Fees and thresholds

Values are operator configuration, not fixed USDC/USDD amounts. Current canonical settings include
`FEE_FLAT_TO_NEXUS`, `FEE_FLAT_TO_SOLANA`, `FEE_REFUND_SOLANA`, `FEE_NEXUS_DISPOSITION`,
`FEE_BPS`, `MIN_DEPOSIT_SOLANA_TOKEN`, `MIN_CREDIT_NEXUS_TOKEN`, and
`DUST_CREDIT_NEXUS_TOKEN`. The service converts between independently configured decimals and
raises an omitted or too-low processing minimum to twice the corresponding output flat fee.
Legacy names such as `FLAT_FEE_USDC`, `FLAT_FEE_USDD`, `DYNAMIC_FEE_BPS`,
`MIN_DEPOSIT_USDC`, and `MIN_CREDIT_USDD` remain accepted compatibility aliases where implemented;
conflicting canonical and legacy values are rejected for `_compat_env` settings.

---

## Provider swapService Asset Standard v2 (Planned)

> **Implementation status:** This is a target architecture and migration contract only. The current
> configurable single-pair service still uses the name-addressed v1 heartbeat/registration described
> above. Do not configure a v2 record as the live waterline source: address-based selection,
> discovery validation, the complete v2 writer, and multi-instance isolation are not implemented.

### Design decision

The provider record SHALL carry the exact field/value:

```json
{"distordia-type": "swapService"}
```

This is better than treating a signature-chain-local asset name as the service identity. A Nexus
signature chain can own any number of swapService provider assets, so one operator can run several
token pairs or isolated deployments without competing for one local name. Type discovery and
instance identity are intentionally separate:

- `distordia-type=swapService` identifies the class of asset and can return many records;
- the asset's Nexus register address is the canonical identity used for reads and updates;
- immutable `service_id` identifies the logical deployment across monitoring/indexing systems;
- the built-in Nexus `owner` identifies the provider signature chain;
- a local asset name, if present, is only a human-friendly alias and MUST NOT be required or assumed
  unique across providers or service instances.

The type field alone MUST NOT be used to select a writable asset. A service that discovers two type
matches and updates the first one could corrupt another instance's waterlines or advertised terms.

### Provider-record goals

A single v2 asset gives users and auditors a complete, non-secret overview of the deployment:

1. provider identity, contact, source and terms;
2. exact chain/network and token identities, not just display tickers;
3. custody, quarantine and fee-account addresses needed to verify backing and fund flows;
4. enabled directions and the deposit/mapping contract;
5. the complete effective fee, minimum, dust and cap policy;
6. software/schema versions, public-terms hash and effective timestamp;
7. service status, pause reason, liveness and safe waterlines.

Do not publish PINs, sessions, API credentials, signer paths, private RPC URLs, webhook URLs, internal
database paths or any other secret/operational capability. Balances remain chain-derived: clients
query the published treasury and vault addresses instead of trusting copied balance fields.

### Canonical fields

#### Immutable identity and pair contract

These values define one deployment. Changing one creates a new service record/address rather than
silently changing the meaning of an existing service.

| Field | Required value / meaning | Example |
|-------|--------------------------|---------|
| `distordia-type` | Exact type discriminator | `swapService` |
| `schema_version` | Provider-record schema | `2` |
| `service_id` | Provider-chosen globally stable identifier | `<PROVIDER_PAIR_INSTANCE_ID>` |
| `nexus_network` | Nexus network identifier | `mainnet` |
| `nexus_token_name` | Display/API token name; never the sole security identity | `<NEXUS_TOKEN_SYMBOL>` |
| `nexus_token_address` | Immutable Nexus token register address | `8ABC...` |
| `nexus_token_decimals` | Nexus token precision | `6` |
| `nexus_treasury_address` | Nexus account receiving input liquidity | `8Cuy...` |
| `nexus_quarantine_address` | Nexus disposition/quarantine account, or `-` if unsupported | `8Qrt...` |
| `nexus_fee_address` | Nexus fee destination/accounting address, or `treasury` | `treasury` |
| `solana_cluster` | Solana cluster or genesis identifier | `mainnet-beta` |
| `solana_token_symbol` | Display ticker only | `<SOLANA_TOKEN_SYMBOL>` |
| `solana_token_mint` | Canonical SPL mint address | `EPjF...` |
| `solana_token_decimals` | SPL mint precision | `6` |
| `solana_vault_address` | SPL token account holding service liquidity | `Bg1M...` |
| `solana_quarantine_address` | Self-owned quarantine token account, or `-` | `7Qrt...` |
| `solana_fee_address` | Fee destination/accounting address, or `vault` | `vault` |
| `enabled_directions` | Explicit comma-separated directions | `solana-to-nexus,nexus-to-solana` |
| `user_mapping_type` | User asset protocol/type for Nexus→external routing | `nexusBridge` |

The Nexus token register address and Solana mint are the authorization/reconciliation identities.
Tickers are untrusted presentation metadata and may collide.

#### Mutable provider and protocol metadata

| Field | Purpose | Example |
|-------|---------|---------|
| `provider` | Operator/provider name | `distordia` |
| `contact` | Public support/security contact | `https://example.org/contact` |
| `source_url` | Source repository/release provenance | `https://github.com/.../swapService` |
| `terms_url` | Human-readable terms and incident policy | `https://example.org/swap/terms` |
| `software_version` | Running service release | `1.2.0` |
| `memo_prefix` | Solana deposit memo prefix | `nexus:` |
| `mapping_schema_version` | Supported user mapping-asset schema | `1` |

#### Mutable fee, threshold and exposure terms

All decimal values are canonical non-scientific token-unit strings and must be exactly representable
at the published precision. Basis points are non-negative integers. Zero explicitly disables a fee;
an omitted field does not.

| Field | Units / semantics | Example |
|-------|-------------------|---------|
| `fee_flat_to_nexus` | Nexus output token units deducted from Solana→Nexus output | `0.1` |
| `fee_bps_to_nexus` | Basis points applied to Solana input | `10` |
| `fee_flat_to_solana` | Solana output token units deducted from Nexus→Solana output | `0.5` |
| `fee_bps_to_solana` | Basis points applied to Nexus input | `10` |
| `fee_refund_solana` | Solana token units retained from a Solana-side refund | `0.1` |
| `fee_nexus_disposition` | Nexus token units retained from an authorized refund/disposition | `0` |
| `micro_fee_pct_solana_input` | Percent retained when Solana input is below the process minimum | `100` |
| `micro_fee_pct_nexus_input` | Percent retained when Nexus input is below the process minimum | `100` |
| `min_input_solana` | Minimum processed Solana-side input | `0.2` |
| `dust_input_solana` | Solana input below which policy ignores/rejects the item | `0` |
| `min_input_nexus` | Minimum processed Nexus-side input | `1.0` |
| `dust_input_nexus` | Nexus input below which policy ignores the item | `0.01` |
| `max_input_solana` | Maximum accepted Solana-side input; `0` means no configured cap | `1000` |
| `max_input_nexus` | Maximum accepted Nexus-side input; `0` means no configured cap | `1000` |
| `daily_payout_cap_solana` | Rolling 24-hour Solana-output cap; `0` means no configured cap | `5000` |
| `terms_version` | Monotonic public fee/limit revision | `7` |
| `terms_effective_at` | Unix timestamp at which this revision became effective | `1706012000` |
| `terms_hash` | Hash of canonical **public** pair/fee/limit fields only | `sha256:ab12...` |

The service must compute execution terms and this published table from the same validated fee-policy
object. A monitor can therefore detect a stale or tampered record, and a user can calculate the
expected output before sending funds. `terms_hash` must never include secrets.

#### Mutable operational state

| Field | Purpose | Example |
|-------|---------|---------|
| `status` | `online`, `paused`, `maintenance`, `retired` or `starting` | `online` |
| `pause_reason` | Public non-secret reason code; `-` when not paused | `backing-deficit` |
| `last_poll_timestamp` | Unix timestamp of the last completed service poll cycle | `1706012456` |
| `last_safe_timestamp_solana` | Fail-closed Solana scan waterline | `1706012300` |
| `last_safe_timestamp_nexus` | Fail-closed Nexus scan waterline | `1706012200` |
| `record_updated_at` | Unix timestamp of the terms/status publication | `1706012456` |

Field names for the two waterlines may remain configurable during migration, but v2 writers and
readers must publish/validate the selected names in one schema and must not infer a waterline from a
missing field.

### Identity, discovery and update rules

1. **Discovery is read-only.** Index/list assets matching exact `distordia-type=swapService`, then
   validate schema, owner, immutable identities and required fields client-side. The exact list
   filter syntax must be proven against the target Nexus build; an unsupported filter is not an
   empty authoritative result.
2. **Selection is explicit.** Configure `NEXUS_SERVICE_ASSET_ADDRESS` (target variable name) and,
   in production, expected `service_id` and owner. Never select the first type match.
3. **Runtime access is by address.** Read and update the selected register address. A local name can
   be printed by tooling but is not needed by the poller or writer.
4. **Verify before trust.** Before reading a waterline or writing a heartbeat, compare owner, exact
   type, schema, service ID, token identities and custody addresses with local validated config.
   Any mismatch holds ingestion and alerts; it does not create or update another asset.
5. **One record per instance.** Two processes under one signature chain must have distinct service
   IDs, asset addresses, state databases and lock paths. Each process updates only its configured
   address.
6. **No silent field loss.** Creation must include the complete v2 field set and pass the Nexus
   register-size budget check. Update failure is atomic and cannot advance local waterlines.

### Example v2 provider record

```json
{
  "owner": "a1b2c3d4e5f6...",
  "address": "98Xbi3JoT5iNFYZ...",
  "distordia-type": "swapService",
  "schema_version": "2",
  "service_id": "<PROVIDER_PAIR_INSTANCE_ID>",
  "provider": "distordia",
  "contact": "https://example.org/contact",
  "source_url": "https://github.com/distordialabs-brutus/swapService",
  "software_version": "1.2.0",
  "nexus_network": "mainnet",
  "nexus_token_name": "<NEXUS_TOKEN_SYMBOL>",
  "nexus_token_address": "8ABC...",
  "nexus_token_decimals": "6",
  "nexus_treasury_address": "8Cuy...",
  "solana_cluster": "mainnet-beta",
  "solana_token_symbol": "<SOLANA_TOKEN_SYMBOL>",
  "solana_token_mint": "EPjF...",
  "solana_token_decimals": "6",
  "solana_vault_address": "Bg1M...",
  "enabled_directions": "solana-to-nexus,nexus-to-solana",
  "fee_flat_to_nexus": "0.1",
  "fee_bps_to_nexus": "10",
  "fee_flat_to_solana": "0.5",
  "fee_bps_to_solana": "10",
  "fee_refund_solana": "0.1",
  "fee_nexus_disposition": "0",
  "min_input_solana": "0.2",
  "min_input_nexus": "1.0",
  "dust_input_nexus": "0.01",
  "terms_version": "7",
  "terms_effective_at": "1706012000",
  "terms_hash": "sha256:ab12...",
  "status": "online",
  "pause_reason": "-",
  "last_poll_timestamp": "1706012456",
  "last_safe_timestamp_solana": "1706012300",
  "last_safe_timestamp_nexus": "1706012200",
  "record_updated_at": "1706012456"
}
```

The abbreviated example omits some required custody/fee/limit fields for readability; the creation
tool must emit every canonical field from the tables above and reject an incomplete record.

### Migration from the v1 named heartbeat

1. Implement and test address-based Nexus read/update support and the complete generic pair/fee
   configuration without changing live behavior.
2. Create a **new** v2 asset from validated configuration. `format=basic` v1 records have a fixed
   field set, so they cannot safely be relabeled or assumed complete.
3. Record and independently verify the returned address, owner, `service_id`, exact
   `distordia-type`, pair identities and custody addresses.
4. Run one compatibility release that can read the old named v1 asset only behind an explicit
   fallback flag while publishing the selected v2 address for monitors.
5. Switch the service and monitors to `NEXUS_SERVICE_ASSET_ADDRESS`; verify two service instances on
   one signature chain cannot read or update each other's waterlines.
6. Retire the v1 fallback only after target-node tests prove the exact hyphenated field, address-based
   reads/updates, atomic updates, size limits and multi-asset discovery behavior.

---

## Legacy Provider Heartbeat Asset Standards (v1; Current Implementations)

These are current compatibility schemas, not provider v2 and not multi-pair routing.

### Recommended current v1 registration (`register_service.py`)

The exact field contract comes from `src.nexus_client.SERVICE_RECORD_FIELDS`:

```text
# immutable on creation
distordiaType provider memo_prefix
nexus_token nexus_treasury_address nexus_token_register_address
solana_token solana_vault_address solana_vault_mint

# mutable/runtime terms and liveness
last_poll_timestamp last_safe_timestamp_solana last_safe_timestamp_nexus
status version contact fee_flat_to_nexus fee_flat_to_solana fee_bps
min_to_nexus min_to_solana
```

`register_service.py --show` derives values from `SWAP_PAIR`; `--create` creates the named basic
asset. The runtime still selects and updates it by `NEXUS_HEARTBEAT_ASSET_NAME`. Symbols are display
metadata; the Nexus register address and Solana mint are the token identities.

### Older compatibility heartbeat (`create_heartbeat_asset.py`)

This helper persists the following literal v1 metadata names. Keep them exact when maintaining an
existing asset:

```text
distordiaType provider version supported_chains supported_tokens
nexus_treasury_address nexus_treasury_token
solana_vault_address solana_vault_token solana_vault_mint
last_poll_timestamp last_safe_timestamp_solana last_safe_timestamp_nexus
```

`create_heartbeat_asset.py` still defaults `supported_chains=solana`,
`supported_tokens=USDD:USDC`, `nexus_treasury_token=USDD`, and `solana_vault_token=USDC`. Those are
**legacy defaults**, not runtime constants. For another configured pair, pass for example:

```bash
python create_heartbeat_asset.py --name <HEARTBEAT_ASSET_NAME> \
    --supported-chains solana \
    --supported-tokens <NEXUS_TOKEN_SYMBOL>:<SOLANA_TOKEN_SYMBOL> \
    --nexus-treasury-token <NEXUS_TOKEN_SYMBOL> \
    --solana-vault-token <SOLANA_TOKEN_SYMBOL> \
    --nexus-treasury-address <NEXUS_TREASURY_ACCOUNT> \
    --solana-vault-address <SOLANA_VAULT_ACCOUNT> \
    --solana-vault-mint <SOLANA_TOKEN_MINT>
```

`format=basic` fixes the field set. The runtime heartbeat updater can update only fields already
present and startup validation requires `last_poll_timestamp` plus both configured waterline names.
A successful write is persisted locally in the legacy `heartbeat(name,last_beat,wline_sol,wline_nxs)`
table; these database column names remain unchanged for compatibility.

### Monitor interpretation

Query the configured named asset and compare `last_poll_timestamp` with the operator's advertised
polling policy. Treat a fresh timestamp as liveness only, not proof that swaps are safe or caught up.
Waterlines identify the oldest recovery boundary accepted by the service. They advance only from
scan evidence; live empty Nexus results and mutable nonzero-offset pagination hold. On restart,
positive waterlines and complete reconstruction are mandatory before the service proceeds.
