# swapService Architecture and Development Review — 2026-09-21

**Reviewed runtime source:** `814c0ae8cbe0e65036a3b01d1eb8028e4dcb8ad3` (`main`)

**Previous reviewed source:** `91ce0b866a4e155bd690f70b6533124467c7318f`

**Runtime delta:** `814c0ae` changes `src/state_db.py`, `src/dashboard.py` and
`tests/test_recovery_safety.py`; `3da29ba` is documentation/evidence only

**Release verdict:** **HARD BLOCKED for production and real funds**

## Scope and candidate separation

This review evaluates the architecture and the two commits after the September 17 source. The
financially material change is `814c0ae`, which attempts to repair current-v1 chain-only
refund/quarantine reconstruction. It adds `refund evidence held` / `quarantine evidence held`, routes
a fresh chain-only reconstruction into those states, and exposes them in the read-only dashboard.
No dependency or workflow changed.

The worktree also contains pre-existing concurrent provider-v2 work that is not part of the deployable
runtime or this publication candidate:

- modified `src/config.py` — SHA-256
  `e6b7f28d60e4d26c6fed1fea0ab869aaa727fb7be3672dd6d002ba1f658f52eb`;
- untracked `src/service_record.py` — SHA-256
  `1487382a259b373f08f3ec667075e88d1485bb99042dccb4d1469b16bcf8c6ec`;
- untracked `tests/test_service_record_v2.py` — SHA-256
  `9039c042f660dfeddf0eaaaa0c36a1f35969e777f2cb9f8345c34a7f81055497`;
- four untracked raw diff captures under `docs/review_evidence/2026-09-15/` with the exact hashes
  recorded in the September 21 runtime evidence.

Those seven paths match the September 17 identities and were preserved. Shared-worktree pytest
includes the untracked provider-v2 tests and is labelled accordingly. Targeted bypass probes were run
from an archived exact `814c0ae` tree so the dirty proposal could not affect them. The publication gate
uses the real Git index with only the explicit review documents/evidence staged; it does not stage a
disposable runtime candidate.

No service was started. No credential, signer, live RPC mutation, token transfer, payout, refund,
quarantine action, Nexus asset update or other chain write was performed. External transports were
replaced only at their invocation boundary in isolated temporary-database tests.

## Findings

### Critical/P0 — `814c0ae` contains fresh wipeout recovery, but legacy manufactured terminal rows bypass it

The repair is valid on its new branch. When `reconstruct_confirmed_solana_sig_disposition()` finds no
existing terminal disposition row, it now:

1. reconstructs only the exact observed output as `reserved` / `submitted` / `confirmed` cap spend;
2. inserts or updates the source into `refund evidence held` / `quarantine evidence held`;
3. retains the entire source principal in `get_unresolved_solana_liability_units()`;
4. writes no `refunded_sigs` / `quarantined_sigs` terminal row;
5. writes no fee; and
6. leaves the hold unselected by refund/quarantine send workers while exposing it in `api_issues()`.

Exact-HEAD probes established this for both disposition kinds with a one-unit observed transfer against
a 10,000,000-unit source. That closes the fresh-database branch identified on September 17.

The branch condition is only `terminal is None`. Before `814c0ae`, the same chain-only recovery wrote
a terminal row directly and booked `source principal - observed output` as a fee. On an in-place
upgrade, database restore or backup containing such a row, the new code takes the existing-terminal
branch. A matching `refund_confirmed` / `quarantine_confirmed` row is treated as surviving frozen
intent even though the schema records no provenance showing that its terms were frozen before
submission rather than manufactured after observing the transfer.

The exact-HEAD bypass probe seeds the terminal shape produced by the old recovery, with a 10,000,000
source and one-unit output. Current code returns success for both refund and quarantine, leaves no
pending liability and books a 9,999,999-unit fee. This is the unsafe pre-repair result preserved across
upgrade. No default-collected test covers this migration path; the new backup test seeds a
`submitting` row as genuine frozen intent instead.

**Required repair:** introduce immutable pre-submission provenance/evidence versioning and an
append-only migration. Existing terminal disposition rows whose provenance is absent, legacy or
recovery-only must be treated conservatively: reconstruct exact observed cap spend, reverse or hold
unproven fee classification, restore a quantified evidence-held source and prohibit automatic send or
terminal re-acceptance. Genuine locally prepared rows may terminalize only when the provenance and all
frozen source/kind/recipient/output/fee/terms fields match exact chain evidence. The migration must be
idempotent and cover in-place DB, SQLite backup/WAL and database-loss reconstruction.

### High — positive-net Solana inputs below the configured processing minimum still cross the Nexus debit boundary

Durable ingestion correctly retains every positive custody delta; that must not be reversed. The real
worker still validates memo/account/max size and then tests only whether output after fees is positive.
It never consumes `MIN_DEPOSIT_SOLANA_UNITS` before persisting Nexus debit intent and invoking the debit
transport.

The exact-HEAD probe inserts a 1,000,000-unit positive source, patches the processing minimum to
1,000,001, keeps exact output math positive and invokes the real
`process_unprocessed_solana_deposits()` worker with only account validation and Nexus transport
replaced. The worker calls the transport once and advances the source to
`debited, awaiting confirmation`.

