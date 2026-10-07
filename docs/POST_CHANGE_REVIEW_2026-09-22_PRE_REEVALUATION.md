# swapService — Current Engineering Evaluation and Remediation Plan

## 2026-09-22 A/B/C repairs — offline acceptance approved

The requested batches are complete in the dirty single-pair candidate based on
`a1f19b109681da693331583e30fa192ccee17d2d`. Final independent runtime/integration review is
**APPROVED**; the parent verified all reviewed runtime/test hashes and reran the full gate.
This is **not approval for production, live-chain use or real funds**. Nothing was staged,
committed, pushed or deployed.

| Batch | Accepted offline behavior |
|---|---|
| A — recovery | Strict duplicate-key/exact-type provenance validation; atomic DDL/data migration with safe rollback and lock release; conservative full-principal evidence holds, inferred-fee reversal and preserved cap spend; in-place, online-backup and DB+WAL acceptance. |
| B — minimum policy | Shared exact-integer min/max/decimal/fee classifier before destination/refund routing; below-minimum and nonpositive-output principal stays held without fees or sends; frozen decisions survive restart; live/recovery/public v1 terms agree. |
| C — capacity holds | Typed atomic refusal/admission, needed/used/cap diagnostics, frozen-term retries, conflict liability retention and alerts. Payouts above the current nonzero cap stay held without starving fitting work, including across kinds and worker limits. |

**Parent gate:** 565 tests + 77 subtests passed. Separate shards: recovery 35 + 52 subtests;
recovery/SDK 36 + 52 subtests; receipt/payout/Nexus-fee/SDK 85; policy/cap/recovery acceptance
129. Dependency consistency, compilation, Markdown links, whitespace and disposable-index
inventory (274 active lines) passed. Real Git index and baseline unrelated files are unchanged.

See the [acceptance report](RECOVERY_INPUT_CAP_ACCEPTANCE.md) and
[independent closure review](review_evidence/2026-09-22/final-runtime-review.md) for exact scope,
reviewed hashes and the historical RED/GREEN record. Earlier counts below belong to their dated
snapshots and are not the current acceptance result.

### Remaining release gates

1. Explicitly authorized devnet/test infrastructure: both directions, provider pagination/finality,
   authoritative chain readback, crash/unknown outcomes and actual backup operations.
2. Operational acceptance: alerts, incident response, hold-resolution and key-rotation rehearsal.
3. A separate publication/release decision and exact-commit CI; this dirty candidate includes
   preserved provider-v2/config work that this repair neither integrates nor approves. Rebuild the
   inventory for the precise publication scope. Optional receipts remain outside this acceptance.

## Historical snapshot — 2026-09-22 startup containment before A/B/C completion

> This section is preserved verbatim as a dated pre-completion snapshot. Its statements that strict
> migration, the shared classifier or typed capacity holds were missing are superseded for the current
> dirty candidate by the status above. Its test counts describe that snapshot only.

The `a1f19b1` provenance repair already migrates legacy disposition terminals when
chain evidence rediscovers them. It does not by itself prevent an older terminal
outside the bounded recovery scan from retaining an inferred fee and no liability.
The local startup candidate audits both persisted terminal tables in one read
snapshot before heartbeat/history lookup. Missing or rejected provenance refuses
startup with `solana_terminal_provenance_unresolved`; a schema/read failure returns
`solana_terminal_provenance_audit_failed`. The guard does not manufacture cap spend,
rewrite fees, or treat a current fee configuration as historical authorization.

Focused collected coverage exercises both disposition kinds, absent/unknown/recovery-only
provenance, a current marker without matching evidence, accepted current and migrated-v0
intent, and failed audit lock release. This is containment, **not closure of the P0
migration gate**: conservative quantified-hold migration, backup/WAL acceptance and
strict provenance validation remain required. In particular, the committed validator
still permits JSON boolean/integer equality and duplicate keys; its stricter replacement
is part of separate pre-existing uncommitted work and is not included in this candidate.
An isolated HEAD-plus-candidate execution was denied by unattended execution policy.
Do not interpret shared-working-tree test results as clean-candidate verification or
publish this candidate until those dependencies and its exact verification are resolved.

