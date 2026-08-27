# USDC ↔ USDD Bidirectional Swap Service

User-facing guide for performing swaps between USDC (Solana) and USDD (Nexus).

Operator / setup documentation: see **`SETUP.md`**.  
Security hardening: **[`docs/SECURITY.md`](docs/SECURITY.md)**.
Configuration reference: **`CONFIG.md`**.

<details>
<summary><b>Review &amp; audit documents</b> (which one is current?)</summary>

| Document | Status | Read it for |
|----------|--------|-------------|
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | **Current remediation plan** | Authoritative issue register, severity, repair priority, exit criteria and deployment definition. |
| [`docs/POST_CHANGE_REVIEW_2026-08-24.md`](docs/POST_CHANGE_REVIEW_2026-08-24.md) | Review evidence | Independent/static review evidence for commit `1e4f20c` that feeds the current evaluation. |
| [`docs/DEVELOPMENT_REVIEW_2026-08-24.md`](docs/DEVELOPMENT_REVIEW_2026-08-24.md) | Fix history | Original review, Critical findings, and the repair sequence that led to `1e4f20c`. |
| [`docs/RISK_ASSESSMENT.md`](docs/RISK_ASSESSMENT.md) | History with current update | Whole-system risk history and current safety-gate note. |
| [`docs/STATE_MACHINES.md`](docs/STATE_MACHINES.md) | Current with resolution note | Server-side state machines and repaired ambiguity/waterline invariants. |
| [`docs/SWAP_INITIATOR_STATE_MACHINES.md`](docs/SWAP_INITIATOR_STATE_MACHINES.md) | Current user-flow description | User-facing flow only; it is not fund-safety verification. |
| [`docs/AUDIT_FINDINGS.md`](docs/AUDIT_FINDINGS.md) | History | The first audit pass, superseded. |

</details>

---

## Quick Overview
The service lets you swap:
- USDC → USDD: Send USDC to the service vault with a memo that specifies a Nexus (USDD) account.
- USDD → USDC: Send USDD to the treasury and publish an asset mapping your USDD transaction `txid` to a Solana receival account.

Thresholds & Fees (defaults – operator may change):
- Minimum swap amounts (smaller = treated as fees, **no tokens are sent back**):
  - USDC → USDD: `0.2 USDC` (nets ~0.0998 USDD after the 0.1 flat fee + 0.1%)
  - USDD → USDC: `1.0 USDD` (nets ~0.499 USDC after the 0.5 flat fee + 0.1%) — this direction carries a larger flat fee, so its minimum is higher. Sending less will **not** be swapped.
- Flat fee (USDC path) & dynamic fee (bps) may apply as configured.

---

## How to swap USDD for USDC

### USDC->USDD

Send USDC from a solana wallet which allow memos in the following format:

- Send to: `Bg1MUQDMjAuXSAFr8izhGCUUhsrta1EjHcTvvgFnJEzZ`

> ⚠️ **This is the vault address of *this* deployment.** Always verify it against the
> operator's on-chain heartbeat asset (`solana_vault_address`) before sending funds.
> **If you deploy a fork, you MUST replace this address and the one in the CLI example
> below with your own `VAULT_USDC_ACCOUNT`** — otherwise your users' USDC is sent to
> the original operator's vault.
- Memo/note: `nexus:<USDD receival account>`
- Amount: minimum `0.2 USDC`

