# Complete findings — swapService review 2026-09-25

## Scope and identity

```text
repository:      /home/brutus/github/swapService
review base:     184f5d6a45ecd8f53ae37cfdd09e4b63092d1842
source HEAD:     17f65a3e3b45281162c1604cd0a695a36dc55991
source tree:     6e39b479f2e9379b57554568b237a59cbe175a42
initial index:   6e39b479f2e9379b57554568b237a59cbe175a42
range commits:   809d45c191b90a47af159866ed5ad7399ceb7f89
                 17f65a3e3b45281162c1604cd0a695a36dc55991
```

Reviewed range paths:

```text
README.md
src/dashboard.py
src/startup_recovery.py
src/state_db.py
tests/legacy_frozen_names.py
tests/test_critical_safety.py
tests/test_dashboard_recovery_admission.py
tests/test_empty_database_recovery.py
tests/test_recovery_acceptance.py
tests/test_recovery_safety.py
docs/EVALUATION.md
docs/TOKEN_PAIR_LITERAL_INVENTORY.md
```

Shared callers/dependencies additionally inspected: `.github/workflows/ci.yml`, `src/main.py`,
`src/config.py`, `src/nexus_client.py`, `src/solana_client.py`, `src/swap_solana.py`,
`tests/test_solana_capacity_holds.py`, `docs/STATE_MACHINES.md` and the prior recovery follow-up plan.

No runtime, test, dependency or workflow file was edited. No staging, commit, push, production credential,
network transaction or live-chain operation occurred.

## Initial dirty-worktree preservation

The review began with only these unrelated paths dirty/untracked:

```text
 M docs/RECOVERY_INPUT_CAP_ACCEPTANCE.md
?? docs/POST_CHANGE_REVIEW_2026-09-22_PRE_REEVALUATION.md
?? docs/review_evidence/2026-09-22/REEVALUATION.md
?? docs/review_evidence/2026-09-22/reevaluation-financial.md
?? docs/review_evidence/2026-09-22/reevaluation-operations.md
?? docs/review_evidence/2026-09-22/swap-reeval-cap-wipe-probe.py
?? docs/review_evidence/2026-09-22/swap-reeval-db-loss-probe.py
?? vision.md
```

Their initial SHA-256 values were:

```text
1956f9d4451c1fe3bc73a0f5ce709333ee1f8ab5e984db981195ad941667cc3f  docs/RECOVERY_INPUT_CAP_ACCEPTANCE.md
04b4f8b171ee5ee8cfc7c0282c4e1d534c3d21a84ddef89707d6fc5964339646  docs/POST_CHANGE_REVIEW_2026-09-22_PRE_REEVALUATION.md
9f3740d9229e9ca22aabfb72b2386cd2ae9b9b6fcb610247f4f785a151be05b2  docs/review_evidence/2026-09-22/REEVALUATION.md
d6b41133704c738bb21cfa7f0df4cd5f8197cda176301fa8b605bca5f6551965  docs/review_evidence/2026-09-22/reevaluation-financial.md
7fd3343e61706fd92db424ccab6c617912199187eec4f961c6a627867a7e7116  docs/review_evidence/2026-09-22/reevaluation-operations.md
559dccaa089d9079e8cb586fdb116f9eed1089a313bc6fa43d823c3e6b5b4a87  docs/review_evidence/2026-09-22/swap-reeval-cap-wipe-probe.py
84712e6af0b0b4d9bfcdb813dbf0ce402cace84a4d867c78a94f2bf58b2cb136  docs/review_evidence/2026-09-22/swap-reeval-db-loss-probe.py
05e26f6078aff83b23718615b1c7d4a4e5f4f157852008fdc639ce959764b349  vision.md
```

They were read where relevant and not edited. The initial real index had no cached diff.

## Positive controls verified

### Empty-custody startup latch

- `src/state_db.py:304-316` creates a singleton `recovery_admission_holds` table.
- `src/state_db.py:1739-1777` validates exact positive waterlines, takes `BEGIN IMMEDIATE`, preserves an
  existing latch and writes the hold only when every recognized source lifecycle/deposit-hold table is empty.
- `src/startup_recovery.py:677-699` invokes the latch after waterline validation and before both chain
  reconstruction scans and reference seeding.