Local verification of the shared tree: **505 tests + 77 subtests passed**; the new
startup admission module passed **13 tests**. Recovery alone passed **35 tests + 52
subtests**, recovery with installed SDK passed **36 tests + 52 subtests**, and the
receipt/payout/Nexus-fee/SDK shard passed **85 tests**. Compilation, dependency
consistency, local Markdown links and whitespace passed. Literal inventory passed
against a disposable index containing only this candidate (**274 active lines**).
The shared tree still includes the pre-existing recovery and provider-v2 work; those
files and the real Git index were not included in or modified by this repair.

The historical review baseline and evidence below remain unchanged.

**Reviewed runtime source HEAD:** `814c0ae8cbe0e65036a3b01d1eb8028e4dcb8ad3`.
**Delta since the September 17 source:** `3da29ba` published review documentation and `814c0ae`
changed `src/state_db.py`, `src/dashboard.py` and recovery tests. The separate unstaged/untracked
provider-v2 proposal remains outside the deployable runtime.
**Status:** fresh chain-only disposition recovery is contained, but an upgrade/back-up migration bypass
and two previously identified financial-state blockers remain; **not approved for real funds**.

This is the current issue register. The complete earlier evaluation and state-machine notes,
including pre-existing staged documentation, are preserved in the
[pre-repair snapshot](POST_CHANGE_REVIEW_2026-09-13_PRE_REPAIR_DOCUMENTATION.md).
Dated reviews describe their own snapshots, not the current working tree.

## Architecture and scope

One process bridges one configured classic-SPL-token/Nexus-token pair. `config.SWAP_PAIR`
provides immutable token/custody identities, independent decimals, display metadata and fee terms.
Gross conversion is 1:1 in whole-token units before fees and rounding; this is not market pricing.
Native SOL, Token-2022, arbitrary-chain routing and simultaneous pairs are not implemented.

Helius is a **trusted primary Solana history provider**, not an untrusted hint requiring a second
attestor. Application validation still requires correct network selection, exact integer amounts,
transaction success/finality and durable continuation. Core RPC is an operational alternative;
one provider's cursor must never be interpreted under another query or network.

Every financial side effect follows:

```text
persist intent → execute once → record remote identity →
resolve uncertain outcome against the chain → finalize atomically
```

A timeout is not failure. A bounded empty lookup is not proof of non-execution. A finalized
signature alone does not prove the intended transfer. Unknown outcomes retain liabilities and
budget capacity. Public recovery waterlines must not pass unresolved obligations that would be
lost with the database.

## Current repair batch

| Area | Implementation / required acceptance |
|---|---|
| Trusted Helius ingestion | Full parsed pages, fixed query identity and atomic provider-token persistence are integrated. Endpoint precedence, known-network conflict rejection and unambiguous API-key network selection are regression-tested. The final review exposed an already-finalized replay bypass; replay now validates the actual provider endpoint before every promotion. Independent closure review passed. |
| Finality and unsupported evidence | Holds freeze provenance and retain principal as a liability; unknown amounts fail accounting closed. Public recovery waterlines remain behind holds. Replay uses ≤256-signature status batches, validates all batches before promotion and fairly reaches beyond 1,000 holds. Liability reads use one SQLite snapshot during concurrent promotion. |
| Classic SPL parsing | Exact vault deltas support ordinary classic-SPL transfers. A matching outer ATA-create/inner-initialization bundle is recognized as one creation; ambiguous sources, duplicate or conflicting evidence remain held. |
| Refund/quarantine recovery — E-015, E-018 | `814c0ae` contains fresh current-v1 chain-only evidence: it records exact proven cap spend, creates no terminal row/fee, retains the complete source principal in an evidence hold and exposes that hold on the dashboard. **Open P0 migration bypass:** a terminal row manufactured by the pre-`814c0ae` recovery is accepted as if it proved pre-submission frozen intent; replay can preserve the old inferred fee and missing liability. Existing terminal rows/backups require provenance-aware migration or conservative hold before this exit closes. |
| Receipt outbox — E-016 | Settlement atomically retains an owner-independent obligation. Builder/payload failures enter explicit manual review. The shared all-string schema rejects overflowing JSON numbers before coercion; publication continues to valid later rows. Authenticated owner binding and budgeted one-shot creation remain separate. Focused independent receipt review/testing passed; its optional hash audit was incomplete. |
| Simplification — E-010 | Shared receipt contract, one authoritative durable ingestion/admission path, one registration lookup per publication batch and completed-query seen-row cleanup reduce duplicate policy and work. Non-durable scanner helpers and the budget-bypassing receipt claim were retired; database compatibility remains supported. |
| Test isolation — E-005, E-017 | Shared offline defaults support standalone modules, real installed-SDK checks and deposit/critical-safety collection in both orders. These gates pass on the current candidate. |

