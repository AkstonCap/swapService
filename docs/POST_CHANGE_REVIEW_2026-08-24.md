# Post-Change Architecture and Code Review — 2026-08-24

**Reviewed commit:** `1e4f20cbdfef4b2570f1c0bd85fc5c20c91e15fe` (`Add all changes`)

## Verdict

- **Critical-fix scope:** the committed lookup, liability, backing, surplus-action and waterline repairs are materially safer and their focused regression suite passes.
- **Overall code review:** **REQUEST CHANGES**.
- **Deployment:** **HARD BLOCKED**. Two further fund-safety hazards remain, along with two previously documented High findings and no enforceable full-suite/CI gate.

## Critical deployment blockers

### P-1 — Nexus refunds are not crash-safe or idempotent

`refund_nexus_token()` calls `transfer_nexus_between_accounts()` directly (`src/nexus_client.py:491-522`). The transfer:

- writes no durable intent before `finance/debit/account`;
- records no returned Nexus txid;
- treats a CLI timeout/non-zero result as a definite failure even though the node may have accepted the debit;
- has no on-chain reference lookup before retry.

All four automatic Nexus-refund paths retry this boolean operation (`src/swap_nexus.py:215-255, 549-569, 600-620`). A timeout or crash after the Nexus debit but before the local `refunded_txids` write can therefore send the same refund twice. The `is_processed_txid()` check inside `refund_nexus_token()` does not close this window: the original credit remains in `unprocessed_txids`, and successful refunds are archived in `refunded_txids`, not `processed_txids`.

**Required architecture:** persist refund intent and a unique reference before debit; treat every transport/unparsed result as ambiguous; reconcile the reference against Nexus before retry, refund completion or quarantine.

### P-2 — Default heuristic Nexus filtering can make an incomplete scan look empty and complete

`USE_NEXUS_WHERE_FILTER_USDD` defaults to true (`src/config.py:268-270`). The poller appends a heuristic nested-array expression even though its own comment says array members cannot be addressed directly (`src/swap_nexus.py:643-659`). If a Nexus build accepts the query but returns an empty result rather than an explicit error, the poller considers enumeration complete and advances the Nexus waterline to approximately now (`src/swap_nexus.py:941-947`). Any filtered-out treasury credits then fall permanently below the checkpoint.

This behavior has not been observed against the target Nexus build; that uncertainty is itself incompatible with a fail-closed money-ingestion path.

**Required architecture:** disable the heuristic filter by default (preferably remove it), enumerate without a lossy server filter, and add a live-node acceptance test proving every treasury CREDIT above the dust floor is returned before allowing any waterline advance.

## High findings

### H-1 — Mixed-decimal thresholds and public terms are still scaled with the wrong token

- `MIN_CREDIT_NEXUS_UNITS` and `DUST_CREDIT_NEXUS_UNITS` derive from `FLAT_FEE_TO_SOLANA_UNITS` (`src/config.py:219,247-260`).
- `build_service_record()` formats `MIN_DEPOSIT_SOLANA_UNITS` with the Nexus-decimal formatter (`src/nexus_client.py:1174-1179`).
- `tests/test_token_pair.py` checks that published minimums are present, not exact under mismatched decimals.

Executed 8-decimal Solana / 6-decimal Nexus example:

- enforced Nexus minimum: **100.0 tokens**, where the intended 2×0.5-token default is **1.0**;
- Nexus dust floor: **5.0 tokens**, where 0.05 is intended;
- published `min_to_nexus`: **20.0**, where the Solana-side minimum is 0.2.

This can retain valid user credits as fees and publishes unsafe terms.

### H-2 — Double-mint reconciliation remains blind and unit-inconsistent

- `processed_sigs` does not persist the Nexus destination or memo (`src/state_db.py:558-587`).
- Confirmation archives the processed row and deletes its `unprocessed_sigs` source (`src/nexus_client.py:386-390`).
- Reconciliation later left-joins back to the deleted source to recover the destination (`src/balance_reconciler.py:79-100, 146-169, 276-297`), so completed recipients disappear and `checked_addresses` may be zero.
- Amounts are stored/aggregated as token-unit floats and truncated with `int()` (`src/balance_reconciler.py:103-125, 182-190`).
- Fallback fee math uses the reverse-direction flat fee and returns float despite `-> int` (`src/balance_reconciler.py:66-72`).
- Per-account failures are silently skipped (`src/balance_reconciler.py:316-325`).

Executed 10.5-token example: actual USDC→Nexus output is 10.3895 tokens; the reconciler fallback computes 9.9895 and then completed amounts are truncated to 10. A green reconciliation result is therefore not trustworthy.

## Quality and operational findings

### M-1 — No enforceable full-suite or CI gate

- All five legacy executable test scripts pass individually.
- `python -m pytest -q tests/test_critical_safety.py`: **22 passed**.
- `python -m pytest -q`: **exit 3 during collection; no tests ran**, because import-time scripts mutate shared modules/environment and call `sys.exit()`.
- No GitHub Actions workflow exists.
- `SETUP.md` calls its list a pre-flight check but omits `tests/test_critical_safety.py` (`SETUP.md:373-384`).

### M-2 — Documentation hygiene drift

- The review document still identifies the pre-fix head at `docs/DEVELOPMENT_REVIEW_2026-08-24.md:5`; resolution text is current but the identity is ambiguous.
- `.github/copilot-instructions.md:189` still links to removed root `SECURITY.md` instead of `docs/SECURITY.md`.
- Stale operator-facing document paths remain in `CONFIG.md:173` and `SETUP.md:504,514,517-520`; moved security/state-machine/audit documents are still named as if they were in the repository root.
- `git diff --check HEAD^..HEAD` reports two trailing-whitespace additions in the development review.
- README says “Critical repaired locally” although the changes are committed and on `origin/main`.

## What looks good

- Failed, empty, malformed and truncated debit/reference lookups no longer authorize automatic retry/refund.
- Receival-asset lookup distinguishes complete absence from failure/malformed data.
- Incomplete receival lookups hold instead of entering refund.
- Unresolved Solana liabilities are deducted from spendable surplus and backing.
- Backing/supply failures pause new exposure.
- Automated non-idempotent surplus actions are hard-disabled.
- Nexus enumeration errors, malformed CREDIT contracts and truncation hold the waterline; processing never proposes a checkpoint.
- Frozen schema/status compatibility tests pass; this commit introduced no database schema change.
- Added-line scan found no hardcoded secret, shell execution, unsafe deserialization, eval/exec or interpolated SQL issue.

## Verification performed

| Check | Result |
|---|---|
| Five legacy standalone scripts | Passed individually |
| `python -m pytest -q tests/test_critical_safety.py` | 22 passed |
| Python byte-compilation | Passed |
| `python -m pip check` | Passed |
| Frozen schema/status test | Passed |
| Full `python -m pytest -q` | Failed during collection, exit 3 |
| Relative Markdown link scan | One project-owned broken link |
| `git diff --check HEAD^..HEAD` | Two whitespace findings |
| Live Solana/Nexus integration | Not run |

## Required order before deployment

1. Make Nexus refunds intent-first and reference-reconciled.
2. Remove/disable heuristic Nexus server filtering; prove ingestion and waterlines on the live target node.
3. Correct mixed-decimal thresholds, dust, and published terms with exact tests.
4. Persist immutable completed-swap destination and integer base-unit outputs; rebuild reconciliation fail-closed.
5. Convert tests into isolated pytest tests, add CI, and include the critical suite in documented pre-flight checks.
6. Run devnet/testnet tests for both directions, refund, quarantine, restart recovery, ambiguous CLI results, pagination and finality.
