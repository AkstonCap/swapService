# Independent Development and Architecture Review — 2026-08-24

**Review window:** 2026-08-17 00:00 CEST through 2026-08-24 15:51 CEST
**Baseline:** `12c23998b698548a1fc9196468d6195856bdc00a`
**Reviewed head:** `492153607224ad9bfc650051177f6b4c2130fbc5`

## Verdict

**Original review verdict:** do not deploy against real funds. The reviewed head contained three Critical fund-safety failures and three High reliability/correctness failures.

## Resolution update — 2026-08-24

The three Critical findings are **repaired in the working tree with focused regression coverage**:

1. **C-1 lookup ambiguity:** `BatchLookup` now carries values plus completeness. CLI/API/parse errors, exhausted pagination, and negative history scans are incomplete. Only a positive txid/reference match is actionable automatically; missing values never authorize retry/refund. Confirmation timeout age is measured from the debit attempt.
2. **C-2 unresolved liabilities:** all rows in `unprocessed_sigs` are summed as gross Solana liabilities and subtracted from spendable surplus and available backing, including refund and quarantine states. Non-idempotent automatic surplus minting and fee conversion are safety-disabled; surplus produces an operator alert instead.
3. **C-3 Nexus waterline:** enumeration errors, malformed responses, and pagination truncation hold the waterline. The processing pass no longer advances a checkpoint without scan evidence.

The related **H-1 fail-open backing check is also repaired**: unavailable supply now raises, and backing-check errors pause new exposure.

Verification added in `tests/test_critical_safety.py`: **22 tests passed**, alongside all five pre-existing standalone test scripts, byte-compilation, and dependency checks. No live Solana/Nexus test was performed.

A follow-up independent review identified and drove four additional fail-closed repairs:

- receival-asset lookup now distinguishes complete absence from CLI/API/schema failure;
- incomplete receival lookups hold both the initial and trade-balance-check states rather than entering refund;
- processing never proposes a Nexus waterline, even with active rows, and pagination truncation holds before considering rows;
- malformed CREDIT contracts (missing/invalid operation, sender, destination, or positive amount) invalidate enumeration and hold the waterline.

**Deployment is still blocked** until independent review and the standing live devnet/testnet matrix pass. High findings H-2 (mixed-decimal thresholds) and H-3 (durable reconciliation data) remain open.

## Critical findings

### C-2026-08-24-1 — Incomplete Nexus lookups are treated as proof of absence

- `src/nexus_client.py:241-270` collapses CLI errors, parse failures, exceptions, and transactions outside the latest 200 results into an empty map.
- `src/nexus_client.py:317-326` interprets a missing entry as “never appeared” and can schedule a Solana refund.
- Reference lookup has the same shape: a bounded 100-result scan at `src/nexus_client.py:659-702` feeds retry/refund decisions at `src/nexus_client.py:414-435`.
- Confirmation timeout is measured from the original deposit timestamp, not the debit-attempt timestamp (`src/nexus_client.py:322`).

**Impact:** an executed Nexus mint may be retried, or a user may receive both Nexus tokens and a Solana refund.

**Required architecture:** tri-state lookup (`FOUND`, `AUTHORITATIVELY_NOT_FOUND`, `INCOMPLETE/FAILED`). Retry/refund is forbidden on the third state. Prefer direct txid/reference queries or demonstrably complete pagination.

### C-2026-08-24-2 — Surplus mint ignores unresolved refund/quarantine liabilities

`src/main.py:283-307` mints apparent vault surplus after checking only a status allowlist. It omits `to be refunded`, `refund sent, awaiting confirmation`, `to be quarantined`, `quarantine sent, awaiting confirmation`, and `quarantine failed`. Those liabilities later leave the vault through `src/solana_client.py:771-900`.

**Impact:** an invalid deposit can be counted as surplus, minted to the Nexus fee account, and then refunded/quarantined from the vault, leaving the bridge under-backed. `src/fees.py:141-218` has the same conceptual exposure.

**Required architecture:** explicit unresolved-liability ledger in Solana base units. Surplus = vault assets − circulating liability − unresolved outbound liabilities. Surplus minting also needs intent-before-action, reservation, and ambiguity resolution.

### C-2026-08-24-3 — Nexus waterline advances after enumeration failure

Nexus page errors only break enumeration (`src/swap_nexus.py:700-718`). No `fetch_failed` state survives. With no parsed rows/timestamps, `src/swap_nexus.py:906-919` can advance the waterline to near-now; later polls skip credits at or below it (`src/swap_nexus.py:682-690,742-743`).