### Verified current candidate

The parent reran these gates against a disposable index containing the actual working candidate,
including new files. The real staged entries were byte-for-byte unchanged by that verification.

| Gate | Actual result |
|---|---|
| Full pytest suite | **405 tests + 71 subtests passed** |
| Helius ingestion + deposit scanner | **70 tests passed** |
| Recovery standalone | **33 tests + 46 subtests passed** |
| Recovery + installed-SDK boundary | **34 tests + 46 subtests passed** |
| Receipt + payout + Nexus fee lifecycle + SDK | **85 tests passed** |
| Deposit then critical safety | **169 tests + 25 subtests passed** |
| Critical safety then deposit | **169 tests + 25 subtests passed** |
| Dependency consistency / compilation / Markdown links / whitespace | Passed |
| Candidate token-literal inventory | Passed; **274 active lines** |

These are offline results, not live-chain acceptance. The final independent ingestion review and
its narrow replay-provider closure review completed; the latter independently passed the same
70-test ingestion/scanner suite and additional core-replay probes. Runtime code was held unchanged
during that review and the final full-suite verification. See the [repair report](POST_CHANGE_REVIEW_2026-09-13_BLOCKERS.md).

### Applied cleanup and deliberately retained boundaries

- Removed `fetch_incoming_deposits_via_helius`, `_fetch_deposits_helius`,
  `_fetch_deposits_core_rpc`, `process_helius_deposits`, `core_get_transactions_for_address`
  and `get_signatures_confirmation`; parser/SDK coverage now exercises authoritative scanners.
- Removed the ingestion `min_units` parameter: all positive principal must enter the liability
  lifecycle, where processing policy may classify it. History filtering cannot discard it.
- Centralized pure receipt schema/name validation in `receipt_contract.py`; independent exact-payout
  validation at the database boundary remains intentional defense, not duplicate code to delete.
- Purged completed-query `scan_seen` rows atomically and reused provider registration within a receipt
  batch. Kept immutable hold provenance, explicit unknown-outcome states and frozen database names.
- Deferred broader typed-evidence/DB-layer restructuring and combined recovery-history projections:
  these change safety-critical interfaces and are not justified as cosmetic cleanup.

## Existing controls retained

- **E-014:** Nexus source identity is `(txid, contract_id)` throughout admission, payout memos,
  operator intents and terminalization; an unpaid sibling remains independent. Legacy identity
  sentinels cannot authorize a guessed fresh transfer.
- **E-001 / E-007:** automatic Nexus refunds/quarantine remain disabled. The separate operator
  workflow prepares, authorizes, executes once, resolves positive evidence and finalizes one exact
  source. Live operational acceptance remains required.
- **E-002:** no heuristic amount filter hides Nexus credits. Mutable multi-page offset scans hold
  checkpoints; processing-only passes cannot claim enumeration completeness.
- **E-003 / E-004:** exact mixed-decimal money calculations and durable completed-payout evidence
  support fail-closed reconciliation. Errors/unhealthy results latch an exposure pause.
- **E-008 / E-009 / E-012:** explicit production admission requires configured pair/fee terms,
  positive exposure caps, alerting, quarantine destinations and authenticated Nexus HTTPS transport.
  The dashboard uses bearer-header authentication, not query tokens. Live TLS/proxy/alert verification
  remains an operator gate.
- **E-013:** earlier compatibility-tested dependency remediation is retained. This repair does not
  upgrade dependencies; a dependency-consistency check is not a fresh vulnerability audit.
