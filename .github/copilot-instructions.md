# Engineering Guidance: swapService

## Scope and source of truth

This is a custodial bridge for **one configurable Solana SPL token / Nexus token pair per deployment**, not a fixed-ticker exchange or multi-pair routing engine. The historical `USDC` / `USDD` display defaults and compatibility names do not select token identity. Current configuration and behavior come from [`src/config.py`](../src/config.py) and the runtime, not from dated audit reports.

Use [`README.md`](../README.md) for user instructions, [`SETUP.md`](../SETUP.md) for operations, [`CONFIG.md`](../CONFIG.md) for settings, and [`docs/EVALUATION.md`](../docs/EVALUATION.md) for current release gates. The provider-v2 schema is planned; do not implement or advertise it merely because an example appears in [`ASSET_STANDARD.md`](../ASSET_STANDARD.md).

## Architecture

| Component | Responsibility |
|---|---|
| [`src/config.py`](../src/config.py) | Canonical immutable `SWAP_PAIR`, fee policy, compatible environment inputs |
| [`src/main.py`](../src/main.py) | Production admission, mandatory recovery, reconciliation/exposure gate and chain polling |
| [`src/swap_solana.py`](../src/swap_solana.py) | Solana input admission and checkpoint handling |
| [`src/swap_nexus.py`](../src/swap_nexus.py) | Nexus credit admission, asset mapping, frozen Solana payout terms and finalization |
| [`src/solana_client.py`](../src/solana_client.py) | Classic SPL transfers, RPC/Helius reads, Solana processing and payout evidence |
| [`src/nexus_client.py`](../src/nexus_client.py) | Nexus API/development-CLI boundary, token-register validation, durable transfer operations and public registration |
| [`src/state_db.py`](../src/state_db.py) | SQLite lifecycle state, exact source identity, claims, transfer intents and atomic fees/finalization |
| [`src/startup_recovery.py`](../src/startup_recovery.py) | Evidence-driven reconstruction from validated custody checkpoints |
| [`src/nexus_memo.py`](../src/nexus_memo.py) | Strict composite payout memo and evidence types |
| [`nexus_transfer_operator.py`](../nexus_transfer_operator.py) | Explicit exact-source disposition workflow; not an automatic refund loop |

Detailed states and actual persisted strings are in [`docs/STATE_MACHINES.md`](../docs/STATE_MACHINES.md). Do not rename them based on a documentation label.

## Configurable identity, units and compatibility

- Route by `SWAP_PAIR.solana.mint` and `SWAP_PAIR.nexus.register_address`, with the corresponding custody accounts. A symbol/ticker match alone is not authorization.
- Prefer implemented canonical environment keys such as `SOLANA_TOKEN_MINT`, `SOLANA_VAULT_ACCOUNT`, `NEXUS_TOKEN_REGISTER_ADDRESS` and `NEXUS_TREASURY_ACCOUNT`. Consult the exact alias/precedence policy before adding an input: not every historic setting has a generic alias.
- Conflicting canonical/legacy identity, custody, precision and fee values are rejected by `_compat_env`; do not replace that with silent precedence.
- Token precision is independently configurable on each side. The backing model is 1:1 in **whole-token units**, not base units and not market value.
- Keep money as integer base units; use the existing exact conversion helpers and conservatively round liabilities up and payouts down. Do not assume equal decimals or floating-point equivalence.
- Fees are directional: Nexus output, Solana output, Solana refund and separately authorized Nexus disposition are distinct amounts/scales. Use `SWAP_PAIR.fees` and the existing formatting helpers rather than a universal flat fee.
- Effective minimums include fee-derived floors. Dust and sub-minimum policy are separate; never let a filter silently hide qualifying deposits while advancing a checkpoint.
- Legacy environment attributes, SQLite columns, retry/reservation keys and lifecycle values can be compatibility contracts. Preserve them until an explicit migration and recovery tests establish that renaming cannot replay an in-flight action.
- Mint configurability does not add native-SOL or Token-2022 transfer support. The implemented Solana transfer program is classic SPL Token.

## Money and state invariants

### Solana input → Nexus output

Validate finalized deposit evidence, the configured memo prefix and the recipient account's immutable Nexus token register. Store exact output terms and a durable reference before a transaction-generating call. Resolve uncertain outcomes from attributable chain evidence; an empty history or timeout is not proof that a debit did not happen.

### Nexus input → Solana output

