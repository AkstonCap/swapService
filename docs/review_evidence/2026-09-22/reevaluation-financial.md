# Independent financial architecture re-evaluation — swapService `85030c8`

## Verdict

**BLOCKED for production/release acceptance.** The previously accepted local-database A/B/C controls remain green, but one consequential current-integration gap is proven: after total SQLite loss, the externally pinned Solana waterline reconstructs the raw incoming source but not B's frozen policy decision or C's frozen capacity-hold intent. Startup can return `recovery_complete=True`; later production ingestion recreates the source as new work and current configuration can authorize a different route, destination, payout, and fee.

No live network, real key, secret, install, repository edit, stage, or commit was used. External boundaries were mocked and all databases were temporary.

## 1. Critical fund-loss / skipped-deposit paths

No additional direct duplicate-send or skipped-deposit path was proven in the bounded review. The high-severity contract mutation below is nevertheless a release blocker because it authorizes a side effect that contradicts previously frozen disposition evidence.

## 2. High — total DB loss discards frozen B/C obligations and current policy can reroute retained principal

**Classification:** latest A/B/C integration gap/regression at `85030c890fa6f3bb7db97e068e5cf80827d21b28`, not an inherited malformed-storage hypothesis. B and C are new/current local durable protocols, but wipeout recovery preserves only their source through the older waterline mechanism. The path is reachable after ordinary cap refusal followed by loss of the SQLite DB/WAL.

### Production caller path

- `src/state_db.py:108-119` stores B's `policy_decision` and `policy_evidence` only in `unprocessed_sigs`; `src/state_db.py:504-516` stores C's frozen retry only in `solana_payout_capacity_holds`.
- `src/state_db.py:2306-2375` correctly freezes a decision across ordinary process restart while the DB survives.
- `src/swap_solana.py:43-74` pins the published waterline behind the oldest retained source. This preserves raw chain discoverability after DB loss, not local frozen terms.
- `src/startup_recovery.py:343-481` reconstructs outgoing Solana memo evidence; `src/startup_recovery.py:607-843` can return complete without reconstructing an unsent policy/cap hold (there is no outgoing memo yet).
- On later incoming re-enumeration, `src/state_db.py:1742-1777` inserts the source as `ready for processing` with null policy fields.
- `src/solana_client.py:1049-1063` then classifies against **current** config and freezes a new decision. If still refundable, `src/solana_client.py:1329-1390` also recomputes current refund fee and destination before creating a new disposition intent.
- `src/main.py:296-317` treats the preceding startup result as the admission gate, so the absence of recovered frozen B/C evidence is not itself a startup latch.

### Reproduction A — frozen oversized refund becomes a Nexus mint

Script: `/tmp/swap-reeval-db-loss-probe.py`

1. Current production workers classified a 1,100-unit source under max 1,000 as `refund_oversized`.
2. Refund cap 50 produced a typed C hold for 1,090 units; zero Solana sends; full 1,100-unit liability remained.
3. The real waterline helper published 879, behind source timestamp 1,000.
4. The SQLite DB/WAL was removed. Empty-chain startup scans were supplied through the real `perform_startup_recovery()` caller; it returned complete.
5. The source was re-admitted through the real scan-page commit. After max changed to 2,000, the real deposit worker classified it `payable` and invoked the Nexus debit boundary for 1,100 units.

Observed result:

```text
before_wipe:
  source = ["refund capacity held", "refund_oversized", 1100]
  capacity_hold = ["refund", 1090, 50]
  capacity_evidence_present = true
  liability_units = 1100
  solana_send_calls = 0
waterline:
  source_timestamp = 1000
  published = 879
  behind_source = true
after_total_db_loss:
  startup_recovery_complete = true
  startup_recovery_error = null
  source_re_admitted = 1
  capacity_hold_count = 0
  deposit_worker = [1, 0, 0, 0]
  nexus_send_calls = 1
  nexus_send_amount = 1100
  source = ["debited, awaiting confirmation", "payable", 1100, 1100,
            "nexus-debit-after-wipe"]
```

### Reproduction B — same refund route sends changed C terms

Script: `/tmp/swap-reeval-cap-wipe-probe.py`

The policy remained oversized/refund across wipeout, isolating C's frozen intent loss. Before loss, cap refusal froze destination `original-destination`, payout 1,090, and fee 10. After DB loss/re-ingestion, cap was raised and mutable refund fee/destination changed. The real refund worker submitted 1,080 to `changed-destination` instead of promoting the original frozen intent.

```text
before_wipe:
  send_calls = 0
  destination = original-destination
  payout_units = 1090
  fee_units = 10
  refund_fee_term = 10
after_total_db_loss_and_reingestion:
  worker_submitted = 1
  send_calls = 1
  send_args = ["changed-destination", 1080]
  source_status = "refund sent, awaiting confirmation"
  destination = changed-destination
  payout_units = 1080
  fee_units = 20
  refund_fee_term = 20
```

This is not based on corrupt or manually malformed storage: all initial rows were created by production classifiers/workers, the hold was a normal typed cap refusal, and the only destructive event was the explicitly in-scope total database loss.

## 3. Test/evidence gap

The focused existing suite remains green but covers process restart with the same database, not SQLite wipeout of unsent policy/cap holds:

