# swapService Independent Repository Evaluation — 2026-09-07

**Review window:** changes after the 2026-09-05 review commit
**Baseline:** `2e10f292`
**Reviewed committed head:** `6568446d551c785a2b96d16217b468e78a93defd`
**Committed range:** 4 commits; 7 files; 395 insertions and 191 deletions
**Pre-review worktree:** `main` matched `origin/main`; only `tests/test_critical_safety.py` was modified and unstaged
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The reviewed commits make two material local repairs. Incoming Nexus queue and terminal tables now use `(txid, contract_id)`, and both live admission and Nexus reconstruction preserve two treasury CREDIT siblings independently. The migration retains legacy txid-only rows under the explicit `contract_id=-1` sentinel. Recovery's producer also rejects missing/blank txids, non-built-in-positive timestamps, non-built-in non-negative confirmation counts, invalid contract ids, missing credit sources and non-exact amounts instead of returning a complete scan.

Those repairs close the prior malformed-recovery-schema finding locally and close the narrow live/rebuild multi-CREDIT admission defect. They do **not** complete contract identity end to end. The operator transfer-intent schema and CLI still identify a held source only by txid, and finalization queries and deletes every source row sharing that txid. An executed local probe finalized sibling 0, deleted both sibling source rows and archived only one `contract_id=-1` refund row. Wipeout payout reconstruction also parses the new `nexus_txid:<txid>:<contract_id>` memo as one opaque txid and archives it under `contract_id=-1`; Nexus reconstruction then requeues the real `(txid, contract_id)` credit. That can make an already-paid credit payable again after database loss.

Mutable offset pagination and non-latching startup recovery also remain open. The preserved working-copy test correctly specifies repeatable pagination evidence, but it is red against committed code: a changed second pass is never requested and `fetch_deposits_since()` returns `complete=True`. `main.run()` still prints and ignores returned recovery errors and exceptions before entering the service loop.

## Change assessment

| Commit | Assessment |
|---|---|
| `594a111` — persist credit contract identities | **Partial repair.** Composite identity is correct in live admission, recovery admission, queue rows, payout memo construction and normal processing. It is absent from operator intent source identity, fee journal identity and Solana wipeout reconstruction. |
| `5714df3` — retain terminal credit contract identity | **Partial repair.** Normal processed/refunded/quarantined helpers and duplicate sets can address exact siblings, but operator finalization's direct SQL omits `contract_id` and deletes by txid. The dormant `_quarantine_txid()` helper also still archives under the `-1` default. |
| `6568446` — reject malformed Nexus credit scans | **Repaired locally.** Producer-level malformed qualifying-credit fixtures pass. Target-node response-shape acceptance remains required. |
| `a62106a` — refresh token-pair inventory | **Documentation maintenance only.** The index-aware inventory is current at the committed head. |

## Severity-ordered findings

### Critical — operator finalization can erase a valid CREDIT sibling

The newly migrated lifecycle tables permit multiple rows per txid, but `nexus_transfer_intents` remains unique on `source_txid`; deterministic intent id/reference generation also hashes only `source_txid` (`src/state_db.py:242-267`, `:501-509`, `:541-599`). The operator CLI accepts only `--txid`, and `_held_credit()` rejects anything other than exactly one matching row (`nexus_transfer_operator.py:29-38`, `:129-133`). Two held siblings are therefore not independently selectable or authorizable.

More seriously, finalization reads `unprocessed_txids WHERE txid = ?`, inserts a refund/quarantine row without `contract_id`, and executes `DELETE FROM unprocessed_txids WHERE txid = ?` (`src/state_db.py:794-855`). In the local SQLite probe, finalizing a 3,000,000-unit refund for contract 0 returned `true`, removed both contract 0 and the unrelated 4,000,000-unit contract 1 row, and left only `("shared-credit", -1, 3.0)` in `refunded_txids`.

**Required exit:** migrate transfer-intent source identity, intent id/reference, operator selection, audit evidence, finalization lookup, terminal insert and source deletion to exact `(source_txid, source_contract_id)`. Refuse legacy `-1` rows without explicit manual disposition. Prove two held siblings can be prepared and finalized independently and that finalizing either cannot mutate the other.