- **E-011:** current documentation is separated from historical evidence. Persisted legacy names
  are migration contracts, not proof that the bridge only supports its original token pair.

## Remaining release gates

### Local execution gate — green; safety review remains open

- Full suite, focused financial regressions and installed-SDK/config-order shards passed.
- Dependency consistency, byte compilation, local Markdown links and whitespace passed.
- Token-literal inventory passed against the actual candidate in a disposable index; real staged
  entries were unchanged. The working candidate has not been staged or committed.
- The September 15 review identified three committed-runtime blockers. `814c0ae` closes the fresh-wipeout
  unsafe branch but not the upgrade/back-up branch: terminal rows created by the earlier lossy recovery
  have no provenance marker and are still accepted as frozen intent. Solana input processing still lacks
  the post-ingestion minimum classifier, and disposition cap refusal still lacks typed durable state/alert.
  The September 21 exact-HEAD bypass probes reproduce both remaining behaviors and the legacy-terminal
  migration bypass while proving the new evidence hold itself is non-sendable and liability-counted.
- The earlier optional receipt hash audit did not complete; no signed/full hash attestation is claimed.

Any subsequent runtime edit requires renewed affected-path review and test verification.

### Target-chain acceptance — E-006

These are **not established by offline fixtures**:

- Helius target-network history, real nested transaction shapes, continuation/restart, confirmed
  versus finalized behavior and concurrent arrivals over bounded ranges.
- Both bridge directions, mixed decimals, refund/quarantine, and exact authoritative readback.
- Accepted-but-unparsed responses, timeout before/after acceptance, crashes at durable boundaries,
  backup/WAL restore and database-loss reconstruction without skipped deposits or double payout.
- Nexus reference/contract fields, pagination/completeness, TLS and POST semantics on the intended build.

No live financial transaction is authorized by this repair. Run the matrix on explicitly approved
Solana devnet and Nexus test infrastructure before any production decision.

### Optional receipts — E-016

Receipt assets and names spend operator NXS. Keep `NEXUS_SWAP_RECEIPTS_ENABLED=false` for production;
the admission gate still rejects an explicit enablement. Required separate acceptance covers the
actual creation/name cost, budget adequacy, timeout-after-acceptance, indexing delay, exact owner and
payload readback, duplicate detection, and receipt-capable registration migration. A fixed-field v1
asset cannot gain `receipt_schema` through a heartbeat update.

### Operations — E-007 / E-009

Rehearse hold resolution, alert delivery, incident response, backups and key rotation. Maintain
independent authorization for ambiguous financial dispositions. Unknown/legacy evidence remains
held, not silently rewritten or released by an upgrade.

## Prioritized development plan

1. **P0 — finish disposition-recovery migration safety.** Keep the `814c0ae` fresh-chain hold. Add
   durable provenance that distinguishes an intent frozen before submission from a terminal row created
   by legacy chain-only reconstruction; migrate every existing/refetched `refund_confirmed` and
   `quarantine_confirmed` row conservatively. Unknown provenance must restore proven cap spend but move
   the source into a quantified operator hold with no inferred fee. Audit backup/WAL and in-place upgrades.
2. **P1 — shared Solana-input admission policy.** Add one exact minimum/micro classifier used by live
   processing and recovery without restoring lossy history filtering.
3. **P1 — typed disposition-cap hold.** Persist dashboard-visible and alerted disposition cap-refusal
   evidence separately from lifecycle/evidence conflicts.
4. Repair and independently review those boundaries, then execute the target-chain matrix and operator
   rehearsals with recorded authoritative evidence.
5. Enable optional receipts only after their separate cost/schema gate; otherwise leave them disabled.
6. Pursue provider-v2 and remaining configuration consolidation as a separate versioned migration,
   after its published fields map to enforced runtime policy and its secret/publication controls pass.

### Batch 7 — Complete configurability and provider asset v2 **(in progress; provider v2 remains documentation only)**

The implemented canonical pair object is not the planned provider-v2 contract. Remaining work:

- Consolidate network, custody, complete fee/minimum/dust terms and a deterministic terms fingerprint.
- Replace named-v1 heartbeat identity with an explicitly selected immutable asset address; validate
  owner, `distordia-type=swapService`, schema, `service_id` and complete pair/custody identity.
