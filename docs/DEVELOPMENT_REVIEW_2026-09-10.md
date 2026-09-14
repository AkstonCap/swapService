# swapService Independent Architecture and Development Review — 2026-09-10

**Review-start HEAD:** `3bd8f23f60c1658816ddb986c701ec81a6777143` (`main`, matching `origin/main`)
**Review-start tree:** `6adf72c03412d07cddd7d1d3c66bee30b60df5c3`
**Prior reviewed source:** `1116a4a867553fa4d37ea338f121ee56aa702075` from [`DEVELOPMENT_REVIEW_2026-09-09.md`](DEVELOPMENT_REVIEW_2026-09-09.md)
**Delta:** ten commits; 25 files; 1,706 insertions and 292 deletions
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The delta closes the previously reproduced forward-path daily-cap bypass: primary Nexus→Solana payouts and Solana-side refund/quarantine sends now reserve an append-only obligation before RPC, unknown outcomes retain capacity, and the old read/send/best-effort-write helper refuses use. Receipt publication gained a lifetime expected-cost reservation ledger and explicit production admission rejection. Test module isolation is materially improved: the complete installed-dependency suite and all configured order shards pass.

The new payout protocol does not satisfy its own settlement and recovery exits. Solana refund/quarantine confirmation trusts signature status only. It does not reject a transaction `err`, can accept a merely `confirmed` status through its numeric confirmation fallback, and never reads the transaction to match the configured mint, vault source/authority, exact recipient, integer amount and memo. An executed offline probe terminalized a refund, booked its terminal state and deleted the source liability from a finalized status carrying an instruction error. A second probe accepted confirmed-not-finalized status while configured for finalized ingestion.

Successful database-wipe recovery also reconstructs an exact paid primary source without reconstructing recent payout-cap consumption. The executed probe returned `recovery_complete=True`, archived the paid payout, reported zero cap usage, then admitted another obligation equal to the entire configured cap. The service-wide ceiling is therefore not restart/restore safe.

Production remains blocked independently by the previously open target Nexus/Solana integration matrix, receipt registration/cost semantics and operational acceptance. No live RPC mutation or financial transaction was performed.

## Severity-ordered findings

### Critical — failed or merely confirmed Solana dispositions can be archived as settled

`get_signatures_confirmation()` builds an accepted-finality set but then returns true when either the status string is accepted **or** the numeric confirmation count meets the threshold (`src/solana_client.py:985-1026`). It never inspects the status object's `err`. Both refund and quarantine workers consume that boolean (`src/solana_client.py:1029-1157`) and call `confirm_solana_sig_disposition()`.

The database finalizer checks only locally recorded signature and units before appending `confirmed`, booking a fee, terminalizing the disposition row and deleting `unprocessed_sigs` (`src/state_db.py:2611-2693`). It receives no transaction object and cannot prove that the signature executed the intended transfer. This can erase a still-owed refund liability after a failed transaction and can mark quarantine complete even though funds never moved.

Executed synthetic evidence:

- a status with `confirmationStatus="finalized"` and non-null `err` caused `check_sig_confirmations()` to return one, set `refunded_sigs.status="refund_confirmed"` and remove the source row;
- with finalized ingestion configured, `confirmationStatus="confirmed"`, `confirmations=2` and no error was accepted as true;
- neither case invoked a transaction read or exact transfer matcher.

**Required exit:** every refund and quarantine terminal transition must consume successful finalized transaction evidence and compare the direct signature, configured vault authority/source, configured classic SPL mint, exact frozen destination, exact integer output and exact versioned source memo. Reject non-null transaction errors, missing/malformed instruction evidence, Token-2022 or other-program instructions, duplicate/conflicting transfers, and confirmed-only status. Keep the liability and reservation held on every mismatch or unavailable read. Add collected regressions through the actual workers, not only direct database finalizer tests.

### Critical — payout-cap consumption is lost by successful wipeout recovery

