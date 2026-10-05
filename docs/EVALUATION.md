# swapService — Current Engineering Evaluation and Remediation Plan

## Current verdict — 2026-10-02

**Release blocked.** Reviewed range and candidate identity:

```text
base:        ed73c513ee22f9626502273aa0d8e42a4c238b7a
source HEAD: ee10b6e20dfe85f15347386adecb9dc99db55bb5
index tree:  a73785b8653e3ad03c9216072b7366999ba1e854
```

Four published startup containments now hold retained ordinary dispositions, ready rows with prior debit
metadata, ready rows with capacity siblings, and ready rows with terminal siblings. They preserve full
principal and conflicting evidence rather than deriving current terms, repeating a Nexus debit or deleting
a disputed source. The focused changed-area gate passed 324 tests; keep all four controls.

The staged sealed-custody candidate adds an independent one-use witness, exact whole-SQLite-image and
schema admission, configuration/source fingerprints, pinned chain genesis checks, recovery-before-running,
per-cycle lease verification, quiescent sealing and a read-only snapshot dashboard. The complete offline
suite passed 947 tests plus 77 subtests. This materially closes the previous missing-database dashboard and
non-durable-startup-truth gaps **only for an exact independently approved image**.

It is not release-acceptable yet:

1. `build_fingerprint()` omits the executed root `swapService.py`; a scratch mutation that performed a
   pre-admission side effect retained the approved fingerprint. Installed interpreter/package artifacts are
   also not attested by hashing `requirements.txt`.
2. Genesis equality is not node health/sync/freshness. Heartbeat validation accepts a name-resolved object
   with a different address, owner, provider, pair and vault when the three required fields parse.
3. Required witness/genesis configuration and audited initial/restore certificate generation are not
   integrated into the normal setup/configuration path.
4. Malformed capacity evidence can still starve later eligible frozen work; non-capacity Solana holds still
   lack audited resolution; target-chain acceptance remains absent.

See the [October 2 review](DEVELOPMENT_REVIEW_2026-10-02.md),
[sealed-custody architecture note](maintenance/sealed-custody-admission.md), and the
[current repair plan](plans/2026-09-25-recovery-admission-and-capacity-fairness.md).

### Maintenance increment — root-entrypoint fingerprint drift

**Implemented locally, unpublished; Batch 0 remains blocked.** The in-process
`build_fingerprint()` now includes the required root `swapService.py` alongside the
existing runtime sources and declared requirements. A collected regression first
reproduced an unchanged digest after adding a pre-admission side effect; it now
requires a changed digest without executing the inspected entrypoint. Additional
fixtures reject changed, missing and non-file entrypoints before witness-permit
consumption, preserve the custody image/receipt absence, and exercise exact-build
claim, completion and next-generation sealing.

The focused admission module returned **29 passed**; the clean-environment shared-tree
suite returned **952 passed, 77 subtests passed**. Dependency consistency, compilation,
Markdown links, the existing index's literal inventory and whitespace checks passed.
These results cover the local candidate, not a published commit or production artifact.
The fix depends on the pre-existing staged/uncommitted custody implementation, so it
is intentionally not committed or pushed independently of that feature. The pre-existing
index is unchanged. No live chain or real-fund operation was performed.

This fixes only accidental root-entrypoint drift in the in-process certificate. It
cannot prevent an altered wrapper from executing before the checker; the external
trusted launcher/immutable image, interpreter and installed-artifact attestation in
Batch 0 remain required. Previously approved build digests must not be reused or
silently rewritten for the changed manifest.

### Maintenance increment — running-interpreter fingerprint drift (2026-10-03)

**Implemented in the local candidate; Batch 0 and publication remain blocked.** Local
commit `ebedff796bd201d0c0b690074922cfde21a7a883` already contains the root-entrypoint
repair above and the sealed-custody implementation. This increment addresses the next
missing byte identity: `build_fingerprint()` now incorporates a domain-separated SHA-256
of the running Linux interpreter obtained from `/proc/self/exe`, not `PATH`,
`sys.executable`, a version label or an installed-package declaration. Missing, unreadable,
empty, non-regular or changing executable evidence refuses fingerprint construction.
Streaming reads compare descriptor/path identity, byte size and nanosecond modification
and change times before and after hashing; candidate interpreter bytes are never executed.

Collected coverage in `tests/test_custody_interpreter.py` first reproduced unchanged build
identity after interpreter-byte drift. It now covers drift, invalid evidence, in-place
mutation/replacement/truncation during hashing, sanitized read failure, real running-binary
selection despite spoofed labels, unchanged custody bytes/no receipt/no permit consumption
on rejection, and exact-build claim, completion and next-generation sealing.
The focused five-module custody gate returned **67 passed**; the clean Python 3.12 complete
suite returned **967 passed, 77 subtests passed**. All boundaries remain offline.

`origin/main` is `dba5f358bbe82e09acfbcb3582ae1f8d5d2abeda`, based on `7b2d1c4`, and does
not contain `src/custody_admission.py`. The local prerequisite commits `ee10b6e` and
`ebedff7` are absent from that branch. Publishing this increment would therefore also
publish the larger custody feature or require resolving divergent documentation; neither
is authorized as this narrow maintenance issue. No force push or implicit feature
publication is allowed by this increment.

This is **in-process drift containment only**, not interpreter trust or pre-execution
attestation. Shared libraries, standard library/bytecode, installed dependencies and the
external trusted launcher/immutable image remain Batch 0 exits. Protected filesystem
writing is still required; metadata checks are not an immutable-image guarantee. The
new fingerprint intentionally invalidates prior approvals, including unchanged-interpreter
approvals under the previous manifest. Do not silently rewrite or reuse certificates.
Production and real funds remain blocked.

### Maintenance increment — foundational native-runtime drift (2026-10-04)

