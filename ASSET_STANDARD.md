# Nexus Bridge Asset Standard

This document defines the Nexus Asset standard for bridging tokens **from Nexus to external chains**. Users must create and maintain a **mutable** asset containing their destination chain receival address to receive funds after sending tokens to the bridge treasury.

> **Extensibility Note:** This standard is designed to support bridging to multiple destination chains (Solana, Ethereum, etc.) and multiple token pairs. The current implementation supports **USDD → USDC (Solana)**, but the asset schema is chain-agnostic.

## Purpose

When performing an outbound bridge (Nexus → External Chain):
1. User sends Nexus tokens (e.g., USDD) to the bridge treasury
2. User updates their bridge asset with the **txid** of the Nexus debit transaction
3. Bridge service queries assets filtering by `txid_toService` and verifies the **owner** matches the sender
4. Bridge service sends destination tokens to the `receival_account` on the specified `toChain`

The owner verification is critical for security: only the same signature chain that sent tokens can specify where bridged tokens are received.

---

## Asset Specification

### Required Fields

| Field | Purpose | Mutability | Example |
|-------|---------|------------|----------|
| `txid_toService` | The Nexus transaction hash of the token debit to treasury | **Mutable** (updated per swap) | `01b88ff8707638ac...` |
| `receival_account` | Destination address on target chain | **Mutable** (update if needed) | `5zq2GuFxVq...` (Solana) |

### Recommended Fields (Chain Routing)

| Field | Purpose | Mutability | Default | Example |
|-------|---------|------------|---------|----------|
| `toChain` | Target blockchain identifier | **Mutable** | `solana` | `solana`, `ethereum`, `polygon` |

> **Note:** When `toChain` is omitted, the bridge assumes `solana` for backward compatibility.

### Optional Fields (Informational)

| Field | Purpose | Mutability | Example |
|-------|---------|------------|----------|
| `distordiaType` | Asset type identifier | Immutable | `nexusBridge` |
| `fromToken` | Source token ticker on Nexus | **Mutable** | `USDD`, `NXS`, `DIST` |
| `toToken` | Destination token ticker | **Mutable** | `USDC`, `ETH`, `MATIC` |

---

## Format Options

### Recommended: `basic` Format

The simplest option for most users. All fields are mutable strings.

#### Create Asset (one-time setup)
```bash
nexus assets/create/asset name=distordiaBridge format=basic \
    txid_toService="" \
    receival_account="<YOUR_DESTINATION_ADDRESS>" \
    toChain=solana \
    distordiaType=nexusBridge \
    fromToken=USDD \
    toToken=USDC \
    pin=<YOUR_PIN>
```

#### Update Asset (per swap)
```bash
nexus assets/update/asset name=distordiaBridge format=basic \
    txid_toService=<NEXUS_DEBIT_TXID> \
    pin=<YOUR_PIN>
```

#### Update Receival Account (optional, if address changes)
```bash
nexus assets/update/asset name=distordiaBridge format=basic \
    receival_account=<NEW_DESTINATION_ADDRESS> \
    pin=<YOUR_PIN>
```

#### Switch Destination Chain (for multi-chain users)
```bash
nexus assets/update/asset name=distordiaBridge format=basic \
    toChain=ethereum \
    receival_account=<YOUR_ETHEREUM_ADDRESS> \
    pin=<YOUR_PIN>
```

---

### Alternative: `JSON` Format (Type-Safe)

Full type safety with explicit field definitions. Recommended for programmatic usage.

#### Create Asset
```bash
nexus assets/create/asset name=distordiaBridge format=JSON json='[
    {"name":"distordiaType","type":"string","value":"nexusBridge","mutable":false},
    {"name":"fromToken","type":"string","value":"USDD","mutable":true,"maxlength":16},
    {"name":"toToken","type":"string","value":"USDC","mutable":true,"maxlength":16},
    {"name":"toChain","type":"string","value":"solana","mutable":true,"maxlength":32},
    {"name":"txid_toService","type":"string","value":"","mutable":true,"maxlength":128},
    {"name":"receival_account","type":"string","value":"<YOUR_DESTINATION_ADDRESS>","mutable":true,"maxlength":128}
]' pin=<YOUR_PIN>
```

#### Update Asset (per swap)
```bash
nexus assets/update/asset name=distordiaBridge format=basic \
    txid_toService=<NEXUS_DEBIT_TXID> \
    pin=<YOUR_PIN>
```

