# swapService Independent Architecture and Development Review — 2026-09-09

**Review-start HEAD:** `1116a4a867553fa4d37ea338f121ee56aa702075` (`main`, matching `origin/main`)
**Review-start tree:** `7666f7e3edde6ba4c6c8d85965781d6af58e4564`
**Prior evidence:** [`DEVELOPMENT_REVIEW_2026-09-08.md`](DEVELOPMENT_REVIEW_2026-09-08.md), with the newer implementation snapshot in [`POST_CHANGE_REVIEW_2026-09-07.md`](POST_CHANGE_REVIEW_2026-09-07.md)
**Delta after the 2026-09-08 implementation snapshot (`917505b74f0b095d7c6ed202f55d4b94fb1378a5`):** one documentation-only commit, seven files, 251 insertions and 14 deletions; no `src/`, `tests/`, dependency or CI delta
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

No newer runtime repair exists after the 2026-09-08 review. Current HEAD adds and aligns documentation only. Git reports an empty diff from `917505b` to `1116a4a` for `src/`, `tests/`, `requirements.txt` and `.github/workflows/ci.yml`, so the prior implementation findings remain applicable without reopening defects already closed by the 2026-09-07 payout/source-identity repair.

The full installed-dependency suite again passes in its default collection order: **271 tests and 33 subtests**. Exact-source identity, atomic fee/finalization, payout-evidence, receipt and installed-SDK focused modules also pass in their independently suitable processes. The closed local controls remain present.

Release is still blocked by three unchanged gates. The primary Nexus→Solana payout calls a helper that neither checks the rolling cap nor writes its ledger. The other Solana send helper is not a safe substitute because its cap sequence is non-atomic and treats a post-send ledger-write failure as logging only. Optional receipt assets remain default-off and have no NXS spend budget/accounting or target-node acceptance. Finally, the default green test order is not composable: the previously documented alternate order again fails three payout tests, and `test_recovery_safety.py` alone fails two scanner fixtures under the installed SDK.

## Severity-ordered findings

### Critical — there is still no durable service-wide Solana payout budget

The production payout branch freezes terms and claims the exact Nexus source before calling `send_solana_token_to_account_with_sig()` (`src/swap_nexus.py:337-371`). That helper validates/builds/submits the transfer but never reads `DAILY_PAYOUT_CAP_SOLANA_UNITS`, calls `state_db.payouts_since()`, reserves capacity or records accepted spend (`src/solana_client.py:1568-1607`). Production startup checks only that the configured cap is positive (`src/main.py:89-100`).

The 2026-09-08 direct mocked probe already demonstrated a 900-unit send under a one-unit cap with zero cap-read and ledger-write calls. That exact runtime is unchanged at current HEAD. A new direct one-line probe was not approved during this review; no replacement result is claimed.

Simply routing the primary payout through `send_solana_token()` would still not meet the financial-state-machine exit. That helper reads aggregate spend before RPC, submits, then records after success (`src/solana_client.py:979-1043`). Two workers can observe the same remaining capacity and oversubscribe it; timeout/crash after acceptance has no durable reservation; and a `record_payout()` exception is logged without failing or holding the accepted obligation. The cap therefore needs its own intent-first reservation/settlement protocol, not a helper-level preflight check.

**Required exit:** reserve rolling-window capacity atomically with exact payout preparation; retain reservations across submitted/unknown outcomes; settle only from exact finalized evidence; release only from authoritative non-execution or reviewed disposition; and route primary payouts, refunds and quarantine sends through that one protocol. Prove concurrency, window-boundary, duplicate, timeout-after-acceptance, crash/restart and ledger-write-failure behavior.

### High when enabled — receipt publication spends NXS outside a budget/admission boundary

`swap_receipts._create()` invokes `assets/create/asset` with an optional deterministic name (`src/swap_receipts.py:127-149`). The pinned upstream Nexus API documentation cited in the prior review assigns an NXS cost to asset creation and another to naming. Runtime/schema comments still call this a non-money or non-monetary side effect (`src/swap_receipts.py:1-4`, `src/main.py:571-572`, `src/state_db.py:327-328`). There is no NXS reservation, aggregate cap, cost journal, create txid accounting, budget-exhaustion admission or operator alert.

The existing at-most-once create protocol is still useful: the obligation is frozen with confirmed payout finalization, `pending` is claimed before create, an ambiguous result remains `creating`, and exact owner/payload readback is required before `published`. This prevents a blind duplicate create for one obligation; it does not bound aggregate spend across new obligations.