**Implemented locally; Batch 0 and publication remain blocked.** The first unresolved
priority remains artifact identity. Root-entrypoint and interpreter-byte containments
in local `ebedff7` and `89fddc7` are preserved, not reimplemented. This increment binds
the on-disk files backing conventional executable `libpython`, C/math-library and
Linux-loader mappings in `/proc/self/maps` into a domain-separated build digest.
Paths are checked against the observed device/inode before streaming their bytes;
descriptor/path size, mode and nanosecond timestamps are rechecked, as is the selected
mapping set. Empty, missing, malformed, unreadable, deleted, anonymous or inconsistent
native evidence refuses fingerprint construction with a sanitized error. Reads are
bounded by the observed size; a raced-in FIFO cannot block the file open.

The initial regression reproduced unchanged build identity after native-file drift.
Race regressions then reproduced acceptance of in-place mutation, replacement,
truncation and changed mappings; all now refuse. A fresh service/dashboard subprocess
regression caught a separate-process identity mismatch when every native extension was
included. The final manifest is deliberately limited to foundational runtime libraries:
normal extension import differences, ASLR, mapping order and duplicate segments do not
change it. Conventional glibc/musl and `ld`/`ld64` loader names are collected; unsupported
names are not attested. Rejection preserves the original ready witness permit, custody
bytes and receipt absence. Exact-build fixtures claim, complete, report healthy and seal
a next generation. Independent review found no blocking defect in this narrow scope.

The six-module focused gate returned **108 passed**, including **41 native-artifact
cases**. The clean Python 3.12 complete suite returned **1008 passed, 77 subtests passed**
in 85.40 seconds. Dependency consistency, compilation, local Markdown links, the
intended index's token-literal inventory (**274 active lines**) and candidate whitespace
passed. The three required CI-isolation shards returned **35 passed/52 subtests**,
**36 passed/52 subtests** and **85 passed**. All chain boundaries remain offline;
no live send, production credential or real-fund operation was used.

This is **in-process on-disk drift containment**, not pre-execution or mapped-memory
attestation. Other shared libraries/extensions, standard library/bytecode, installed
packages, alternate filenames and the external trusted launcher/immutable image remain
Batch 0 exits. The manifest change invalidates existing approvals; never silently rewrite
certificates. `origin/main` remains `dba5f358bbe82e09acfbcb3582ae1f8d5d2abeda`, lacking the
local custody prerequisite and diverging from local `main`. A passing narrow local commit
cannot authorize publishing that larger feature or reconciling remote documentation.
No push or remote CI claim is made. Production and real funds remain blocked.

### Maintenance increment — installed solders extension drift (2026-10-04)

**Implemented locally; Batch 0 and publication remain blocked.** The first unresolved
priority is complete executable-artifact identity. Existing root-entrypoint, interpreter
and foundational-library repairs are preserved. This narrow increment adds a
domain-separated fingerprint of the installed `solders.solders` native extension used
for transaction construction. `PathFinder` locates the conventional extension without
executing its package initializer or library. Exact extension-loader/origin agreement,
absolute path and recognized interpreter suffix are required. Nonblocking streaming
reads are bounded by the observed size, with descriptor/path identity, mode, size and
nanosecond timestamp checks plus repeated extension discovery. Missing, empty,
non-regular, unreadable, inconsistent or changing evidence refuses admission with a
sanitized error before witness-permit consumption.

The initial collected regression reproduced unchanged build identity after extension
byte drift; it now requires a changed digest without execution. The **26-case** new
module covers discovery without package side effects, invalid evidence, mutation,
replacement, truncation, growth, discovery changes, stat/open races and FIFO refusal.
Real offline witness fixtures preserve the ready permit, full custody image and receipt
absence on rejection, then admit the original artifact, complete a healthy lease and
seal the next generation. Separate service/dashboard processes retain equal build
identity. The seven-module focused gate returned **134 passed**; the clean Python 3.12
complete suite returned **1034 passed, 77 subtests passed** in 84.97 seconds. Independent
review found no blocking defect in this narrow scope. Final static and isolation gates
are recorded with the maintenance commit report. No live-chain or real-fund operation
was performed.

This is **in-process on-disk extension drift containment**, not trusted pre-execution
attestation, loaded-memory identity or complete installed-package verification. Python
wrappers, bytecode, other SDK/dependency artifacts, standard library and the external
trusted launcher/immutable image remain Batch 0 exits. A changed fingerprint invalidates
prior approvals; never silently rewrite or reuse certificates. Fresh fetch resolves
`origin/main` to `a28c958`, diverging from local `main` and still lacking the custody
prerequisite. Publishing this repair would also publish that larger feature and require
remote-documentation reconciliation, outside this one-issue scope. No force push or
remote CI claim is authorized. Production and real funds remain blocked.

### Maintenance increment — installed solders Python-source drift (2026-10-05)

**Implemented locally; Batch 0 and publication remain blocked.** The first unresolved
priority remains complete executable-artifact identity. Existing root-entrypoint,
interpreter, foundational-library and native solders-extension repairs are preserved.
This increment binds the installed `solders/__init__.py` and the seven wrappers directly
imported by the runtime: `hash.py`, `instruction.py`, `keypair.py`, `message.py`,
`pubkey.py`, `signature.py` and `transaction.py`. A domain-separated source manifest uses
`PathFinder` without executing inspected packages/modules, requires conventional absolute
source paths and exact `SourceFileLoader`/origin agreement, streams bounded nonblocking
reads, and rechecks descriptor/path metadata, the complete discovery result and earlier
files before accepting the digest. Missing, empty, non-regular, unreadable, bytecode-only,
conflicting or changing selected evidence refuses admission before permit consumption.