> **Note:** Even with JSON format creation, updates use `format=basic` for simplicity. The type constraints from creation remain enforced.

> **Multi-chain Note:** The `receival_account` maxlength of 128 accommodates various chain address formats (Solana: 44, Ethereum: 42, etc.).

---

### Alternative: `raw` Format (Minimal)

For power users who want minimal overhead. Stores data as a single string blob.

> ⚠️ **Not recommended:** The service queries specific fields; raw format may not be compatible.

If used, the data string must be parseable as JSON with the required fields:

```bash
nexus assets/create/raw name=distordiaBridge format=raw \
    data='{"txid_toService":"<TXID>","receival_account":"<DESTINATION_ADDRESS>","toChain":"solana"}' \
    pin=<YOUR_PIN>
```

---

## Complete Swap Flow (USDD → Solana USDC Example)

### Step 1: Create Asset (One-Time Setup)

```bash
# Using basic format (recommended)
nexus assets/create/asset name=distordiaBridge format=basic \
    txid_toService="" \
    receival_account=5zq2Gu4...<YOUR_SOLANA_USDC_ATA> \
    toChain=solana \
    distordiaType=nexusBridge \
    fromToken=USDD \
    toToken=USDC \
    pin=1234
```

Expected result:
```json
{
    "success": true,
    "address": "87Wai2JoS4hNAEVXZVmejLS6pK21XQWKoLAkaep5aXFdrYnJJyk",
    "txid": "01230bbc8f0d72aaaff13471e34520d..."
}
```

### Step 2: Send Nexus Tokens to Treasury

```bash
nexus finance/debit/token from=USDD to=<TREASURY_ACCOUNT> amount=10.5 pin=1234
```

**Capture the `txid` from the response:**
```json
{
    "success": true,
    "txid": "01b88ff8707638acff63e05ca48dec9c79d5b9d754b065ae8f35e0b6cb8b90c6..."
}
```

### Step 3: Update Asset with Transaction ID

```bash
nexus assets/update/asset name=distordiaBridge format=basic \
    txid_toService=01b88ff8707638acff63e05ca48dec9c79d5b9d754b065ae8f35e0b6cb8b90c6... \
    pin=1234
```

### Step 4: Wait for Token Delivery

The bridge service will:
1. Detect your Nexus token debit to treasury and fetch your genesis ID (owner of debiting account)
2. Query assets for `txid_toService=<your_txid>` AND `owner=<your_owner_genesis>`
3. Read `toChain` to determine the destination blockchain (defaults to `solana`)
4. Validate the `receival_account` is a valid address on the target chain
5. Send net tokens (minus fees) to your `receival_account` on the specified chain

---

## Asset Query Used by Service

The service uses this query pattern to find your asset:

```bash
nexus register/list/assets:asset/owner,txid_toService,receival_account,toChain \
    results.txid_toService=<NEXUS_DEBIT_TXID> \
    results.owner=<SENDER_OWNER_HASH>
```

