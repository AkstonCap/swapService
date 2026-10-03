# Recovery admission and capacity-fairness repair plan — 2026-09-25

**Current assessment: 2026-10-02.** This maintained plan is updated in place rather than
duplicated under a new date. Published Batch 1 containment now includes retained ordinary dispositions,
debit-submission metadata, capacity siblings and terminal siblings. The staged sealed-custody candidate
implements an exact-image continuity gate plus durable dashboard authorization, but introduces a P0
artifact-attestation exit and does not close service identity, node freshness, witness operations,
capacity fairness or live acceptance.

## Decision and scope

Reviewed `HEAD`: `ee10b6e20dfe85f15347386adecb9dc99db55bb5`; staged index tree:
`a73785b8653e3ad03c9216072b7366999ba1e854`; compared with
`ed73c513ee22f9626502273aa0d8e42a4c238b7a`.

Keep all accepted containment controls:

- the durable empty-custody startup latch;
- the monotonic Solana recovery boundary and source-specific historical holds;
- strict holding of missing/invalid retained ready-row policy;
- unconditional holding of retained ordinary refund/quarantine states;
- holding of ready rows with any retained debit-submission field;
- holding of ready rows with any disposition-capacity sibling; and
- holding of ready rows with any processed/refunded/quarantined sibling.

Also preserve the staged candidate's fail-closed witness state machine, claim-before-init ordering,
whole-file/schema verification, recovery-before-running, per-cycle lease check, worker drain, quiescent
seal and read-only snapshot dashboard while repairing the exits below.

Production and real funds remain blocked. This plan makes no dependency upgrade, provider-v2 cutover,
receipt enablement, live transaction, commit, or publication authorization.

## Current batch status

| Batch | Status at reviewed candidate | Evidence and remaining exit |
|---|---|---|
| 0 — executable artifact identity | **Blocked / P0** | Local `ebedff7` binds root-entrypoint drift; the October 3 maintenance increment additionally binds running Linux interpreter bytes. These are in-process checks only: installed artifacts/shared libraries and independent pre-execution attestation remain open. The custody prerequisite is absent from `origin/main`, so this narrow increment cannot publish the larger feature implicitly. |
| 1 — restore/image admission | **Partial** | Four published row containments plus staged exact-image witness are green offline. External approval must establish financial coherence; exact bytes alone cannot discover an incomplete/pre-fix approved image. |
| 2 — durable startup outcome | **Implemented offline in staged candidate** | Claimed/running/held witness plus local receipt suppress stale healthy dashboard values. Requires artifact and independent deployment acceptance. |
| 3 — eligible capacity FIFO | **Open** | Malformed oldest evidence still blocks a younger fitting hold. |
| 4 — read-only dashboard | **Implemented offline in staged candidate** | One read-only snapshot and missing-path no-create tests pass; retain witness-before/after consistency and target deployment acceptance. |
| 5 — service/chain admission | **Blocked** | Heartbeat owner/address/pair is unbound; genesis-only checks omit Solana health/root and Nexus sync/tip freshness. |
| 6 — witness operations and hold resolution | **Open** | Required settings/bootstrap certificate ceremony are not integrated; non-capacity Solana holds lack audited disposition. |
| 7 — target acceptance/release | **Open** | No live node, crash/restore or operator rehearsal evidence. |

## Repair order

### Batch 0 — bind the artifact before repository code executes

**Priority: P0 executable integrity.**

An in-process fingerprint cannot prove the approved executable when the root entrypoint that invokes the
checker is outside its manifest. Define an external trusted launcher or immutable deployment image whose
digest covers `swapService.py`, every imported runtime module, dependency artifact and interpreter. The
witness certificate must bind that identity; a changed wrapper cannot run code before refusal.

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

**Priority: P2 hardening. Status: implemented offline in the staged candidate.**

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

The staged candidate makes heartbeat validation fatal and pins both genesis identities, but that is only
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

## Gate after every batch

1. Independently review final runtime functions and shared callers.
2. Run the new focused collected module, the complete suite and all CI isolation shards.
3. Run dependency consistency, byte compilation, local Markdown links, token-literal inventory and
   whitespace checks.
4. Record exact commit/tree, runtime/test SHA-256, commands and results.
5. Prove the real index and unrelated dirty/untracked work are unchanged.
6. Keep local, live, publication-CI and release decisions separate.