All eight initial drift regressions reproduced unchanged build identity before the fix.
The new module also covers discovery without execution, invalid files/loaders/paths,
stat/open races, mutation/replacement/truncation/growth, changes to previously read files,
and changed discovery. Offline witness fixtures preserve the ready permit, full custody
image and receipt absence on rejection, then admit the exact approved build, complete a
healthy lease and seal the next generation. Existing fresh service/dashboard subprocess
identity coverage remains green. The focused eight-module custody suite returned
**233 passed**. Independent review found no blocker in this narrow scope. Complete-suite
and static/isolation gate results are recorded in the maintenance commit report.

This is **in-process on-disk source drift containment**, not proof of executed bytecode,
loaded modules, complete installed dependencies or trusted pre-execution admission.
Other wrappers/SDK artifacts (including indirect solders imports), standard library,
bytecode and the external trusted launcher/immutable image remain Batch 0 exits.
The changed manifest invalidates prior approvals; never silently rewrite certificates.
Fresh fetch still resolves `origin/main` to `a28c958`, lacking the custody prerequisite
and diverging from local `main`. A narrow local commit cannot authorize publishing that
larger feature or reconciling unrelated remote documentation. No push or remote CI claim
is made. Production and real funds remain blocked; no live-chain operation was used.

### Maintenance increment — eager indirect solders source drift (2026-10-05)

**Implemented locally; Batch 0 and publication remain blocked.** The first unresolved
priority is executable-artifact identity. This increment preserves the previous repairs
and adds the eighteen mandatory top-level Python wrappers eagerly imported by the pinned
`solders==0.26.0` initializer. The source manifest now binds the initializer plus all
25 mandatory flat wrappers, including `account`, `system_program`, `sysvar` and
`transaction_status`, even when service/dashboard import sets differ. Existing
non-executing source discovery, exact loader/origin validation, bounded nonblocking
reads and complete discovery/metadata rechecks apply without changing admission ordering.

All eighteen new drift regressions first reproduced unchanged build identity; the
expanded manifest now changes the digest without executing inspected source. An independent
expected inventory and AST inspection of the installed initializer check the scope.
Expanded invalid-file, loader, race and offline witness tests cover the added wrappers:
rejection preserves the ready permit, full custody bytes and absence of receipts/sidecars;
restoring the exact artifact permits claim, healthy completion and next-generation seal.
The focused eight-module gate returned **445 passed**. Independent review found no
blocking defect in this narrow scope. The clean Python 3.12 complete suite returned
**1330 passed, 77 subtests passed** in 101.37 seconds, with no skips. All three
CI-isolation shards passed (**35/36/85 tests**, with **52 subtests** in each recovery
shard). Dependency consistency, compilation, Markdown links, intended-index literal
inventory (**274 active lines**) and candidate whitespace checks passed.

This is **in-process on-disk drift containment**, not proof of executed bytecode or
trusted pre-execution attestation. Nested `solders.token`/`solders.rpc` packages,
optional `litesvm`/`transaction_metadata`, other installed artifacts, standard library,
bytecode and the external trusted launcher/immutable image remain Batch 0 exits.
The changed manifest invalidates prior approvals; never silently rewrite certificates.
Fresh fetch resolves `origin/main` to `a28c958`, still lacking the local custody
prerequisite and diverging from local `main`. Publishing this increment would implicitly
publish that larger feature and reconcile unrelated documentation, outside this repair.
No push or remote CI claim is made. Production and real funds remain blocked;
all chain boundaries were offline.

### Maintenance increment — eager solders token-initializer drift (2026-10-06)

**Implemented locally; Batch 0 and publication remain blocked.** The first unresolved
priority remains executable-artifact identity. Existing repairs bind the solders root
initializer and mandatory flat wrappers but omit the mandatory `solders.token` package
initializer that the pinned root initializer eagerly imports. This narrow increment
adds `token/__init__.py` to the deterministic on-disk source manifest. Non-executing
`PathFinder` discovery requires the conventional source initializer, exact source-loader
origin/path agreement and exactly its own package search directory. Existing bounded
nonblocking reads and whole-manifest discovery/metadata rechecks apply to the new file.

A collected regression first reproduced an unchanged build digest after inserting a
side effect into the token initializer. It now requires changed identity without
executing inspected source. Expanded tests cover missing/empty/non-file/unreadable
sources, invalid loaders/origins/package paths, flat-module substitution, mutation,
replacement, truncation, growth and discovery changes during reads, plus stat/open
races. Offline witness fixtures preserve the ready permit, full custody bytes and
receipt/sidecar absence on rejection, then claim, complete, report healthy and seal
with the exact restored artifact. The focused eight-module custody gate returned
**459 passed**, including equal fresh service/dashboard build identities. Full-suite,
static and CI-isolation gate results are recorded in the maintenance commit report.

This remains **in-process on-disk drift containment**, not executed-bytecode identity,
complete dependency verification or independently trusted pre-execution attestation.
Other nested token/RPC modules, optional SDK artifacts, other dependencies, standard
library/bytecode and the external trusted launcher/immutable image remain Batch 0 exits.
The changed manifest invalidates prior approvals; never silently rewrite certificates.
Fresh fetch still resolves `origin/main` to `a28c958`, lacking the local custody
prerequisite and diverging from local `main`. Publishing this increment would implicitly
publish that larger feature and require unrelated documentation reconciliation, outside
this repair. No push or remote CI claim is made. Production and real funds remain blocked;
all exercised chain boundaries were offline.

### Current acceptance register

| Area | Status | Next executable exit |
|---|---|---|
| Four retained-source conflict containments | **Accepted narrowly offline** | Preserve in every later batch; zero transport/full liability regressions remain collected |
| Sealed image/witness continuity | **Implemented, partial** | Externally attest every executed byte and dependency artifact; rehearse independent deployment and restore |
| Dashboard startup truth/read-only snapshot | **Accepted for staged offline scope** | Keep missing-path/no-write and witness-transition tests; target deployment still required |
| Heartbeat/service and chain admission | **Blocked** | Exact owner/address/schema/pair/terms plus Solana health/root freshness and Nexus sync/tip freshness before mutation |
| Capacity eligible progress | **Blocked** | Move malformed/conflicting rows outside automatic FIFO without authorizing them |
| Hold resolution/live acceptance | **Blocked** | Evidence-bound operator workflow and explicit devnet/testnet matrix |