Asset mapping remains the existing `txid_toService` plus matching sender `owner`, with `receival_account` pointing to the configured mint's existing token-account address. This payout path does not resolve owner wallets to ATAs or create accounts. Internal source identity is the exact `(txid, contract_id)` pair, not a transaction-wide delete key.

Freeze terms and claim the exact source before submission. Outbound memos use `nexus_txid:<txid>:<contract_id>`. Submission does not create a processed terminal row. Finalization requires full successful finalized evidence matching source, signature, vault, mint, recipient and frozen output; it commits per-contract fees, terminal evidence and exact queue removal atomically. Preserve siblings.

### Holds and operator disposition

Automatic Nexus refunds and treasury-to-quarantine transfers are disabled. Mapping timeouts and ambiguous outcomes remain liabilities, not retry/refund permission. The operator protocol requires an exact source contract, durable intent, independent authorization, one execution claim and authoritative resolution/finalization. A claimed interrupted operation becomes `outcome_unknown`, not a new attempt.

Existing Solana refund/quarantine mechanisms are separate; do not infer an automatic Nexus refund from them. Never present a held status as evidence that funds have actually moved.

## Recovery and checkpoints

- Startup must receive affirmative complete recovery before exposure-producing work. Missing or zero custody checkpoints, malformed records and incomplete scans refuse startup; do not turn those failures into warnings.
- Default top-level heartbeat fields are `last_safe_timestamp_solana` and `last_safe_timestamp_nexus`; configured overrides must match the actual asset. Old nested payloads are not interchangeable.
- The component that proved complete enumeration owns checkpoint advancement. A processing-only pass has no scan evidence.
- Any requested nonzero mutable Nexus offset holds the live checkpoint, including a short or empty later page. Positive persisted credits do not prove complete coverage.
- Composite payout reconstruction must combine attributable payout evidence with the exact source credit; never fabricate zero-amount terminal records or guess legacy contract identity.
- Repeated scans/restarts cannot make incomplete history authoritative. Do not initialize waterlines to the current time to bypass recovery.

## External call boundaries

Use existing helpers rather than writing a second transport or bypassing a durable intent:

- Production Nexus calls use authenticated HTTPS configured by `NEXUS_API_URL`, `NEXUS_API_USER` and `NEXUS_API_PASSWORD`. The CLI is a development compatibility path; never require production PIN/session secrets in child-process arguments.
- Public account/asset lookups use the project's `register/*` helpers. Owned-account mutations use the appropriate `finance/*` or `assets/*` methods. Verify the target node's actual API contract; do not invent generic `register/create/asset`, `register/write/asset` or `finance/history` calls.
- Distinguish token-supply debits from a holder's account debit. Serialize Nexus base-unit values to exact whole-token amount strings at the API boundary using existing code, not float conversion.
- With the installed Solana SDK, `Client.get_transaction` takes `solders.signature.Signature`; the `before` cursor for `get_signatures_for_address` is also a `Signature`. A string accepted by a mock is not SDK compatibility proof.
- Use the existing timeout/error wrappers for reads. Never blindly retry a non-idempotent financial call on transport failure.
- Use structured, secret-redacted logging and alerts. Never log PINs, sessions, passwords, RPC credentials or keypair contents.

## Verification workflow

Tests are automated; **mainnet observation is not the test strategy**. Use an environment with the declared dependencies installed:

```bash
python -m pytest -q
python -m compileall -q src tests
python -m pip check
python scripts/check_markdown_links.py
python scripts/check_token_pair_inventory.py
```

The inventory check reads the Git index, including documentation. Verify the intended candidate with a disposable index if user changes must remain unstaged; a check against an unchanged index does not validate working-tree edits. Preserve existing work and do not commit/push unless explicitly requested.

For SDK changes, include the real-installed-SDK subprocess regression in `tests/test_solana_sdk_boundary.py`; most other safety tests intentionally stub SDK modules. The real-SDK test can skip when the dependency is absent, so run it in the installed-dependency environment and verify it actually executes.

Add failing regressions before changing money behavior. Cover acceptance-before-timeout, crash before remote-ID persistence, duplicate invocation, exact sibling isolation, rollback and recovery. Keep tests offline with temporary databases and mocked external transports; do not start the service, use production credentials or call a live chain to inspect documentation.

For a schema change, test both a fresh database and an existing-data migration, repeated initialization, immutable evidence and exact-source finalization. `CREATE TABLE IF NOT EXISTS` alone does not migrate an existing schema.

A read-only dashboard and green local tests do not approve custody operations. Live target-chain finality, history coverage, migration and operational acceptance remain separate release gates.