- `src/main.py:296-317` starts no service poller when recovery is incomplete or returns an error.
- There is no normal latch-clear path; initialization and later source insertion do not clear it.
- Tests cover below-minimum/nonpositive policy holds, refund/quarantine capacity holds, repeated restart,
  latch write/read failure, online backup and copied DB+WAL restore.

Verdict: **accepted as narrow total-empty-database containment**. It does not reconstruct principal or
prove any nonempty database complete.

### Dashboard latch visibility

- `src/dashboard.py:138-183` reads the latch through SQLite `mode=ro`, sanitizes SQLite errors, rejects
  malformed reason/waterlines and treats missing schema as unknown.
- `src/dashboard.py:186-256` suppresses backing ratio, fees and rolling payout use while the latch is held or
  unreadable; open obligations become unknown in the renderer while row counts remain labelled local.
- `src/dashboard.py:258-355` adds one actionable recovery issue.
- The shipped renderer no longer chains `Element.append()` when adding a table header, so nonempty issues
  render instead of raising.
- `tests/test_dashboard_recovery_admission.py` executes the actual JavaScript through Node for held,
  unknown and not-held cases.

Verdict: **accepted for the empty-custody latch only**.

### Retained A/B/C controls

The reviewed commits do not weaken strict disposition provenance migration, exact integer input policy,
intent-first capacity reservation, immutable held retry terms, exact terminal evidence, full-principal
liability or individually-impossible-cap non-starvation. Existing focused and full suites passed at the
exact source.

## Findings

### F-1 — High: one retained source row bypasses restore containment

Source: `src/state_db.py:1755-1771`.

The latch uses `any(SELECT 1 ...)` over source/terminal/deposit-hold tables. It neither proves database
completeness nor binds rows to a coherent restore/deployment generation. A stale/partial restore, a
database already populated by old replay, or a synthetic unrelated source avoids the latch.

Fresh probe sequence:

1. Create a 1,100-unit incoming source under max 1,000/refund fee 10/cap 50.
2. Run the real deposit and refund workers; retain a 1,090-unit refund capacity hold and 1,100 liability.
3. Delete DB/WAL/SHM, initialize a partial database and retain one unrelated processed source.
4. Change max to 2,000 and cap to 10,000.
5. Run the real startup recovery with complete mocked chain enumeration.
6. Commit the source through the real deposit-page transaction and run the real deposit worker.

Observed:

```json
{
  "startup_recovery_complete": true,
  "startup_recovery_error": null,
  "admission_latch_rows": 0,
  "source_re_admitted": 1,
  "deposit_worker": [1, 0, 0, 0],
  "nexus_send_calls": 1,
  "nexus_send_amount": 1100
}
```

Only the Nexus transport boundary was mocked. This proves changed authorization after partial restore, not
a live debit, duplicate or realized loss.

Containment: require independently verified coherent DB+WAL/online-backup evidence; otherwise remain paused.

Exit: complete restore/deployment identity or quantified source-specific non-sendable recovery holds. See
Batch 1 in `docs/plans/2026-09-25-recovery-admission-and-capacity-fairness.md`.

### F-2 — High operability: startup refusal is not general dashboard admission

Sources: `src/startup_recovery.py:607-675`, `src/dashboard.py:165-188`.

The latch is reached only after terminal-provenance and heartbeat/waterline checks, and records only the
empty-custody reason. An empty latch table maps to `not_held`. The renderer treats `not_held` as resolved.

Fresh probe retained a healthy snapshot and made heartbeat lookup return an empty object:

```json
{
  "startup_recovery_complete": false,
  "startup_recovery_error": "heartbeat_missing",
  "dashboard_admission": {"status": "not_held", "liabilities_complete": null},
  "dashboard_ratio_bps": 20000,
  "dashboard_issue_count": 0
}
```

`main.run()` still refuses, so no side-effect bypass was shown. The dashboard can mislead an operator about
the service's actual startup admission.

Exit: durable startup-owned pending/held/complete state, written across every recovery phase; healthy values
only after complete, read with metrics/counts from one SQLite snapshot.

### F-3 — High operability: malformed oldest capacity evidence starves valid work

Sources: `src/state_db.py:4191-4266`, `src/state_db.py:4823-4836`,
`src/solana_client.py:1265-1427`.