The sections below retain prior implementation detail and dated evidence. Their September 30 status labels
are historical where they conflict with this register; they do not supersede the October 2 verdict.

## Previous verdict — 2026-09-30

**Release blocked.** Reviewed range:

```text
base:        ed73c513ee22f9626502273aa0d8e42a4c238b7a
source:      1b267f2b708e484ec27ce53d0c85db4592d148c2
source tree: 7eaad283abb2255b312f6e6c1dabbc9582056d03
```

Two commits since the review base add conservative startup containment. `17a9f17` holds every retained
ordinary refund/quarantine state before its worker can derive a first disposition from current terms.
`1b267f2` holds a ready row when any debit transaction ID, reference or frozen output remains, even if
its input policy is valid. The first reproduced current-term Solana sends; the second reproduced a second
mocked Nexus debit that overwrote retained debit identity. Real startup and actual-worker regressions now
require zero transport, full liability and preserved evidence for both classes. Keep both controls.

They do **not** establish coherent restore admission. Status-specific containment is not a complete
deployment/restore identity or all-status evidence audit. General startup refusal can still look healthy
on the dashboard, malformed capacity evidence can still starve valid work, and dashboard summary can
create a missing database. See the [September 30 review](DEVELOPMENT_REVIEW_2026-09-30.md) and the
[current repair plan](plans/2026-09-25-recovery-admission-and-capacity-fairness.md).

## Implemented containment — empty-database startup visibility

**Implemented, reporting-only containment; R-1 remains open.** The read-only dashboard
now exposes the durable empty-database admission latch in `/api/summary`, `/api/issues`
and a prominent recovery banner. It reports total liabilities and open obligations as
unknown rather than treating absent local rows as zero. While admission is held or its
evidence is unreadable/malformed, backing ratio, fee totals and rolling payout usage are
unavailable; a retained metrics snapshot cannot make the page look recovered. Local row
counts remain explicitly local, and the backing-deficit banner cannot incorrectly claim
refunds/quarantine continue during recovery refusal.

The reader does not clear holds or write recovery state. Missing admission tables produce
an explicit unknown status, not a healthy result; raw database errors are not published by
the admission reader. An empty latch table means only `not_held`, with no claim of complete
liabilities or successful recovery. Operators must restore and independently verify coherent
custody evidence, never seed rows, clear holds or send funds manually to bypass admission.
Collected tests in `tests/test_dashboard_recovery_admission.py` cover the real startup latch,
retained healthy metrics, repeated reads/reinitialization, missing/malformed evidence, sanitized
read failure, and execution of the shipped JavaScript renderer (Node.js required for that shard).
Browser verification also exposed a pre-existing chained `Element.append()` call that broke
nonempty issue tables; the renderer now appends the header separately so recovery issues display.

This does not reconstruct principal, quantify lost liabilities, validate partial/stale restores,
or implement source-specific authorization/resolution. Those R-1/R-3 release gates remain open.

## R-1 maintenance containment — empty-database startup

**Implemented, narrow containment; R-1 is not closed.** Startup now records a durable
`recovery_admission_holds` latch when validated nonzero custody checkpoints meet a database
with no source lifecycle rows on either chain and no durable Solana deposit holds. It refuses
reconstruction before either chain scanner or reference seeding can make that database look
recovered. The normal `main.run()` gate reports `empty_custody_database_recovery_held` and
does not start pollers. Reinitialization, newer checkpoints and later source insertion do not
clear the latch; database read/write failures also refuse admission.

This intentionally blocks an empty new deployment too: there is no safe automatic bootstrap
exception and no latch-clearing command. Restore and independently verify a coherent custody
backup; do not seed dummy rows, alter checkpoints or delete the hold to force startup. Presence
of some retained rows is **not** proof of complete history or backup validity. Partial/stale
restores, databases already populated by an older unsafe replay, source-specific reconciliation,
quantified recovery visibility and an audited bootstrap/resolution protocol remain open under
R-1/R-3. No principal is reconstructed by this containment path, so an empty dashboard after
refusal must not be interpreted as zero liabilities or safe backing.

Collected coverage in `tests/test_empty_database_recovery.py` exercises the real startup caller
after DB/WAL loss of below-minimum, nonpositive-output, refund-cap and quarantine-cap holds;
durable refusal across restarts; persistence failures; and online-backup/DB+WAL restoration of
original disposition terms with exactly one mocked submission. Existing scanner tests retain
source history to exercise reconstruction past this new admission gate. All chain boundaries
are offline; this is not live-chain acceptance or independent release approval.

## Independent re-evaluation — 2026-09-28

The three newer repairs close the exact source-admission paths they claim:

1. unseen Solana inputs at or before the startup boundary become quantified historical-authorization
   holds through both core and Helius page committers;
2. retained ready rows with no policy become non-sendable before reconstruction; and
3. retained ready rows with partial, malformed, source-conflicting or nonpayable policy receive the same
   hold without rewriting their raw evidence, principal, reservations or capacity evidence.

The focused two-module shard returned **49 passed**. The three direct actual-worker families returned
**23 passed** across both providers, four lost-policy dispositions, three timestamp classes and twelve
invalid-policy forms. They exercised the real startup, deposit, refund and quarantine workers with only
external transport and provider boundaries replaced.

Fresh residual probes found that a retained row already labelled `to be refunded` or
`to be quarantined`, with both policy fields absent, is not audited because startup selects only
`ready for processing`. Recovery returned complete and each real disposition worker made one mocked
1,090-unit send from 1,100 units using the current 10-unit refund fee and current destination. This is
the same missing historical-authorization class in a different lifecycle state, not a failure of the
three narrower accepted controls.