Receipt-enabled finalization also still resolves provider owner before committing an otherwise proven Solana→Nexus payout (`src/nexus_client.py:1002-1028`). Existing fixed-field v1 registrations cannot gain `receipt_schema` through heartbeat updates, and startup does not require a receipt-capable registration. Target-node JSON create, Query DSL filtering/projection, index visibility, duplicate coverage and owner/address shape remain unexecuted.

**Containment/exit:** keep `NEXUS_SWAP_RECEIPTS_ENABLED=false`. Before enabling it, add an NXS budget and accounting state machine, require a receipt-capable registration and authoritative owner at admission, separate publication availability from already-proven settlement, and pass the target-node create/rejection/timeout/indexing/readback/restart matrix.

### High engineering gate — default pytest order hides process-global contamination

`tests/test_critical_safety.py` replaces Solana, solders, requests and dotenv modules in `sys.modules` during collection. Receipt and recovery modules set environment defaults before importing real configuration. Results depend on which module initialized process-global dependencies/config first.

Executed with the pinned dependencies installed:

- default complete order: **271 passed, 33 subtests passed**;
- receipt→payout→fee→SDK order: **3 failed, 51 passed**;
- `test_recovery_safety.py` alone: **2 failed, 13 passed, 8 subtests passed**;
- recovery→SDK order: **2 failed, 14 passed, 8 subtests passed**;
- receipt, payout, fee, identity, critical-safety and real-SDK modules each pass in the separately recorded processes.

The failures are exact-payout fixture/config mismatches caused by collection state. They are not evidence that production may accept incomplete payout proof: the isolated real-SDK boundary passes and the default suite retains the strict production assertions. They do prove that one green collection order is not a reliable engineering gate.

**Required exit:** remove collection-time module replacement, scope/restore environment and config per fixture or subprocess, make recovery/payout fixtures valid under the real installed SDK, and run the sensitive modules independently and in at least two orders in CI.

### External acceptance remains open

No target Nexus node or Solana devnet/testnet was used. Current local mocks do not prove Nexus account-history completeness, equal-timestamp ordering, pagination stability, JSON asset creation, Query DSL filtering, finality fields, HTTP POST/TLS behavior, accepted-but-unparsed outcomes or crash/restart behavior against the target builds.

## Closed controls retained; do not reopen

The runtime/test delta after the prior implementation review is empty, and the focused/default suites preserve these local repairs:

- exact Nexus `(txid, contract_id)` identity through admission, lifecycle tables, operator intents, deterministic references, audit and sibling-scoped finalization;
- strict composite payout memo parsing and database-wipe reconstruction that cannot synthesize a paid marker from sparse/legacy evidence;
- startup refusal on missing checkpoints, incomplete scans and recovery errors;
- multi-page mutable-offset scans cannot authorize checkpoint/recovery completeness;
- frozen exact payout terms and one full successful finalized Solana transaction proof before source finalization;
- atomic per-contract fee journal, terminal state and exact source removal;
- installed-SDK `Signature` construction at transaction and pagination request boundaries;
- receipt create claim and exact owner/payload readback, while the feature remains disabled.

Missing, malformed, bounded or conflicting evidence must continue to retain liabilities and must never authorize resubmission or compensation.

## Architecture and development-plan update

[`EVALUATION.md`](EVALUATION.md) remains the authoritative current issue register and development plan. This review updates it to:

1. make the global payout-budget protocol the open P0 implementation batch;
2. specify atomic reservation/settlement semantics rather than redirecting callers to the existing helper;
3. add the default-off receipt NXS cost/admission batch;
4. mark the engineering gate partial until sensitive modules compose under installed dependencies;
5. preserve the target-chain/live matrix ahead of production approval;
6. keep provider-v2 explicitly planned rather than implying implementation.

[`STATE_MACHINES.md`](STATE_MACHINES.md) now identifies this review as the current evidence while preserving all persisted status/schema names and historical snapshots.

## Verification executed

| Check | Exact result |
|---|---|
| Review-start Git state | `main`; HEAD and `origin/main` both `1116a4a867553fa4d37ea338f121ee56aa702075`; clean real index/worktree |
| Runtime/test/dependency/CI diff, `917505b..1116a4a` | Empty |
| Default interpreter full suite | **270 passed, 1 SDK test skipped, 33 subtests passed** in 46.08s |
| Disposable Python 3.11 venv with pinned requirements, pytest and Ruff | Installed successfully |
| Installed-dependency full suite | **271 passed, 33 subtests passed** in 48.08s |
| `tests/test_critical_safety.py` | **136 passed, 25 subtests passed** |
| `tests/test_nexus_identity.py` | **43 passed** |
| `tests/test_payout_review_regressions.py` | **17 passed** |
| `tests/test_nexus_fee_lifecycle.py` | **12 passed** |
| `tests/test_swap_receipts.py` | **24 passed** |
| `tests/test_solana_sdk_boundary.py` | **1 passed**, no skip |
| `tests/test_recovery_safety.py` alone | **2 failed, 13 passed, 8 subtests passed** — process-global SDK/config fixture contamination |
| Receipt→payout→fee→SDK order | **3 failed, 51 passed** — same isolation class |
| Recovery→SDK order | **2 failed, 14 passed, 8 subtests passed** — same isolation class |
| Direct daily-cap bypass rerun | Not executed: approval for the one-line synthetic probe was denied; the unchanged call graph and 2026-09-08 executed reproduction are the evidence |
| Target Nexus/Solana live matrix | **Not run** |