- Create a new complete fixed-field registration rather than relabelling v1, with an explicit
  compatibility release and migration evidence.
- Test multiple services/assets per signature chain, address/name disagreement, altered terms and
  every configured fee/decimal/legacy-alias conflict. Never select the first type match.
- Publish only non-secret identity, terms, custody, status and liveness data; never private RPC URLs,
  PINs, sessions or keys.

See the [planned standard](../ASSET_STANDARD.md#provider-swapservice-asset-standard-v2-planned),
[configuration reference](../CONFIG.md), [state machines](STATE_MACHINES.md), and
[token-literal inventory](TOKEN_PAIR_LITERAL_INVENTORY.md). Provider-v2 is not a prerequisite to
correctly repairing the existing single-pair bridge, and is not implemented by this batch.

## 2026-09-15 architecture and development review addendum

This dated addendum does not rewrite the September 13 candidate evidence. The independent
[2026-09-15 review](DEVELOPMENT_REVIEW_2026-09-15.md) inspected committed
`d0acd721..6b1f052` and the separate dirty provider-v2 candidate.

### Current local status corrections

- Helius durable ingestion, current disposition discovery, receipt-outbox retention and their
  isolation suites remain verified locally. Helius is trusted; a second attestor is not required.
- **P0 recovery intent gap:** wipeout reconstruction accepts any positive current-v1 disposition
  output not exceeding source principal, then books the difference as a fee. Because the memo binds
  no frozen output, fee or terms revision, chain-only evidence must count actual cap spend but retain
  the source as unresolved instead of terminalizing it.
- **P1 Solana policy gap:** removing unsafe ingestion-time minimum filtering was correct, but no
  processing classifier replaced it. A positive-net deposit below `MIN_DEPOSIT_SOLANA_UNITS`
  currently reaches the Nexus debit boundary. Implement one shared live/recovery classifier rather
  than filtering history.
- **P1 operational gap retained:** refund/quarantine cap refusal remains generic and log-only. Persist
  typed capacity evidence and alert it separately from lifecycle/evidence conflicts.

### Dirty provider-v2 candidate status

`src/service_record.py` and `tests/test_service_record_v2.py` are untracked, with related unstaged
configuration additions. They are new work and remain **library-only / not merge-ready**:

- no live registration, heartbeat, startup-recovery or waterline caller imports the module;
- the claimed default-v2/legacy-fallback configuration is therefore not a runtime migration;
- published micro percentages are parsed settings that current money paths do not enforce;
- exact-secret equality checks do not prevent a credential embedded in a public URL;
- zero/non-monotonic terms revisions and target Nexus address-based create/update/readback remain
  unimplemented.

Do not mark provider-v2 implemented or default until every published field maps to an enforced
runtime/recovery policy and the address-selected Nexus migration passes multi-asset target-node
acceptance. Production and real funds remain hard-blocked.

## 2026-09-16 no-runtime-delta verification

The [September 16 review](DEVELOPMENT_REVIEW_2026-09-16.md) found no tracked runtime,
dependency or workflow change after the September 15 source baseline. All ten paths in the prior
runtime manifest still match byte-for-byte. The real index equals the tracked HEAD tree and excludes
the unstaged `src/config.py` additions plus untracked provider-v2 implementation/test.

Fresh execution in an isolated Python 3.11 environment with the pinned requirements produced:

- full shared-tree suite: **434 passed, 71 subtests passed**;
- recovery: **33 passed, 46 subtests passed**; recovery plus installed SDK: **34 passed,
  46 subtests passed**;
- receipt/payout/Nexus-fee/SDK shard: **85 passed**;
- dependency consistency, literal CI compilation, Markdown links, CI/shared/index whitespace and the
  real-index token inventory: passed; inventory remains **274 active lines**;
- focused blocker probes: **3 committed-runtime reproductions passed** and **2 dirty provider-v2
  reproductions passed**. Passing means the probes still observed the documented unsafe behavior.

The full suite necessarily covered the shared working tree, including the unchanged dirty v2 proposal.
An isolated exact-HEAD materialization was denied by unattended approval policy and was not rerouted,
so this is not represented as a clean-checkout or exact-HEAD CI result. No live-chain operation ran.

## 2026-09-17 review and next coding batches

The [September 17 review](DEVELOPMENT_REVIEW_2026-09-17.md) found no tracked implementation,
dependency or workflow delta after the September 16 report. The committed runtime manifest and the
three concurrent provider-v2 worktree hashes are unchanged. This is a source-identity result, not a
new implementation claim.

Deeper inspection of default-collected tests makes the remaining exits more specific:

- Current recovery tests positively expect chain-only current-v1 disposition evidence to archive the
  source and derive a fee as `source principal - observed output`. Those assertions codify the P0
  unsafe inference; a green suite cannot close it.
- The collected ingestion test correctly proves that a positive below-minimum deposit enters durable
  state, but no collected worker test requires below/boundary/above-minimum behavior. The real worker
  checks only whether output after fees is positive before reaching the cross-chain debit boundary.
- The disposition-cap test proves only that capacity refusal leaves the generic source state unchanged.
  It does not require a durable typed reason, exact capacity evidence, dashboard visibility or alert.

Implement and review these batches in order:

1. **P0 — intent-safe disposition recovery.** For current-v1 chain-only evidence, record positively
   proven spend for cap accounting but retain a quantified unresolved liability and do not create a
   terminal fee. Permit automatic terminal reconstruction only from surviving exact frozen intent or
   a new pre-submission evidence version binding source, kind, recipient, output, fee and terms.
2. **P1 — shared Solana-input admission policy.** Classify every positive durable input before the
   cross-chain debit in both live processing and reconstruction. Define exact below/boundary/above
   behavior, integer rounding and any micro percentage once; make public terms derive from the same
   executable policy without restoring ingestion-time filtering.
3. **P1 — typed disposition-cap hold.** Atomically distinguish capacity refusal from lifecycle or
   evidence conflict, retain needed/used/cap units, expose it to operators, alert it and support a
   restart-safe retry when capacity becomes available. No remote send may occur while held.
4. **P1 after those repairs — provider-v2 integration.** Keep the current dirty library outside the
   deployable runtime until address-selected create/read/update, exact owner/type/schema/service and
   custody validation, monotonic terms, explicit v1 fallback, secret-safe public fields and actual
   caller integration pass target-node tests.

Each batch must add default-collected acceptance tests through the real worker/finalizer/recovery
caller, not helper-only probes. Required cases include wipeout and backup/WAL restore, policy change,
duplicate and conflicting evidence, crash boundaries, below/exact/above thresholds with equal and
unequal decimals, both disposition kinds, cap exhaustion and later release, restart, operator
surfaces and proof that transport send helpers remain uncalled on every hold. The exact publication
and live-acceptance gates are enumerated in the dated review. Production and real-fund admission
remain hard-blocked.

## 2026-09-21 recovery-repair assessment

The [September 21 review](DEVELOPMENT_REVIEW_2026-09-21.md) executes the `814c0ae` repair from an
exact archived HEAD and separates it from the unchanged dirty provider-v2 proposal. Seven targeted
cases establish both the repaired branch and the remaining bypasses:

- Fresh wipeout evidence for both dispositions creates an evidence hold, counts only the one-unit
  observed spend against the cap, books no fee, preserves the full quantified source liability,
  appears in the dashboard and is selected by neither send worker.
- A terminal row in the exact shape emitted by the pre-repair chain-only recovery bypasses the new
  `terminal is None` hold branch. For both refund and quarantine, replay accepts that row, retains no
  pending liability and books `source - observed output` as fee. The schema has no provenance that
  distinguishes this manufactured row from a genuine pre-submission frozen intent.
- The real Solana-input worker still sends a positive-net input one unit below a patched processing
  minimum to the mocked Nexus transport boundary.
- Refund and quarantine cap refusal still leave the original generic status and persist no typed
  per-obligation capacity record.

Therefore `814c0ae` is a valid containment improvement, not a complete P0 closure. Implement Batch 1
above before the shared classifier and typed-cap batches. Then rerun exact-HEAD probes, the complete
configured gate and the non-mutating target-chain acceptance matrix. No live financial operation was
performed by this review.
