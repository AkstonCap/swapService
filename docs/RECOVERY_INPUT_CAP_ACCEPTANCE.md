# Recovery, input-policy and capacity-hold acceptance report

- **Report date:** 2026-09-22
- **Base HEAD:** `a1f19b109681da693331583e30fa192ccee17d2d`
- **Original acceptance candidate:** dirty single-pair implementation on the base above.
- **Publication verified on re-evaluation:** `85030c890fa6f3bb7db97e068e5cf80827d21b28` on local and origin `main`; all 21 reviewed runtime/test hashes match.
- **Current decision:** **RELEASE BLOCKED — unsent B/C authorization is not recovered after total DB/WAL loss.**
- **Historical decision:** the original bounded offline A/B/C review approved its tested scope; see the qualification below.

## Re-evaluation qualification

The published A/B/C implementation still passes its existing tests, but the September 22 broader
review reproduced an untested cross-boundary defect: a pinned public waterline preserves source
discoverability, not frozen policy/cap-hold authorization after total database loss. Startup can
return complete and subsequent replay can choose a new route or refund amount/destination under
current configuration. **B/C restart acceptance is limited to surviving frozen database evidence.**
Restore that evidence or retain a quantified non-sendable recovery hold; this latter fail-closed
contract still requires implementation. See [current finding R-1](EVALUATION.md)
and the [independent financial review](review_evidence/2026-09-22/reevaluation-financial.md).
The original approval below is historical, not a fresh whole-system approval.

## Original offline acceptance record

Batches A, B and C are implemented and have green attributed offline evidence. Batch B received
independent batch-scoped quality approval. Batch C's first independent spec review was blocked; both
findings were subsequently repaired and covered by real-worker regressions. The final independent
whole-candidate runtime review is **APPROVED** and the parent-owned exact-candidate gate passed.
The [independent closure report](review_evidence/2026-09-22/final-runtime-review.md) records the
reviewed runtime/test hashes, which the parent verified against the current files.

This report is not a release record. It does not approve production, live-chain operation, real funds,
provider-v2, optional receipts or a dependency upgrade. Nothing was staged, committed, pushed or
published as part of this documentation update.

## Scope and acceptance map

| Batch | Implemented contract | Primary repository evidence | Current status |
|---|---|---|---|
| A — recovery provenance/migration | Strict provenance decoding and exact types; atomic DDL/data migration; legacy/unknown terminal conversion to a quantified full-principal evidence hold; preserved proven cap spend and reversed inferred fee; in-place, online-backup and DB+WAL restore behavior | [`tests/test_recovery_acceptance.py`](../tests/test_recovery_acceptance.py), [`tests/test_recovery_safety.py`](../tests/test_recovery_safety.py), [`tests/test_recovery_terminal_admission.py`](../tests/test_recovery_terminal_admission.py), [`src/state_db.py`](../src/state_db.py), [`src/startup_recovery.py`](../src/startup_recovery.py) | Offline accepted; final independent integration review and parent gate passed |
| B — Solana input policy | One pure strict-integer min/max/decimal/fee classifier before destination routing; full-principal non-sendable holds for below-minimum and non-positive output; exact boundaries; established oversized refund route; frozen restart/config behavior; shared live/recovery path | [`tests/test_solana_deposit_policy.py`](../tests/test_solana_deposit_policy.py), [`src/solana_deposit_policy.py`](../src/solana_deposit_policy.py), [`src/solana_client.py`](../src/solana_client.py), [`src/dashboard.py`](../src/dashboard.py) | Offline accepted; batch and final independent integration review passed |
| C — refund/quarantine capacity holds | Closed typed outcomes; atomic source/hold/reservation transition; exact needed/used/cap diagnostics; full-principal liability; visible/rate-limited alerts; oldest-first retry from original frozen terms; no retry for conflicts, malformed evidence or submitted/unknown outcomes | [`tests/test_solana_capacity_holds.py`](../tests/test_solana_capacity_holds.py), [`tests/test_payout_budget.py`](../tests/test_payout_budget.py), [`tests/test_critical_safety.py`](../tests/test_critical_safety.py), [`src/state_db.py`](../src/state_db.py), [`src/solana_client.py`](../src/solana_client.py), [`src/dashboard.py`](../src/dashboard.py) | Offline accepted; spec and fairness blockers repaired; final independent integration review passed |