**Required repair:** one dependency-neutral exact-integer classifier must be consumed by live
processing, startup reconstruction/replay and public terms. It must define below, exact-boundary and
above-minimum behavior and any micro-fee policy without filtering history. Every positive source remains
durable; only an explicitly payable classification may cross the Nexus debit boundary.

### High operational — refund/quarantine cap refusal still has no typed durable cause

`prepare_solana_sig_disposition()` still returns an undifferentiated `False` for capacity exhaustion,
source conflict and lifecycle conflict. Both workers log `budget_or_claim_refused` and retain the
original generic `to be refunded` / `to be quarantined` status. They persist no per-obligation
needed/used/cap values and issue no dedicated alert. The next cycle retries the same generic state.

Exact-HEAD probes filled the cap and tested both kinds. No disposition-specific budget event was
written and the generic source status remained unchanged. This is safe against immediate send but is
not an actionable or auditable operational state.

**Required repair:** return a typed preparation result and atomically persist a retryable cap hold with
obligation identity, kind and exact needed/used/cap units before transport. Persist lifecycle/evidence
conflicts in distinct manual states. Expose and alert both classes, and prove restart plus later
capacity release sends once.

### High evidence gap — target-chain semantics remain unexecuted

Offline fixtures do not prove Helius target-network pagination/finality, real nested classic-SPL
transaction shapes, Nexus reference/contract pagination and finality, accepted-but-unparsed responses,
timeout after acceptance, or restart/backup/database-loss behavior on the intended builds. Helius is
the trusted Solana history provider; no second attestor is required, but application validation,
network binding, exact amount/account matching and durable cursor completeness remain mandatory.
Nexus API source-of-truth is exact structured response/readback, never console prose or bounded absence.

No live financial mutation was authorized for this review. Production remains blocked after local
repairs until an explicitly approved Solana devnet/Nexus test matrix passes.

### Dirty provider-v2 candidate — unchanged and still outside runtime

The concurrent provider-v2 hashes are unchanged. It remains library-only: no tracked registration,
heartbeat, recovery, waterline, receipt or money-path caller imports `src/service_record.py`. Its
published micro percentages are not enforced by the committed live/recovery policy, and prior URL
substring secret-containment/address-selected migration gaps remain open. Do not merge or advertise it
as the default before the money-path batches and its separate target-node acceptance complete.

## Positive controls actually verified

- The exact archived `814c0ae` probes passed **7 cases**: two fresh evidence-hold safety cases, two
  legacy-terminal bypass reproductions, one real-worker below-minimum reproduction and two typed-cap
  refusal reproductions.
- Fresh chain-only holds retain quantified principal, count only proven spend, write no fee/terminal,
  appear in the dashboard and cannot reach either Solana send worker.
- Existing frozen-intent recovery still checks exact terminal source, recipient, output, memo,
  signature and lifecycle evidence before finalization; the open issue is provenance, not removal of
  those comparisons.
- The exact archived source keeps remote actions mocked and state in temporary SQLite databases.
- The dirty provider-v2 files and four raw evidence diffs were preserved byte-for-byte and excluded
  from the staged candidate.

These establish containment and reproduce the remaining local bypasses. They do not establish
production readiness or live external semantics.

## Verification record

The final executed gate and exact counts are preserved in
[`review_evidence/2026-09-21/verification.log`](review_evidence/2026-09-21/verification.log).
Source/index/worktree identities are preserved in
[`review_evidence/2026-09-21/runtime-hashes.log`](review_evidence/2026-09-21/runtime-hashes.log).
The complete finding-to-evidence map is preserved in
[`review_evidence/2026-09-21/findings.md`](review_evidence/2026-09-21/findings.md).

| Gate | Result |
|---|---|
| Exact-`814c0ae` targeted bypass probes | **PASS — 7 passed in 2.53s**; passing includes three expected unsafe-behavior reproductions plus four containment/diagnostic cases |
| Exact-`814c0ae` full suite | **PASS — 405 passed, 71 subtests passed in 84.52s** after initializing Git metadata in the archived tree; the first archive run had one infrastructure-only inventory failure because `git ls-files` requires a repository |
| Exact-`814c0ae` recovery + budget + payout-review shard | **PASS — 70 passed, 46 subtests passed in 12.95s** |
| Shared-worktree complete pytest suite | **PASS — 434 passed, 71 subtests passed in 81.67s**; includes 29 dirty provider-v2 tests absent from HEAD |
| Dependency consistency / byte compilation / Markdown links | **PASS** — no broken requirements; compile exit 0; local links OK |
| Recovery standalone / recovery + installed SDK | **PASS — 33 + 46 subtests / 34 + 46 subtests** |
| Receipt + payout + Nexus-fee + SDK shard | **PASS — 85 passed** |
| Real staged-index token-literal inventory / whitespace | **PASS — 274 active lines**; cached and worktree whitespace clean; only explicit review paths staged |
| Exact publication SHA GitHub Actions | Required after push; local results do not substitute |
| Target Helius/Solana/Nexus matrix | **NOT RUN** |

