# swapService Independent Architecture and Development Review — 2026-09-12

**Review-start HEAD:** `d0acd721d4af534c2b1313565ff3677cfb59e73e` (`main`, matching `origin/main`)
**Review-start tree:** `6bd38469c8f69dc04dce097268397d2b7b976740`
**Prior reviewed source:** `3bd8f23f60c1658816ddb986c701ec81a6777143` from [`DEVELOPMENT_REVIEW_2026-09-10.md`](DEVELOPMENT_REVIEW_2026-09-10.md)
**Delta:** eight commits; 24 files; 1,227 insertions and 208 deletions
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The delta closes the two Critical defects reproduced on 2026-09-10 in their direct forward/primary paths. Refund and quarantine workers now freeze the exact recipient and versioned memo, read the submitted Solana transaction at finalized commitment, reject transaction errors and require one exact configured-vault/configured-mint transfer before terminalization. Startup recovery rebuilds exact timestamped rolling-cap events for primary Nexus→Solana payouts. Primary cap refusal now persists a distinct reason, appears in dashboard/API issues, contributes durable exposure to the cap display and emits the rate-limited critical alert. Production admission also requires explicit pair identities, precisions and every fee term.

The global payout-cap/recovery exit is still failed. The repaired forward disposition paths emit `swapService:v1:refund:<source_sig>` and `swapService:v1:quarantine:<source_sig>`, while `scan_memos_since_timestamp()` recognizes only the superseded `refundSig:` and `quarantinedSig:` forms. `_rebuild_recent_payout_budget()` restores only primary `nexus_payout` evidence. An executed offline probe gave the scanner one successful finalized exact transfer for each current memo form; both scans returned `complete=True` with empty refund/quarantine evidence, and `_rebuild_solana_from_waterline()` returned green with zero disposition markers. Database-loss recovery can therefore omit recent disposition spend and terminal evidence while authorizing startup.

Receipt publication is better isolated from payout settlement and now requires a receipt-capable matching provider record at startup. However, if the provider-owner lookup is unavailable during an exact payout finalization, current code deliberately archives the payout and creates no durable receipt/outbox row. The collected test asserts this permanent absence. This is safe for payout idempotency and bridge liveness, but the enabled publication obligation is silently lost rather than held for later provider revalidation.

Production remains blocked independently by the target Nexus/Solana matrix, receipt fee/create/query acceptance and operational rehearsals. No live RPC mutation, transfer, refund, quarantine movement or Nexus asset create was performed.

## Severity-ordered findings

### Critical — current refund/quarantine payouts are invisible to wipeout recovery and the global cap

The forward workers construct the current memos at `src/solana_client.py:1621-1625` and send them at `src/solana_client.py:832-850,952-970`. The recovery scanner at `src/solana_client.py:2128-2310` recognizes primary `nexus_txid:` and only legacy `refundSig:` / `quarantinedSig:` disposition markers. It does not classify either `swapService:v1` disposition form. The recovery parser then treats empty disposition maps as valid (`src/startup_recovery.py:369-438`), and the cap rebuilder accepts only `NexusPayoutEvidence` with obligation kind `nexus_payout` (`src/startup_recovery.py:441-468`).

Executed synthetic evidence:

- one finalized successful classic-SPL transfer with `swapService:v1:refund:deposit-signature` produced `complete=True`, `refund_sigs={}` and a green zero-refund rebuild;
- the same result occurred for `swapService:v1:quarantine:deposit-signature`;
- both cases used the configured vault and mint, exact positive integer output, direct transaction signature, null transaction error and authoritative signature-page `blockTime`.

This is not merely missing live-chain proof. The local parser positively ignores the current protocol and can certify an incomplete reconstruction. The rolling 24-hour cap can undercount after database loss, and terminal disposition evidence is not restored.

**Required exit:** parse current and legacy forms explicitly. For every current disposition, require exact direct signature, null error, finalized status, configured vault authority/source, classic SPL mint, frozen destination, integer output, unique versioned memo and chain timestamp. Reconstruct the exact source terminal state and `reserved`/`submitted`/`confirmed` cap events, or keep startup incomplete when source context is insufficient. Add collected mixed primary/refund/quarantine wipeout, backup/WAL restore, duplicate/conflict and window-boundary tests before the live matrix.

### High when receipts are enabled — transient provider lookup permanently drops the publication outbox

At `src/nexus_client.py:1003-1038`, receipt-owner lookup failure is converted to `receipt_payload=None`; exact payout finalization then proceeds. `tests/test_swap_receipts.py:455-479` requires the payout to become terminal and requires `get_swap_receipt()` to remain `None`. There is no durable row a later successful provider read can replay.

The direction of the decoupling is correct: public metadata publication must never reopen or block a proven token payout. The implementation should still atomically retain an owner-independent receipt intent or explicit `publication blocked` evidence with the payout, and allow the publisher to freeze and validate the authoritative owner before any NXS-spending create. Until then, enabled receipt coverage is not durable. Production admission already rejects receipt mode.

### High operational — disposition cap refusal remains generic and log-only

Primary cap refusal is now a distinct `payout cap held` state with a durable reason, dashboard/API issue and `solana_payout_cap_held` alert (`src/swap_nexus.py:359-387`, `src/dashboard.py:56-80`). Refund/quarantine reservation refusal still returns false before writing a disposition row (`src/state_db.py:2639-2645`); callers leave the source as `to be refunded` / `to be quarantined` and emit only a structured warning (`src/solana_client.py:833-841,953-961`). Those rows are visible, but the operator cannot distinguish cap exhaustion from a concurrent claim or state conflict and receives no required/used/cap evidence.