## Batch A acceptance

### Provenance and conservative migration

`pre_submission_v1` evidence rejects duplicate object keys at every depth, non-finite constants,
missing/extra or malformed fields, and values whose runtime type is not exact. In particular, JSON
booleans and integral floats cannot satisfy integer fields. Source and payout identifiers remain
byte-exact. Valid frozen `legacy_pre_submission_v0` evidence remains supported.

Absent, malformed, recovery-only or unknown authorization provenance does not validate an old terminal
row. Migration removes the unsafe terminal, reverses its inferred fee, retains independently proven cap
spend, restores the complete source principal in `refund evidence held` or `quarantine evidence held`,
and records an idempotent migration audit. The held source is dashboard-visible, liability-counted and
selected by neither disposition send worker.

### Transaction and restore boundary

`PRAGMA journal_mode=WAL` runs before the migration transaction. `BEGIN IMMEDIATE` starts before any
schema or data migration statement, and the wrapper owns commit/rollback/close. Injected late DDL,
cap, fee, source and migration-audit failures roll back as a unit and permit a safe retry. Acceptance
covers an in-place legacy database, SQLite online backup and copied DB+WAL restore.

### Repository test coverage

- [`test_in_place_upgrade_holds_old_recovery_terminal_even_outside_scan_range`](../tests/test_recovery_acceptance.py)
- [`test_restart_migration_holds_terminal_with_nonexact_intent_evidence`](../tests/test_recovery_acceptance.py)
- [`test_current_provenance_retains_frozen_authorization_across_restart_and_config_change`](../tests/test_recovery_acceptance.py)
- [`test_real_legacy_database_backup_restore_migrates_atomically_and_idempotently`](../tests/test_recovery_acceptance.py)
- [`test_pre_provenance_ddl_migration_failure_is_atomic_and_retryable`](../tests/test_recovery_acceptance.py)
- [`test_legacy_migration_failure_rolls_back_terminal_fee_and_hold_then_retries`](../tests/test_recovery_acceptance.py)

The links intentionally target durable repository source rather than session-local review artifacts.

## Batch B acceptance

The shared policy takes only exact positive integer input and immutable integer terms:
minimum/maximum input units, input/output decimals, flat output fee and fee basis points. It floor-scales
decimals and fee arithmetic without floats. Decision precedence is:

1. input below minimum → `hold_below_minimum`;
2. input above a nonzero maximum → `refund_oversized`;
3. calculated output not positive → `hold_nonpositive_output`;
4. otherwise → `payable`.

Minimum and maximum equality pass their respective size checks; payout still requires positive
output after conversion and fees. Both hold decisions persist as `policy held, non-sendable`,
retain full principal, create no fee and return before account/memo validation or Nexus transport.
Payable invalid destinations retain the established refund route. Live Helius and recovery/core
admission converge on the same real worker gate. Frozen payable decisions survive restart/config drift,
and previously submitted intent is not reclassified.

`MICRO_DEPOSIT_FEE_PCT` is intentionally absent from executable policy and public v1 terms: there is no
supported percentage micro-deposit disposition. This repair does not integrate provider-v2.

Primary coverage is in [`tests/test_solana_deposit_policy.py`](../tests/test_solana_deposit_policy.py),
including real-worker no-transport/full-liability cases, mixed decimals, exact boundaries, non-positive
output, invalid destination precedence, rollback, restart/config drift, historical intent and shared
live/recovery ingestion.

## Batch C acceptance

