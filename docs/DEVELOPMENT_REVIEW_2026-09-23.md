# swapService development review — 2026-09-23

## Verdict

**Release blocked; local A/B/C safety is implemented, total-DB-loss/live acceptance is not.**

Reviewed source range:

```text
base:       da79e0928c2dc7c39648734d9ad329637c87eae6
source:     85030c890fa6f3bb7db97e068e5cf80827d21b28
HEAD tree:  a89d8904a200cafce86a5ecd002fa90978f2be13
index tree: a89d8904a200cafce86a5ecd002fa90978f2be13
```

The two commits in range are `a1f19b1 fix(recovery): migrate legacy disposition provenance` and
`85030c8 chore: update swapService with recovery and deposit policy changes`. Runtime and tests were
clean at HEAD; pre-existing documentation work was dirty and was read/merged rather than replaced.
This review changed documentation only. No staging, commit, push, credential access, network mutation
or live financial operation occurred.

## Findings in priority order

### 1. High financial contract defect — total DB/WAL loss discards unsent authorization

The waterline correctly remains behind unresolved Solana principal, so source history can be
rediscovered. That does not recover policy evidence in `unprocessed_sigs` or frozen refund/quarantine
intent in `solana_payout_capacity_holds`. `perform_startup_recovery()` can return complete because an
unsent hold has no outgoing chain memo. Incoming replay then inserts a fresh `ready for processing`
source, and workers freeze current policy/fee/destination terms.

Two fresh offline real-caller probes reproduced this at the reviewed source:

1. A 1,100-unit source had been frozen as `refund_oversized` with a 1,090-unit refund under max 1,000,
   then capacity-held under cap 50. The public waterline was 879, behind source timestamp 1,000.
   After deleting DB/WAL, startup returned complete, the source was re-admitted, max changed to 2,000,
   and the real deposit worker invoked the mocked Nexus debit boundary for 1,100 units.
2. Keeping the oversized route, DB loss plus fee/destination drift changed the submitted refund from
   original destination/1,090/fee 10 to changed destination/1,080/fee 20.

This proves neither live loss nor a duplicate payment. It proves that current total-loss recovery can
authorize a side effect inconsistent with the earlier frozen economic contract. Ordinary restart and
verified-backup acceptance remain valid only when the frozen database evidence survives.

**Containment:** do not resume an existing deployment from an empty/recreated database merely because
its public waterline permits source re-enumeration. Restore verified DB+WAL/online-backup evidence or
remain paused for explicit reconciliation.

**Exit:** reconstruct exact historical authorization from an independent durable source, or admit
rediscovered sources into quantified, visible, non-sendable recovery holds. Collected acceptance must
cover both policy holds and both disposition kinds through total loss, terms drift, multi-page replay
and worker limits, with zero transport and full liability absent restored authorization.

### 2. High operability defect — malformed oldest capacity hold blocks later valid retry

The typed capacity protocol fails closed on malformed frozen evidence, but the global oldest-hold
query still includes that row when deciding whether a later valid hold may proceed. A fresh real
refund-worker probe created two ordinary 50-unit refund holds behind 60 units of used capacity,
corrupted only the older hold's frozen JSON, released the budget and ran the worker twice.

Observed result:

```json
{
  "initial_worker_submissions": 0,
  "workers_after_release": [0, 0],
  "send_calls": 0,
  "liability_units": 120,
  "holds_after_release": [
    ["old-malformed", "rolling Solana payout cap exhausted", 1],
    ["young-valid", "waiting behind older Solana payout capacity hold", 3]
  ],
  "sources_after_release": [
    ["old-malformed", "refund capacity held", 60],
    ["young-valid", "refund capacity held", 60]
  ]
}
```

No unsafe send or liability deletion occurred. The defect is permanent starvation of later valid
work and contradicts an unqualified eligible-FIFO/progress claim. Existing tests cover malformed
refusal and impossible-cap fairness separately, but not valid work behind an oldest malformed or
conflicting row.

**Exit:** atomically move malformed/conflicting/unknown-submission rows into a durable operator-action
class outside automatic eligible FIFO while preserving frozen evidence, principal, diagnostics and
non-sendability. Add both-worker, cross-kind, over-limit, restart and resolution tests; valid younger
original intent must submit exactly once.

