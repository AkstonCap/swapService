# Recovery admission and capacity-fairness repair plan — 2026-09-25

## Governing vision and portfolio traceability

Read [the repository vision](../../vision.md) and [Distordia alignment/dependency map](../DISTORDIA_ALIGNMENT.md) before assigning work. Authority is master Distordia strategy/customer evidence → portfolio roadmap/strategy decisions → repository vision → this development plan → tasks/code/tests/external evidence and human release.

**Portfolio purpose:** primary O4 attributable settlement and bounded risk, supported by O3
reproducible provenance and O1 inspectable/open evidence contracts. O5 remains an external-
acceptance objective; service-record identity is an O2 prerequisite, not proof of namespace
authority. The current one-pair operator-custodial bridge remains transitional SD-002. This is a
non-Atlas settlement hypothesis, not marine Class A evidence; SD-003–SD-008 and non-custody,
slashing, regulatory and adoption claims remain open. The alignment map supplies ownership,
upstream prerequisites and human gates. Every batch below must carry these qualifications plus its
exact production paths and collected acceptance tests.

**Current assessment lineage:** exact source
`2c4ed319d251836f01dfb83de68da71b1c6c6a23`, matching `origin/main`, now publishes the sealed-
custody runtime and selected in-process artifact-drift increments. Fresh offline collection found
1,522 tests; all passed with 77 subtests. Ten custody modules passed 667 tests and ten recovery/
capacity modules passed 414 tests plus 52 subtests. These results accept the finite selected drift
and recovery containments only. They do not close trusted pre-execution artifact identity, complete
restore authority, service/node freshness, witness operations, capacity fairness, hold resolution
or live acceptance. Production and real funds remain blocked.

## Decision and scope

Historical October 2 identities: committed runtime/documentation-publication base `7b2d1c4e3c9d3b2f006a083f9372cfadf80830fc`; separately reviewed local documentation `HEAD` `ee10b6e20dfe85f15347386adecb9dc99db55bb5`; unpublished runtime index tree `a73785b8653e3ad03c9216072b7366999ba1e854`; compared with review base
`ed73c513ee22f9626502273aa0d8e42a4c238b7a`.

Keep all accepted containment controls:

- the durable empty-custody startup latch;
- the monotonic Solana recovery boundary and source-specific historical holds;
- strict holding of missing/invalid retained ready-row policy;
- unconditional holding of retained ordinary refund/quarantine states;
- holding of ready rows with any retained debit-submission field;
- holding of ready rows with any disposition-capacity sibling; and
- holding of ready rows with any processed/refunded/quarantined sibling.

Also preserve the current implementation's fail-closed witness state machine, claim-before-init ordering,
whole-file/schema verification, recovery-before-running, per-cycle lease check, worker drain, quiescent
seal and read-only snapshot dashboard while repairing the exits below.

Production and real funds remain blocked. This plan makes no dependency upgrade, provider-v2 cutover,
receipt enablement, live transaction, commit, or publication authorization.

## Current batch status