## Ordered repair batches and executable acceptance tests

### Batch A — P0 provenance-safe disposition migration

Implementation boundary: disposition tables and migrations, intent preparation, startup recovery,
fee journal, cap ledger, unresolved liabilities, dashboard/operator diagnostics and backup tooling.

Required default-collected acceptance:

1. Create a pre-repair database by executing the old chain-only reconstruction for both disposition
   kinds with one-unit output against a large principal, then upgrade. Require exact one-unit cap spend,
   no retained/inferred fee, no terminal row and one full-principal evidence hold.
2. Repeat from an SQLite online backup and from a copied database plus WAL. The post-upgrade state and
   accounting must be identical and idempotent across repeated startup.
3. Seed a genuinely prepared `submitting`, `awaiting confirmation` and already-confirmed row carrying
   immutable pre-submission provenance. Matching exact chain evidence may terminalize once; wrong
   recipient/output/kind/signature/source/terms/provenance must retain a hold.
4. Seed absent, malformed, duplicated and recovery-only provenance. Fail closed before fee/source
   deletion and before any transport; unknown origin must never be upgraded by matching current config.
5. Change fee, minimum, quarantine destination and terms version after submission. Recovery must use
   frozen historical terms only.
6. Inject failures between migration, cap event, fee reversal/hold and source writes. One transaction
   must preserve either the original conservative state or the complete migrated hold; no mixed state.
7. Exercise `perform_startup_recovery`, backing/surplus accounting, dashboard and both workers. The hold
   is quantified/operator-visible, pins recovery as designed and never authorizes an automatic send.
8. Add an explicit future memo/evidence version only if it binds exact intent before submission; legacy
   current-v1 remains conservative.

### Batch B — P1 shared Solana-input classifier

Implementation boundary: one exact policy module, live worker, startup replay/recovery and public terms.

Required default-collected acceptance:

1. Invoke the real worker with positive below, exactly-at and above-minimum sources; only authorized
   classifications reach a mocked Nexus transport boundary.
2. Cover equal and unequal token decimals, flat and basis-point fees, integer rounding and any supported
   micro percentage with exact value assertions.
3. Cover invalid memo/account, oversized input and non-positive output interactions; each source gets one
   durable, unambiguous disposition and no double fee.
4. Replay the same evidence through recovery and require byte-equivalent classifications. A
   below-minimum source cannot reappear as an ordinary payout after restart or policy change.
5. Prove ingestion still retains every positive custody delta and all incomplete pages/holds keep
   waterlines safe.
6. Generate public registration terms from the same executable classifier; parsed-but-unused settings
   must fail admission rather than be advertised.

### Batch C — P1 typed disposition-cap hold

Implementation boundary: typed reservation result, atomic source transition, diagnostics, dashboard/API,
alerts and retry scheduling.

Required default-collected acceptance:

1. Exhaust cap for refund and quarantine through each real worker. No transport call occurs and exactly
   one typed hold persists obligation/kind/needed/used/cap units.
2. Restart with the hold, age confirmed spend out or release approved capacity, and prove one transition
   to prepared/submitted with one send.
3. Source mismatch, competing lifecycle, opposing disposition and budget database failure each use a
   distinct fail-closed result; none masquerades as capacity exhaustion.
4. Concurrent claims cannot oversubscribe, duplicate holds or duplicate sends; rollback keeps source and
   budget state atomic.
5. Dashboard/API and alert payload show exact non-secret diagnostics. Alert delivery failure cannot erase
   the durable hold.

### Batch D — independent final-tree review and target-chain acceptance

After A–C, rerun exact-HEAD bypass probes, full CI and an independent review of the final source. On
explicitly approved Solana devnet/Nexus test infrastructure, execute both directions, mixed decimals,
continuation/restart, target finality, malformed and multi-contract responses, timeout before/after
acceptance, crash at every durable boundary, backup/WAL restore, database loss, cap exhaustion/release
and operator rehearsal. Record exact authoritative readback; do not use real production funds.

### Batch E — optional receipts and provider-v2 only after A–D

Keep optional NXS-spending receipts disabled until their separate cost/indexing/readback/schema gate
passes. Integrate provider-v2 only after address-selected create/read/update, exact
owner/type/schema/service/pair/custody validation, monotonic terms migration, explicit legacy fallback,
URL/structured-field secret containment and proof that every published economic term is enforced by
live and recovery paths.

## Publication acceptance

The documentation publication is acceptable only if:

1. the real index contains only `docs/EVALUATION.md`, `docs/STATE_MACHINES.md`, this dated review and the
   explicit September 21 evidence artifacts;
2. the real staged-index inventory, whitespace, links, dependency, compilation, complete pytest and
   configured isolation shards pass;
3. the commit contains no runtime, dependency, workflow or pre-existing dirty path;
4. the pushed remote `main` SHA exactly equals the local full SHA; and
5. GitHub Actions `CI` succeeds for that exact SHA.

A green documentation commit does not clear the release. Batches A–C plus target-chain acceptance remain
mandatory before production or real-fund admission.