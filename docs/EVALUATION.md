# swapService — Current Engineering Evaluation and Remediation Plan

## Current verdict — 2026-09-28

**Release blocked.** Reviewed range:

```text
base:        9f12211811331bae741702757e9d8259a16d55ff
source:      6769f7a1bb68dd2a975f4b39aa910f2405d38d42
source tree: 54f06ad253326f21673d5ca0536921bc323e04b6
```

The three reviewed commits add conservative Solana-side maintenance containment:
`c3d36c9` holds unseen inputs at or before a monotonic startup boundary, `70c572d`
holds retained ready rows without frozen policy, and `6769f7a` extends that audit to
partial, malformed, source-conflicting and nonpayable ready-row policy. All three controls
passed real-worker offline regressions and should be kept.

They do **not** establish coherent restore admission. A retained non-ready refund or
quarantine row with no frozen policy remains outside the startup audit; fresh actual-worker
probes reached the mocked Solana transport under current fee and destination terms. General
startup refusal can still look healthy on the dashboard, malformed capacity evidence can
still starve valid work, and dashboard summary can create a missing database. See the
[September 28 review](DEVELOPMENT_REVIEW_2026-09-28.md) and the
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

## Remaining findings, in repair order

### R-1 — High: partial/stale restore admission remains lifecycle-incomplete

The empty-database latch and three new Solana controls are useful containment, not a coherent-restore
protocol. Table non-emptiness still permits startup without proving that policy, capacity, fee, cap and
terminal evidence belong to one complete generation. The previous source-only-ready replay to a current-
term Nexus debit is now blocked by `record_solana_recovery_boundary()` and the page-commit boundary.

The current residual path is a retained **non-ready** source. Startup audits only
`status = 'ready for processing'`. A partial restore can retain `to be refunded` or
`to be quarantined` while losing both policy fields and any frozen capacity intent. Fresh offline probes
at the reviewed source made recovery report complete and then exercised each real disposition worker.
Each called the mocked Solana send boundary for 1,090 units from a 1,100-unit source using the current
10-unit fee and current resolved destination. The full principal remained a local liability pending
confirmation, so this proves replacement of historical authorization and one externally attempted send,
not realized loss or duplicate settlement.

Relevant current code is `state_db.py:1805-1853`, which audits only ready rows, and
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
lifecycle components, non-ready rows and Nexus-side recovery still require the broader admission protocol.
Do not clear or manually retry these holds; production remains blocked.

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

### R-1c — High operability: general startup refusal is not durable dashboard admission

The dashboard reads only `recovery_admission_holds`. An empty table becomes `not_held`, although startup
can fail before the latch (for example heartbeat missing/malformed) or during later reconstruction. A fresh
probe retained a healthy metrics snapshot, made heartbeat lookup return missing and observed startup
`recovery_complete=False`; the dashboard still returned ratio `20000`, `not_held` and zero issues.
`main.run()` remains fail-closed, so this is operator misinformation rather than a transport bypass.

**Exit:** persist one startup-owned `pending`/`held`/`complete` state with sanitized reason/timestamps.
Only a valid durable `complete` result may expose healthy metrics, and admission plus metrics/counts must be
read from one SQLite snapshot. Cover every startup failure and crash boundary, not only empty custody.

### R-1d — Medium hardening: the dashboard can create a missing database

`_recovery_admission_status()` uses `mode=ro`, but `api_summary()` then calls state helpers whose ordinary
`sqlite3.connect(DB_PATH)` creates the file when it is absent. The review reproduced this against a
temporary missing path. This does not clear a hold or authorize startup, but violates the read-only
boundary and can mutate filesystem state before service initialization.

**Exit:** use one read-only connection/transaction or dedicated read-only state API for each dashboard
response. Assert no DB/WAL/SHM creation or byte change for all endpoints and snapshot-consistent behavior
when admission changes concurrently.

### R-2 — High deployment-safety gap: invalid registration is alert-only

`src/main.py:370-379` reports failed heartbeat validation or an exception but does not return;
execution can continue to pollers. The independent reviewer exercised that path offline. The
current validator checks readability/fields/parseable waterlines, not complete owner/address/
pair/terms identity. Authoritative network and freshness admission is also missing: known Solana
hostname/label checks exist, but custom endpoints are not checked against genesis/health/root
freshness, and Nexus network/sync/tip freshness is not an enforced startup gate.

**Exit:** add explicit fail-closed registration and authoritative chain-identity/freshness admission
before mutable startup/polling. Test mismatch, unavailable evidence, stale/unsynced nodes and
validator exceptions with zero poller/chain-write calls; validate semantics on intended nodes.
Do not equate a configured network label or a successful recovery mock with this gate.

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

1. **Close R-1 first** across every retained nonterminal state, beginning with real refund and quarantine
   workers over rows whose policy/capacity evidence is missing, malformed or inconsistent. Keep all three
   new holds, the empty-DB latch and retained A/B/C controls; do not replace them with current-term replay.
2. Close R-1c so every startup refusal is durable and healthy dashboard values require a complete
   admission record from the same snapshot.
3. Repair R-1b without releasing malformed/conflicting evidence to transport: separate operator-action
   rows from validated eligible FIFO and prove later valid work progresses with liability conserved.
4. Close R-1d with genuinely read-only snapshot-consistent dashboard APIs, then close R-2 and R-3 through
   independent caller-level review.
5. Keep provider-v2 and receipts disabled/unclaimed as runtime capabilities until their separate
   migration/cost gates pass. Preserve compatibility; no dependency upgrade is part of this review.
6. On explicitly approved Solana devnet/Nexus test infrastructure, run both directions, mixed decimals,
   provider pagination/concurrent arrivals, finality, exact readback, Nexus references,
   accepted-but-unparsed/timeout outcomes, durable-boundary crashes, backup/WAL and total-loss recovery.
   Rehearse alerts, holds, incident response, key rotation and TLS/session controls.
7. Re-review the final runtime identity, run the complete configured gate, then make a separate release
   decision. Production and real funds remain blocked; no live acceptance was performed.

The executable current plan is the
[September 25 recovery admission and capacity-fairness plan](plans/2026-09-25-recovery-admission-and-capacity-fairness.md).
The [September 23 follow-up](plans/2026-09-23-financial-recovery-follow-up.md) and original
[A/B/C implementation plan](plans/recovery-input-cap-repairs.md) remain historical implementation records.