| Batch | Status at reviewed candidate | Evidence and remaining exit |
|---|---|---|
| 0 — executable artifact identity | **Blocked / P0; finite containment accepted offline** | Current in-process evidence binds all repository `src/*.py`, root `swapService.py`, requirements, the running Linux interpreter, conventional executable libpython/libc/libm/loader mappings, the installed `solders` extension, selected root/flat/token/RPC/optional wrappers and selected RPC request/response/error wrappers. Repository imports still execute first; other transitive Solana/HTTP/stdlib/bytecode/shared-library artifacts, mapped-memory identity and independent pre-execution attestation remain open. |
| 1 — restore/image admission | **Partial / P0** | Four retained-row conflict containments, the empty-DB latch, recovery boundary and exact-image witness are green offline. External approval must establish financial coherence; exact bytes cannot discover an incomplete/pre-fix approved image, and no all-status coherent-restore audit exists. |
| 2 — durable startup outcome | **Implemented narrowly offline** | Claimed/running/held witness plus local receipt suppress stale healthy dashboard values. Independent-process contention/crash coverage, artifact authority and deployment acceptance remain open. |
| 3 — eligible capacity FIFO | **Open; defect freshly reproduced** | An older malformed refund hold retained full principal and made zero sends, but two runs still blocked a younger valid fitting hold and increased its attempt count to 3. |
| 4 — read-only dashboard | **Accepted for current offline scope** | One read-only snapshot and missing-path no-create tests pass; retain witness-before/after consistency and target deployment acceptance. |
| 5 — service/chain admission | **Blocked; defects freshly reproduced** | A mismatched heartbeat owner/address/provider/pair/vault passed when three scalar fields parsed; genesis-only Solana/Nexus checks passed without health/sync/freshness. |
| 6 — witness operations and hold resolution | **Open** | Required evidence export, independently administered bootstrap/restore ceremony, real-process contention/crash rehearsal and evidence-bound non-capacity Solana disposition are absent. |
| 7 — target acceptance/release | **Open / O5 unvalidated** | No authorized live node, crash/restore or operator rehearsal evidence. |

## Repair order

### Batch 0 — bind the artifact before repository code executes

**Priority: P0 executable integrity.**

The current in-process fingerprint now includes the root entrypoint, repository sources, running
interpreter and a finite selected native/solders manifest. It still cannot prove the approved
executable: repository imports and root code run before the checker, while unselected transitive
artifacts, bytecode, standard-library/shared-library code and mapped memory remain outside the
manifest. Define an external trusted launcher or immutable deployment image whose independently
approved digest covers the complete execution closure before repository code runs. The witness
certificate must bind that authority; changing any pre-admission byte cannot produce a side effect
before refusal.

Acceptance:

- mutate each executable/imported source file and each installed dependency artifact in isolation;
- inject a pre-import side effect into `swapService.py` and require that it never executes;
- change interpreter/image identity and require refusal before witness claim, database open or chain I/O;
- boot an exact approved artifact, then complete, run and seal one generation; and
- prove the artifact verifier/launcher is outside the mutable artifact it attests.

### Batch 1 — prove a complete or conservatively held restore

**Priority: P0 financial authorization. Status: partial, not accepted as complete.**

#### Accepted Batch 1 containment increments

At the reviewed candidate, preserve these verified controls:

1. before chain rebuilding, atomically persist a monotonic Solana boundary;
2. route previously unseen sources at/before it through both page committers into quantified,
   non-promotable historical-authorization holds;
3. audit every retained `ready for processing` row independently of timestamp and worker limit;
4. retain only exact, matching, payable frozen policy as worker-eligible;
5. preserve raw evidence, principal, reservations and capacity rows when holding invalid policy;
6. hold every retained ordinary refund/quarantine state before it can derive current disposition terms;
7. hold a ready row when any debit transaction ID, reference or frozen debit output is non-NULL;
8. hold a ready row when any disposition-capacity sibling remains; and
9. hold a ready row when any processed/refunded/quarantined terminal sibling remains.

These reviewed controls close the source-only-ready path. Subsequent maintenance containment also
holds every retained `to be refunded`, `to be quarantined` and `quarantine failed` row, even with valid
input policy: those paths would otherwise create a first disposition from current fee/destination terms.
It preserves all raw evidence and liabilities. Frozen-capacity and in-flight/unknown-submission protocols
remain unchanged, not certified by this slice. Legitimate interrupted ordinary work is held too; do not
clear its status or send manually. See the [current evaluation](../EVALUATION.md).

Additional maintenance containment holds ready rows with any retained debit transaction ID,
reference or frozen debit amount, even with valid payable policy. Missing/expired reservations
cannot prove non-submission; raw evidence and principal are preserved. Collected real-worker
regressions reproduce the prior duplicate-debit attempt and cover rollback, repeated startup,
worker limits and unchanged valid/in-flight work. This is not a complete lifecycle audit.

