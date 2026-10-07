# swapService development review — 2026-09-25

## Verdict

**Release blocked; empty-database containment is verified, partial-restore admission and capacity progress
remain open.**

```text
review base:  184f5d6a45ecd8f53ae37cfdd09e4b63092d1842
source HEAD:  17f65a3e3b45281162c1604cd0a695a36dc55991
source tree:  6e39b479f2e9379b57554568b237a59cbe175a42
index tree:   6e39b479f2e9379b57554568b237a59cbe175a42
```

The range contains `809d45c fix(recovery): latch empty custody database startup` and
`17f65a3 fix(dashboard): expose unresolved custody recovery admission`. This review changed documentation
and evidence only. It did not edit runtime/tests/dependencies, stage, commit, push, access production
credentials, call a live chain or move funds.

## What the changes establish

### Empty-custody latch — accepted as narrow containment

Startup now validates positive custody waterlines, then transactionally records a singleton hold when all
recognized source lifecycle/deposit-hold tables are empty. It returns before either chain reconstruction or
reference seeding. Reinitialization and later source insertion do not clear the hold, and `main.run()` starts
no poller while recovery is incomplete. Coherent online-backup and DB+WAL restores preserve frozen
refund/quarantine intent and avoid the latch.

This closes the exact total-DB/WAL-loss path reproduced on September 23. It does not validate partial/stale
restores, databases populated by the old unsafe replay or new-deployment bootstrap.

### Dashboard latch admission — accepted as narrow visibility

A valid held latch or unreadable/malformed latch evidence produces a prominent recovery banner and issue,
marks total obligations/backing/fees/rolling payout use unknown, labels row counts local and avoids the
false “refunds continue” claim. The actual shipped JavaScript renderer is exercised under Node. The
nonempty issue-table `append()` bug found during the change is repaired.

This is visibility for one latch, not a complete startup admission ledger.

## Remaining findings in order

### 1. High financial authorization — partial/stale restore bypasses the latch

The exemption is `any()` retained row across recognized source tables. A fresh real-caller probe retained
one unrelated processed source while losing a 1,100-unit source frozen as a 1,090-unit refund under a
50-unit cap. Startup returned complete with no latch. Replay re-admitted the source after the maximum
changed, and the real deposit worker called the mocked Nexus debit boundary for 1,100 units.

No live loss or duplicate payment was demonstrated. The proven defect is replacement of missing historical
authorization by current policy after a partial restore.

**Exit:** prove a coherent restore/deployment identity or retain every rediscovered source without exact
historical authorization as a quantified non-sendable recovery hold. Cover stale/partial/pre-fix restores,
every policy/disposition kind, terms drift, pagination, worker limits and restart.

### 2. High operability — general startup refusal can look healthy on the dashboard

A fresh probe retained a healthy metrics snapshot and made heartbeat lookup return missing. Startup returned
`recovery_complete=False` with `heartbeat_missing`, but `/api/summary` reported
`recovery_admission.status=not_held`, ratio 20,000 bps and `/api/issues` reported zero issues. Main still
refused pollers; the gap is operator truth, not a side-effect bypass.

**Exit:** persist startup-owned pending/held/complete admission across every recovery phase and expose
healthy metrics only with a valid complete record from the same SQLite snapshot.

### 3. High operability — malformed oldest capacity evidence still starves valid work

After capacity was released, two real refund-worker passes encountered an older malformed hold and a
younger valid fitting hold. They sent zero, retained 120 units of liability and left the younger hold waiting
behind the older row with attempt count 3. Safe refusal remains intact; eligible FIFO progress does not.

**Exit:** atomically move malformed/conflicting/unknown-submission rows into a durable non-sendable
operator-action scheduler class outside automatic FIFO. Prove same/cross-kind progress beyond worker limits,
restart, concurrency and later audited resolution.

### 4. Medium hardening — dashboard summary is not filesystem read-only

Against a missing configured path, `api_summary()` created a SQLite file. `_ro_conn()` itself is read-only,
but summary calls ordinary state helpers afterward. No authorization was granted.

**Exit:** use one read-only connection/transaction or dedicated read-only state API; create no DB/WAL/SHM
file and do not mix admission from one snapshot with metrics/counts from another.

### 5. Existing separate blockers remain

Registration/network/sync admission is incomplete and heartbeat validation remains alert-only after
recovery. Non-capacity Solana holds lack an audited evidence-bound resolution protocol. Provider-v2 remains
unwired and optional receipts remain disabled pending target acceptance. No live provider/finality/
pagination/TLS/Nexus completeness/crash-after-acceptance/operator rehearsal ran.

## Verification

Exact tracked source was run in a detached disposable Git worktree:

```text
pip check                                      passed
compileall src/*.py/tests                      passed
local Markdown links                           passed
literal inventory                              passed (274 active lines)
pytest -q                                      592 passed, 77 subtests passed in 74.88s
recovery standalone                            35 passed, 52 subtests passed
recovery + installed SDK                       36 passed, 52 subtests passed
receipt/payout/Nexus-fee/SDK shard             85 passed
```

Focused shared-tree execution:

```text
empty DB + dashboard admission + capacity      58 passed in 7.62s
recovery/policy/cap/latch/dashboard             191 passed, 52 subtests passed in 17.95s
review probes                                   exit 0; all four findings reproduced
source commit whitespace                        passed
first disposable docs candidate                 592 passed, 77 subtests passed in 77.03s
candidate whitespace/links/inventory/compile    passed
```

The shared dirty worktree's complete suite reported `591 passed, 77 subtests passed, 1 failed`: the
pre-existing untracked `vision.md` links to strategy files outside the repository and the local link checker
rejects links that escape the repository. It was read as context and left unchanged. The exact tracked
source gate is green. This distinction is not live or publication acceptance.

Raw logs, probe source/output, reviewed hashes, exact commands, source references and scope details are in
[the complete findings artifact](review_evidence/2026-09-25/findings.md).

## Ordered repair

1. Partial/stale/pre-fix restore admission and source-specific no-authorization holds.
2. Durable complete startup admission for dashboard/operator truth.
3. Operator-action classification outside automatic capacity FIFO.
4. Genuinely read-only, snapshot-consistent dashboard APIs.
5. Registration/network admission and audited Solana hold resolution.
6. Authorized target-infrastructure acceptance, final independent review, exact-candidate gate and a
   separate release decision.

Executable acceptance criteria are in
[the 2026-09-25 development plan](plans/2026-09-25-recovery-admission-and-capacity-fairness.md).