The worker loader returns typed `malformed_evidence`, preserving principal and preventing send. The global
oldest-hold query nevertheless includes the malformed row when evaluating a younger hold.

Fresh probe after capacity release:

```json
{
  "workers_after_release": [0, 0],
  "send_calls": 0,
  "liability_units": 120,
  "holds": [
    ["old-malformed", "rolling Solana payout cap exhausted", 2],
    ["young-valid", "waiting behind older Solana payout capacity hold", 3]
  ]
}
```

Safe refusal is intact. The valid younger original intent cannot progress.

Exit: durable operator-action scheduler classification outside automatic eligible FIFO, preserving raw
frozen evidence and liability. See Batch 3 in the plan.

### F-4 — Medium: dashboard summary creates a missing SQLite database

Sources: `src/dashboard.py:186-229`, `src/state_db.py:6292-6306` and the payout-budget helper called by
summary.

`_recovery_admission_status()` uses a read-only URI. `get_metrics_snapshot()` and other state helpers then
open ordinary SQLite connections. The probe called `api_summary()` against a nonexistent temporary path:

```json
{
  "database_created_by_summary_read": true,
  "admission": {"status": "unknown", "liabilities_complete": false}
}
```

No hold was cleared and no startup authority was granted. The behavior still contradicts the dashboard's
read-only boundary and permits cross-connection snapshot races.

Exit: one read-only connection/transaction per endpoint, with no file creation and one admission/metrics/
counts snapshot.

### F-5 — Existing deployment and operator blockers unchanged

- `src/main.py:370-379`: heartbeat asset validation failure/exception is alert-only after recovery.
- Custom Solana/Nexus endpoint authoritative network, health/sync and tip freshness admission remains open.
- Provider-v2 remains unwired and its legacy opt-in is inert.
- Non-capacity Solana holds have no audited evidence-bound resolution protocol.
- Optional receipts and provider-v2 remain separate disabled/unaccepted capabilities.
- No live provider/finality/pagination/TLS/Nexus completeness/reference/crash-after-acceptance/operator
  rehearsal ran.

## Executed commands and results

All shared-tree commands ran from `/home/brutus/github/swapService` with scratch under
`/home/brutus/.hermes/profiles/principal-dev/cache/scratch`.

### Exact-source full local gate

A detached disposable Git worktree at the exact source excluded every pre-existing dirty/untracked path:

```bash
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q src *.py tests
.venv/bin/python scripts/check_markdown_links.py
.venv/bin/python scripts/check_token_pair_inventory.py
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/test_recovery_safety.py
.venv/bin/python -m pytest -q tests/test_recovery_safety.py tests/test_solana_sdk_boundary.py
.venv/bin/python -m pytest -q \
  tests/test_swap_receipts.py tests/test_payout_review_regressions.py \
  tests/test_nexus_fee_lifecycle.py tests/test_solana_sdk_boundary.py
```

```text
pip check: No broken requirements found.                            exit=0
compileall: no output.                                               exit=0
Markdown: Local Markdown links: OK                                  exit=0
inventory: current (274 active lines)                               exit=0
full suite: 592 passed, 77 subtests passed in 74.88s                exit=0
recovery: 35 passed, 52 subtests passed                             exit=0
recovery + SDK: 36 passed, 52 subtests passed                       exit=0
receipt/payout/Nexus-fee/SDK: 85 passed                             exit=0
```

Raw log: `docs/review_evidence/2026-09-25/exact-source-gate.log`.

### Focused shared-tree tests

```bash
.venv/bin/python -m pytest -q \
  tests/test_empty_database_recovery.py \
  tests/test_dashboard_recovery_admission.py \
  tests/test_solana_capacity_holds.py
# 58 passed in 7.62s

.venv/bin/python -m pytest -q \
  tests/test_recovery_acceptance.py \
  tests/test_recovery_terminal_admission.py \
  tests/test_recovery_safety.py \
  tests/test_solana_deposit_policy.py \
  tests/test_solana_capacity_holds.py \
  tests/test_empty_database_recovery.py \
  tests/test_dashboard_recovery_admission.py
# 191 passed, 52 subtests passed in 17.95s
```