Fees (USDC→USDD): 0.1 USDC flat + 0.1% of amount. The USDD→USDC direction charges 0.5 USDC flat + 0.1%.

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
nexus finance/debit/account from=<YOUR_USDD_ACCOUNT> to=<TREASURY_ACCOUNT> amount=10.5 pin=<PIN>
# Response includes "txid": "01b88ff8..."
```
> **Note:** Use `finance/debit/account` with your USDD account name or address. The `finance/debit/token` command debits from the token supply and is reserved for the token creator.

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
- Minimum: `1.0 USDD` (`MIN_CREDIT_USDD`). Smaller amounts are recorded and treated as fees — no USDC is sent
- If no asset mapping within `REFUND_TIMEOUT_SEC` (default 1 hour) → the credit is held
  for operator review. Automatic Nexus refunds are disabled until their durable,
  crash-safe protocol is implemented.

High‑level flow:
1. You send USDD to the service treasury.
2. You obtain the resulting transaction `txid` (returned by the CLI / wallet).
3. You create (or update) an asset you own adding fields:
  - `txid_toService` : the txid from step 2
  - `receival_account` : either your Solana USDC token account (ATA) OR your Solana wallet address (the service will derive the ATA if it already exists). 
4. Service detects the credit, queries assets filtering by `txid_toService=<txid>` AND `owner=<sender_owner_hash>`, validates the receival account, then sends net USDC.
5. If no matching asset appears before the refund timeout, the credit is held for
   operator review; it is not automatically refunded.

Detailed steps:

1) Ensure your Solana wallet already has (or will auto-create) a USDC ATA.
  - Most consumer wallets (Phantom, Solflare, Glow) auto-create it on first receive.
  - Power users can pre-create it: `spl-token create-account <USDC_MINT>`.

2) Send USDD to the treasury
  - To: `NEXUS_USDD_TREASURY_ACCOUNT`
  - Amount: ≥ `MIN_CREDIT_USDD` (default 1.0). Amounts below the threshold are recorded and treated as micro credits (100% fee); no USDC will be sent.
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
  - No asset found within `REFUND_TIMEOUT_SEC`: credit is held for operator review.
  - Invalid/malformed `receival_account`: credit is held for operator review.
  - Solana send failures enter the separately tracked payout-confirmation/quarantine flow;
    they are not a reason to retry a Nexus refund automatically.
  - Micro credit (< threshold): recorded as fees instantly; no asset lookup needed.

Notes
- Asset owner must match the sender’s `owner` field of the USDD credit; otherwise it is ignored.
- You can batch multiple swaps by using multiple assets or updating the same asset sequentially (only the row with matching `txid_toService` is considered).
- Tiny USDD credits below `MIN_CREDIT_USDD` (default 1.0) are treated as fees (100% micro fee policy). They are recorded in the service database (sender, amount, txid) so they can be traced; credits below `DUST_CREDIT_USDD` (default 0.01) are ignored entirely as spam.
- Keep the asset published before the timeout to avoid manual operator intervention.

---

## Summary of Both Directions

### USDC → USDD (Solana to Nexus)
1. Send USDC to the vault token account (`VAULT_USDC_ACCOUNT`) with memo: `nexus:<NEXUS_USDD_ACCOUNT>`.
2. Service validates the Nexus account & token, mints/sends USDD minus fees.
3. Invalid or missing memo → refund (flat fee may apply). Tiny deposits ≤ flat fee are treated as fees.

### USDD → USDC (Nexus to Solana)
1. Send USDD to treasury (`NEXUS_USDD_TREASURY_ACCOUNT`).
2. Publish asset with `txid_toService` + `receival_account` (Solana wallet or USDC ATA).
3. Service finds mapping, validates address / existing ATA, and sends net USDC. If the
   mapping is missing past the timeout → hold for operator review (no automatic Nexus refund).

---

## Common Questions
**Q: How fast are swaps?**  Depends on polling and chain confirmation. Typical: a few Solana blocks (USDC→USDD) or one Nexus credit + mapping publish cycle (USDD→USDC).  
**Q: Can I reuse the same asset?** Yes—just update its fields for each new `txid_toService`, or create per‑swap assets.  
**Q: What if I forget to publish the asset?** After the timeout the credit is held for
operator review. Automatic Nexus refunds are intentionally disabled pending a durable
idempotent refund protocol.
**Q: Do you create my USDC ATA?** No. Ensure it already exists (most wallets auto‑create on first receive).  
**Q: Are sub‑threshold amounts lost?** They are treated as fees/donations per policy; do not send below the published minimum.  

---

## Minimal Cheat Sheet

USDC→USDD:
```
Send USDC to <VAULT_USDC_ACCOUNT>
Memo: nexus:<YOUR_USDD_ACCOUNT>
Amount ≥ 0.2 USDC
```

USDD→USDC:
```
Send USDD to <NEXUS_USDD_TREASURY_ACCOUNT>
Grab txid from CLI output
Publish or update asset with fields: {"txid_toService":"<TXID>","receival_account":"<SOL_OR_USDC_TOKEN_ACCOUNT>"}
Amount ≥ 1.0 USDD
```

---

## How It Works

### USDC → USDD (Solana to Nexus)
1. User sends USDC to your vault USDC token account (`VAULT_USDC_ACCOUNT`).
2. The same transaction must include a Memo: `nexus:<NEXUS_ADDRESS>`.
3. Service validates the Nexus address exists and is for the expected token (`NEXUS_TOKEN_NAME`, e.g., USDD).
4. If valid, the service mints/sends USDD on Nexus to that address (amount normalized by decimals).
5. If invalid/missing memo or wrong token, the service refunds the USDC back to the source SPL token account with a memo explaining the reason. A flat fee (`FLAT_FEE_USDD`, the USDC→USDD fee) is deducted from the refund. On successful swaps, a dynamic fee in bps (`DYNAMIC_FEE_BPS`) is also retained. Tiny deposits ≤ `FLAT_FEE_USDC` are treated as fees and not processed further.

Notes:
- Amounts are handled in base units and normalized between `USDC_DECIMALS` and `USDD_DECIMALS`.
- The refund is sent to the original SPL token account the deposit came from (not a wallet owner).

### USDD → USDC (Nexus to Solana)
1. User sends USDD to the Nexus USDD Treasury account (`NEXUS_USDD_TREASURY_ACCOUNT`).
2. User creates or updates a Nexus asset with `txid_toService` set to the debit transaction hash and `receival_account` set to their Solana USDC ATA or wallet address.
3. Service detects the credit, queries assets filtering by `txid_toService` and `owner` (must match the sender's genesis ID).
4. If valid mapping found, the service sends USDC from the vault to the `receival_account`. The recipient must already have a USDC ATA (the service will not create it).
5. If no mapping is found within `REFUND_TIMEOUT_SEC` (default 1 hour), the USDD is held
   for operator review. Invalid receival accounts also hold; automatic Nexus refunds remain
   disabled until a durable idempotent protocol exists. On successful sends, a flat fee
   (`FLAT_FEE_USDC`) and optional dynamic fee (`DYNAMIC_FEE_BPS`) are deducted from the
   USDC output.

Policy notes on USDD → USDC:
- Tiny USDD credits ≤ `MIN_CREDIT_USDD` are treated as fees (no USDC is sent).
- See [ASSET_STANDARD.md](ASSET_STANDARD.md) for the full asset specification and [SWAP_INITIATOR_STATE_MACHINES.md](docs/SWAP_INITIATOR_STATE_MACHINES.md) for the step-by-step user flow.

### Loop-Safety and Reliability
- Actions that can incur fees (mint, send, refunds) are guarded by attempt limits and cooldowns:
  - `MAX_ACTION_ATTEMPTS` attempts per unique item (tx/signature).
  - `ACTION_RETRY_COOLDOWN_SEC` between attempts.
- Processed state is persisted; items are only marked processed after a successful outcome.
- Solana transfers include confirmation attempts.
 - If all refund attempts fail:
   - USDC→USDD path: the remaining refundable amount (after the last attempt's flat fee) is moved from the vault USDC token account to a self-owned quarantine USDC token account.
   - USDD→USDC path: no automatic Nexus quarantine or refund debit is issued. The source
     credit is held for operator review; any future Nexus move must use a durable intent,
     chain-reference resolution and separately authorized disposition.
   - In both cases, the event is recorded for manual inspection.

## Finding and verifying a bridge

Every operator publishes a single Nexus asset describing their bridge: the token pair, the
vault and treasury backing it, current fees and minimums, the deposit memo format, and a
liveness timestamp. Read it before sending funds:

```bash
python3 register_service.py --inspect <bridgeAssetName>
# or directly:  nexus assets/get/asset name=<bridgeAssetName>
```

Check that `solana_vault_address` matches where you are about to send, that `status` is
`online`, and that `last_poll_timestamp` is recent.

## Public Heartbeat (Free, On-Chain)

The service updates a Nexus Asset after each poll cycle. **Anyone can read it on-chain to
check whether the bridge is alive and what it is currently backing** — use it to verify the
vault address before sending funds.

> **Operators:** the heartbeat asset is *not* optional. The Solana poller reads its waterline
> from it, so without one no USDC deposit is ever ingested. See
> [SETUP.md § Nexus Setup](SETUP.md#nexus-setup--asset-mapping).

- One-time cost: create an Asset (1 NXS fee for asset creation, + optionally 1 NXS for adding a local name). Updates are free as long as they are not more frequent than every 10 seconds (there's a congestion fee of 0.01 NXS for more frequent transactions).
- Fields published: `last_poll_timestamp`, `last_safe_timestamp_solana`, `last_safe_timestamp_nexus`, plus transparency fields (treasury and vault addresses) per [ASSET_STANDARD.md](ASSET_STANDARD.md).

Setup steps (operators) — use the helper, which validates input and confirms before spending:
```bash
python3 create_heartbeat_asset.py --name distordiaBridgeHeartbeat --dry-run   # preview, spends nothing
python3 create_heartbeat_asset.py --name distordiaBridgeHeartbeat            # ~1 NXS
```
Then set **`NEXUS_HEARTBEAT_ASSET_NAME`** in `.env` (plus `HEARTBEAT_ENABLED=true`).

> The service looks the asset up **by name**, not by address — `NEXUS_HEARTBEAT_ASSET_ADDRESS`
> is recorded for reference only and is not read. An asset created without `--name` is
> unreachable by the service.
>
> The asset must carry `last_poll_timestamp`, `last_safe_timestamp_solana` and
> `last_safe_timestamp_nexus`. `format=basic` fixes the field set at creation, so a missing
> field makes **every** heartbeat update fail atomically. The service validates this at
> startup and prints the fields the asset actually has.

How clients check status:
- Read the asset throught the `register` api: `register/get/assets:asset address=<ASSET_ADDRESS>`
  - Or by name: `register/get/assets:asset name=<ASSET_NAME>`
- Extract `results.last_poll_timestamp` (unix seconds).
- Consider the service online if `now - last_poll_timestamp <= grace`, where `grace ≈ 2–3 × POLL_INTERVAL`.

Waterline (optional):
- The service can also honor per-chain “waterline” timestamps stored on the same asset to bound how far back it scans:
  - Default field names: `last_safe_timestamp_solana` and `last_safe_timestamp_nexus` (configurable via `HEARTBEAT_WATERLINE_SOLANA_FIELD` / `HEARTBEAT_WATERLINE_NEXUS_FIELD` — these **must** match the asset, since `format=basic` fixes its fields at creation)
  - Pollers skip on-chain items strictly older than their respective waterline (with a small safety margin). Idempotency still prevents double-processing if you later move the waterline.

## Setting Up Your Own Swap Service (Operators)

Operator installation, configuration, account creation and running the service are
documented in one place — **[`SETUP.md`](SETUP.md)** — so there is a single source of truth:

| Step | Where |
|------|-------|
| Prerequisites, RPC/Nexus access | [SETUP.md § Prerequisites](SETUP.md#prerequisites--api-access-requirements) |
| Install | [SETUP.md § Installation](SETUP.md#installation) |
| Solana vault keypair + USDC ATA | [SETUP.md § Solana Setup](SETUP.md#solana-setup) |
| Nexus accounts + heartbeat asset | [SETUP.md § Nexus Setup](SETUP.md#nexus-setup--asset-mapping) |
| Every environment variable | [`.env.example`](.env.example) and [CONFIG.md](CONFIG.md) |
| Running in production | [SETUP.md § Running](SETUP.md#running) |
| Choosing the token pair | [SETUP.md § Choosing the token pair](SETUP.md#choosing-the-token-pair) |
| Registering the bridge on-chain | `python3 register_service.py --show` → [SETUP.md § Nexus Setup](SETUP.md#nexus-setup--asset-mapping) |
| Monitoring dashboard | [SETUP.md § Operator Dashboard](SETUP.md#operator-dashboard) |
| Security hardening | [SECURITY.md](docs/SECURITY.md) |

> This README previously duplicated the whole setup guide and its own `.env` template.
> Both had drifted out of sync with the code, so they were removed rather than maintained twice.

### Operator Dashboard (web UI)

A read-only web UI for watching swaps, spotting problems and checking that the vault is
fully backed. It runs as a **separate process** from the swap service:

```bash
python3 dashboard.py            # then open http://127.0.0.1:8787
```

It binds to localhost only. To view it from your laptop, tunnel rather than exposing it:

```bash
ssh -L 8787:127.0.0.1:8787 operator@your-host
```

**At a glance**

| Panel | Tells you |
|-------|-----------|
| Backing ratio | Vault USDC ÷ circulating USDD. Turns red below 1.0 — you are under-collateralised |
| Vault / Circulating | Current balances on each chain |
| Open items · Quarantined | How much is in flight, and how much needs a human |
| 24h payouts | Outbound USDC against `DAILY_PAYOUT_CAP_USDC`, with a utilisation bar |
| Heartbeat | Age of the last on-chain beat — your liveness signal |
| **Issues** tab | Everything needing action: unverified debits, pending refunds, quarantined items, USDD marked quarantined but *not actually moved*, and actions that used up their retries |
| Transaction tabs | Pending / completed / refunded / quarantined per direction, plus payouts and the fee ledger |

Banners appear on their own for a **backing-deficit pause**, **stale metrics** (the service
has stopped writing — likely down or wedged) and **under-collateralisation**.

**It cannot touch your funds.** The dashboard opens the database read-only, has no
mutating endpoints and no retry/refund buttons, and holds no vault keypair, Nexus PIN or
RPC key. Manual intervention stays on the CLI. Full configuration, the token/TLS rules for
non-localhost binds, and the reasoning behind those choices are in
[SETUP.md § Operator Dashboard](SETUP.md#operator-dashboard).

```bash
python -m pytest -q tests/test_legacy_scripts.py   # dashboard and legacy safety checks run isolated
```

## Nexus API Docs
Official Nexus API docs are included in the `Nexus API docs/` folder for reference.

## License
This project is provided as-is. Use at your own risk.
