# Independent Financial State-Machine Review — 2026-09-03

**Review window:** 2026-09-02 16:22:20 +0200 through 2026-09-03 16:17:47 +0200
**Baseline:** `f73b4d68c0f94ffe1f678e213bdc35730798428d`
**Reviewed head:** `8769dcf3e90b49731f7c10ebb11b31808dde1e89`
**Scope:** 11 commits; 12 committed files; 617 insertions, 60 deletions; three pre-existing uncommitted documentation files
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The finality, zero-input reconciliation and canonical fee-policy changes are material safety improvements. The full suite is green and no new automatic double-payment path was found. Batch 7 now has a useful immutable canonical-pair foundation, but it is only partial and does not establish pair neutrality or production acceptance.

A new High defect blocks recovery correctness: database reconstruction does not use the live Nexus-credit admission/accounting semantics. A credit that the live poller would retain as below-minimum fees or classify as over-cap can instead be queued for payout during startup recovery. Existing reference-only finality, target-node pagination/finality and live-chain acceptance gates also remain open.

## Severity-ordered finding

### High — startup recovery diverges from live Nexus-credit classification

Live polling ignores only sub-dust credits (`src/swap_nexus.py:714-718`), records dust-to-minimum credits as fee-only with exact units and a fee-ledger entry (`src/swap_nexus.py:720-750`), sends over-cap credits to refund-pending (`src/swap_nexus.py:753-770`), and separately handles positive-but-zero-net credits (`src/swap_nexus.py:772-799`).

Startup reconstruction checks only the dust threshold and whether canonical output is positive (`src/startup_recovery.py:102-117`). Every positive-output credit is then queued as `pending_receival` (`src/startup_recovery.py:134-147`). Its fee-only path writes neither `amount_usdd_units` nor a fee-ledger entry (`src/startup_recovery.py:118-132`); `mark_processed_txid()` permits the exact-unit field to remain null (`src/state_db.py:1357-1378`), while reconciliation requires exact units (`src/balance_reconciler.py:121-145`).

The added recovery test covers only zero output (`tests/test_critical_safety.py:137-174`), not positive-output/below-minimum, cap handling or fee-ledger parity.

**Required exit:** extract one shared Nexus-credit classifier used by both live polling and recovery. It must cover treasury-contract selection, dust, minimum, cap, canonical payout, exact-unit persistence, fee booking and terminal status. Add parity tests for every branch before relying on wipeout recovery.

## Positive controls verified

- Nexus confirmation policy is constrained to a positive integer at import and runtime (`src/config.py:229-251`).
- Completed mints require strictly positive Solana input and Nexus output (`src/balance_reconciler.py:79-91`).
- Completed-mint reconciliation consumes the shared configured finality threshold (`src/balance_reconciler.py:391-400`).
- Bidirectional payouts, Solana refund/quarantine, Nexus disposition and published flat-fee terms consume the canonical fee policy.
- Negative flat/refund/disposition fees and basis points outside `0..4999` fail configuration (`src/config.py:294-339`); canonical/legacy conflicts fail closed.
- `SWAP_PAIR` is immutable (`src/config.py:457-524`), but provider-v2 identity, complete thresholds/caps/micro-policy routing and the full mixed-decimal live matrix remain outstanding.

## Verification

| Check | Exact result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | **PASS — 134 passed, 16 subtests passed in 12.24s** |
| `python3 scripts/check_markdown_links.py` | **PASS — Local Markdown links: OK** |
| `python3 scripts/check_token_pair_inventory.py` | **PASS — 660 active lines** |
| `python3 -m compileall -q src *.py tests` | **PASS** (independent inspection run) |
| `python3 -m pip check` | **PASS — no broken requirements** (independent inspection run) |
| `git diff --check` | **PASS** |
| `pyflakes`, `ruff`, `mypy`, `pip-audit` | **Not available** |
| Live Nexus/Solana target matrix | **Not run** |

The pre-existing uncommitted changes in `ASSET_STANDARD.md`, `SETUP.md` and `docs/EVALUATION.md` are prospective architecture/plan material. They were reviewed as context and preserved, but are not runtime evidence and are not included in this review commit.

## Required repair order

1. Unify live and recovery Nexus-credit classification and add branch-parity tests.
2. Preserve confirmation/finality evidence in reference/manual resolution before enabling any complete adoption path.
3. Validate target-node transaction identity and provider-v2 custody identity at the trust boundary.
4. Complete Batch 7 pair, fee, threshold, cap and micro-policy routing and its conflict/mixed-decimal matrix.
5. Execute the target Nexus pagination/finality/crash matrix and both-chain live acceptance suite.
6. Keep automatic Nexus refund/quarantine execution disabled and admit no real funds until all gates pass.