### 3. High deployment gate — registration and network/sync admission remain fail-open/incomplete

`main.run()` refuses incomplete startup recovery, but heartbeat validation failure/exception is
alert-only at `src/main.py:370-379`. Provider-v2 remains library/test code rather than the production
registration/recovery contract; its legacy-opt-in setting is inert. Custom Solana endpoints and Nexus
nodes are not admitted by authoritative genesis/network plus health/sync/tip freshness evidence.
This was already identified on September 22 and remains unchanged at the reviewed source.

**Exit:** fail before mutable startup/pollers on invalid provider identity, wrong network, unavailable
or stale/unsynced evidence. Validate the intended target nodes, not only mocks.

### 4. High operability gate — no audited resolution for non-capacity Solana holds

Policy, recovery-evidence, malformed/source/lifecycle-conflict and unknown-submission holds are
visible and non-sendable, but no audited Solana resolution command binds independent evidence,
actor/rationale, exact readback, cap accounting and atomic terminalization. The separate Nexus
operator protocol does not cover these rows. Direct-chain/manual-SQL advice is not an acceptable
substitute.

**Exit:** implement a reviewed operator protocol or explicitly approve permanent retention. If role
separation is required, enforce distinct identities rather than storing labels only.

### 5. Publication and optional-migration gates remain separate

- CI run `35755684698` for source SHA `85030c8` passed runtime gates but failed committed-range
  whitespace in historical `.diff` artifacts. This source-SHA result does not predict the result for
  a later documentation-only `HEAD~1..HEAD` delta. This review did not rewrite those artifacts or
  weaken the gate.
- Provider-v2 is unwired and its realistic fixture exceeds the repository's declared 1,024-byte
  estimator budget. This is not proof of a Nexus node wire limit, but blocks an assumed cutover.
- Optional receipts remain disabled pending target cost/schema/create/readback acceptance.
- No live provider, finality, pagination, TLS, Nexus completeness/reference, crash-after-acceptance or
  operator-rehearsal acceptance was performed.

## Independent assessment of the four requested areas

| Area | Implemented local safety verified | Boundary not established |
|---|---|---|
| Provenance migration | Legacy/unknown terminal rows are audited and conservatively converted to full-principal evidence holds; inferred fees are reversed; proven cap spend is preserved; strict JSON type/duplicate handling, atomic DDL/data rollback, in-place, online-backup and copied DB+WAL tests pass. | It cannot reconstruct a separate unsent B/C authorization that disappeared with the whole database. |
| Deposit policy | One strict-integer min/max/decimal/flat-plus-bps classifier runs before memo/account routing; below-minimum and nonpositive output retain full principal; frozen decisions survive restart/config drift while SQLite survives; public v1 terms use the same policy. | Rediscovered sources after wipeout are reclassified using current configuration. Live target behavior is untested. |
| Capacity-hold retry | Typed outcomes, atomic hold/reservation, exact diagnostics, full liability, original-term retry, impossible-cap non-starvation and alert/dashboard visibility pass for valid retained evidence. | Wipeout loses the intent. An oldest malformed/conflicting row remains non-sendable but can starve later valid work. Non-capacity/operator-action resolution is absent. |
| DB-loss recovery | Chain-only terminal disposition evidence is conservative; cap spend and paid composite identities can be reconstructed; public waterlines preserve raw source discoverability. | Complete recovery of unsent policy/cap intent is not implemented. Startup can report complete and later replay can authorize current terms. |

## Positive controls actually executed

- Full suite: **565 passed, 77 subtests passed**.
- Provenance migration/terminal admission/policy/cap modules: **129 passed**.
- Recovery standalone: **35 passed, 52 subtests passed**.
- Recovery plus installed SDK: **36 passed, 52 subtests passed**.
- Receipt/payout/Nexus-fee/SDK isolation: **85 passed**.
- Legacy script/frozen-name compatibility: **5 passed**.
- Dependency consistency, Python compilation, local Markdown links, literal inventory and working-diff
  whitespace passed. Inventory remained **274 active lines**.
- Both DB-loss probes and the malformed-oldest retry probe used temporary SQLite databases, disabled
  dotenv loading, blocked socket connection attempts and mocked only external chain/send boundaries.

