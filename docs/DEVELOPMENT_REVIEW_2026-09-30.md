# swapService development review — 2026-09-30

## Verdict

**Release blocked. The two changes since `ed73c51` are accepted as narrow offline recovery
containment; they do not establish coherent restore admission or live-chain readiness.**

```text
review base:  ed73c513ee22f9626502273aa0d8e42a4c238b7a
source HEAD:  1b267f2b708e484ec27ce53d0c85db4592d148c2
source tree:  7eaad283abb2255b312f6e6c1dabbc9582056d03
remote main:  1b267f2b708e484ec27ce53d0c85db4592d148c2
```

Reviewed commits:

```text
17a9f17 fix(recovery): hold retained ordinary disposition rows
1b267f2 fix(recovery): hold ready rows with retained debit metadata
```

This review changed documentation only. It did not edit runtime/tests/dependencies, commit, push,
access production credentials, call a live chain or move funds. The dirty acceptance record and
untracked September 22 evidence/vision files were preserved and excluded from the candidate gate.

## Accepted changes

### Retained ordinary dispositions — accepted as conservative containment

At startup, `record_solana_recovery_boundary()` now changes every retained ordinary refund,
quarantine and legacy failed-quarantine source to `historical_solana_authorization_missing` in the
same transaction as the replay boundary. The audit runs before reconstruction and is independent of
source timestamp, worker limits and policy validity. It changes only status; principal, raw policy,
submission metadata, reservations and capacity evidence remain intact.

This closes the September 28 path in which a partial restore retained a disposition status but lost
its frozen intent, allowing the actual refund/quarantine worker to derive current fee and destination
and call mocked Solana transport. Valid frozen capacity retries and in-flight/finality states remain
on their existing protocols; they are not certified by this change.

### Ready rows with retained debit metadata — accepted as conservative containment

The first fix exposed a sibling inconsistent lifecycle: a ready row with exact payable policy but a
retained debit transaction ID, reference or frozen output remained selectable. Missing or expired
reservation did not prove that the previous remote call had not occurred. The deposit worker could
reserve again, allocate a new reference and overwrite retained identity.

`1b267f2` adds an atomic startup update before ready-policy validation. Any non-NULL debit-submission
field holds the row, including blank, zero, negative and malformed values. SQL `NULL` is the only
absence representation accepted at this boundary. Raw evidence and absent/expired/active reservations
are preserved; in-flight states are not rewritten.

An isolated copy of exact source `17a9f17` plus the new regression module returned **41 failures**.
The transaction-ID-only case called the mocked Nexus debit again for 1,090 units with reference `1`.
At current HEAD, the two real-worker recovery modules return **159 passed**, with zero mocked transport
for held rows, full liability, dashboard visibility, atomic rollback on failed writes, repeated-startup
stability, replay non-promotion and progress for younger valid work.

## Severity-ordered findings

### High — restore admission remains a set of status patches, not a complete protocol

A nonempty database still does not prove that source, policy, disposition, capacity, reservation,
submission, fee and terminal evidence belong to one coherent deployment generation. Current startup
contains the reproduced unseen-source, invalid-ready-policy, ordinary-disposition and ready-with-debit-
metadata classes, but does not define and validate the required evidence tuple for every in-flight,
unknown, awaiting-confirmation, capacity-held, terminal and competing lifecycle state.

**Coding contract:** one startup-owned `BEGIN IMMEDIATE` audit must enumerate every selectable status
and its exact evidence schema before either chain rebuild or worker selection. Missing, malformed or
contradictory evidence becomes a quantified non-sendable hold without deleting or repairing fields from
current configuration. Any audit-write failure rolls back the boundary and every status change and calls
no scanner, reference lookup or transport. A verified restore/bootstrap identity must bind deployment,
database generation, both custody/query identities and complete lifecycle/cap/fee evidence; table
non-emptiness, schema existence, timestamps and chain rediscovery are not proof.

### High operability — malformed oldest capacity evidence still starves valid work

