# swapService Architecture and Development Delta Review — 2026-09-17

**Reviewed source HEAD:** `91ce0b866a4e155bd690f70b6533124467c7318f` (`main`, matching
`origin/main` at review start)

**HEAD/index tree at review start:** `1f4cce83362c739682cf81d830d20a2c19519c4a`

**Tracked runtime baseline:** `6b1f052a2018f0315603e64a440d71cb612e8212`

**Delta after the September 16 report source:** one documentation commit, `91ce0b8`; no tracked
runtime, dependency or workflow change

**Release verdict:** **HARD BLOCKED for production and real funds**

## Scope and candidate separation

The reviewed commit only publishes the September 16 review, its evaluation/state-machine updates and
review evidence. `git diff 57b2de0..91ce0b8` contains seven documentation/evidence paths and no
runtime, dependency or workflow path. `git diff 6b1f052..91ce0b8` contains no tracked Python,
requirements or workflow change. The committed runtime therefore remains the September 15 source
baseline byte-for-byte.

The real index had no staged entries and resolved to the HEAD tree at review start. It excludes the
pre-existing unstaged `src/config.py` provider-v2 additions and untracked `src/service_record.py` /
`tests/test_service_record_v2.py`. Their SHA-256 identities exactly match the September 16 report, so
this review treats them as unchanged concurrent work, not new evidence or deployable code. Four
untracked raw diff captures under `docs/review_evidence/2026-09-15/` were also preserved and excluded.

No service was started. No credential, signer, live RPC mutation, token transfer, payout, refund,
quarantine action, Nexus asset mutation or other chain write was performed. Tests used temporary
state and existing offline/mocked transport boundaries.

## Findings

### Critical — current-v1 disposition recovery still invents intent from spend

This is the same P0 blocker, with stronger source/test evidence rather than a new implementation
delta.

`state_db.reconstruct_confirmed_solana_sig_disposition()` accepts any positive observed output not
exceeding the source principal. It then:

1. creates or upgrades a terminal refund/quarantine row;
2. reconstructs confirmed rolling-cap spend from the observed output;
3. derives `fee_units = source_amount - payout_amount`;
4. inserts that difference as a fee; and
5. deletes a matching unresolved source row.

The current-v1 memo binds only disposition kind and source signature. It does not bind intended
recipient, output, fee or terms revision. Startup validation checks that a refund returns to the
observed source token account, but it has no frozen intended quarantine recipient to compare and
therefore accepts an arbitrary quarantine destination from otherwise valid chain evidence. Positive
chain evidence proves actual spend; it does not prove the economic authorization that produced it.

The default-collected test
`StartupReconstructionTests::test_state_reconstructs_wipeout_and_backup_dispositions_atomically_idempotently`
positively expects both wipeout and backup reconstruction to create a terminal row and book the
source/output difference as a fee. Its quarantine case also uses the source token account as the
quarantine destination. The suite is green because it codifies the unsafe inference, not because the
exit is satisfied.

**Required repair:** current-v1 chain-only evidence may reconstruct exact observed cap spend, but must
retain a quantified unresolved/manual-review liability, must not create a terminal fee, and must not
delete the source obligation. Automatic terminal reconstruction requires surviving frozen intent or
a new evidence version committed before submission that binds source, kind, recipient, output, fee
and terms.

### High — durable Solana ingestion still has no processing minimum classifier

The ingestion repair correctly retains every positive custody delta. The real worker then checks memo,
destination validity, maximum size and whether output after fees is positive. It never consumes
`MIN_DEPOSIT_SOLANA_UNITS` or an executable micro policy before the Nexus debit boundary. The threshold
is used by configuration/startup messaging and public-record construction, not by this financial
transition.

The default-collected
`test_positive_deposit_below_processing_minimum_still_enters_lifecycle` proves only safe ingestion.
Collection contains no live-worker below/exact/above threshold matrix. Consequently the existing green
suite permits a positive-net below-minimum input to become `debit in flight` and submit a Nexus debit.
Recovery eventually feeds the same unclassified lifecycle.

**Required repair:** one pure exact-integer classifier must be shared by live processing and recovery.
It must define below, exact-boundary and above-minimum outcomes, percentage/rounding behavior and
terminal/hold/refund treatment. Every positive input remains durably discoverable; the repair must not
restore a history filter.

### High operational — disposition cap refusal still loses its reason