Forward primary admission calls `prepare_nexus_payout()` with the configured cap, atomically reserving capacity with the source claim (`src/swap_nexus.py:348-364`, `src/state_db.py:3097-3186`). Startup reconstruction calls the same function without `payout_cap_solana_units` (`src/startup_recovery.py:250-278`). The finalizer settles a budget event only when a reservation already exists; otherwise it archives the paid source and skips the budget ledger (`src/state_db.py:3334-3348`).

The existing wipeout round-trip test proves source reconstruction but does not assert cap state. The focused 2026-09-10 probe used one exact composite payout and returned a successful recovery with `nexus_payouts_reconstructed=1`; `payout_budget_used(86400)` was zero, and a second full-cap reservation succeeded immediately.

In-place upgrades are less exposed because `_payout_budget_usage_in_transaction()` merges recent legacy `payouts` rows with new events. A database restore/rebuild has no equivalent backfill, and pre-upgrade accepted sends whose legacy ledger write failed are also not recoverable from local state alone.

**Required exit:** recovery must reconstruct conservative cap events from complete exact Solana transaction evidence and the configured rolling window before it can return green. Each reconstructed event must preserve immutable obligation identity, signature, integer output and an authoritative chain timestamp. If the scan cannot prove complete window coverage, startup must retain an exposure pause/manual budget hold rather than report success. Prove database wipe, WAL/backup restore, in-place migration, mixed legacy/new rows, window boundaries, duplicates and accepted-but-unrecorded outcomes. After recovery, existing spend plus new reservations must never exceed the cap.

### High operational — cap refusal is not an actionable visible hold

The old helper emitted `payout_cap_exceeded`; the new reservation function returns false, and callers emit structured logs only (`src/state_db.py:2278-2291`, `src/swap_nexus.py:353-363`, `src/solana_client.py:833-840,941-948`). No runtime Python call emits the advertised alert event. A primary cap-refused credit remains `ready for processing`, but that status is absent from `dashboard.TXID_ISSUE_STATUSES` (`src/dashboard.py:50-80`). Refund/quarantine rows remain visible through their existing statuses, but the primary hold can be missed beyond the aggregate open count.

**Required exit:** atomically retain a distinct no-send cap-held reason/status or equivalent immutable hold evidence, expose it in dashboard/API output with obligation and required/used/cap units, and deliver a rate-limited critical alert through the configured route. Prove no RPC call, repeated-loop behavior, alert delivery/failure isolation and automatic re-evaluation only after genuine rolling capacity becomes available.

### High when enabled — receipt publication remains externally unaccepted

Receipt creation now reserves a positive raw-NXS expected cost against a lifetime budget before crossing the create boundary (`src/state_db.py:1675-1734`) and records parseable response identity. Production admission rejects explicit receipt enablement. These are useful containment controls.

They do not establish target-node actual cost, fee identity, named-asset semantics, owner/address shape, Query DSL completeness, indexing delay, timeout-after-acceptance resolution or the fixed-field provider-registration migration. The expected cost is operator-configured rather than authoritatively read back. Keep `NEXUS_SWAP_RECEIPTS_ENABLED=false` in production.

**Required exit:** preserve production rejection until a receipt-capable provider record exists and the target node proves create cost, exact transaction/address readback, rejection, delayed indexing, duplicate handling, timeout/crash/restart and budget accounting. A published receipt must never affect an already-proven payout settlement.

### External and operational acceptance remains open

No target Nexus node or Solana devnet/testnet was used. The review does not establish Nexus account-history completeness, equal-timestamp ordering, stable pagination, API POST/TLS behavior, Solana transaction/finality semantics, timeout-after-acceptance behavior, restore operations, alert delivery, key rotation or two-person disposition policy.

## Positive controls verified in the changed code