Green tests establish the tested local controls, not total-loss or live acceptance. The diagnostic
probes are not default-collected tests; a successful probe command currently reproduces a defect.
After the documentation merge, the complete suite was rerun and again passed: **565 passed, 77
subtests passed in 68.06s**.

## Exact commands and results

All commands ran from `/home/brutus/github/swapService` with scratch under
`/home/brutus/.hermes/profiles/principal-dev/cache/scratch`.

### Repository identity and range

```bash
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
git rev-parse 'da79e0928c2dc7c39648734d9ad329637c87eae6^{commit}'
git write-tree
git log --oneline da79e0928c2dc7c39648734d9ad329637c87eae6..HEAD
```

```text
85030c890fa6f3bb7db97e068e5cf80827d21b28
a89d8904a200cafce86a5ecd002fa90978f2be13
da79e0928c2dc7c39648734d9ad329637c87eae6
a89d8904a200cafce86a5ecd002fa90978f2be13
85030c8 chore: update swapService with recovery and deposit policy changes
a1f19b1 fix(recovery): migrate legacy disposition provenance
```

### Offline gate

```bash
export TMPDIR=/home/brutus/.hermes/profiles/principal-dev/cache/scratch
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q src *.py tests
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/test_recovery_safety.py
.venv/bin/python -m pytest -q tests/test_recovery_safety.py tests/test_solana_sdk_boundary.py
.venv/bin/python -m pytest -q \
  tests/test_swap_receipts.py tests/test_payout_review_regressions.py \
  tests/test_nexus_fee_lifecycle.py tests/test_solana_sdk_boundary.py
.venv/bin/python -m pytest -q \
  tests/test_recovery_acceptance.py tests/test_recovery_terminal_admission.py \
  tests/test_solana_deposit_policy.py tests/test_solana_capacity_holds.py
.venv/bin/python -m pytest -q tests/test_legacy_scripts.py tests/legacy_frozen_names.py
.venv/bin/python scripts/check_token_pair_inventory.py
.venv/bin/python scripts/check_markdown_links.py
git diff --check
```

```text
No broken requirements found.                                              exit=0
compileall produced no output.                                             exit=0
565 passed, 77 subtests passed in 66.89s                                  exit=0
35 passed, 52 subtests passed in 2.21s                                    exit=0
36 passed, 52 subtests passed in 2.62s                                    exit=0
85 passed in 7.12s                                                        exit=0
129 passed in 13.25s                                                      exit=0
5 passed in 1.41s                                                         exit=0
Token-pair literal inventory is current (274 active lines).               exit=0
Local Markdown links: OK                                                  exit=0
git diff --check produced no output.                                      exit=0
```

### Total-loss probes

The two excluded, local-only September 22 probes were executed independently with this exact wrapper;
replace `<probe>` with each path shown below:

```bash
TMPDIR=/home/brutus/.hermes/profiles/principal-dev/cache/scratch \
.venv/bin/python -c 'import runpy,socket,sys; import dotenv; dotenv.load_dotenv=lambda *a,**k:False; runpy.run_path("tests/conftest.py"); socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(RuntimeError("network forbidden")); runpy.run_path(sys.argv[1],run_name="__main__")' \
  <probe>
```

```text
docs/review_evidence/2026-09-22/swap-reeval-db-loss-probe.py
exit=0; startup_recovery_complete=true; source_re_admitted=1;
capacity_hold_count=0; nexus_send_calls=1; nexus_send_amount=1100.

docs/review_evidence/2026-09-22/swap-reeval-cap-wipe-probe.py
exit=0; worker_submitted=1; send_calls=1;
send_args=["changed-destination",1080]; fee changed 10 -> 20.
```

The same wrapper ran the session-only malformed-oldest probe:

```text
/home/brutus/.hermes/profiles/principal-dev/cache/scratch/swap-cap-malformed-starvation-probe.py
exit=0; workers_after_release=[0,0]; send_calls=0; liability_units=120;
young-valid remained "refund capacity held" and attempt_count reached 3.
```

No new raw probe script was added to the repository.
The two September 22 probe paths and their evidence ledger remain local-only worktree references;
they are not links or dependencies in the exact five-document publication scope.