#### Required per-state evidence contract

Implement the next slice as a closed table, not another isolated status exception:

| State family | Required evidence before automatic selection | Failure disposition |
|---|---|---|
| Ready/new debit | Exact payable policy matching source identity and principal; no retained debit-submission field | Quantified historical-authorization hold; preserve every field and reservation |
| Ordinary refund/quarantine | Proven same-run admission transition, or a separately frozen full disposition intent | Startup hold; never reconstruct fee, output or destination from current configuration |
| Capacity-held refund/quarantine | Exact source/kind/destination/output/fee/memo plus valid budget event and no lifecycle conflict | Durable non-sendable operator-action state outside automatic eligible FIFO |
| Debit/disposition in flight or outcome unknown | Durable immutable intent and remote identity/reference sufficient for authoritative resolution | Resolution-only hold; never return to new-work selection or release capacity from bounded absence |
| Awaiting confirmation | Exact submitted identity and complete expected transfer/debit evidence | Confirm only from exact successful finality evidence; otherwise retain liability |
| Terminal/competing lifecycle | Exact source-scoped terminal proof, fee and cap effects with no sibling conflict | Preserve both sides and refuse startup/selection pending audited resolution |

The audit must run in the same startup transaction as the recovery boundary, before scanners or workers.
Any invalid type—including blank identifiers, zero/negative numbers where positive values are required,
booleans or malformed legacy storage—fails closed. A failed hold write rolls back the boundary and every
status change. Held rows cannot consume worker limits, while coherent positive controls must submit the
original frozen intent exactly once. Add a schema-driven parameterized test so every selectable status and
every persisted evidence column appears in at least one positive and one negative case.

#### Next Batch 1 implementation slice

Create one startup-owned transactional audit over **every nonterminal Solana source status** before any
worker can be selected. Define the required evidence tuple per status: source identity/principal, frozen
policy, disposition kind/destination/output/fee/memo, reservation/submission identity and capacity event.
A missing, malformed or contradictory tuple moves the source to a distinct quantified non-sendable
recovery status without deleting any evidence. Ordinary refund/quarantine workers must reject rows whose
startup audit is absent or non-complete; they must never manufacture first-time frozen intent from current
configuration after restart.

A database with one surviving source row is not proof that every other obligation and frozen decision
survived. Replace the current `any(source row)` exemption with an admission protocol that can distinguish:

1. a verified coherent restore;
2. a genuinely new deployment approved through an audited bootstrap;
3. a partial, stale, conflicting or unknown restore; and
4. a deployment already populated by pre-fix replay.

For cases 3 and 4, do not classify rediscovered principal under current terms. Either reconstruct exact
historical authorization from an independent durable source or create a quantified, source-specific,
non-sendable recovery hold. Preserve network, vault, mint, token program, commitment, source signature,
source timestamp/account/memo, exact integer principal and the missing/conflicting authorization reason.
Count it in liabilities and pin recovery progress.

#### Collected RED acceptance

Use the real startup caller, deposit page commit, deposit worker and both disposition workers with only
chain/address/send boundaries replaced. Cover:

- a stale restore containing one unrelated processed source while an older policy or capacity obligation
  is absent;
- partial restores retaining only source rows, only capacity evidence, only terminal rows, or only cap
  events;
- retained rows in every nonterminal status, especially ordinary refund/quarantine rows with no capacity
  hold, partial policy, a mismatched reservation, or missing submission identity;
- a database populated by the old unsafe replay before upgrade;
- below-minimum and nonpositive policy decisions plus refund and quarantine capacity holds;
- fee, minimum, maximum, destination, quarantine-account and cap drift;
- multiple pages, equal timestamps, more obligations than each worker limit, restart and duplicate replay;
- missing/corrupt/conflicting backup identity and restore manifests; and
- positive online-backup and copied DB+WAL restores.