### Critical — database-wipeout payout recovery does not decode the composite memo

The payout path now writes `nexus_txid:<txid>:<contract_id>` (`src/swap_nexus.py:374-378`). Both Solana memo scanners retain everything after `nexus_txid:` as one dictionary key (`src/solana_client.py:1818-1824`, `:1946-1953`). Startup reconstruction treats that key as a txid and calls `mark_processed_txid()` without a source contract id (`src/startup_recovery.py:192-220`, `:281-312`).

The local wipeout probe reconstructed `("paid-credit:1", -1, "solana-payout-sig", "processed")`; the subsequent authoritative Nexus rebuild independently queued `("paid-credit", 1, 3000000)`. The synthetic marker cannot suppress the actual paid source identity. Processing that queue can issue a second Solana payout.

**Required exit:** define one strict memo DTO with backward-compatible parsing for legacy `nexus_txid:<txid>` and exact parsing for `nexus_txid:<txid>:<contract_id>`. Recovery must archive/check the real composite source identity and must not create zero-evidence terminal rows. Add a wipeout round trip proving an already-paid sibling is never queued while an unpaid sibling remains recoverable.

### High — recovery pagination can still assert completeness over a mutable range

`fetch_deposits_since()` uses `offset=page*100` and returns complete on a short/empty page or an old timestamp (`src/nexus_client.py:2056-2133`). It has no snapshot id, cursor, stable high boundary, repeat scan comparison, monotonic-order proof, duplicate policy or equal-timestamp boundary rule.

The pre-existing dirty test `test_recovery_deposit_scan_requires_stable_repeatable_pagination_evidence` supplies a changed second scan. Current code consumes only the first two pages and incorrectly returns `complete=True`; the focused run fails exactly on that assertion. This working-copy test is valid evidence of an unimplemented requirement, not committed behavior.

**Required exit:** use a target-proven stable cursor/snapshot protocol, or compare a fully bounded range against an immutable anchor and fail closed on any change. Until then, all multi-page wipeout recovery remains incomplete.

### High — startup recovery failure is still not an exposure latch

`perform_startup_recovery()` returns explicit errors for malformed heartbeat and incomplete Nexus scans, but missing heartbeat/all-zero checkpoints still use a bounded recent fallback (`src/startup_recovery.py:363-459`). `main.run()` prints the returned summary without checking `error`, `recovery_incomplete` or per-chain completeness; exceptions are printed and ignored (`src/main.py:355-361`). The later reconciliation latch cannot discover a source liability omitted by recovery.

**Required exit:** abort startup non-zero or latch a distinct recovery pause before any poller can create exposure. Missing heartbeat, zero checkpoints, bounded fallback, incomplete chain enumeration and reconstruction exceptions must all remain blocked until explicit complete evidence or an attributable manual recovery disposition exists.

### High — fee accounting is not composite or crash-atomic

Nexus fee writes carry `txid` but no `contract_id`, and the fee journal has no uniqueness constraint connecting an entry to a source CREDIT (`src/state_db.py:310-320`; `src/startup_recovery.py:134-150`; `src/swap_nexus.py:794-873`). Fee insertion and terminal classification are separate transactions. A crash after `add_fee_entry()` and before `mark_processed_txid()` can duplicate the fee during recovery; sibling fees sharing one txid are not individually attributable.

**Required exit:** add source contract identity and a uniqueness rule to fee evidence, and commit fee classification plus terminal source state atomically. Prove crash/restart and two fee siblings do not duplicate or collapse entries.

## Closed or positively verified controls

- Composite primary keys and migration preserve legacy rows under `contract_id=-1` and admit new siblings without collision.
- Live admission and Nexus reconstruction retain two payable treasury CREDIT contracts independently.
- Normal processed/refunded lookup helpers can query exact contract identities; a refunded sibling does not suppress another sibling.
- Recovery producer rejects the malformed qualifying-credit fields covered by `6568446`.
- Existing automatic Nexus refund/quarantine execution remains disabled in the service loop.
- No live RPC request, transaction, transfer or fund operation was performed in this review.