**Impact:** a transient CLI/RPC failure can move the checkpoint past real user credits, permanently orphaning funds in the treasury.

**Required architecture:** mirror the Solana invariant—enumeration failure holds the waterline completely. Advancement requires explicit proof that the scanned interval is complete and every qualifying credit is durably recorded.

## High findings

### H-2026-08-24-1 — Backing checks fail open

`src/fees.py:237-255` returns `False` (do not pause) on an exception, and `src/nexus_client.py:1005-1022` converts a failed supply read into zero. `src/main.py:270-276` can therefore overwrite its initial fail-safe state with “not paused.” This contradicts `STATE_MACHINES.md` and the B-16 resolution claim.

**Fix:** unavailable vault/supply input is a distinct error state that pauses all new exposure and emits a Critical alert.

### H-2026-08-24-2 — Mixed-decimal token-pair thresholds are unsafe

- Nexus minimum enforcement compares values scaled with different token decimals (`src/config.py:219,247-253`).
- Nexus dust derives from a Solana-scaled value (`src/config.py:258-260`).
- `min_to_nexus` formats a Solana-base value with a Nexus-decimal formatter (`src/nexus_client.py:1118`, formatter at `:158-173`).
- `tests/test_token_pair.py:89-92` checks only that a value is non-empty.

**Impact:** mixed-decimal pairs can publish or enforce thresholds off by powers of ten.

**Fix:** calculate each threshold in its own token's base units; make formatting take explicit source decimals; assert exact mixed-decimal values.

### H-2026-08-24-3 — Double-mint reconciliation is not trustworthy

Completed rows lose destination context when `unprocessed_sigs` is deleted (`src/nexus_client.py:364-365`). The reconciler later joins against that deleted table (`src/balance_reconciler.py:79-100,276-297`), truncates token amounts with `int()` (`:185-190`), uses a reverse-direction fee with a mismatched return type (`:66-72`), and silently skips per-account failures (`:316-325`).

**Impact:** reconciliation can report success while checking zero completed recipients or incorrect amounts.

**Fix:** persist exact destination and Nexus output base units in durable completed records. A run that checks zero accounts, encounters skipped rows, or cannot prove completeness must be unhealthy—not green.

## Positive architecture changes

- Frozen DB names, retry keys, reservation values, and lifecycle status strings survived the generic token rename.
- Central multiuser session injection is coherent.
- `rescale_units()` uses conservative rounding in the reviewed backing paths.
- The dashboard remains read-only, requires authentication for non-local exposure, and avoids unsafe HTML insertion.

These improvements are worthwhile, but they do not offset the fund-safety blockers above.

## Verification

| Check | Result |
|---|---|
| `tests/test_smoke.py` | Passed standalone |
| `tests/test_token_pair.py` | Passed standalone |
| `tests/test_session.py` | Passed standalone |
| `tests/test_frozen_names.py` | Passed standalone |
| `tests/test_dashboard.py` | Passed standalone |
| `python -m compileall -q src tests *.py` | Passed |
| `python -m pip check` | Passed |
| `python -m pytest -q` | **Failed at collection (exit 3)** |
| `python -m unittest discover` | **Failed** |
| `git diff --check <baseline>..HEAD` | **Failed:** four whitespace errors |

The files under `tests/` are import-time executable scripts that mutate global modules/environment and call `sys.exit()`. Passing them one by one is useful evidence, but it is not a composable test suite or enforceable CI gate.

## Documentation status

- `RISK_ASSESSMENT.md`: production gate is reopened; “no known defects remain” is not supportable. P-2/P-3 batching must be reclassified because bounded batching is unsafe when absence is inferred.
- `STATE_MACHINES.md`: “ambiguity is never treated as failure,” fail-safe pause, and waterline claims do not match code.
- `EVALUATION.md`: double-mint reconciliation must be marked operationally untrustworthy; this review supersedes the open-item summary.
- `README.md`: this review is now the current review document. Mixed-decimal registration minimums are not safe until fixed.

## Required repair order

1. Enforce tri-state Nexus lookups and hold all money decisions on incomplete results.
2. Hold Nexus waterlines on every incomplete/error path and test truncated pagination.
3. Replace status allowlists with a durable liability ledger before any surplus action.
4. Make backing reads fail closed.
5. Correct mixed-decimal thresholds/public terms with exact tests.
6. Redesign reconciliation around durable integer completed-state data.
7. Convert script tests into isolated pytest tests and add CI.
8. Run the standing devnet/testnet matrix only after static/unit blockers are closed.