A fresh actual refund-worker probe again placed a malformed older capacity hold before a valid younger
hold, released the cap blocker and ran the worker twice. Both runs returned zero, no transport occurred,
full 120-unit liability remained, and the younger row's reason stayed `waiting behind older Solana payout
capacity hold`. Refusal is safe; global eligible-FIFO progress is false.

**Coding contract:** transactionally move malformed, source-conflicting and unknown-submission rows to a
durable operator-action scheduler class outside automatic eligible FIFO while preserving source,
principal, raw evidence, cap usage and diagnostics. Prove same-kind and cross-kind younger valid work
submits exactly once across restart and beyond worker limits. No reclassification may authorize transport.

### High operability — startup refusal can still look healthy

A fresh heartbeat-missing probe returned `recovery_complete=False`, but dashboard summary returned
`recovery_admission.status=not_held`, retained ratio `20000` bps and zero issues. `main.run()` remains
fail-closed at the recovery gate; the defect is stale operator authorization information.

**Coding contract:** persist startup `pending`, `held` and `complete` outcomes with sanitized reason,
waterlines and restore identity. Only a valid `complete` record may expose healthy metrics. Read admission,
metrics, liabilities and issue counts from one read-only snapshot; every startup failure/crash boundary
must produce held/unknown operator state and zero poller starts.

### High deployment safety — heartbeat/provider/network validation is still alert-only

Current `main.run()` alerts on an invalid heartbeat result or validator exception and then continues to
balances, reconciliation and pollers. Known Solana hostname checks do not prove a custom endpoint's
network/health/root freshness, and Nexus network/sync/tip freshness is not an enforced startup gate.
Provider-v2 remains library-only and its legacy opt-in flag does not govern the production callers.

**Coding contract:** before mutable startup, require authoritative Solana network/genesis, health and root
freshness plus Nexus network/genesis, synchronization and tip freshness, and validate the exact configured
provider identity. Missing, stale, wrong-network or malformed evidence and validator exceptions must return
false before database mutation or any poller/chain write. Repeat against explicitly approved target nodes.

### Medium — dashboard summary still creates a missing database

The fresh missing-path probe confirmed that `api_summary()` creates the configured SQLite file after its
initial read-only admission check because later helpers use writable connections.

**Coding contract:** route each dashboard response through one read-only URI connection and transaction.
Assert no database/WAL/SHM creation or byte change for every endpoint, sanitized unknown values for missing
schema, and snapshot consistency under a concurrent admission transition.

### Separate release gates

Non-capacity Solana holds still lack an evidence-bound audited resolution workflow; provider-v2 and optional
receipts remain disabled/unaccepted migrations; Nexus completeness/reference/TLS semantics and Solana
provider pagination/finality remain untested on intended infrastructure. No local fixture establishes
accepted-but-unparsed response, timeout-after-acceptance, crash, backup/restore, total-loss or operator
rehearsal behavior on target chains.

## Verification

All external boundaries in probes were mocked or blocked. Temporary SQLite databases were used.

| Command/shard | Result |
|---|---|
| Exact `17a9f17` + retained-debit regression module | **41 failed**; reproduced one second mocked Nexus debit and non-held conflicting rows |
| Current two retained-source modules | **159 passed in 36.73s** |
| Isolated documentation candidate | **775 passed, 77 subtests passed in 98.67s** |
| Shared-tree complete suite | **774 passed, 77 subtests passed; 1 failed** only because preserved untracked `vision.md` contains intentional links outside the repository |
| Recovery standalone | **35 passed, 52 subtests passed** |
| Recovery plus installed SDK | **36 passed, 52 subtests passed** |
| Receipt/payout/Nexus-fee/SDK shard | **85 passed** |
| Dependency consistency, byte compilation and local Markdown links | Passed |
| Index-aware token-pair inventory | Passed; **274 active lines** |
| Candidate/range whitespace | Passed |
| Fresh malformed-FIFO/dashboard probes | Reproduced all three documented defects without transport |

The isolated candidate was built from exact source HEAD with only the five intended review-document paths
added; every preserved dirty/untracked path was excluded. Its complete output is retained in the scratch
gate log supplied with this review. The shared-tree Markdown failure is not a candidate defect: `vision.md`
is untracked review context that this task was required to preserve, not a publication path. No live-chain
or release acceptance ran.

## Reviewed runtime/test hashes (SHA-256)

```text
51b2b172542ff6eb1a8f37c6cda5b6d36f27def7779823efa0b548a1f51fb29a  src/state_db.py
3a54a824a745384ced02e2c44d3f2c5486d5b87e7d5432b2da5c246760976b2b  src/startup_recovery.py
3fc1e381c423db6c8918b9cfe1f605b4da59a8056af1c345819b43b2ad26b332  src/solana_client.py
184dea5d83396a74667988e3b300b892152b1c23f5f385d6293282ed6f95c84e  src/solana_deposit_policy.py
3f109caa95e1a005ead47cd797a97633b192548bcf6a2055c981c9fd7429e2bf  src/main.py
6775499702d5285c3350e9244b116526e7b6e3921364184bab9c122971ce56ff  src/dashboard.py
d5bce9f81b402b3c42816b07a50e83bd2690a36d55d6d9285972680628fc03f6  tests/test_retained_source_recovery.py
d648414eef14e1f29a4a075c0e380a831861d937acf0d08b0c45068bd550c155  tests/test_retained_ready_submission_recovery.py
946a698efd3bf299230afe17fe9659747998db00a32832741df1006e73abc1a7  tests/test_recovery_safety.py
```

## Ordered repair and release gates

1. Keep both accepted containments and implement the closed per-state startup evidence contract.
2. Persist truthful startup admission and read all operator authorization data from one read-only snapshot.
3. Move invalid capacity rows outside automatic FIFO without reducing liabilities or authorizing transport.
4. Make provider/registration and both chains' identity/freshness fail closed before mutable startup.
5. Add audited Solana hold resolution; keep provider-v2 and receipts outside runtime until separate gates pass.
6. Run explicitly authorized target-infrastructure pagination, finality, exact-readback, unknown-outcome,
   crash/restore and operator-rehearsal matrices against one exact candidate, then make a separate release
   decision.

The maintained executable plan is
[the recovery admission and capacity-fairness plan](plans/2026-09-25-recovery-admission-and-capacity-fairness.md).