## Runtime and focused-test SHA-256

```text
e6b7f28d60e4d26c6fed1fea0ab869aaa727fb7be3672dd6d002ba1f658f52eb  src/config.py
eb79683a5aaacf7d47732deac063d166cff987486dc9c8e717c7b42f68d95d89  src/dashboard.py
3f109caa95e1a005ead47cd797a97633b192548bcf6a2055c981c9fd7429e2bf  src/main.py
7e392979aba8ac6cd06c005104fcc2861190add028ffadb44d2caced0cff29f3  src/nexus_client.py
a607bb0594be61c0a9e2429f178df8c8711e1b17572dcad9d2ba32baabed84f3  src/solana_client.py
184dea5d83396a74667988e3b300b892152b1c23f5f385d6293282ed6f95c84e  src/solana_deposit_policy.py
919e1127a5b914d360ac45c33d97d787e30500fc0f43bd5a3d0e1541806aae8b  src/startup_recovery.py
a226b450fe689fed1b121c76ece8903ea05dbf68f67f7bdc61cdc8d973fc97c1  src/state_db.py
3cf6cdaa8e5170992fdc384cc25fffaf6508ec42ab50eb0cca34e9e17065f8e5  src/swap_solana.py
437593ca7eaa3a6a5cb276a95af8f51d51eabd9b6c286835af28ac161a3b8119  tests/test_recovery_acceptance.py
5f8ea97c578fe995abb565a8df75366bdc0a4e1e851b5a750a723c3cf746112b  tests/test_recovery_safety.py
8b1965b9267b7bd0a1045dc6157e964ebd4e7249fba41143eda108fea9de9e7f  tests/test_recovery_terminal_admission.py
a0afaa85d20a80222f05bff5c61bc4d353163f22f00a4e43219921b9b984bbb3  tests/test_solana_capacity_holds.py
889cef2aa6f062ffce16ee1cba064707202e3cefe8c45ecf4f94c60c95d9bdad  tests/test_solana_deposit_policy.py
5a64f7ab587b986ad24a6adef3842ae897abf8b6c1ab9043cd5f43057f0f713  tests/test_solana_sdk_boundary.py
```

Probe hashes:

```text
84712e6af0b0b4d9bfcdb813dbf0ce402cace84a4d867c78a94f2bf58b2cb136  docs/review_evidence/2026-09-22/swap-reeval-db-loss-probe.py
559dccaa089d9079e8cb586fdb116f9eed1089a313bc6fa43d823c3e6b5b4a87  docs/review_evidence/2026-09-22/swap-reeval-cap-wipe-probe.py
cc262d0d99e7aa07c2d5d97e445a0c3f7ff7b00402f71a873d966398ba9cc950  session-only malformed-oldest probe
```

The real-index SHA-256 was
`bebecf20ec1b93aaeaffadddc691f64bd0be7d3cad017fca4f88e5473c4cecc1` before execution and matched
after the final documentation gate. `git write-tree` remained
`a89d8904a200cafce86a5ecd002fa90978f2be13`, and `git diff --cached --name-status` remained empty.

## Documentation paths changed by this review

The review merged into three paths that were already dirty at task start and created two new dated
follow-up documents:

```text
docs/EVALUATION.md
docs/STATE_MACHINES.md
docs/plans/recovery-input-cap-repairs.md
docs/plans/2026-09-23-financial-recovery-follow-up.md
docs/DEVELOPMENT_REVIEW_2026-09-23.md
```

Pre-existing dirty `docs/RECOVERY_INPUT_CAP_ACCEPTANCE.md` and all September 22 untracked reports and
probe scripts were preserved without review-authored edits. They must not be swept into publication
merely because they are present in the worktree. No runtime, test, dependency or workflow path was
modified.

For exact-docs-only preparation, a disposable index based on source SHA `85030c8` staged exactly the
five paths above. Its `git diff --cached --check` and index-aware token inventory passed at 274 active
lines. A source-tree archive overlaid with only those five documents, and therefore excluding every
September 22 untracked file, passed the local Markdown-link checker. The real index remains untouched;
the publisher must repeat the index-aware inventory and candidate gates after staging the same five
paths, then verify CI for the resulting exact SHA.
