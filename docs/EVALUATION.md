# swapService — Current Engineering Evaluation and Remediation Plan

**Date:** 2026-08-24
**Evaluated code:** `1e4f20cbdfef4b2570f1c0bd85fc5c20c91e15fe`
**Status:** Current issue register and repair priority for `swapService`

This document replaces the old June code-level audit as the current engineering evaluation. Historical findings and their original line references remain available in [`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) and [`RISK_ASSESSMENT.md`](RISK_ASSESSMENT.md). The independent post-change evidence behind this evaluation is in [`POST_CHANGE_REVIEW_2026-08-24.md`](POST_CHANGE_REVIEW_2026-08-24.md).

## 1. Executive verdict

**Do not deploy against real funds.**

The recent repair work materially improved the bridge:

- failed, empty, malformed and truncated debit/reference lookups no longer authorize automatic retry or refund;
- receival-asset lookup distinguishes complete absence from lookup failure;
- incomplete receival lookups hold rather than entering the refund path;
- unresolved Solana deposits are deducted from backing and spendable surplus;
- backing and circulating-supply errors fail closed;
- non-idempotent automatic surplus actions are disabled;
- incomplete or malformed Nexus enumeration holds the waterline;
- the processing pass never proposes a Nexus checkpoint.

Those controls are valuable and their focused regression suite passes. They do not make the service production-ready. Two direct fund-safety hazards remain, the mixed-decimal contract is incorrect, the double-mint detector can report a false green, and there is no enforceable full-suite or CI gate.

### Current severity summary

| Severity | Count | Meaning |
|---|---:|---|
| Critical deployment blocker | 2 | Can lose funds or permanently skip user deposits |
| High | 2 | Money contract or safety detector is materially wrong |
| Medium / operational | 5 | Evidence, response, custody or maintenance gap |
| Low / hygiene | 3 | Documentation and dead-code cleanup |

### Release gates

| Gate | Status |
|---|---|
| No ambiguous state-changing operation is retried blindly | **CONTAINED** — automatic Nexus refunds are disabled; durable refund protocol remains required |
| No checkpoint advances from incomplete/lossy enumeration | **CONTAINED** — heuristic Nexus amount filter removed; target-node enumeration evidence remains required |
| Exact money math for arbitrary configured decimals | **FAIL** |
| Durable completed-state data supports reconciliation | **FAIL** |
| One composable automated test command | **PASS locally** — `python -m pytest -q` runs the complete suite |
| CI enforces tests and static checks | **ENFORCED** — `.github/workflows/ci.yml` runs dependency, compilation, Markdown-link, test and whitespace gates on push/PR |
| Live devnet/testnet matrix | **NOT RUN** |

---

## 2. Critical deployment blockers

### E-001 — Nexus refunds are not crash-safe or idempotent

**Severity:** Critical
**Priority:** P0 — contain immediately, then implement durable protocol

`refund_nexus_token()` calls `transfer_nexus_between_accounts()` directly (`src/nexus_client.py:491-522`). The operation:

- writes no durable refund intent before `finance/debit/account`;
- does not persist a returned Nexus transaction id;
- maps CLI timeout, exception and non-zero result to `False`, even though the node may have accepted the debit;
- performs no on-chain reference lookup before a retry.

Four automatic refund paths call this boolean operation and retry it (`src/swap_nexus.py:215-255, 549-569, 600-620`). A process crash or timeout after Nexus accepts the refund but before the local `refunded_txids` write can therefore send the same refund twice.

The current `is_processed_txid()` check does not provide idempotency. The source credit remains in `unprocessed_txids`, while a completed refund is archived in `refunded_txids`, not `processed_txids`.

#### Immediate containment

Disable automatic Nexus refunds. Hold affected rows for operator review and alert with txid, sender, amount, reason and age. This reduces availability but removes the double-refund path while the durable protocol is built.

#### Required permanent repair

1. Persist refund intent before the Nexus debit: source txid, destination, exact base units, unique reference, attempt timestamp and status.
2. Execute the debit using that persisted reference.
3. Treat timeout, process interruption, non-zero exit and unparsed output as `outcome_unknown`.
4. Resolve the reference on-chain before retrying, marking complete or quarantining.
5. Persist the Nexus refund txid before removing the source queue row.
6. Never infer non-execution from a bounded or failed history scan.

#### Exit criteria

- Crash between debit and local completion cannot produce a second refund.
- Timeout after a simulated accepted debit holds and resolves; it never retries blindly.
- Restart recovery reconstructs every refund intent.
- Tests cover accepted/parsed, accepted/unparsed, timeout-before-submit, timeout-after-submit, crash, restart and duplicate invocation.

---

### E-002 — Default heuristic Nexus filtering can hide credits and still advance the waterline

**Severity:** Critical deployment blocker
**Priority:** P0 — remove before any live-fund run

`USE_NEXUS_WHERE_FILTER_USDD` defaults to true (`src/config.py:268-270`). The poller appends a heuristic `contracts.amount` filter despite documenting that array members cannot be addressed directly (`src/swap_nexus.py:643-659`).

If the target Nexus build accepts the expression but returns an empty result instead of an explicit error, the poller considers the scan complete. With no queued rows or page timestamps it advances the waterline to approximately now (`src/swap_nexus.py:941-947`). Real treasury credits filtered out by the server then fall permanently below the checkpoint.

This behavior has not been observed against the target node. For a money-ingestion path, unverified filtering semantics are not safe enough to enable by default.

#### Immediate containment

- Remove the server-side filter, or set it permanently off until a live acceptance test proves lossless behavior.
- Enumerate transactions without a lossy amount predicate and apply dust/minimum policy locally after durable capture.

#### Exit criteria

- A target-node test creates credits below dust, between dust/minimum, and above minimum; every expected credit is returned by enumeration.
- Unsupported/malformed query behavior produces an explicit incomplete scan and holds the waterline.
- Empty results can advance only after an unfiltered, validated scan.
- Pagination, processing caps and concurrent new transactions cannot move the checkpoint past an unpersisted credit.

---

## 3. High-priority correctness issues

### E-003 — Mixed-decimal thresholds and published terms use the wrong scale

**Severity:** High
**Priority:** P1 — fix before claiming token-pair agnosticism

The service is configurable for different decimals, but several minimum/dust values cross token scales incorrectly:

- `MIN_CREDIT_NEXUS_UNITS` derives from `FLAT_FEE_TO_SOLANA_UNITS` (`src/config.py:219,247-253`).
- `DUST_CREDIT_NEXUS_UNITS` derives from the same Solana-scaled value (`src/config.py:258-260`).
- `build_service_record()` formats `MIN_DEPOSIT_SOLANA_UNITS` with `_format_nexus_amount()`, which always uses Nexus decimals (`src/nexus_client.py:1174-1179`; formatter at `src/nexus_client.py:159-174`).
- `tests/legacy_token_pair.py:89-91` checks only that a published minimum contains a digit or decimal point; it does not assert exact cross-decimal values.

Executed 8-decimal Solana / 6-decimal Nexus example with default 0.5/0.1 flat fees:

| Value | Current | Intended |
|---|---:|---:|
| Nexus minimum | 100.0 tokens | 1.0 token |
| Nexus dust floor | 5.0 tokens | 0.05 token |
| Published `min_to_nexus` | 20.0 | 0.2 |

Valid user credits can be ignored as dust or retained as fees, and the public service record gives unsafe terms.

#### Required repair

- Compute every fee/minimum/dust value directly in the base units of the token it applies to.
- Use explicit `format_solana_units()` and `format_nexus_units()` helpers; never infer scale from the destination.
- Define direction-specific dynamic-fee inputs clearly.
- Add exact assertions for 6/6, 8/6, 6/8, 9/6 and zero-decimal configurations.

#### Exit criteria

For every supported decimal pairing, published terms exactly match enforced thresholds and a client can compute the actual output before sending funds.

---

### E-004 — Double-mint reconciliation is blind and unit-inconsistent

**Severity:** High
**Priority:** P1 — safety detector must fail closed before deployment

The current reconciler cannot reliably discover completed mint recipients:

- `processed_sigs` stores no Nexus destination or memo (`src/state_db.py:558-587`).
- Confirmation archives the processed row and removes `unprocessed_sigs`, which held the memo (`src/nexus_client.py:386-390`).
- Reconciliation later left-joins to that deleted row to recover the destination (`src/balance_reconciler.py:79-100,146-169,276-297`).
- `run_balance_reconciliation()` may therefore check zero addresses and return no discrepancies.

Its amount math is also inconsistent:

- token-unit floats are truncated with `int()` (`src/balance_reconciler.py:103-125,182-190`);
- fallback mint math uses the reverse-direction flat fee and returns float despite `-> int` (`src/balance_reconciler.py:66-72`);
- per-account failures are silently skipped (`src/balance_reconciler.py:316-325`).

Executed 10.5-token example:

- actual USDC→Nexus output: 10.3895 tokens;
- reconciler fallback: 9.9895 tokens;
- archived/comparison values truncate to whole tokens.

A green result is not evidence of balance correctness.

#### Required repair

1. Migrate `processed_sigs` to persist immutable destination, original memo, exact Solana input base units and exact Nexus output base units.
2. Populate those fields atomically when completing a swap; never recover required context from a deleted queue row.
3. Rebuild all reconciliation math in integer base units using the same production fee function as the debit path.
4. Return `healthy=False` when no expected addresses are checked, rows cannot be parsed, history is incomplete or any account calculation fails.
5. Alert on incomplete reconciliation separately from a confirmed discrepancy.

#### Exit criteria

A seeded completed mint is discovered after the queue row is deleted, checked in exact base units, and produces zero delta. A deliberately duplicated mint produces a positive discrepancy. Zero checked addresses cannot report healthy.

---

## 4. Medium and operational issues

### E-005 — Enforceable full-suite test command and CI

**Priority:** P1, before large repair batches

**Status: contained.** The legacy scripts now run as pytest-managed subprocess cases, so
`python -m pytest -q` is the complete local command. GitHub Actions workflow
`.github/workflows/ci.yml` runs on pushes and pull requests and enforces dependency
consistency, byte-compilation, local Markdown-link verification, the complete pytest suite
and whitespace checking. The checked-in link verifier also caught and corrected the stale
Copilot-instructions security-document path.

**Remaining release evidence:** every production candidate still needs a green remote CI run
from GitHub, plus the separate live-chain matrix in E-006.

### E-006 — No live-chain acceptance matrix

**Priority:** P1 release evidence

No test has exercised the current service against the target Nexus CLI/node and Solana devnet/testnet. The remaining highest-risk assumptions concern exactly those boundaries: CLI timeout semantics, transaction-reference fields, query/filter behavior, finality, pagination and restart recovery.

Required matrix:

- both swap directions;
- refund and quarantine;
- accepted but unparsed Nexus result;
- timeout before/after chain acceptance;
- process crash and restart at each intent/action boundary;
- pagination and processing caps;
- malformed API bodies;
- Solana finalized/confirmed behavior;
- waterline monotonicity and no skipped deposits.

### E-007 — Ambiguous items can remain held without a complete operator-resolution workflow

**Priority:** P2

The fail-closed lookup changes correctly hold uncertain rows, but held states need explicit operator lifecycle support: alert after an age threshold, dashboard classification, evidence display, documented manual resolution commands and an auditable disposition. `debited, awaiting confirmation` is not included in `dashboard.SIG_ISSUE_STATUSES` (`src/dashboard.py:48-55`).

### E-008 — Nexus PIN and session are exposed in process arguments

**Priority:** P2 custody hardening

State-changing Nexus calls pass `pin=` and, in multiuser mode, `session=` through argv. Local users can read them through process inspection. Use a Nexus-supported unlocked session, stdin, protected credential channel or isolated service account. Do not invent a transport the target CLI does not support; verify the mechanism on the actual build.

### E-009 — Exposure controls and alerting are optional by default

**Priority:** P2 operational hardening

Per-swap and daily payout caps default to disabled (`0`), and alert delivery is optional. Before production, require non-zero values appropriate to vault size and require at least one tested alert channel. Startup should refuse production mode when these controls are absent.

---

## 5. Low-priority cleanup

### E-010 — Stale/dead configuration and helper paths

`DEBIT_VERIFY_GRACE_SEC` describes an automatic negative-lookup conclusion that no longer occurs. Legacy single-item lookup helpers and disabled fee-conversion code should be removed or clearly isolated so future changes do not accidentally reactivate unsafe behavior.

### E-011 — Documentation relocation and identity drift

- `.github/copilot-instructions.md:189` links to removed root `SECURITY.md`.
- Stale moved-document paths remain in `CONFIG.md:173` and `SETUP.md:504,514,517-520`.
- The prior development review identifies pre-fix head `4921536` while its resolution text describes later code.
- The reviewed commit contains two trailing-whitespace findings in the old review document.

### E-012 — Dashboard bearer token may appear in URLs

The dashboard accepts `token` from the query string (`src/dashboard.py:444-447`). Query credentials can leak through history, logs and referrers. Prefer the Authorization header and reject query-token authentication when bound beyond loopback.

---

## 6. Architecture assessment

### Sound decisions

- SQLite state is the local source of truth and WAL/atomic reference behavior is tested.
- State-machine strings and on-disk schema are protected by compatibility tests.
- Solana sends use memos/signatures for recovery and idempotency.
- Unresolved liabilities reduce available backing.
- Automatic surplus movements are disabled until an idempotent protocol exists.
- Lookup and waterline changes now prefer a visible hold over an unsafe inferred success.
- Dashboard access is read-only at the SQLite layer.

### Architectural rule to apply everywhere

Every state-changing cross-chain action must follow the same protocol:

```text
persist intent -> execute once -> record returned identity ->
resolve ambiguous outcome against chain -> finalize local state
```

A timeout is not failure, an empty bounded scan is not absence, and a warning is not a safety control. This rule already protects the repaired Solana→Nexus debit path; it must also govern Nexus refunds, quarantine transfers, fee movements and any future automated maintenance action.

---

## 7. Prioritized repair plan

### Batch 0 — Immediate containment

**Goal:** remove active paths that can lose funds before broader refactoring.

1. ✅ Disable automatic Nexus refunds; hold and alert instead.
2. ✅ Disable/remove heuristic Nexus server filtering.
3. ✅ Add dashboard/alert visibility for every held state, including chain references, held-refund reason and a safe required action.

**Containment exit:** met in the current branch: no ambiguous Nexus debit is retried automatically and no filtered enumeration can advance a checkpoint. The permanent refund protocol and target-node acceptance evidence remain required release work.

### Batch 1 — Establish the engineering gate

**Goal:** make every later change verifiable.

1. ✅ Convert legacy executable checks into isolated pytest cases (one fresh interpreter per case; no import-time `sys.exit()`).
2. ✅ Make one command run the complete suite.
3. ✅ Add CI and static/document checks.
4. Add exact mixed-decimal regression cases and failing reconciliation fixtures before production changes.

**Progress:** `python -m pytest -q` now collects and runs the entire local suite. CI gates
the same suite plus dependency, compilation, Markdown-link and whitespace checks. The
pre-production mixed-decimal and reconciliation regression fixtures remain open.

### Batch 2 — Durable Nexus refund protocol

Implement intent, unique reference, txid capture, ambiguous-outcome resolution and restart recovery. Re-enable automatic refunds only after focused and live tests pass.

### Batch 3 — Exact cross-decimal money contract

Separate token scales for all fees, thresholds, dust, output calculations and public terms. Keep every persisted/computed amount in integer base units.

### Batch 4 — Durable completed-state model and reconciliation

Add the required migration and populate immutable destination/input/output fields. Replace the current reconciler and make incomplete evidence fail closed.

### Batch 5 — Live integration and operational gates

Run the full matrix on the target Nexus build plus Solana devnet/testnet. Require configured caps, quarantine accounts, alerting and production supervision before enabling live funds.

### Batch 6 — Custody and maintainability hardening

Move secrets out of argv where supported, remove dead paths/config, fix documentation links, add structured logging and document incident/recovery procedures.

---

## 8. Verification snapshot

| Check | Current result |
|---|---|
| `tests/legacy_smoke.py` | Enforced as an isolated pytest case |
| `tests/legacy_token_pair.py` | Enforced as an isolated pytest case, but cross-decimal threshold assertions are insufficient |
| `tests/legacy_session.py` | Enforced as an isolated pytest case |
| `tests/legacy_frozen_names.py` | Enforced as an isolated pytest case |
| `tests/legacy_dashboard.py` | Enforced as an isolated pytest case |
| `python -m pytest -q tests/test_critical_safety.py` | 25 passed |
| Python byte-compilation | Passed |
| Dependency consistency | Passed |
| Full `python -m pytest -q` | Passed locally (30 collected tests) |
| CI workflow | `.github/workflows/ci.yml` — push/PR quality gate |
| Live integration | Not run |

## 9. Definition of deployment-ready

Deployment may be reconsidered only when:

- E-001 through E-006 are closed with tests and read-back evidence;
- the complete suite and CI are green from a clean checkout;
- reconciliation cannot report healthy with incomplete evidence;
- exact mixed-decimal public terms match enforcement;
- devnet/testnet restart, timeout, refund and waterline tests pass on the target node build;
- operational caps, quarantine destinations and alert delivery are configured and tested;
- an independent reviewer approves the resulting diff.