`prepare_solana_sig_disposition()` returns only `False` when capacity cannot be reserved. Refund and
quarantine workers log a generic `budget_or_claim_refused` event and leave the source in `to be
refunded` / `to be quarantined`. The same return shape covers capacity exhaustion and source/lifecycle
conflict. No durable row retains needed/used/cap units; no typed state is visible in the dashboard;
no dedicated alert is emitted.

The default-collected `test_quarantine_disposition_cap_refusal_leaves_source_retryable` explicitly
expects the unchanged generic state and absence of a partial disposition. That is useful containment
(no send and no partial intent) but not the required operational state machine. The primary payout path
already has a typed `payout cap held` state, alert and dashboard surface; disposition workers do not.

**Required repair:** atomically persist a typed retryable cap hold with obligation identity and exact
needed/used/cap units before transport. Expose and alert it separately from evidence/lifecycle
conflicts, and prove restart plus later-capacity release without duplicate send.

### Dirty provider-v2 proposal — unchanged and outside runtime

The dirty hashes are unchanged. No tracked registration, heartbeat, startup-recovery, waterline,
receipt or money-path caller imports `src/service_record.py`. Its builder publishes micro percentages
that the committed money paths do not enforce. Secret collision checks compare whole public values,
so a configured secret embedded in a public URL remains publishable. The untracked tests cover exact
secret equality, not substring/structured-URL leakage. Address-selected Nexus create/update/readback,
strict owner/type/schema/service/pair/custody validation, monotonic terms migration and explicit
legacy fallback are still absent from the deployable runtime.

Provider-v2 is not a prerequisite to repair the three committed-runtime blockers. Do not merge or call
it the default until its fields map to enforced runtime/recovery policy and its target-node migration
passes.

## Positive controls actually established

- The reviewed HEAD and upstream branch SHA matched at review start.
- The real index had no staged entries and resolved to the HEAD tree.
- All nine tracked runtime paths in the prior manifest retain their September 15 SHA-256 values.
- All three concurrent provider-v2 worktree paths retain their September 16 SHA-256 values.
- Default collection finds **434 tests** in the shared tree, including the untracked provider-v2 test
  module; collection succeeds in 0.40 seconds.
- Dependency consistency, Python 3.11 byte compilation, local Markdown links before candidate links,
  real-index token-pair inventory and real-index/shared-tree whitespace checks pass.
- The inventory is index-aware, excludes dirty runtime from the publication candidate and reports
  **274 active lines** against the real index.

These establish source identity and local execution hygiene. They do not establish clean exact-HEAD
execution, repair any blocker or prove external-chain semantics.

## Verification record

The available repository virtual environment is Python 3.11 with the pinned requirements and pytest.
CI uses Python 3.12. A Python 3.12 dependency probe was denied by unattended approval policy and was
not retried or rerouted; no local 3.12 result is claimed. Optional `ruff`, `mypy`, `pyflakes`, `pylint`,
`bandit` and `pip-audit` modules are not installed in the repository virtual environment, so no broad
lint, type or fresh vulnerability-audit result is claimed.

| Gate | Exact result |
|---|---|
| `.venv/bin/python -m pip check` | **PASS** — `No broken requirements found.` |
| `.venv/bin/python -m compileall -q src *.py tests` with external bytecode cache | **PASS** — exit 0 |
| `.venv/bin/python scripts/check_markdown_links.py` before the dated report existed | **PASS** — `Local Markdown links: OK` |
| `.venv/bin/python scripts/check_token_pair_inventory.py` against the real index | **PASS** — **274 active lines** |
| `.venv/bin/python -m pytest --collect-only -q` | **PASS** — **434 tests collected in 0.40s** |
| Initial full shared-tree suite after adding links but before creating this report | **EXPECTED INTERMEDIATE FAIL** — **1 failed, 433 passed, 71 subtests passed in 132.31s**; only the two not-yet-created `DEVELOPMENT_REVIEW_2026-09-17.md` links failed |
| Final full shared-tree suite | **PASS** — **434 passed, 71 subtests passed in 134.84s** |
| Recovery standalone | **PASS** — **33 passed, 46 subtests passed in 8.91s** |
| Recovery plus installed-SDK boundary | **PASS** — **34 passed, 46 subtests passed in 9.31s** |
| Receipt + payout + Nexus-fee + SDK CI shard | **PASS** — **85 passed in 26.43s** |
| Final candidate Markdown links / whitespace | **PASS** — links OK; shared and real-index whitespace clean |
| Documentation-only disposable-index inventory | **PASS** — **274 active lines**; candidate whitespace clean |
| Fresh focused blocker probes | **NOT RUN** — execution of temporary out-of-tree probes was denied and not rerouted; the September 16 probes remain historical evidence only |
| Target Helius/Solana/Nexus matrix | **NOT RUN** |