The previously reported malformed-oldest capacity starvation, stale healthy dashboard after heartbeat
failure, and database creation by a missing-path summary read also reproduced unchanged. No live chain,
production credential or real send was used. See the [September 28 review](DEVELOPMENT_REVIEW_2026-09-28.md);
[September 25](DEVELOPMENT_REVIEW_2026-09-25.md) remains the pre-repair baseline.

## Independent re-evaluation — 2026-09-30

The published `17a9f17` ordinary-disposition fix closes the September 28 mocked-send reproductions:
startup changes retained ordinary refund, quarantine and legacy failed-quarantine rows to the existing
historical-authorization hold before either disposition worker can select them. Policy validity,
timestamp, worker limit and retained capacity/terminal siblings do not exempt an inconsistent source
status. Raw evidence, reservations and full principal remain unchanged.

Review then found a sibling lifecycle conflict at `17a9f17`: a retained ready row with valid payable
policy plus any previous debit-submission field remained selectable. In an isolated copy of that exact
source, the new regression module returned **41 failures**; the transaction-ID-only case called the mocked
Nexus debit a second time for 1,090 units and replaced its prior identity. `1b267f2` adds one atomic SQL
containment before policy validation: any non-NULL debit transaction ID, reference or frozen output holds
the row. Blank, zero, negative and malformed values also hold because none proves non-submission.

The two real-worker modules now return **159 passed**. They cover ordinary dispositions, valid/invalid
policy, every debit field alone and combined, absent/expired/active reservations, repeated startup, both
page committers, rollback, timestamps outside scan ranges, worker-limit progress and unchanged in-flight
states. This accepts both commits only as narrow containment. Coherent restore identity, complete per-state
evidence schemas, Nexus-side recovery and audited hold resolution remain open.

An isolated candidate containing the maintained architecture, plan and review update returned
**775 passed, 77 subtests passed**. Dependency consistency, byte compilation, local Markdown links,
the index-aware token-pair inventory (274 active lines), candidate/range whitespace and all three CI
isolation shards passed. Local execution does not establish target-chain semantics or release approval.

Fresh offline probes also reproduced malformed-oldest capacity starvation, stale healthy dashboard data
after heartbeat-missing startup refusal, and creation of a missing SQLite file by a dashboard summary read.
The current heartbeat validator still alerts and continues rather than forming a fail-closed chain/provider
identity gate. No live chain, production credential or real send was used.

## Architecture and verified progress

One process bridges one configured classic SPL token ↔ Nexus token pair. `config.SWAP_PAIR`
contains token/custody identity, independent decimals and fee terms. Gross conversion is 1:1 in
whole-token units before fees/rounding, not market pricing. Native SOL, Token-2022, arbitrary
chains and simultaneous pairs remain outside scope. Helius is a trusted primary provider; a
second attestor is not required. Exact amounts, success/finality and complete enumeration remain
application responsibilities.

| Area | Current implementation and acceptance boundary |
|---|---|
| A — disposition recovery, E-015/E-018 | Strict provenance parsing, conservative legacy-terminal migration, full-principal evidence holds, inferred-fee reversal and preserved proven cap spend. Atomic DDL/data rollback, in-place upgrade, online-backup and copied DB+WAL tests pass. This does not reconstruct an unsent B/C authorization that was lost with SQLite. |
| B — Solana input policy | Shared strict-integer minimum/maximum/decimal/fee classifier runs before destination routing. Below-minimum/nonpositive-output holds retain principal without fees. Frozen decisions survive restart **when the database survives**. |
| C — typed capacity holds | Durable typed outcomes, exact capacity diagnostics, liability/alert visibility, original-term retry and eligible FIFO are implemented for valid retained evidence. An individually impossible payout does not starve fitting work. A malformed oldest hold is fail-closed but can globally block a later valid fitting hold; see R-1b. Frozen retry requires retained intent evidence. |
| Ingestion/finality | Positive principal is durably retained; query/provider continuation is bound; unsupported/finality evidence is held; liability totals use one SQLite snapshot. Public waterlines pin behind unresolved sources. Source rediscovery alone does not reconstruct historical authorization. |
| Nexus identity/reconciliation, E-001–E-004/E-014 | Composite `(txid, contract_id)` identity, exact payout evidence, integer math and fail-closed backing controls remain. Automatic Nexus compensating transfers remain disabled; a narrow explicit operator protocol exists. |
| Optional receipts, E-016 | Durable outbox and independent positive-evidence/budget controls remain; production enablement is separately blocked pending cost/schema/target-chain acceptance. |
| Provider-v2 | Builder/validator and tests are committed, but registration, heartbeat, recovery and inspection still use v1. This is library-only implementation, not a default-v2 runtime migration. |

See [state machines](STATE_MACHINES.md) and the published
[historical A/B/C acceptance record](RECOVERY_INPUT_CAP_ACCEPTANCE.md). That tracked record documents
the original tested scope; the total-loss and scheduler qualifications in this evaluation are newer.

## September 30 findings retained for traceability

The October 2 acceptance register supersedes statuses in this section. R-1c and R-1d are implemented in
the staged offline witness/dashboard candidate but remain integration-gated by artifact and witness
operations. R-2 is no longer alert-and-continue, yet exact heartbeat identity and node freshness remain
blocked. R-1b, R-3 and live/provider gates remain open.

### R-1 — Historical partial/stale restore analysis

The empty-database latch and three new Solana controls are useful containment, not a coherent-restore
protocol. Table non-emptiness still permits startup without proving that policy, capacity, fee, cap and
terminal evidence belong to one complete generation. The previous source-only-ready replay to a current-
term Nexus debit is now blocked by `record_solana_recovery_boundary()` and the page-commit boundary.