For every incomplete case require startup refusal or explicit source-specific recovery holds, zero Nexus
and Solana transport through the actual deposit/refund/quarantine workers, full integer liability, no
inferred fee/reservation/terminal row, and no current-term replacement authorization. For a verified
restore require the original decision, destination, amount, memo, fee and terms evidence exactly once.

#### Implementation constraints

- Do not use table non-emptiness, schema existence, a recent mtime, current configuration or source
  rediscovery as restore proof.
- Bind any restore manifest to deployment identity, database generation, both custody/query identities,
  complete lifecycle/cap/fee evidence and an independently retained integrity value.
- Do not add a manual latch-delete or dummy-row bootstrap bypass.
- Keep the existing empty-database latch as immediate containment until this stronger gate is accepted.

### Batch 2 — make all startup admission outcomes durable and truthful

**Priority: P1 operator safety. Status: implemented offline in the staged witness candidate.**

The external witness now owns `ready`, `claimed`, `running` and permanent `held` states. A local receipt
binds the matching lease to the live process/file identity. Recovery, session and heartbeat checks must
finish before `running`; every runtime failure after claim attempts a permanent hold. The dashboard
requires exact external running evidence plus the local live receipt and otherwise suppresses healthy
metrics as unknown/held. This supersedes absence-of-local-latch readiness.

Keep this batch open for integration acceptance until Batch 0 artifact identity and Batch 6 witness
deployment are accepted. A witness state is only as authoritative as its independent anti-rollback host,
credential separation and exact artifact/image approval.

#### Retained acceptance matrix

- heartbeat missing/malformed/exception and zero checkpoint;
- terminal-provenance audit failure;
- Solana and Nexus incomplete/malformed scans, cap-window failure and reference-seed failure;
- empty, partial and verified restored databases;
- crash after claim, after each chain rebuild and immediately before/after completion;
- repeated startup, concurrent dashboard reads and database read/write failures; and
- retained healthy metrics from an earlier run.

For every non-running state require the banner and `/api/issues` entry, unknown backing/open-obligation/
fee/cap totals, no “refunds continue” claim and zero poller starts. A dashboard result may call admission
healthy only when the same response is bracketed by one exact stable external running lease and a matching
live-process receipt.

### Batch 3 — move invalid capacity rows outside automatic eligible FIFO

**Priority: P1 progress without weakening transport safety.**

Only transactionally validated frozen holds may participate in automatic global FIFO. When frozen intent
is malformed, source/terminal lifecycle conflicts, submission state is unknown, or another non-retryable
condition is found, atomically classify the row into a durable operator-action scheduler state. Retain the
original blob, full principal, diagnostics and non-sendability. Do not repair evidence from current terms.

Eligible FIFO remains global across refund and quarantine kinds. Impossible-under-current-cap rows retain
the existing special handling and become eligible again only after a sufficient reviewed cap change.

#### Collected RED acceptance

- malformed refund and quarantine rows ahead of valid same-kind and cross-kind work;
- source missing, source drift, opposing terminal and already-submitted conflicts;
- more operator-action rows than each worker limit;
- cap `0`, exact boundary, cap decrease/increase, rolling-window aging and restart;
- concurrent refund/quarantine workers;
- alert deduplication and durable issue visibility; and
- reviewed evidence-bound resolution of the blocked row.

Require each younger eligible frozen intent to submit exactly once. The blocked row must retain its source,
raw evidence, principal and diagnostics. No operator-action transition may release a pre-RPC/unknown
reservation, delete a source, invent a fee or authorize transport.

### Batch 4 — make the dashboard actually read-only and snapshot-consistent

**Priority: P2 hardening. Status: implemented offline in the current source.**

`api_summary()` now uses one `mode=ro` connection and transaction for metrics, counts and payout exposure,
then rechecks the exact witness lease. A missing path returns unknown without creating DB/WAL/SHM. Keep
the acceptance below collected and repeat it against the final externally attested artifact.

Acceptance:

- no database, WAL or SHM file is created or changed by any dashboard endpoint;
- missing/unreadable schema returns sanitized unknown values;
- a latch/admission transition concurrent with summary rendering cannot produce `not_held` plus healthy
  retained metrics; and
- repeated summary/issues/transaction reads leave a complete database dump byte-for-byte unchanged.

### Batch 5 — bind service identity and node readiness

The current implementation makes heartbeat validation fatal and pins both genesis identities, but that is only
partial. Before database mutation, require exact heartbeat address, owner, schema, pair/custody identity
and terms. Require Solana health/root freshness and Nexus `synchronized=true`, `syncing=false`, expected
mode/network and fresh tip from the exact configured endpoints. Wrong-typed/missing/stale evidence starts
no recovery scan or worker and cannot be repaired from a configured label.

### Batch 6 — operationalize the witness and hold resolution

Add a read-only evidence-export tool, reviewed initial/bootstrap and restore ceremonies, complete
configuration templates, independent TLS witness deployment and crash-held/manual recovery runbook. The
runtime token must never issue an initial deployment. Provide an evidence-bound Solana hold-resolution
protocol or explicitly approve permanent retention; no direct-send/manual-SQL bypass.

Keep provider-v2 and optional receipts disabled until their own wire-size, owner/schema, cost,
create/readback and migration acceptance succeeds.

### Batch 7 — live acceptance and release

Only then run explicitly authorized target-infrastructure acceptance for provider pagination, authoritative
network/finality, Nexus completeness/reference/TLS, both bridge directions, accepted-but-unparsed outcomes,
durable-boundary crashes, backup/restore, total loss and operator rehearsal.

## Batch grounding and executable acceptance matrix

Every row uses the **explicit non-Atlas settlement hypothesis** evidence class; none inherits marine
Class A evidence. O4 is the primary vision outcome. O3 applies to provenance/recovery/artifact
proof, O1 to inspectable evidence contracts, O2 only to the exact namespace/service-identity
prerequisite, and O5 only to authorized external validation. A named human owner and approver must
replace the role labels below at kickoff.