Concise machine-result evidence is preserved in
[`review_evidence/2026-09-17/final-gates.log`](review_evidence/2026-09-17/final-gates.log),
SHA-256 `b0c07e09cba45dbe15e46283cc9f8ad5f8d6be63e7c94c0928cb0b83517d9645`.
Source/index/worktree identities are preserved in
[`review_evidence/2026-09-17/runtime-hashes.log`](review_evidence/2026-09-17/runtime-hashes.log),
SHA-256 `c2bb19928beed1aac5e31cc622e58c406dcbd583d0ec385faf7bdc8b8a69178d`.

The complete suite necessarily exercises the shared working tree and therefore includes the unchanged
dirty provider-v2 files. No isolated exact-HEAD materialization was attempted after the documented
unattended denial. The report must not present the shared-tree count as a clean-checkout, committed-only
or remote-CI result.

## Runtime identities and hashes

| Path | Committed-index SHA-256 | Current worktree SHA-256 | Scope |
|---|---|---|---|
| `src/config.py` | `f8e4e295aacbc0236dc332f33917c78e413a7611790e48f52646cdd2a570d868` | `e6b7f28d60e4d26c6fed1fea0ab869aaa727fb7be3672dd6d002ba1f658f52eb` | dirty proposal differs from deployable HEAD |
| `src/nexus_client.py` | `20ed25032231735972f2565049c6014eca060964c98fed373282600a27cf19d6` | same | tracked runtime |
| `src/receipt_contract.py` | `b6afe7a8abb0f4027cbf9a78a2052b5a87920ba872d0088322092c9a95e8533b` | same | tracked runtime |
| `src/service_record.py` | absent | `1487382a259b373f08f3ec667075e88d1485bb99042dccb4d1469b16bcf8c6ec` | untracked proposal |
| `src/solana_client.py` | `bc2c4108f65fd4585cfe3166a9503c6065c83f3067eb4794ce19c96bf19e2bd6` | same | tracked runtime |
| `src/startup_recovery.py` | `25a7528fff6c80e3bb9737e6494fa29f7cc295e1e8d35e21184a29fed9dcbd48` | same | tracked runtime |
| `src/state_db.py` | `4ae733f36aac1c5b85124832eae9df8150d6ac5910c16b33dbb15da3243578cb` | same | tracked runtime |
| `src/swap_nexus.py` | `1374281b0bf62cc7a86c4af945b9d7d7bf14674372f8b68ca24aea7bebc7f4d2` | same | tracked runtime |
| `src/swap_receipts.py` | `745b894cbc2c39789036d07579c40a31a906e26bd3742b8e24d1e5b6318ff81f` | same | tracked runtime |
| `src/swap_solana.py` | `3cf6cdaa8e5170992fdc384cc25fffaf6508ec42ab50eb0cca34e9e17065f8e5` | same | tracked runtime |
| `tests/test_service_record_v2.py` | absent | `9039c042f660dfeddf0eaaaa0c36a1f35969e777f2cb9f8345c34a7f81055497` | untracked proposal test |

Preserved untracked raw-evidence SHA-256 values:

- `committed-since-sept12.diff` — `ca03c11322630c8ecab22c8702fcc6b46d755b807941a4164ac09bc4ae703791`
- `dirty-config.diff` — `f16cebe66b69a9fae7eedf99e1e2fadc55bd6f66c7878e21a47cf126f7633404`
- `dirty-service-record.diff` — `85e94ec6a52f9a71538d8a8352cdca173d8740050879ed0753847d86fbb7b91b`
- `dirty-service-record-tests.diff` — `e421348127283b0c00e8e5b08d448df4d3d2719be5395d408f0f15d72d887282`

## Next coding batches and collected acceptance tests

### Batch A — P0 intent-safe disposition recovery

Implementation boundary: memo/evidence versioning, startup scanner, recovery finalizer, terminal tables,
fee journal, cap ledger, unresolved liabilities and operator surface.

Required default-collected tests:

1. Wipe the database, present a one-unit current-v1 refund against a large source and require: exact
   one-unit cap spend, no fee, no terminal source deletion and a quantified unresolved liability.
2. Repeat for quarantine with an arbitrary destination; require a manual hold, not terminal quarantine.
3. Restore a backup containing exact frozen intent; matching chain evidence may terminalize once and
   books only the frozen fee. Wrong recipient/output/kind/terms retain the liability.