Refund and quarantine preparation returns exactly one typed result: `prepared`, `capacity_held`,
`current_cap_too_low`, `source_conflict`, `malformed_evidence`, `db_failure` or `already_submitted`. Only `prepared` may proceed
to transport. Under `BEGIN IMMEDIATE`, admission evaluates current rolling capacity and atomically
writes the source state, hold or reservation, disposition intent and frozen evidence.

A capacity hold preserves source/kind/obligation, needed/used/cap units, timestamps, reason, retry
count and the original destination/output/memo/source/fee/service terms. The complete source principal
remains an unresolved liability. Dashboard/API detail and rate-limited warnings expose the durable
state; alert failure cannot erase it or trigger a send.

Retry is oldest-first among eligible holds and applies current capacity to validated original frozen terms. It does
not resolve a new destination or recompute payout terms after configuration drift. Conflicting terminal
state is reported as `source_conflict` while retaining source, hold, liability and visibility. Malformed
evidence stays held. Pre-RPC durable intent and submitted/unknown outcomes cannot be released into a
second send. Concurrent exact-boundary workers admit/send once.

A frozen payout larger than the current nonzero cap returns `current_cap_too_low`. Its full principal
and exact intent remain held with an explicit operator action; rolling-window aging cannot fix it.
Already diagnosed impossible holds are filtered before worker limits and do not starve fitting work,
including the other disposition kind. A higher cap (or the established unlimited cap `0`) can restore
eligibility without regenerating terms. Unchanged impossible holds do not emit repeated alerts.

### Superseded Batch C blocker review

The first independent spec review returned **BLOCKED** for two real-worker defects:

1. held retries recomputed mutable destination/fee terms instead of consuming frozen intent;
2. worker conflict prechecks could delete the source before typed preparation, orphaning the hold and
   removing full principal from liability/dashboard detail.

Both findings are superseded for the current implementation by
[`test_restart_retries_frozen_capacity_hold_after_confirmed_spend_ages_out_once`](../tests/test_solana_capacity_holds.py)
and
[`test_real_worker_capacity_hold_terminal_conflict_retains_full_liability`](../tests/test_solana_capacity_holds.py).
The repair also retains malformed-evidence coverage in
[`test_worker_alerts_on_malformed_hold_without_send_or_evidence_rewrite`](../tests/test_solana_capacity_holds.py).
Final independent closure review approved these repairs and the subsequent impossible-cap fairness
repair. The latter adds real-worker same/cross-kind, bounded-window and cap-change regressions.

## Verification ledger

Counts below are preserved with their source and chronology. A historical green run only supports the
candidate snapshot tested by that runner; it is not silently promoted to the parent final gate.