The reviewed residual path was a retained **non-ready** source. That startup audited only
`status = 'ready for processing'`. A partial restore can retain `to be refunded` or
`to be quarantined` while losing both policy fields and any frozen capacity intent. Fresh offline probes
at the reviewed source made recovery report complete and then exercised each real disposition worker.
Each called the mocked Solana send boundary for 1,090 units from a 1,100-unit source using the current
10-unit fee and current resolved destination. The full principal remained a local liability pending
confirmation, so this proves replacement of historical authorization and one externally attempted send,
not realized loss or duplicate settlement.

Relevant code at the reviewed source was `state_db.py:1805-1853`, auditing only ready rows, and
`solana_client.py:1267-1602`, where new non-capacity disposition work derives fee and destination from
current configuration when no frozen capacity intent exists.

**Containment:** keep the empty-DB latch and all three new holds. Do not treat any nonempty/partial database
as verified; resume an existing deployment only from independently validated coherent DB+WAL/online-
backup evidence, otherwise remain paused. Do not manually run refund or quarantine workers over restored
rows whose frozen policy/capacity intent is absent or inconsistent.

**Implemented maintenance containment — unseen pre-startup Solana inputs:** startup now persists
an additive, monotonic `solana_recovery_boundary` before chain reconstruction. Both deposit page
committers retain a previously unseen source at or before that boundary as
`historical_solana_authorization_missing` rather than `ready for processing`. The full observed
principal, source and custody/query provenance remain in `solana_deposit_holds`; liabilities and
checkpoint pinning include those rows. Historical holds cannot be automatically promoted, including
through repeated pages or direct hold replay, and are excluded before the automatic replay limit.
The dashboard issues endpoint exposes them with exact integer principal and a no-manual-send warning.
Retained lifecycle/finality/parser rows are not reclassified by this unseen-source containment; the
additional source-only ready-row containment below applies at startup.

This is **not R-1 closure**: the boundary is the greater of the startup local timestamp, Solana
checkpoint and previously retained boundary, not an independent restore manifest or authoritative
chain-clock certificate. Inputs received while offline can conservatively require permanent holds
until an audited resolution protocol exists. Pre-fix replay rows, partial restores containing an
incomplete lifecycle component, unseen sources outside enumeration, Nexus-side reconstruction and
clock/identity assurance remain open. Do not infer complete liabilities from startup success or use
post-boundary timestamps as proof of a coherent restore. Empty-database startup still refuses replay.

Collected coverage in `tests/test_partial_restore_deposit_recovery.py` exercises stale backups retaining
one unrelated processed source after lost below-minimum/nonpositive/refund/quarantine decisions, both
page committers, changed terms, multiple pages, cutoff equality, restart/duplicate replay, full liability,
zero mocked sends, retained hold eligibility, query rollback, upgrade and persistence failures.

**Implemented maintenance containment — retained source-only ready rows:** in the same startup
transaction as the replay boundary, every retained `ready for processing` Solana row with both
`policy_decision` and `policy_evidence` absent becomes `historical_solana_authorization_missing`.
No timestamp or worker-limit exemption applies. Source fields, full principal, submission metadata,
reservations and capacity evidence are not rewritten or released. Existing liability/checkpoint logic
continues to include these non-sendable rows; the dashboard exposes their hold without suggesting
that retained capacity evidence authorizes retry. Reinitialization and duplicate pages cannot promote them.

The regression first reproduced a current-term Nexus debit from a retained source-only row. Collected
coverage in `tests/test_retained_source_recovery.py` now verifies zero transport for that case, sources
older than scan ranges and newer than the local clock, repeated startup, both page committers, more
held rows than the worker limit, preserved capacity evidence, and atomic rollback/refusal on a failed
hold write. Positive controls retain the original frozen policy through terms drift and admit genuinely
new post-startup sources. Existing online-backup/DB+WAL capacity-intent tests remain applicable.

**Implemented maintenance containment — invalid retained ready-row policy:** startup now validates
every retained ready row's frozen policy with the strict policy parser and exact source comparison.
Partial fields, malformed evidence, mismatched source/decision, and an internally valid nonpayable
decision on a ready row become the same non-sendable historical-authorization hold. No current
configuration repairs the stored evidence. The boundary and all status changes commit atomically;
raw evidence, principal, reservations, submission metadata and capacity evidence remain untouched.
These holds are visible in the existing dashboard issue queue and cannot monopolize the ready-worker
limit. Valid matching payable policy continues under its original terms after configuration drift.

Collected regressions in `tests/test_retained_source_recovery.py` cover partial fields, corrupt JSON,
all frozen source fields, decision/output conflicts and nonpayable-ready state, repeated startup and
both replay providers, zero transport/fees for affected sources, full liability, capacity/reservation
preservation, failed-write rollback and younger valid work behind more held rows than the worker limit.

This remains **narrow R-1 containment**, not closure. A crash after source admission but before the
first policy freeze now conservatively requires an audited resolution that does not yet exist.
Pre-fix rows already classified under replacement terms, apparently valid policy alongside missing
lifecycle components, remaining non-ready states and Nexus-side recovery still require the broader admission protocol.
Do not clear or manually retry these holds; production remains blocked.

**Implemented maintenance containment — retained ordinary dispositions:** the same startup transaction
now moves every retained `to be refunded`, `to be quarantined` and `quarantine failed` source to
`historical_solana_authorization_missing`, independent of timestamp, worker limit or policy validity.
Those worker paths create a first disposition using current fee/destination terms; even a valid input
policy does not authorize that replacement. A retained capacity/terminal sibling cannot exempt an
inconsistent ordinary source status. No policy, principal, reservation, submission metadata or capacity
evidence is overwritten. Existing dashboard warnings, liability accounting and checkpoint pinning apply.