The shared dirty-tree full suite produced `591 passed, 77 subtests passed, 1 failed`. The sole failure is
`test_markdown_link_checker_accepts_tracked_documentation`: pre-existing untracked `vision.md` links to
four strategy files outside the repository, and the checker rejects escaping links. This is a dirty-scope
documentation failure, not an exact-source runtime failure. Raw log:
`docs/review_evidence/2026-09-25/verification-baseline.log`.

### Diagnostic probes

```bash
TMPDIR=/home/brutus/.hermes/profiles/principal-dev/cache/scratch \
.venv/bin/python -c 'import runpy,socket; import dotenv; \
dotenv.load_dotenv=lambda *a,**k:False; runpy.run_path("tests/conftest.py"); \
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(RuntimeError("network forbidden")); \
runpy.run_path("docs/review_evidence/2026-09-25/review-probes.py",run_name="__main__")'
```

Exit `0`; all four assertions reproduced the findings. Source/output:

```text
docs/review_evidence/2026-09-25/review-probes.py
docs/review_evidence/2026-09-25/review-probes.log
```

### Whitespace

```bash
git diff --check HEAD~1 HEAD   # exit 0
git diff --check               # exit 0 before final documentation merge
```

## Reviewed source hashes

Complete manifest: `docs/review_evidence/2026-09-25/reviewed-source.sha256`.

Key SHA-256 values:

```text
278600e8ff5ad55e8a0ea027225469b84f4d019471f1728027ea5ced70239603  src/dashboard.py
f3c737dfdba5c26194e4cbac53030262d8ad8698eae7df1dd1e4448b58330729  src/startup_recovery.py
bf5ad0dc236b5efe46ba2e1fcaf263e8924106978820e66cd22dfa03640ab87b  src/state_db.py
3f109caa95e1a005ead47cd797a97633b192548bcf6a2055c981c9fd7429e2bf  src/main.py
a607bb0594be61c0a9e2429f178df8c8711e1b17572dcad9d2ba32baabed84f3  src/solana_client.py
06be1f09d63be6db6751b31bd50b02b5df236cf8d9ed56ba013657bced462684  tests/test_dashboard_recovery_admission.py
04d158a6ae307920a705ebda82e641d3c09b0fb4a49a981e74488defcd394d90  tests/test_empty_database_recovery.py
a0afaa85d20a80222f05bff5c61bc4d353163f22f00a4e43219921b9b984bbb3  tests/test_solana_capacity_holds.py
```

Evidence SHA-256 captured before the final documentation merge:

```text
d06a1e82035a80797efecbeb66c8687cd458d998a1b08bd0266ddcdd47258963  review-probes.py
5f199e4cf97dcf095d6bbdd41aa4c639cd27130756a5ba73c68b4bba52661dd2  review-probes.log
2456a49df72b27b944eab28e3ee804620f980080c4d2315c0970c408a49d7b38  exact-source-gate.log
efb81047dfa4596f9813b94bb4a7b44a62ad71667001b87c158a8420b13f68a7  verification-baseline.log
```

## Documentation candidate status

A disposable Git index based on source HEAD staged exactly these review-authored publishable paths:

```text
docs/DEVELOPMENT_REVIEW_2026-09-25.md
docs/EVALUATION.md
docs/STATE_MACHINES.md
docs/plans/2026-09-23-financial-recovery-follow-up.md
docs/plans/2026-09-25-recovery-admission-and-capacity-fairness.md
docs/review_evidence/2026-09-25/findings.md
docs/review_evidence/2026-09-25/review-probes.py
docs/review_evidence/2026-09-25/reviewed-source.sha256
```

The first complete candidate produced index tree
`7e456e62a0ab67192eebb27ff9732b4c1f0b4c82`; staged whitespace, local Markdown links, token-literal
inventory and compilation passed, and the full suite returned **592 passed, 77 subtests passed in 77.03s**.
Because this findings section and the dated review were then finalized, the same disposable-index gate is
rerun after those edits; its final tree and output are recorded in the local verification ledger named
below rather than embedded here (embedding the candidate's own tree would change that tree).

The raw `.log` files in this evidence directory are ignored by repository policy and were therefore local
verification evidence, not staged candidate paths. `final-doc-candidate-gate.log` records the candidate
command output. No runtime/test path was staged in the disposable candidate or the real index.