No RPC transaction, receipt asset, token transfer, refund, quarantine or other financial mutation was performed. Test fixtures used temporary SQLite databases and mocked/offline transport boundaries.

## Final candidate gate and hash manifest

A disposable index at `/tmp/swapservice-review-20260909.index` was initialized from HEAD and given
only the five review candidate files. The real index SHA-256 was
`d3a37b2f2c524b5995f96248050718d0263e37537544c246d6df0a6ced82c36e` before and after the gate.
The generated index-aware marker listing contains 21 grouped marker lines, has SHA-256
`c5d17653ce988abc3a5b2074358f02633e60924094c29d0df4768793fc934a19`, and the checker reports
**273 active literal lines**.

Final candidate results:

- `git diff --cached --check` under the disposable index: **pass**;
- pinned-environment `pip check`: **pass**, no broken requirements;
- `compileall` with an external bytecode cache: **pass**;
- Ruff `F821,F822,F823` scoped undefined-name/local-variable check: **pass** (not a broad lint claim);
- local Markdown links under the candidate index: **pass**;
- full installed-dependency suite under the candidate index: **271 passed, 33 subtests passed**;
- real Git index: unchanged; no staging, commit or push.

The reviewed-file SHA-256 artifact is
[`DEVELOPMENT_REVIEW_2026-09-09.sha256`](DEVELOPMENT_REVIEW_2026-09-09.sha256). The exact repeatable
candidate gate is:

```bash
GIT_INDEX_FILE=/tmp/swapservice-review-20260909.index git read-tree HEAD
GIT_INDEX_FILE=/tmp/swapservice-review-20260909.index git add -- \
  docs/EVALUATION.md docs/STATE_MACHINES.md \
  docs/DEVELOPMENT_REVIEW_2026-09-09.md \
  docs/TOKEN_PAIR_LITERAL_INVENTORY.md \
  docs/DEVELOPMENT_REVIEW_2026-09-09.sha256
GIT_INDEX_FILE=/tmp/swapservice-review-20260909.index \
  /tmp/swapservice-review-20260909/bin/python \
  scripts/check_token_pair_inventory.py --list \
  > /tmp/swapservice-token-inventory-20260909.txt
GIT_INDEX_FILE=/tmp/swapservice-review-20260909.index \
  /tmp/swapservice-review-20260909/bin/python \
  scripts/check_token_pair_inventory.py
GIT_INDEX_FILE=/tmp/swapservice-review-20260909.index git diff --cached --check
/tmp/swapservice-review-20260909/bin/python -m pip check
PYTHONPYCACHEPREFIX=/tmp/swapservice-compile-20260909 \
  /tmp/swapservice-review-20260909/bin/python -m compileall -q src *.py tests
/tmp/swapservice-review-20260909/bin/ruff check --select F821,F822,F823 \
  src nexus_transfer_operator.py tests
GIT_INDEX_FILE=/tmp/swapservice-review-20260909.index \
  /tmp/swapservice-review-20260909/bin/python scripts/check_markdown_links.py
PYTHONDONTWRITEBYTECODE=1 \
  GIT_INDEX_FILE=/tmp/swapservice-review-20260909.index \
  /tmp/swapservice-review-20260909/bin/python -m pytest -q -p no:cacheprovider
```

## Release order

1. **P0:** implement and fault-inject the durable service-wide Solana payout reservation/settlement protocol.
2. **P1:** remove test collection-order dependence and require installed-SDK independent/order shards.
3. **P1 if receipts are desired:** retain default-off containment until NXS cost controls, registration migration and target-node receipt acceptance pass.
4. **P1:** execute the complete target Nexus/Solana finality, pagination, timeout, crash/restart, migration and reconciliation matrix on the exact candidate.
5. **P2:** rehearse alert delivery, hold escalation, two-person operator disposition, database/WAL backup/restore and key rotation.

Production and real-fund admission remain hard-blocked. Documentation review does not authorize a deployment or financial operation.