This ensures:
- Only assets matching the specific transaction are considered
- Only the original token sender can specify the destination
- Front-running attacks are prevented (attacker's asset has different owner)
- The `toChain` field routes to the correct blockchain handler

---

## Receival Account Formats

The `receival_account` format depends on the `toChain` value:

### Solana (`toChain=solana`)

| Type | Example | Notes |
|------|---------|-------|
| **Token Account (ATA)** | `5zq2GuFxVq...` (44 chars) | Direct SPL token account address |
| **Wallet Address** | `7HZ2PQXNE...` (44 chars) | Service derives the token ATA |

> **Important:** If using a wallet address, the token ATA must already exist. The service will NOT create it.

### Future Chains (Not Yet Implemented)

| Chain | Address Format | Example |
|-------|----------------|----------|
| `ethereum` | 0x-prefixed hex, 42 chars | `0x742d35Cc6634C0532925a3b844Bc9e7595f3...` |
| `polygon` | 0x-prefixed hex, 42 chars | `0x742d35Cc6634C0532925a3b844Bc9e7595f3...` |
| `arbitrum` | 0x-prefixed hex, 42 chars | `0x742d35Cc6634C0532925a3b844Bc9e7595f3...` |
| `base` | 0x-prefixed hex, 42 chars | `0x742d35Cc6634C0532925a3b844Bc9e7595f3...` |

> **Extensibility:** Address validation logic will be added per-chain as support is implemented.

---

## Validation Rules

| Check | Requirement | Failure Result |
|-------|-------------|----------------|
| `txid_toService` matches | Must match Nexus debit txid exactly | Asset not found |
| `owner` matches | Asset owner must equal token sender's owner | Asset ignored |
| `toChain` supported | Must be a supported chain (currently: `solana`) | Refund triggered |
| `receival_account` valid | Must be valid address for the target chain | Refund triggered |
| Token account exists | Recipient must have existing token account (chain-specific) | Refund triggered |
| Asset published in time | Before `REFUND_TIMEOUT_SEC` (default 1 hour) | Refund triggered |

---

## Fees

| Fee Type | Amount | Notes |
|----------|--------|-------|
| Asset creation | 1 NXS | One-time cost |
| Asset naming | 1 NXS | Optional, one-time |
| Asset updates | Free | No cost if ≥10s apart |
| Swap fee (flat) | 0.1 USDC | Deducted from output |
| Swap fee (dynamic) | 0.1% | Deducted from output |

---

## Troubleshooting

### Asset Not Found by Service

1. Verify `txid_toService` is correct (use `assets/get/asset name=distordiaBridge`)
2. Ensure asset is owned by the same signature chain that sent tokens
3. Check that asset was updated **after** the Nexus debit (timing matters)

### Invalid Receival Account

**For Solana (`toChain=solana`):**
1. Verify the address is valid base58 (44 characters)
2. Check that token ATA exists for the wallet (most wallets auto-create on first receive)
3. Use `spl-token accounts` to list your token accounts

**For future EVM chains:**
1. Verify address is 0x-prefixed and 42 characters
2. Ensure the address is checksummed correctly

### Unsupported Chain

1. Verify `toChain` is set to a supported value (currently only `solana`)
2. If omitted, chain defaults to `solana`
3. Future chains will be announced with their specific requirements

### Refund Received Instead of Bridged Tokens

1. Asset may not have been found within timeout period
2. Check `receival_account` is valid for the specified `toChain`
3. Review service logs for specific error message

---

## Example Asset State

After creation and one swap:

```json
{
    "owner": "b7392196b83aca438567558462cd0c5d982569c7cefa668500c4bf3e61a03b7a",
    "version": 2,
    "created": 1706012345,
    "modified": 1706012456,
    "type": "OBJECT",
    "distordiaType": "nexusBridge",
    "fromToken": "USDD",
    "toToken": "USDC",
    "toChain": "solana",
    "txid_toService": "01b88ff8707638acff63e05ca48dec9c79d5b9d754b065ae8f35e0b6cb8b90c694b54dd...",
    "receival_account": "5zq2GuFxVq1tVwkFZLdpME9xJ5NXeJMwNNxpKgGjYzWk",
    "address": "87Wai2JoS4hNAEVXZVmejLS6pK21XQWKoLAkaep5aXFdrYnJJyk",
    "name": "distordiaBridge"
}
```

---

## Summary

| Format | Pros | Cons | Recommended For |
|--------|------|------|-----------------|
| **basic** | Simple, easy to use, readable | No type safety | Most users ✓ |
| **JSON** | Type-safe, field-level immutability | Complex creation syntax | Developers |
| **raw** | Minimal overhead | May not work with service queries | Not recommended |

**TL;DR:** Use `basic` format. Create once, update `txid_toService` for each bridge transaction.

---

## Supported Chains

| Chain ID | Status | Token Examples |
|----------|--------|----------------|
| `solana` | ✅ **Supported** | USDC, SOL-wrapped tokens |
| `ethereum` | 🔜 Planned | ETH, ERC-20 tokens |
| `polygon` | 🔜 Planned | MATIC, ERC-20 tokens |
| `arbitrum` | 🔜 Planned | ETH, ERC-20 tokens |
| `base` | 🔜 Planned | ETH, ERC-20 tokens |

> **Open Source:** This bridge is designed as an open-source, chain-agnostic bridge framework. Community contributions for additional chain support are welcome.

---

## Provider swapService Asset Standard v2 (Planned)

> **Implementation status:** This is the target architecture and migration contract. The current
> service still uses the legacy, name-addressed v1 heartbeat described below. Do not configure a v2
> record as the live waterline source until Batch 7 in
> [`docs/EVALUATION.md`](docs/EVALUATION.md#batch-7--complete-configurability-and-provider-asset-v2-in-progress-provider-v2-remains-documentation-only)
> is implemented and accepted on the target Nexus build.

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
| `service_id` | Provider-chosen globally stable identifier | `distordia-usdd-usdc-mainnet-01` |
| `nexus_network` | Nexus network identifier | `mainnet` |
| `nexus_token_name` | Display/API token name; never the sole security identity | `USDD` |
| `nexus_token_address` | Immutable Nexus token register address | `8ABC...` |
| `nexus_token_decimals` | Nexus token precision | `6` |
| `nexus_treasury_address` | Nexus account receiving input liquidity | `8Cuy...` |
| `nexus_quarantine_address` | Nexus disposition/quarantine account, or `-` if unsupported | `8Qrt...` |
| `nexus_fee_address` | Nexus fee destination/accounting address, or `treasury` | `treasury` |
| `solana_cluster` | Solana cluster or genesis identifier | `mainnet-beta` |
| `solana_token_symbol` | Display ticker only | `USDC` |
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
  "service_id": "distordia-usdd-usdc-mainnet-01",
  "provider": "distordia",
  "contact": "https://example.org/contact",
  "source_url": "https://github.com/distordialabs-brutus/swapService",
  "software_version": "1.2.0",
  "nexus_network": "mainnet",
  "nexus_token_name": "USDD",
  "nexus_token_address": "8ABC...",
  "nexus_token_decimals": "6",
  "nexus_treasury_address": "8Cuy...",
  "solana_cluster": "mainnet-beta",
  "solana_token_symbol": "USDC",
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

> **Compatibility only:** the repository currently contains two incompatible `format=basic` v1
> creators. This section documents the record emitted by `create_heartbeat_asset.py`.
> `register_service.py` instead emits `src.nexus_client.SERVICE_RECORD_FIELDS`, including canonical
> token, custody, fee, minimum, status, contact, memo and liveness fields. Because `format=basic`
> fixes the field set, operators must not treat those assets as interchangeable. Both are addressed
> by local name and neither implements v2. New architecture work should target v2 above, not extend
> either v1 schema.

Bridge service operators **should** maintain a public heartbeat asset on Nexus. This allows users and monitoring systems to verify the service is online and processing transactions.

### Purpose

The heartbeat asset provides:
1. **Liveness indication** — `last_poll_timestamp` shows the service's last activity
2. **Waterline boundaries** — Per-chain timestamps bound historical scanning after restarts
3. **Transparency** — Anyone can query the asset to verify service status

### Required Fields

| Field | Purpose | Mutability | Type | Example |
|-------|---------|------------|------|----------|
| `last_poll_timestamp` | Unix timestamp of last service poll cycle | **Mutable** | `uint64` | `1706012456` |
| `last_safe_timestamp_solana` | Solana chain waterline (oldest unprocessed tx timestamp) | **Mutable** | `uint64` | `1706012300` |
| `last_safe_timestamp_nexus` | Nexus chain waterline (oldest unprocessed tx timestamp) | **Mutable** | `uint64` | `1706012200` |

### Recommended Fields (Provider Info)

| Field | Purpose | Mutability | Example |
|-------|---------|------------|----------|
| `distordiaType` | Asset type identifier | Immutable | `nexusBridgeHeartbeat` |
| `provider` | Provider/operator name | Immutable | `distordia`, `mybridge.io` |
| `version` | Service version string | **Mutable** | `1.0.0` |

### Supported Chains & Treasury Fields (Public Transparency)

These fields enable public validation of token backing and holdings. Anyone can query these addresses on their respective blockchains to verify solvency.

| Field | Purpose | Mutability | Example |
|-------|---------|------------|----------|
| `supported_chains` | Comma-separated list of supported destination chains | **Mutable** | `solana,ethereum` |
| `supported_tokens` | Comma-separated list of bridgeable token pairs | **Mutable** | `USDD:USDC,NXS:wNXS` |
| `nexus_treasury_address` | Nexus treasury account holding incoming USDD | Immutable | `8CuyRASoeBCR...` |
| `nexus_treasury_token` | Token ticker held in Nexus treasury | Immutable | `USDD` |
| `solana_vault_address` | Solana vault token account (ATA) holding USDC | Immutable | `Bg1MUQDMjAuX...` |
| `solana_vault_token` | Token ticker held in Solana vault | Immutable | `USDC` |
| `solana_vault_mint` | Mint address of Solana vault token | Immutable | `EPjFWdd5Aufq...` |

> **Multi-chain Extension:** When additional chains are supported, add corresponding `<chain>_vault_address`, `<chain>_vault_token`, and `<chain>_vault_mint` fields.

#### Backing Validation

Third parties can verify token backing by:
1. Reading `nexus_treasury_address` and querying Nexus for the USDD balance
2. Reading `solana_vault_address` and querying Solana for the USDC balance
3. Comparing holdings to ensure backing ratio ≥ 1:1

Example validation query (Nexus side):
```bash
nexus register/get/finance:token address=<nexus_treasury_address>
# Returns: { "balance": <USDD_AMOUNT>, ... }
```

Example validation query (Solana side):
```bash
spl-token balance <solana_vault_address>
# Returns: <USDC_AMOUNT>
```

### Create Heartbeat Asset (Provider Setup)

```bash
nexus assets/create/asset name=distordiaBridgeHeartbeat format=basic \
    distordiaType=nexusBridgeHeartbeat \
    provider=distordia \
    version=1.0.0 \
    supported_chains=solana \
    supported_tokens=USDD:USDC \
    nexus_treasury_address=<YOUR_USDD_TREASURY_ADDRESS> \
    nexus_treasury_token=USDD \
    solana_vault_address=<YOUR_USDC_VAULT_ATA> \
    solana_vault_token=USDC \
    solana_vault_mint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \
    last_poll_timestamp=0 \
    last_safe_timestamp_solana=0 \
    last_safe_timestamp_nexus=0 \
    pin=<PROVIDER_PIN>
```

### Service Updates (Automatic)

The bridge service automatically updates the heartbeat asset after each poll cycle:

```bash
nexus assets/update/asset name=distordiaBridgeHeartbeat format=basic \
    last_poll_timestamp=<CURRENT_UNIX_TIMESTAMP> \
    last_safe_timestamp_solana=<SOLANA_WATERLINE> \
    last_safe_timestamp_nexus=<NEXUS_WATERLINE> \
    pin=<PROVIDER_PIN>
```

> **Note:** Updates are rate-limited to avoid congestion fees (minimum 10 seconds between updates).

### User/Monitor Query

Anyone can check service status by reading the heartbeat asset:

```bash
nexus register/get/assets:asset name=distordiaBridgeHeartbeat
```

**Interpreting the response:**
- **Service online:** `now - last_poll_timestamp <= 3 × POLL_INTERVAL` (typically ~30-60 seconds)
- **Service stale:** `now - last_poll_timestamp > 3 × POLL_INTERVAL` (may be down or restarting)
- **Waterlines:** Indicate how far back the service will scan for unprocessed transactions

### Example Heartbeat Asset State

```json
{
    "owner": "a1b2c3d4e5f6...",
    "version": 3,
    "created": 1706000000,
    "modified": 1706012456,
    "type": "OBJECT",
    "distordiaType": "nexusBridgeHeartbeat",
    "provider": "distordia",
    "version": "1.0.0",
    "supported_chains": "solana",
    "supported_tokens": "USDD:USDC",
    "nexus_treasury_address": "8CuyRASoeBCRgcuA56Awyixxf34vRad5kB9b9H88bUVSJGfB5B7",
    "nexus_treasury_token": "USDD",
    "solana_vault_address": "Bg1MUQDMjAuXSAFr8izhGCUUhsrta1EjHcTvvgFnJEzZ",
    "solana_vault_token": "USDC",
    "solana_vault_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "last_poll_timestamp": 1706012456,
    "last_safe_timestamp_solana": 1706012300,
    "last_safe_timestamp_nexus": 1706012200,
    "address": "98Xbi3JoT5iNFYZVmekMT7pL22YRWLoLBlabq6aYGeeShnKKJzl",
    "name": "distordiaBridgeHeartbeat"
}
```

### Configuration

Providers configure the heartbeat asset in their environment:

```env
HEARTBEAT_ENABLED=true
NEXUS_HEARTBEAT_ASSET_NAME=distordiaBridgeHeartbeat
HEARTBEAT_MIN_INTERVAL_SEC=10
HEARTBEAT_WATERLINE_ENABLED=true
```

### Fees

| Operation | Cost |
|-----------|------|
| Asset creation | 1 NXS (one-time) |
| Asset naming | 1 NXS (optional, one-time) |
| Updates (≥10s apart) | Free |
| Updates (<10s apart) | 0.01 NXS congestion fee |