```text
.venv/bin/python -m pytest -q \
  tests/test_solana_deposit_policy.py \
  tests/test_solana_capacity_holds.py \
  tests/test_recovery_acceptance.py \
  tests/test_recovery_safety.py

151 passed, 52 subtests passed in 10.03s
```

A narrower control selection also passed:

```text
6 passed, 4 subtests passed in 0.68s
```

It covered B config drift with a surviving DB, live/recovery ingestion equivalence, C frozen retry with a surviving DB, and chain-only terminal-disposition containment. Those controls are valid but do not exercise an **unsent** B/C hold through complete DB/WAL loss.

No live provider/finality semantics were tested or approved.

## 4. Operational/custody hardening

Until repair, a nonzero heartbeat waterline plus an empty/recreated local database must not authorize normal processing of recovered incoming Solana sources. Operational recovery should restore a verified DB+WAL/online backup or remain paused for explicit reconciliation; merely re-enumerating source signatures is insufficient to recover their economic contract.

## 5. Positive controls verified

- A's strict terminal disposition provenance and chain-only terminal containment remain represented in the current caller path and focused tests.
- B retains full principal and frozen policy evidence across ordinary restart/config drift while SQLite survives.
- C retains full principal, uses typed cap outcomes, and retries original frozen intent across ordinary restart while SQLite survives.
- The waterline correctly remained behind the unresolved source, so raw principal was discoverable after loss; the defect is loss of frozen authorization, not disappearance of the deposit.
- Focused A/B/C/recovery suite passed as recorded above.

## 6. Executable repair order and exit criteria

### P1 — fail closed on wipeout-recovered sources lacking historical frozen authorization

Implement one of these reviewed contracts before release:

1. reconstruct exact policy/cap-hold evidence from an authoritative durable source outside the lost SQLite files; or
2. when a nonzero existing-deployment heartbeat is paired with a recreated/empty database, admit each recovered incoming source only into a quantified, non-sendable recovery-evidence hold until exact historical disposition is restored or independently authorized.

Do not silently regenerate B/C terms from current config.

Required acceptance, using real startup, scan-page, deposit, refund, and quarantine callers with only external chain/address/send boundaries stubbed:

1. Create below-minimum and non-positive-output policy holds, delete DB/WAL, drift minimum/fees, and replay from the pinned waterline. Require full principal liability, zero Nexus/Solana transport, and an operator-visible fail-closed hold (or incomplete startup).
2. Create oversized-refund and invalid-destination refund/quarantine capacity holds, delete DB/WAL, drift max/refund fee/destination/cap, and replay. Require zero transport unless the **original** exact frozen intent is authoritatively restored.
3. Restore from verified online backup and copied DB+WAL fixtures; after legitimate cap release, require the original destination/amount/memo/fee exactly once.
4. Run the same cases across more than one page and beyond worker limits; the oldest unresolved recovered source must remain quantified and must not starve later safe evidence.
5. Assert startup result, dashboard issue, liability total, policy evidence, capacity hold, reservation/submission journals, and both remote call counts—not only helper return values.

## Identity and hashes

```text
HEAD:       85030c890fa6f3bb7db97e068e5cf80827d21b28
HEAD tree:  a89d8904a200cafce86a5ecd002fa90978f2be13
index tree: a89d8904a200cafce86a5ecd002fa90978f2be13

a226b450fe689fed1b121c76ece8903ea05dbf68f67f7bdc61cdc8d973fc97c1  src/state_db.py
a607bb0594be61c0a9e2429f178df8c8711e1b17572dcad9d2ba32baabed84f3  src/solana_client.py
184dea5d83396a74667988e3b300b892152b1c23f5f385d6293282ed6f95c84e  src/solana_deposit_policy.py
919e1127a5b914d360ac45c33d97d787e30500fc0f43bd5a3d0e1541806aae8b  src/startup_recovery.py
3cf6cdaa8e5170992fdc384cc25fffaf6508ec42ab50eb0cca34e9e17065f8e5  src/swap_solana.py
889cef2aa6f062ffce16ee1cba064707202e3cefe8c45ecf4f94c60c95d9bdad  tests/test_solana_deposit_policy.py
a0afaa85d20a80222f05bff5c61bc4d353163f22f00a4e43219921b9b984bbb3  tests/test_solana_capacity_holds.py
5f8ea97c578fe995abb565a8df75366bdc0a4e1e851b5a750a723c3cf746112b  tests/test_recovery_safety.py
84712e6af0b0b4d9bfcdb813dbf0ce402cace84a4d867c78a94f2bf58b2cb136  /tmp/swap-reeval-db-loss-probe.py
559dccaa089d9079e8cb586fdb116f9eed1089a313bc6fa43d823c3e6b5b4a87  /tmp/swap-reeval-cap-wipe-probe.py
```

The repository was clean at review start. During the review, parent-owned documentation changes appeared in `docs/RECOVERY_INPUT_CAP_ACCEPTANCE.md`, `docs/STATE_MACHINES.md`, and untracked `docs/POST_CHANGE_REVIEW_2026-09-22_PRE_REEVALUATION.md`; this reviewer did not create or modify them. Runtime/test paths above remained at the recorded HEAD hashes. `git diff --check` passed.