| Batch | Objective and production ownership | Prerequisite / pinned source-interface dependency | Human authority boundary | Negative / concurrency / recovery exit |
|---|---|---|---|---|
| 0 — executable authority | O4/O3/O1; deployment launcher/image plus `swapService.py`, `src/custody_admission.py` and complete runtime closure | Published `2c4ed319` sealed-witness and finite-fingerprint behavior is the compatibility baseline; pin an independent launcher/immutable-image authority and its complete closure manifest before implementation. The current in-process digest is not that authority. | Independent artifact approver selects the immutable digest; runtime/operator credentials cannot issue or rewrite it | Mutate every root/module/dependency/interpreter/launcher byte, including pre-import effects; race two processes on one image/permit; kill before/after claim/complete/seal. New default-collected external-launcher module must prove refusal before lock/DB/witness/chain and one exact run/seal. |
| 1 — restore admission | O4/O3; recovery owner for `src/state_db.py`, `src/startup_recovery.py`, `src/solana_client.py`, `src/nexus_client.py` and migration/backup format | Batch 0 external executable admission plus a versioned, independently approved coherent restore/bootstrap identity covering the SQLite image, schema, configuration, witness generation and all policy/capacity/submission/fee/terminal evidence. | Operator separately approves audited new bootstrap or coherent restore; absent history has no send authority | Parameterize every nonterminal status and required field with absent/wrong/conflicting evidence; race audit with scanner/workers; exercise empty, unrelated-row partial, source/capacity/terminal/cap-only, pre-fix, online backup, DB+WAL and total loss. Require zero transport/full liability or exact original intent once. |
| 2 — durable startup outcome | O4/O3; witness/runtime/dashboard in `src/custody_witness.py`, `src/custody_admission.py`, `src/main.py`, `src/dashboard.py` | Batch 0 executable identity and Batch 1 coherent restore admission; preserve the published `ready → claimed → running → ready(next generation)` witness states and exact-image/configuration interfaces. | Independent witness administrator owns bootstrap/hold recovery; service runtime owns only one-use transitions | Use distinct OS processes/connections to contend on claim/complete/hold/seal; kill at each boundary; read from a third connection. Exactly one owner may run, stale green state is forbidden and no claimed/held generation revives automatically. |
| 3 — capacity progress | O4; state/workers/dashboard in `src/state_db.py`, `src/solana_client.py`, `src/dashboard.py` | Batch 1 complete state audit and the accepted frozen-policy, liability, reservation and capacity-hold schemas at `2c4ed319`; malformed/conflicting evidence must move classifications without changing principal or inventing send authority. | Human disposition requires frozen evidence and audit; no direct send/SQL edit | Put malformed/conflicting/unknown oldest rows ahead of same/cross-kind valid holds, beyond worker limits; race refund/quarantine workers; restart and vary cap/age. Younger valid intent sends once; blocked principal/evidence stays unchanged and outside eligible FIFO. |
| 4 — dashboard | O4/O3; `src/dashboard.py` plus witness/read-only state APIs | Batches 1–3 state and liability interfaces plus the accepted read-only snapshot and live-lease match; observers consume only those APIs and never open a writable recovery path. | Read-only observers gain no financial authority | Missing/corrupt DB and witness mismatch remain unknown; witness transitions around one snapshot cannot yield stale green; reads across restart create/change no DB/WAL/SHM bytes. |
| 5 — service/node admission | O4 with O2 prerequisite and O3 provenance; `src/custody_chain.py`, `src/nexus_client.py`, `src/main.py` and exact endpoints | Batch 0 pre-execution boundary plus jointly pinned heartbeat address/owner/schema/pair/custody/terms and authoritative Solana health/root and Nexus sync/mode/network/tip APIs for the exact configured endpoints. | Operator pins address/owner/schema/pair/custody/terms and freshness policy | Wrong/stale/unsynced/wrong-typed evidence, query failure and identity change between reads refuse before DB/recovery. Exact authorized target fixtures must prove health/root and sync/mode/network/tip freshness. |
| 6 — witness/hold operations | O3/O4; evidence exporter, deployment runbook and operator-resolution tool | Batch 2 independent-process witness exit, Batch 3 durable hold classifications, Batch 4 read-only evidence API and Batch 5 exact chain-evidence contract; preserve the narrower witness/snapshot controls already accepted at `2c4ed319`, and pin independent TLS/anti-rollback administration and attributable role identities. | Distinct bootstrap/restore/disposition roles with attributable rationale; permanent retention is an explicit human policy | Export is read-only; TLS/anti-rollback rehearsal survives crash; resolution races workers, verifies exact chain evidence and either finalizes once with cap/fee effects or leaves the quantified hold unchanged. |
| 7 — external acceptance | O5 with O4; exact immutable release artifact and approved Solana/Nexus test infrastructure | Accepted exits for Batches 0–6, one exact externally attested artifact, authorized isolated target infrastructure, pinned provider/wallet/node/service revisions and a no-production-funds test plan. | Explicit target-activity authorization and separate human release decision | Unsupported versions, pagination truncation, finality mismatch, concurrent arrivals, timeout-after-acceptance, crashes at every durable boundary, coherent restore and total loss pass on devnet/testnet. No production funds. |

Current offline commands are recorded in [the evaluation](../EVALUATION.md). Add the missing
matrix rows to default pytest collection rather than accepting standalone probes.

## Gate after every batch

1. Independently review final runtime functions and shared callers.
2. Run the new focused collected module, the complete suite and all CI isolation shards.
3. Run dependency consistency, byte compilation, local Markdown links, token-literal inventory and
   whitespace checks.
4. Record exact commit/tree, runtime/test SHA-256, commands and results.
5. Prove the real index and unrelated dirty/untracked work are unchanged.
6. Keep local, live, publication-CI and release decisions separate.