These are local controls. The canonical account-history projection, contract-id shape, ordering, pagination, equal timestamps, finality and POST transport remain unverified on the target Nexus node.

## Verification

| Check | Exact result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` on the preserved working tree | **FAIL — 1 failed, 157 passed, 25 subtests passed in 19.00s**; only the uncommitted stable-pagination test failed |
| Focused six-test credit/recovery set | **FAIL — 1 failed, 5 passed, 129 deselected, 6 subtests passed in 1.22s**; failure is the same uncommitted pagination test |
| Committed credit identity, migration, terminal sibling and malformed producer cases within that focused run | **PASS** |
| `PYTHONPYCACHEPREFIX=/tmp/swapservice-compile-cache python3 -m compileall -q src *.py tests` | **PASS** |
| `python3 -m pip check` | **PASS — No broken requirements found** |
| `python3 scripts/check_markdown_links.py` before documentation edits | **PASS — Local Markdown links: OK** |
| `python3 scripts/check_token_pair_inventory.py` before documentation edits | **PASS — 651 active lines** |
| `git diff --check 2e10f292..HEAD` and `git diff --check` before documentation edits | **PASS** |
| `ruff`, `pyflakes`, `mypy`, `pip-audit`, `bandit` | **Not installed in the local environment** |
| Local mocked SQLite/operator/wipeout probes | **Reproduced both Critical defects described above**; Solana/Requests dependencies were stubbed, not a live-adapter verification |
| Initial standalone probe import | **BLOCKED by missing `solders`** before dependency stubbing; `pip check` alone does not prove required runtime imports are installed |
| Committed-head GitHub Actions | **PASS** at `6568446d551c785a2b96d16217b468e78a93defd`: [run 33984473153](https://github.com/distordialabs-brutus/swapService/actions/runs/33984473153). This excludes the dirty pagination test and today's uncommitted docs. |
| Target Nexus/Solana live matrix | **Not run** |

A temporary attempt to execute the exact committed tree from `git archive` was blocked before execution by the command-safety layer because the compound command included temporary-directory removal. It was not retried. The working tree differs from committed `6568446` only by the one added pagination test; the full run passed all 157 other collected tests and failed that added test.

## Required repair order

1. **P0:** extend source identity through operator intents/finalization; eliminate every txid-wide read/delete and terminal insert lacking source `contract_id`.
2. **P0:** decode and reconstruct composite Nexus payout memos so wipeout recovery cannot repay an already-paid credit.
3. **P0:** make startup recovery failure abort or latch exposure independently of reconciliation.
4. **P0:** replace mutable offset recovery completeness with target-proven stable-range evidence; keep the preserved red test.
5. **P1:** make Nexus fee classification composite and atomic with terminal source state.
6. **P1:** run migration, two-sibling mixed-disposition, duplicate-page, equal-timestamp, crash/restart and malformed-response fixtures through the full gate.
7. **P1:** run the target Nexus account-history/pagination/finality/timeout matrix and Solana devnet/testnet matrix on the exact candidate commit.
8. Keep automatic Nexus refunds/quarantines disabled and admit no real funds until all release gates pass.

## Publication status and remaining document gate

The parent fetched origin and verified the base branch was aligned. These review updates remain local and unstaged, with the pre-existing red pagination test untouched. Command approval denied verification/setup commands during this unattended review and prohibited further irreversible actions, so no commit or push was attempted.

Before later publication, explicitly stage only the intended documentation, refresh `docs/TOKEN_PAIR_LITERAL_INVENTORY.md` for shifted active lines in `docs/EVALUATION.md` and `docs/STATE_MACHINES.md`, then rerun the index-aware inventory check, Markdown links, whitespace checks and full suite. The pre-edit 651-line inventory pass does not validate this unstaged documentation candidate. Do not accidentally include or delete the coder-owned pagination regression test. Verify the exact replacement remote SHA and its CI after any authorized publication.