Collected real-startup/worker regressions in `tests/test_retained_source_recovery.py` first reproduced
the unauthorized mocked sends. They cover all three ordinary states, missing/partial/corrupt/conflicting
and valid input policies, terms drift (including a fee consuming all principal), restart, both replay
providers, rollback/refusal on persistence failure, retained reservations/capacity evidence, and more
held rows than the worker limit ahead of a younger valid original-term capacity retry. Existing online-
backup and DB+WAL restore tests still verify exact frozen capacity-intent sends.

This is deliberately conservative: legitimate work interrupted before disposition freeze is also held,
with no manual bypass or audited release command. Frozen-capacity and in-flight/finality protocols are
unchanged, not newly certified. Valid ready policies can still coexist with missing lifecycle components;
all-status restore identity/completeness, Nexus-side recovery and operator resolution remain open.
**R-1 and production release remain blocked.**

**Implemented maintenance containment — ready rows with retained debit metadata (2026-09-30):**
startup now also holds every retained ready row with a non-NULL `txid`, `reference` or
`amount_usdd_units`, including blank, zero and malformed values. Valid payable input policy is not
proof that an earlier debit was never submitted. A partial restore retaining only a prior transaction
ID alongside a ready status previously reached a second mocked Nexus debit and overwrote that ID.
The new status-only update commits atomically with the replay boundary and existing startup audits;
raw policy/submission evidence, full principal and active/expired reservations remain unchanged.
No current terms repair the row and no manual retry or hold-clear command is introduced.

`tests/test_retained_ready_submission_recovery.py` covers isolated/combined metadata, absent/expired/
active reservations, both replay providers, repeated startup, dashboard visibility, rollback/refusal,
source timestamps outside scan ranges and beyond the local clock, and more held rows than the worker
limit ahead of one valid original-term debit. Existing in-flight states are explicitly unchanged.
This closes only an inconsistent-ready-state path: valid ready rows with other missing/conflicting
lifecycle components, capacity/finality states, Nexus-side restore admission and coherent-restore
identity remain unaudited by this slice. **R-1 and production release remain blocked.**

**Exit:** bind admission to a complete restore/deployment identity, or audit every retained and
rediscovered nonterminal lifecycle state before any worker can select it. A row without exact historical
policy, disposition and submission evidence must become a quantified, visible, non-sendable recovery
hold; current configuration cannot fill missing fields. Test stale/partial/pre-fix restores containing only
one lifecycle component, every ready/refund/quarantine/in-flight/capacity status, both disposition workers,
terms drift, multi-page replay and worker limits. Require zero transport, full liability and no inferred fee
absent exact historical authorization.

### R-1b — High operability: malformed oldest capacity evidence blocks later valid retries

The retry protocol correctly refuses malformed frozen evidence and retains all principal. However,
its global FIFO query still treats that unresolved row as the oldest eligible hold. A fresh real-worker
probe created two ordinary refund capacity holds, corrupted only the older hold's frozen JSON, released
the blocking budget, and ran the worker twice. Both runs returned zero; the valid younger 50-unit
refund remained `refund capacity held`, its attempt count increased from 1 to 3 with reason
`waiting behind older Solana payout capacity hold`, zero sends occurred, and the full 120-unit
liability remained. This is safe containment, but not progress or the claimed eligible-FIFO behavior.
The probe is session scratch only; its SHA-256 and exact output are recorded in the September 23 review.

Relevant code is `state_db.py:4158-4214,4770-4830` and
`solana_client.py:1289-1409`: loading the oldest hold returns `malformed_evidence`, while the later
valid prepare still selects that malformed source in the global oldest-hold query. The existing
suite covers malformed refusal, but not a younger fitting obligation behind it.

**Exit:** keep malformed/source-conflict/unknown-submission evidence non-sendable, but remove it from
automatic eligible FIFO after atomically promoting it to a distinct operator-action queue, or define
another durable scheduler disposition that cannot authorize transport. Add real refund and quarantine
worker tests with a malformed/conflicting oldest row, more rows than the worker limit, restart, alert
deduplication and later reviewed resolution. Require the younger valid original intent to submit
exactly once without deleting or reducing the blocked row's liability.

### R-1c — Superseded offline: staged witness makes startup refusal externally durable

The staged candidate replaces absence-of-latch readiness with external `ready/claimed/running/held`
evidence plus a local live-process receipt. Recovery, session and heartbeat failures occur before
`running`; failed/ambiguous claimed generations remain non-running or permanently held. The dashboard
suppresses retained healthy values unless the same exact running lease brackets its snapshot. Focused
runtime/dashboard tests cover failure boundaries and witness changes. Keep this integration-gated until
artifact identity and independent witness deployment are accepted.

### R-1d — Superseded offline: staged dashboard is read-only and snapshot-consistent

The staged dashboard uses one `mode=ro` SQLite transaction for summary counts, metrics and payout exposure,
then rechecks the witness lease. Missing-path tests prove no DB creation; concurrent-write tests prove one
snapshot. Preserve these tests against the final artifact and deployment.

### R-2 — Partially superseded: fatal validation remains identity/freshness-incomplete

Heartbeat false/exception now returns before lease completion and workers, and both chain genesis values
are pinned before database migration. However the heartbeat validator still accepts an object with a
mismatched address, owner, provider, pair and vault when its name resolves and three fields parse.
Genesis-only checks do not require Solana health/root freshness or Nexus sync/mode/network/tip freshness.
Session/heartbeat validation also follows database migration and recovery scans.

**Exit:** bind exact service-record address, owner, schema, pair/custody identity and terms, and require
authoritative chain health/sync/freshness before mutable startup. Test mismatch, unavailable evidence,
stale/unsynced nodes, wrong types and validator exceptions with zero database/scanner/poller/chain-write
calls; validate semantics on intended nodes.

### R-3 — High operability gate: non-capacity Solana holds lack audited resolution

Capacity-only holds can retry their retained original intent. Policy, recovery-evidence,
malformed-evidence, source/lifecycle-conflict and unknown-submission holds have no corresponding
audited Solana resolution command. Dashboard visibility is not a disposition protocol.
`nexus_transfer_operator.py` handles only an exact Nexus refund-hold family; actor strings do
not enforce distinct human approval roles.