4. Change current configuration after submission; recovery uses frozen historical terms, never current
   fee/minimum/destination policy.
5. Exercise duplicate scans, conflicting signatures, conflicting local lifecycle, crash before/after
   cap-event and hold writes, backup/WAL restore and idempotent restart through
   `perform_startup_recovery`, not only the database helper.
6. For a new evidence version, prove that the memo/reference committed before send binds exact intent
   and that legacy current-v1 evidence remains conservatively held.

### Batch B — P1 shared input admission classifier

Implementation boundary: one dependency-neutral exact policy consumed by live worker, recovery and
public terms.

Required default-collected tests:

1. Real worker with below, exactly-at and above-minimum positive inputs; only policy-authorized cases
   reach the Nexus debit transport boundary.
2. Equal- and unequal-decimal pairs with exact integer fee/percentage rounding; assert values, not just
   parseability.
3. Invalid memo/account, oversized input and non-positive output interaction with threshold policy;
   each source gets one unambiguous durable disposition.
4. Recovery/replay of the same fixtures produces byte-equivalent classification and never requeues a
   below-minimum source as an ordinary payout.
5. Ingestion still stores every positive custody delta and keeps waterlines safe.
6. Public registration/terms output is generated from the same policy; unsupported or parsed-but-unused
   percentages fail admission instead of being advertised.

### Batch C — P1 typed disposition-cap hold

Implementation boundary: atomic reservation result, source lifecycle, diagnostics, dashboard/API,
alerts and restart retry.

Required default-collected tests:

1. Fill the cap for both refund and quarantine; no transport call occurs and one typed hold persists
   exact obligation/needed/used/cap units.
2. Restart with the hold, free capacity and prove one transition to prepared/submitted with one send.
3. Inject source/lifecycle/evidence conflict and prove it uses a distinct manual state, never the cap
   state.
4. Concurrent claims cannot oversubscribe or create duplicate holds; database failures roll back the
   source and reservation atomically.
5. Dashboard/API and alert payload expose the typed reason and exact units without secrets; alert
   rate-limiting does not hide the durable state.

### Batch D — provider-v2 only after A–C

Required default-collected and target-node tests include real caller imports, address-selected
create/read/update, owner/type/schema/service/pair/custody mismatch rejection, multiple same-type assets,
name/address disagreement, monotonic terms, explicit legacy fallback, URL/structured-field secret
containment, and proof that every published economic field is enforced by live and recovery paths.

## Exact publication gate for this documentation candidate

The parent publication must satisfy all of the following; local shared-tree success is not a
substitute:

1. Stage only `docs/DEVELOPMENT_REVIEW_2026-09-17.md`, `docs/EVALUATION.md`,
   `docs/STATE_MACHINES.md` and the two `docs/review_evidence/2026-09-17/*.log` files. The logs are
   intentionally ignored by the broad `*.log` rule, so add only these exact paths with `git add -f`.
   Explicitly exclude `src/config.py`, `src/service_record.py`, `tests/test_service_record_v2.py` and
   all four untracked September 15 raw diffs. Review `git diff --cached --name-status` before
   committing.
2. Confirm the committed runtime hashes in the table still match and the real index was not changed by
   this review before the parent intentionally stages documentation.
3. Build a disposable index from `91ce0b8`, add only the publication paths, and run the index-aware
   token-pair inventory there. If line-coordinate markers change, update the inventory document rather
   than weakening the checker. A disposable index containing dirty runtime proves the wrong candidate.
4. Run `pip check`, literal CI compilation, local Markdown links, the complete pytest suite, all three
   configured CI isolation shards, candidate-index inventory and candidate whitespace checks. Run on
   Python 3.12 or require exact-head CI to provide that coverage. A dirty shared-tree run must remain
   labelled as such.
5. Commit without runtime paths, then run `git diff --check HEAD~1 HEAD` and read back the committed
   path list. Push only if authorized.
6. Read back the exact remote branch SHA and require the repository CI workflow to pass for that exact
   SHA. Prior local or prior-commit green results do not satisfy publication.
7. Documentation publication does not clear release. Keep production, real funds and optional receipt
   spending disabled until Batches A–C pass independent final-tree review and the approved target-chain
   matrix proves both directions, pagination/completeness, finality, timeout-after-acceptance,
   crash/restart, backup/WAL restore, database loss, cap holds and operator rehearsal. Provider-v2 and
   optional receipts have their additional separate acceptance gates.

No file was staged, committed or pushed by this review.