**Required exit:** atomically persist a distinct cap-held reason for every disposition refusal caused by capacity, expose obligation/needed/used/cap units, emit a rate-limited critical alert and re-evaluate only through the same durable reservation path. Prove no RPC call and no attempt-budget consumption on refusal.

### Medium operational — dashboard silently falls back to incomplete exposure

`dashboard.api_summary()` reads the durable cap ledger, but on any exception silently falls back to a metrics snapshot or the legacy `payouts` table (`src/dashboard.py:150-162`). That preserves read-only UI availability but can display an understated cap without an `unknown`/`unhealthy` flag. The dashboard is not an admission control, so this does not bypass the database cap transaction, but it weakens incident visibility.

**Required exit:** report durable exposure as unavailable/stale and alert on ledger read failure; never render legacy-only fallback as an authoritative global-cap percentage.

### External and operational acceptance remains open

No target Nexus node or Solana devnet/testnet was used. The review does not establish Nexus account-history completeness, stable pagination, API POST/TLS behavior, target Solana parsed-instruction/finality behavior, timeout-after-acceptance handling, restore operations, alert delivery, key rotation or two-person disposition policy. Receipt registration and target-node create/query/cost semantics remain unaccepted, so receipt mode stays disabled in production.

## Positive controls verified in the changed code

- Refund/quarantine settlement now requires direct successful finalized transaction evidence matching the frozen vault, classic SPL mint, token-account recipient, integer output and versioned source memo.
- Primary payout wipeout recovery restores exact timestamped cap events and rejects conflicting/incomplete primary evidence.
- Primary cap exhaustion persists a dashboard-visible hold and reason, emits the configured alert event and is retried only through the durable reservation path.
- Dashboard cap usage includes unsettled durable reservations and reconstructed primary spend in the normal path.
- Production admission requires explicit token identities, custody, both precisions, all directional/refund/disposition fee terms and basis points; explicit zero fees remain valid.
- Receipt-enabled startup requires the fixed registration to advertise the known receipt schema, authoritative owner and exact immutable pair/custody values.
- Receipt lookup failure no longer blocks exact payout settlement; no receipt is fabricated without an authoritative owner.
- Existing composite Nexus source identity, exact primary payout proof, intent-first outbound actions, fail-closed enumeration and test-isolation controls remain present.

These local controls do not offset the Critical current-disposition recovery omission.

## Verification executed

| Check | Exact result |
|---|---|
| Review-start Git state | `main`; HEAD and `origin/main` both `d0acd721d4af534c2b1313565ff3677cfb59e73e`; clean real index/worktree |
| Delta from prior reviewed source `3bd8f23..d0acd72` | 8 commits; 24 files; 1,227 insertions, 208 deletions |
| Existing project virtual environment | Python 3.11.15; `pip check`: no broken requirements |
| Focused changed-path suite before documentation | **245 passed, 33 subtests passed** in 43.19s |
| Current-protocol recovery omission probe | **2 passed** in 0.22s; both tests assert the unsafe ignored-memo behavior described above |
| Staged-candidate configured gates before final prose corrections | `pip check`, byte-compilation, Markdown links, staged 282-line token inventory and cached whitespace: pass; full suite **303 passed, 33 subtests passed** in 61.80s; standalone recovery **16 passed, 8 subtests passed**; recovery→SDK **17 passed, 8 subtests passed**; receipt→payout→fee→SDK **70 passed** |
| Dependency advisories | `pip-audit` is not installed in the project environment; no fresh advisory-scan claim |
| Reviewed source manifest | All 19 listed CI/dependency/runtime/test inputs matched SHA-256 |
| Exact publication-head GitHub Actions | Not available: final staging/publication blocked by command approval; no review commit or push |
| Target Nexus/Solana live matrix | **Not run** |

The first focused-suite attempt without the repository's synthetic configuration failed collection because `SOLANA_RPC_URL` was absent; it is not acceptance evidence. The successful local runs use explicit non-routable/local synthetic addresses, `/bin/false` for the Nexus CLI and no production credentials. No chain mutation was possible or attempted.

## Repair order and executable acceptance

1. **P0:** make startup recovery recognize and exactly validate current refund/quarantine transactions; reconstruct their cap and terminal/source evidence or refuse startup.
2. **P1:** preserve an owner-independent receipt outbox row without coupling exact payout settlement to provider availability or NXS publication.
3. **P1:** add durable actionable cap-held reasons and alerts for refund/quarantine reservation refusal.
4. **P1:** make dashboard durable-exposure failure explicit rather than silently authoritative-looking.
5. **P1:** execute the target Nexus/Solana finality, pagination, timeout, crash/restart, migration, reconciliation and mixed-kind cap matrix on the exact candidate.
6. **P2:** rehearse alert failure, hold escalation, two-person disposition, SQLite/WAL backup/restore and key rotation.

Production and real-fund admission remain hard-blocked. Documentation review does not authorize a deployment or financial operation.

## Publication scope

Only these review documents are intended for the documentation commit:

- `CONFIG.md`
- `docs/EVALUATION.md`
- `docs/STATE_MACHINES.md`
- `docs/DEVELOPMENT_REVIEW_2026-09-12.md`
- `docs/TOKEN_PAIR_LITERAL_INVENTORY.md`
- `docs/DEVELOPMENT_REVIEW_2026-09-12.sha256`

The reviewed-file manifest excludes itself. Final staging/publication was blocked by an explicit command-approval denial and was not retried or rerouted. No review commit or push was created. The six review documents are staged, with subsequent review-report prose corrections still unstaged; this is not a fully staged final publication candidate. A later authorized publication must stage the final named documents and rerun index-aware checks. Parent review independently confirmed the scanner's legacy-only disposition branches and primary-only cap rebuilder in current source.