**Do not follow** the direct-chain-transfer/manual-DB advice in `quarantine_viewer.py:409-418`.
It bypasses intent/cap/evidence protocols. The source text is flagged for repair, not changed in
this documentation-only review.

**Exit:** implement an evidence-bound operator protocol, or explicitly approve permanent retention
as policy. Require actor/rationale, competing-state checks, authoritative exact readback,
capacity accounting, atomic terminalization, replay/crash tests and no blind retries. If two-person
approval is required, enforce distinct identities rather than merely recording labels.

### R-4 — Publication gate: verify the documentation candidate and published SHA

[CI run 36316328344](https://github.com/distordialabs-brutus/swapService/actions/runs/36316328344)
completed successfully for exact source SHA `6769f7a1bb68dd2a975f4b39aa910f2405d38d42`.
Local exact-source dependency, compile, Markdown, inventory, full-suite, isolation and whitespace gates
also pass. This establishes the source baseline only; the documentation changes described here were not
part of that run.

**Exit:** stage only the explicitly reviewed documentation paths, run the index-aware inventory and
candidate whitespace/link/full-suite gates against that exact index, commit, push, read back the remote
branch SHA, and verify CI for that SHA. Preserve the unrelated dirty/untracked September 22 files and
untracked vision context; do not publish them implicitly.

### R-5 — Provider-v2 cutover remains a separate, blocked migration

`src/service_record.py` has no production importer. `ALLOW_LEGACY_PROVIDER_V1` is parsed but
inert; runtime v1 is not disabled by its false default. Immutable asset address, expected owner,
service ID and Nexus network settings do not currently protect registration/recovery callers.
The reviewer and parent measured the v2 fixture with `last_poll=0` at 1,448 bytes using the repository's
`service_record_size()` estimator, above its declared 1,024-byte budget. This is **not** proof of
the target node's exact encoded size limit; it blocks assuming the proposed record fits.

**Exit:** choose and verify a target-valid storage layout before any NXS-spending create, then
wire address-selected create/read/update, startup, heartbeat and recovery to one identity policy.
Enforce explicit legacy opt-in, exact owner/type/schema/service/pair/custody, monotonic terms,
secret-safe publication and agreement between published economics and executable policy. Test
multiple assets, name/address disagreement, readback delay, oversize rejection and migration on
the intended Nexus build. Provider-v2 is not a prerequisite to repairing R-1 in the existing
single-pair bridge; committing the library does not make it integrated.

## Fresh verification at the reviewed runtime — 2026-09-28

The exact tracked source ran in a detached disposable Git worktree, excluding every pre-existing dirty or
untracked documentation path. External boundaries were mocked or blocked; no real transaction was made.

| Executed gate | Result |
|---|---|
| Exact-source complete suite | **641 passed, 77 subtests passed in 81.69s** |
| New partial/retained-source modules | **49 passed in 6.47s** |
| Three direct actual-worker regression families | **23 passed in 3.68s** |
| Recovery standalone | **35 passed, 52 subtests passed in 2.59s** |
| Recovery plus installed SDK | **36 passed, 52 subtests passed in 2.97s** |
| Receipt/payout/Nexus-fee/SDK shard | **85 passed in 6.23s** |
| Dependency consistency, exact compilation and Markdown links | Passed |
| Token-literal inventory | Passed; **274 active lines** |
| Range and last-source-commit whitespace | Passed |
| Residual actual-worker probe | Refund and quarantine each reached one mocked 1,090-unit send without frozen policy |
| Three unchanged-risk probes | Reproduced capacity starvation, stale dashboard readiness and missing-path DB creation |

Remote `main` resolved to the reviewed source, and exact-source CI run 36316328344 is green. That CI does
not cover the documentation candidate. The untracked vision was read as context and not linked as a
published source; all pre-existing dirty/untracked paths retained their original hashes. Exact commands,
outputs and source hashes are retained in the complete local findings artifact for this review.

## Development and release sequence

1. **Close artifact identity first:** externally bind `swapService.py`, every runtime module, interpreter
   and installed dependency artifact before any repository code can execute or claim a witness permit.
2. Preserve all four published restored-source containments and the staged exact-image/witness state
   machine; independently approve image financial coherence rather than equating a matching hash with it.
3. Bind the exact heartbeat owner/address/schema/pair/custody/terms and require Solana health/root freshness
   plus Nexus sync/mode/network/tip freshness before mutable startup.
4. Operationalize independently administered witness bootstrap/restore, then move invalid capacity evidence
   outside eligible FIFO without making it sendable and add evidence-bound Solana hold resolution.
5. Keep provider-v2 and receipts disabled/unclaimed as runtime capabilities until their separate
   migration/cost gates pass. Preserve compatibility; no dependency upgrade is part of this review.
6. On explicitly approved Solana devnet/Nexus test infrastructure, run both directions, mixed decimals,
   provider pagination/concurrent arrivals, finality, exact readback, Nexus references,
   accepted-but-unparsed/timeout outcomes, durable-boundary crashes, backup/WAL and total-loss recovery.
   Rehearse witness loss/ambiguity, alerts, holds, incident response, key rotation and TLS/session controls.
7. Re-review the final immutable artifact, run the complete configured gate, verify exact-head CI, then make
   a separate release decision. Production and real funds remain blocked; no live acceptance was performed.

The executable current plan is the
[September 25 recovery admission and capacity-fairness plan](plans/2026-09-25-recovery-admission-and-capacity-fairness.md).
The [October 2 review](DEVELOPMENT_REVIEW_2026-10-02.md) is the current dated evidence. The
[September 23 follow-up](plans/2026-09-23-financial-recovery-follow-up.md) and original
[A/B/C implementation plan](plans/recovery-input-cap-repairs.md) remain historical implementation records.