| Stage / source | Command or gate | Recorded result | Interpretation |
|---|---|---|---|
| Batch A provenance repair implementer | `.venv/bin/python -m pytest tests/test_recovery_acceptance.py -q` | **56 passed** | Final A acceptance module after strict provenance repair |
| Batch A provenance repair implementer | `.venv/bin/python -m pytest tests/test_recovery_safety.py -q` | **35 passed, 52 subtests passed** | Recovery safety shard |
| Batch A DDL repair implementer | Focused acceptance/recovery/Nexus migration gate | **108 passed, 52 subtests passed** | Includes final DDL transaction-boundary repair |
| Batch A DDL repair implementer | `.venv/bin/python -m pytest -q` | **506 passed, 77 subtests passed** | A-era full dirty-tree run; historical, not the final A/B/C candidate |
| Independent Batch B quality reviewer | `.venv/bin/python -m pytest -q tests/test_solana_deposit_policy.py` | **28 passed** | Batch B focused approval evidence |
| Independent Batch B quality reviewer | `.venv/bin/python -m pytest -q` | **534 passed, 77 subtests passed** | Reviewed B-era dirty candidate; historical |
| Independent Batch B quality reviewer | frozen-name script, scoped compile, `git diff --check` | Passed | Batch-scoped static/compatibility evidence |
| Initial independent Batch C spec reviewer | Full suite and focused gates before blocker repair | **552 passed, 77 subtests passed**; focused gates passed | Green tests did not catch the two documented blockers; explicitly superseded |
| Batch C blocker-fix implementer | `.venv/bin/python -m pytest -q tests/test_solana_capacity_holds.py` | **22 passed** | Final implementer cap module after both blocker repairs |
| Batch C blocker-fix implementer | Capacity/payout/recovery/critical-safety focused gate | **289 passed, 77 subtests passed** | Final implementer focused safety run |
| Batch C blocker-fix implementer | `.venv/bin/python -m pytest -q` | **556 passed, 77 subtests passed** | Latest supplied implementer full dirty-tree run; not claimed as parent final verification |
| Batch C blocker-fix implementer | legacy frozen-name suite; compilation; `git diff --check` | **5 passed**; static checks clean | Final implementer compatibility/static evidence |
| Parent runtime review | [`tests/test_solana_capacity_holds.py`](../tests/test_solana_capacity_holds.py) | **22 passed** | Parent focused rerun supplied while final review continues |
| Parent final candidate gate | Full suite | **565 passed, 77 subtests passed** | Stable candidate verified with disposable index |
| Parent focused gates | Recovery; recovery+SDK; receipt/payout/Nexus-fee+SDK; policy/cap/recovery acceptance | **35 + 52 subtests; 36 + 52 subtests; 85; 129 passed** | Separate commands, overlapping tests; do not sum |
| Final independent whole-candidate runtime review | Code quality, fairness closure and A/B/C integration | **APPROVED; 565 passed, 77 subtests passed** | Reviewed runtime/test hashes match parent candidate |
| Parent static/repository gate | Dependency check, compile, Markdown links, disposable-index inventory, whitespace, real-index/unrelated-file confirmation | **Passed; 274 active inventory lines** | Real index and all baseline unrelated files unchanged |

## Remaining acceptance gates

- **Completed offline:** independent review, parent full/shard execution and repository integrity
  checks above. The working-tree suite includes preserved pre-existing provider-v2 files; it does
  not approve that proposal. The inventory was regenerated for this explicit working candidate,
  including its unchanged config; rerun it if publication excludes those inherited changes.
- **Publication (rechecked 2026-09-22):** the accepted runtime was committed in `85030c8`.
  [Exact-head CI](https://github.com/distordialabs-brutus/swapService/actions/runs/35755684698)
  passed the tests, SDK/configuration shards, compile, dependency and link steps, but **failed**
  committed-range whitespace on historical diff artifacts (194 findings in
  `committed-since-sept12.diff`, 2 in `dirty-config.diff`). Fresh local execution passed
  565 tests + 77 subtests. Prior dirty-tree acceptance remains a historical record, not a
  claim that publication CI is green. See [current evaluation](EVALUATION.md) for broader
  re-evaluation findings and release gates.
- **Tool limits:** no broad Ruff/mypy pass is claimed (not installed). One optional reviewer
  corruption probe was tool-denied before execution and was not rerouted; required collected
  acceptance tests and original fairness reproduction executed successfully.
- **External acceptance:** on explicitly authorized devnet/test infrastructure, exercise provider
  pagination/finality, chain readback, both directions and decimals, crash/unknown boundaries,
  backup/WAL operations, alert delivery and incident/hold-resolution rehearsal.
- **Release decision:** remains a separate human-controlled gate. No production or real-funds approval
  exists, even after offline acceptance becomes green.

## Operator resolution boundary

Use dashboard/API diagnostics, logs, migration audit and authoritative chain evidence. Capacity-only
holds may retry automatically when the rolling window admits the original frozen intent. Provenance,
malformed-evidence, lifecycle-conflict and unknown-submission holds require the reviewed independent
authorization workflow. Do **not** repair state with manual SQL and do **not** bypass a hold by sending
tokens directly.