- Primary, refund and quarantine forward paths reserve exact integer Solana output before RPC in one SQLite write transaction.
- Unsettled reservations consume capacity indefinitely; two concurrent reservations cannot oversubscribe the local cap fixture.
- Returned primary/refund/quarantine signatures are append-only and ledger-write failure retains the source and reservation.
- Primary payout finalization still requires full successful finalized transaction evidence matched to frozen composite source identity and transfer terms.
- The unsafe read/send/best-effort-record helper now refuses runtime use.
- Receipt creation reserves its configured expected NXS cost before create and production startup rejects receipt mode.
- Collection-time SDK/dotenv replacement was removed; standalone and alternate-order tests now compose with the pinned installed SDK.
- Exact Nexus `(txid, contract_id)`, sibling-scoped finalization, atomic fee/source state, strict payout memo parsing and fail-closed Nexus enumeration/recovery controls remain present.

These local controls do not offset the Critical disposition-proof and recovery-cap failures.

## Verification executed

| Check | Exact result |
|---|---|
| Review-start Git state | `main`; HEAD and `origin/main` both `3bd8f23f60c1658816ddb986c701ec81a6777143`; clean real index/worktree |
| Delta from prior reviewed source `1116a4a..3bd8f23` | 10 commits; 25 files; 1,706 insertions, 292 deletions |
| Fresh Python 3.11 virtual environment | Pinned requirements plus pytest, Ruff and pip-audit installed successfully |
| Dependency consistency | `pip check`: no broken requirements |
| Dependency advisories | `pip-audit -r requirements.txt`: no known vulnerabilities |
| Python byte-compilation | Pass with external bytecode cache |
| Ruff scoped undefined-name/local-variable check | `F821,F822,F823`: pass; not a broad lint claim |
| Local Markdown links | Pass |
| Token-pair inventory at review-start index | Pass; 270 active literal lines |
| Complete installed-dependency suite | **288 passed, 33 subtests passed** in 43.92s |
| `tests/test_recovery_safety.py` | **15 passed, 8 subtests passed** |
| Recovery→installed-SDK order | **16 passed, 8 subtests passed** |
| Receipt→payout→fee→SDK order | **59 passed** |
| Failed/finality/recovery focused review probes | **3 passed**; each asserted the unsafe current behavior described above |
| Review-start exact-head GitHub Actions | No run exposed for `3bd8f23`; the newest visible `main` CI run was historical, so no green exact-head claim is made |
| Target Nexus/Solana live matrix | **Not run** |

The initial default interpreter attempt failed collection because `solders` was absent. It is not acceptance evidence; the clean isolated environment above installed every pinned dependency and ran the complete gate. No RPC transaction, token transfer, refund, quarantine movement or Nexus asset create occurred. All focused probes used temporary SQLite files and mocked transport/status boundaries.

## Repair order and executable acceptance

1. **P0:** replace status-only refund/quarantine settlement with one exact successful-finalized transaction evidence adapter; run failed/error, confirmed-only and every wrong-transfer fault through both real workers.
2. **P0:** reconstruct conservative rolling-cap spend during startup recovery or remain exposure-paused; prove wipeout/restore cannot admit a second full-cap obligation.
3. **P1:** add a durable visible cap-held state and verified alert delivery without making cap refusal retry a claimed/unknown send.
4. **P1:** keep receipts production-disabled until registration migration, target-node costs and create/query/readback crash matrix pass.
5. **P1:** execute the complete target Nexus/Solana finality, pagination, timeout, crash/restart, migration and reconciliation matrix on the exact candidate.
6. **P2:** rehearse alert failure, hold escalation, two-person disposition, SQLite/WAL backup/restore and key rotation.

Production and real-fund admission remain hard-blocked. Documentation review does not authorize a deployment or financial operation.

## Publication scope

Only these review documents are intended for the documentation commit:

- `docs/EVALUATION.md`
- `docs/STATE_MACHINES.md`
- `docs/DEVELOPMENT_REVIEW_2026-09-10.md`
- `docs/TOKEN_PAIR_LITERAL_INVENTORY.md`
- `docs/DEVELOPMENT_REVIEW_2026-09-10.sha256`

The reviewed-file manifest excludes itself. Exact remote commit readback and exact-head CI are post-commit evidence and must be reported with the publication result rather than written self-referentially here.
