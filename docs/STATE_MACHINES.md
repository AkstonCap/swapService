# Swap Service State Machines

State machine diagrams for both swap directions in the bidirectional USDC ↔ USDD swap service.

> **Accuracy note (2026-06-15):** this document was re-derived directly from the code. The
> previous version contained transitions the code does not perform (notably an
> auto-refund on debit timeout, and a refund on USDC-confirmation timeout) and omitted the
> ambiguity-resolution states. Status strings below are copied verbatim from the source.
>
> **Resolution note (2026-08-24):** the independent review findings about
> bounded/failed Nexus lookups, fail-open backing checks, and Nexus waterline
> advancement are repaired in the working tree and covered by
> `tests/test_critical_safety.py`. Missing debit and receival-asset lookup values are never actionable
> automatically; incomplete enumeration holds; and only the poller may
> advance a Nexus checkpoint from scan evidence. Live-chain verification is
> still required.
>
> **Weekly review update (2026-08-28, `f614897`):** automatic Nexus refunds and
> treasury-to-quarantine movements remain disabled in the service loop. A separate
> intent-first operator workflow now persists one disposition per source credit,
> requires audited preparation/authorization/execution request, executes once, holds
> ambiguous outcomes, resolves only an exact positive reference/source/destination/
> amount/txid match, and archives only after explicit remote-txid confirmation. This
> protocol and empty Nexus enumeration semantics have not passed the target-node
> crash/pagination matrix, so they are release gates rather than production evidence.
>
> **Weekly review update (2026-08-29, committed `5e7d3b8` plus a separately
> staged proposal):** startup demotes persisted `executing` Nexus transfer
> intents to `outcome_unknown`, and explicit production mode requires payout
> caps and an alert route. Reconciliation still does not latch an exposure pause
> on error/unhealthy output. The staged remote scan includes active recipients
> and refuses multi-page ambiguity, but its exact valid new-recipient case and
> target-node one-page boundary/order semantics remain unproven. See
> `DEVELOPMENT_REVIEW_2026-08-29.md`.
>
> **Weekly review update (2026-08-31, `cc175cb`):** reconciliation now
> latches an exposure pause until an explicit healthy read-back; malformed
> production mode and zero-exit admission are fixed; and submitted transfer
> txids are immutable. Production remains hard-blocked. Empty successful Nexus
> enumeration can still advance the waterline; exact debit resolution collapses
> multiple contracts in one txid; confirmation-count polling does not read back
> the submitted mint's full contract terms; and remote reconciliation cannot
> prove more than one page. See `DEVELOPMENT_REVIEW_2026-08-31.md`.
>
> **Follow-up review (2026-08-31 16:16, `368b064`):** empty enumeration now
> holds, returned evidence preserves contract ids, and confirmation polling
> attempts full-term read-back. The implementation still treats one candidate
> from an incomplete bounded lookup as globally unique, does not normalize the
> target API's nested endpoint-address objects, compares a register address to
> the token-name label, and omits contract id from terminal durable state.
> Production remains hard-blocked; see `DEVELOPMENT_REVIEW_2026-08-31_1616.md`.
>
> **Follow-up review (2026-09-01, `aa71066`):** incomplete reference scans
> now hold, returned endpoint objects are normalized, submitted txids are read
> directly, terminal rows retain `contract_id`, production requires the token
> register and a multiuser session, and logging/common-transport gaps are locally
> repaired. The direct transfer resolver nevertheless terminalizes a transaction
> without checking confirmations; reference-only unknown outcomes have no complete
> stable-range path; and completed-mint reconciliation does not consume the stored
> contract id or require the configured token-register source. Production remains
> hard-blocked; see `DEVELOPMENT_REVIEW_2026-09-01.md`.
>
> **Follow-up review (2026-09-02, `8f9a30f`):** completed-mint reconciliation
> now consumes the persisted contract id and configured token-register source,
> and direct txid lookup holds below the configured confirmation threshold. The
> threshold is not constrained positive, reference-only evidence has no finality
> field, zero-input/positive-output terminal mints can reconcile healthy, and the
> target-node matrix remains unrun. Production remains hard-blocked; see
> `DEVELOPMENT_REVIEW_2026-09-02.md`.

---

## USDC → USDD State Machine (Solana to Nexus)

```mermaid
flowchart TD
    START((Start)) --> Detected[USDC deposit fetched at 'finalized']
    Detected --> ReadyForProcessing["ready for processing"]

    ReadyForProcessing -->|invalid memo / bad Nexus account / over MAX_SWAP_USDC| ToBeRefunded["to be refunded"]
    ReadyForProcessing -->|"net after fees ≤ 0"| ProcessedAsFees["processed, amount after fees <= 0 ✓"]
    ReadyForProcessing -->|"reserve + persist reference"| DebitInFlight["debit in flight"]

    DebitInFlight -->|CLI returned a txid| DebitedAwaiting["debited, awaiting confirmation"]
    DebitInFlight -->|"exception / timeout / unparsable body"| DebitUnverified["debit unverified"]

    DebitUnverified -->|"reference-only scan is failed / bounded / not snapshot-stable"| DebitUnverified
    DebitUnverified -->|no reference recorded| ToBeQuarantined["to be quarantined"]

    DebitedAwaiting -->|">= min confirmations + one returned same-tx exact contract"| Processed["debit_confirmed ✓"]
    DebitedAwaiting -->|"missing / failed confirmation evidence"| DebitedAwaiting

    ToBeRefunded -->|"net ≤ 0"| ProcessedAsFees
    ToBeRefunded -->|"no/invalid sender address"| ToBeQuarantined
    ToBeRefunded -->|USDC refund sent| RefundSent["refund sent, awaiting confirmation"]
    ToBeRefunded -->|send failed| ToBeQuarantined
    RefundSent -->|finalized| RefundConfirmed["refund_confirmed ✓"]

    ToBeQuarantined -->|USDC moved to quarantine| QuarantineSent["quarantine sent, awaiting confirmation"]
    ToBeQuarantined -->|send failed| QuarantineFailed["quarantine failed ✗"]
    QuarantineSent -->|finalized| QuarantineConfirmed["quarantine_confirmed ✓"]

    Stale["age > STALE_DEPOSIT_QUARANTINE_SEC<br/>(while 'ready for processing')"] --> ToBeQuarantined
```

### USDC → USDD State Descriptions

| State | Description | Table | Status value |
|-------|-------------|-------|--------------|
| **Detected** | Deposit fetched from Solana (at `SOLANA_DEPOSIT_COMMITMENT`, default `finalized`) | `unprocessed_sigs` | `"ready for processing"` on insert |
| **ReadyForProcessing** | Awaiting validation + debit | `unprocessed_sigs` | `"ready for processing"` |
| **DebitInFlight** | Reference persisted, Nexus debit issued, outcome not yet known | `unprocessed_sigs` | `"debit in flight"` |
| **DebitUnverified** | Debit outcome **ambiguous** — resolved against the chain, never guessed | `unprocessed_sigs` | `"debit unverified"` |
| **DebitedAwaiting** | A submitted txid is stored. After the separate confirmation count reaches the threshold, direct txid read-back must match reference, configured token-register source, destination and exact units; endpoint objects are normalized and the selected contract id is retained. Missing or failed evidence remains held. Target-node response/finality behavior remains a release gate. | `unprocessed_sigs` | `"debited, awaiting confirmation"` |
| **Processed** | One exact DEBIT contract passed the local confirmation/read-back checks; terminal evidence includes `txid` and `contract_id` | `processed_sigs` | `"debit_confirmed"` |
| **ProcessedAsFees** | Amount after fees ≤ 0 | `processed_sigs` | `"processed, amount after fees <= 0"` |
| **ToBeRefunded** | Validation failed or amount exceeds the configured cap; ambiguity alone never refunds | `unprocessed_sigs` | `"to be refunded"` |
| **RefundSent** | USDC refund broadcast | `unprocessed_sigs` | `"refund sent, awaiting confirmation"` |
| **RefundConfirmed** | Refund finalized | `refunded_sigs` | `"awaiting confirmation"` → `"refund_confirmed"` |
| **ToBeQuarantined** | Refund impossible or attempts spent | `unprocessed_sigs` | `"to be quarantined"` |
| **QuarantineSent** | USDC moved to `USDC_QUARANTINE_ACCOUNT` | `unprocessed_sigs` | `"quarantine sent, awaiting confirmation"` |
| **QuarantineConfirmed** | Quarantine finalized | `quarantined_sigs` | `"awaiting confirmation"` → `"quarantine_confirmed"` |
| **QuarantineFailed** | Quarantine send failed | `unprocessed_sigs` | `"quarantine failed"` |

> **Ambiguity is never treated as failure.** `debit_nexus_token_with_txid()` returns `(False, None)`
> both when the CLI failed *and* when it succeeded but the response could not be parsed.
> Refunding on that signal would mint USDD **and** return the USDC. Instead the row goes to
> `debit unverified` and `resolve_unverified_debits()` asks the chain, keyed on the unique
> per-attempt `reference` persisted *before* the call.

---

## USDD → USDC State Machine (Nexus to Solana)

```mermaid
flowchart TD
    START((Start)) --> Credit[USDD credit to treasury detected]

    Credit -->|"< DUST_CREDIT_USDD"| Ignored["ignored entirely — no row, no accounting"]
    Credit -->|"dust ≤ amount < MIN_CREDIT_USDD"| FeesRecorded["processed as fees ✓<br/>(recorded: sender, amount, txid)"]
    Credit -->|"amount ≤ flat + dynamic fee"| FeesRecorded
    Credit -->|"> MAX_SWAP_USDD"| RefundPending["refund pending"]
    Credit -->|normal| Pending["pending_receival"]

    Pending -->|"asset found, owner matches, valid USDC account"| Ready["ready for processing"]
    Pending -->|"lookup failed / malformed / incomplete"| Pending
    Pending -->|owner mismatch| Pending
    Pending -->|"complete mapping has invalid receival_account"| RefundPending
    Pending -->|"complete absence after REFUND_TIMEOUT_SEC"| TradeBal["trade balance to be checked"]

    Ready -->|"vault cannot cover payout"| Ready
    Ready -->|"net ≤ 0"| FeesRecorded
    Ready -->|attempt| Sending["sending"]
    Ready -.->|"paused (backing deficit)"| Ready

    Sending -->|"USDC sent, sig stored"| Awaiting["sig created, awaiting confirmations"]
    Sending -->|"failed, attempts left"| Sending
    Sending -->|"failed, attempts spent"| RefundPending
    Sending -->|"crash recovery: memo found"| Awaiting

    Awaiting -->|"stored sig finalized"| Processed["processed ✓"]
    Awaiting -->|"not confirmed and age > SOLANA_CONFIRM_TIMEOUT_SEC"| Quarantined["quarantined — manual review ✗"]

    TradeBal -->|asset appeared| Ready
    TradeBal -->|"lookup failed / malformed / incomplete"| TradeBal
    TradeBal -->|"complete lookup still absent"| RefundHold["refund held for operator review"]

    RefundHold -->|"operator prepares, confirms reference and authorizes"| IntentAuthorized["durable transfer intent authorized"]
    IntentAuthorized -->|"one CLI attempt"| IntentOutcome["submitted / outcome_unknown"]
    IntentOutcome -->|"submitted txid: direct exact contract match<br/>at configured confirmation threshold"| IntentCompleted["completed"]
    IntentOutcome -->|"outcome_unknown: bounded reference scan"| IntentOutcome
    IntentCompleted -->|"named operator confirms remote txid"| Disposition["refunded or quarantined ✓"]

    Collecting -->|legacy state| RefundHold
    RefundPending -->|legacy state| RefundHold
```

### USDD → USDC State Descriptions

| State | Description | Table | Status value |
|-------|-------------|-------|--------------|
| **Ignored** | Below `DUST_CREDIT_USDD` — spam floor, deliberately no trace | — | — |
| **FeesRecorded** | Below `MIN_CREDIT_USDD` or ≤ fees; **recorded** so funds stay traceable | `processed_txids` | `"processed as fees"` |
| **Pending** | Credit queued, awaiting asset mapping | `unprocessed_txids` | `"pending_receival"` |
| **Ready** | Mapping resolved and owner-verified | `unprocessed_txids` | `"ready for processing"` |
| **Sending** | USDC send attempted | `unprocessed_txids` | `"sending"` |
| **Awaiting** | Signature stored, awaiting finality | `unprocessed_txids` | `"sig created, awaiting confirmations"` |
| **Processed** | USDC delivered | `processed_txids` | `"processed"` |
| **TradeBal** | Legacy mapping-timeout recheck; complete absence now holds | `unprocessed_txids` | `"trade balance to be checked"` |
| **Collecting** | Legacy refund state converted to a hold | `unprocessed_txids` | `"collecting refund"` |
| **RefundPending** | Legacy refund state converted to a hold | `unprocessed_txids` | `"refund pending"` |
| **RefundHold** | Refund/quarantine requires operator review; no automatic Nexus debit | `unprocessed_txids` | `"refund held for operator review"` |
| **IntentAuthorized** | Operator has confirmed the immutable reference and authorized exactly one CLI debit | `nexus_transfer_intents` | `"authorized"` |
| **IntentOutcome** | CLI result is submitted or unknown. A submitted txid can resolve from one exact direct transaction contract only after the configured confirmation threshold; an `outcome_unknown` reference-only row remains held because the live-offset history scan cannot prove a complete range. | `nexus_transfer_intents` | `"submitted"` / `"outcome_unknown"` |
| **IntentCompleted** | Exact txid/contract/reference/endpoints/units are stored after direct lookup reaches the configured threshold. The setting is not yet constrained positive, so production must reject zero/negative values before this is a valid finality guarantee. | `nexus_transfer_intents` | `"completed"` |
| **Disposition** | A named operator confirms the exact remote txid, then the source moves to its terminal archive | transfer + terminal table | `"refund_confirmed_by_operator"` / `"quarantine_confirmed_by_operator"` |
| **Quarantined** | Ambiguous USDC payout confirmation, manual review | `unprocessed_txids` | `"quarantined"` |

> **A USDC-confirmation timeout quarantines — it does not refund.** The USDC may in fact
> have been sent and only the lookup failed; refunding would pay twice.

### Processing Priority Order (`process_unprocessed_txids`)

| Priority | Status handled | Action | Skipped while paused |
|----------|----------------|--------|----------------------|
| 1 | `pending_receival` (confirmations > 1) | Resolve `receival_account` by (`txid_toService`, `owner`) | No |
| 2 | `ready for processing` | Liquidity check, then send USDC with memo `nexus_txid:<txid>` | **Yes** |
| 3 | `sig created, awaiting confirmations` | Confirm the stored signature; memo scan only as fallback | No |
| 4 | `trade balance to be checked` | Retry lookup, else hold for operator review | No |
| 5 | `collecting refund` | Convert legacy state to an operator hold | No |
| 6 | `refund pending` | Convert legacy state to an operator hold | No |

---

## Paused Mode (Backing Deficit)

When `fees.maintain_backing_and_bounds()` reports a deficit (vault USDC below
`BACKING_DEFICIT_PAUSE_PCT`% of circulating USDD), the loop does **not** skip the cycle.
It runs both pollers with `paused=True`:

| Continues | Stops |
|-----------|-------|
| USDC refunds, quarantine, confirmation checks | New deposit ingestion |
| USDD refunds, quarantine, ambiguity resolution | USDC→USDD debits |
| Waterline held (no fetch ⇒ no advance) | USDD→USDC USDC sends |

A failure of the backing check itself also fails safe to paused. A `backing_deficit_pause`
alert is emitted.

---

## Timeouts & Retry

| Timeout | Config | Default | Applies to | Handler |
|---------|--------|---------|-----------|---------|
| Asset-mapping timeout | `REFUND_TIMEOUT_SEC` | 3600s | **USDD→USDC only** | `process_unprocessed_txids()` P1 |
| Debit-confirmation observation window | `SOLANA_CONFIRM_TIMEOUT_SEC` | 600s | USDC→USDD; a negative/incomplete lookup still holds for manual resolution | `check_unconfirmed_debits()` |
| USDC-confirmation timeout | `SOLANA_CONFIRM_TIMEOUT_SEC` | 600s | USDD→USDC → **quarantine** | `process_unprocessed_txids()` P3 |
| Ambiguous Nexus debit | N/A | held until positive reference evidence or manual resolution | USDC→USDD; a negative, failed or incomplete lookup never authorizes an automatic retry/refund | `resolve_unverified_debits()` |
| Stale deposit | `STALE_DEPOSIT_QUARANTINE_SEC` | 86400s | USDC→USDD | `_process_stale_deposits()` |

**Retry:** `MAX_ACTION_ATTEMPTS` (3) attempts, with `ACTION_RETRY_COOLDOWN_SEC` (300s)
enforced between them. `should_attempt()` returns False for *either* reason;
`attempts_exhausted()` distinguishes them, so a cooldown never causes a premature
quarantine. After exhaustion, eligible USDC-side actions may move USDC to
`USDC_QUARANTINE_ACCOUNT`. USDD-side automatic treasury-to-quarantine transfers
remain disabled; the source enters an operator hold and can move only through the
audited durable-intent workflow.

---

## State Persistence

| Table | Purpose |
|-------|---------|
| `unprocessed_sigs` / `processed_sigs` / `refunded_sigs` / `quarantined_sigs` | USDC→USDD lifecycle |
| `unprocessed_txids` / `processed_txids` / `refunded_txids` / `quarantined_txids` | USDD→USDC lifecycle |
| `attempts` | Retry counters + `last_timestamp` (cooldown) |
| `nexus_transfer_intents` / `nexus_transfer_audit_events` | Immutable Nexus debit inputs plus named operator authorization, execution-request and final-disposition evidence |
| `reservations` | Cross-worker mutual exclusion on money actions |
| `counters` | Atomic Nexus debit `reference` sequence |
| `payouts` | Outbound USDC ledger for the rolling 24h cap |
| `fee_entries` / `fee_summary` | Authoritative fee ledger |
| `waterline_proposals` / `heartbeat` | Waterline plumbing and last known-good values |
| `accounts` | Cached balances |

SQLite runs in **WAL** mode (set in `init_db()`).

### Idempotency Guarantees

**USDC → USDD**
- Solana signature is the primary key; `processed`/`refunded`/`quarantined` sets are checked before acting.
- A unique `reference` is persisted **before** each debit and is the on-chain lookup key for ambiguity resolution.
- `reserve_action("usdc_to_usdd_debit", sig)` prevents two workers acting on one deposit.
- Refund/quarantine sends carry `refundSig:<sig>` / `quarantinedSig:<sig>` memos, checked on-chain before a retry re-sends.

**USDD → USDC**
- Nexus txid is the primary key; mapping validated on (`txid_toService`, `owner`).
- USDC sends carry `nexus_txid:<txid>`; the resulting signature is stored in `unprocessed_txids.sig`.
- Startup recovery rebuilds markers from `nexus_txid:`, `refundSig:` and `quarantinedSig:` memos.

---

## Waterline Invariant

`_advance_solana_waterline()` may only move the Solana waterline to a point proven safe:

| Situation | Waterline |
|-----------|-----------|
| Deposit enumeration failed | held entirely |
| Unprocessed deposits exist | pinned behind the oldest |
| Deposit withheld pending finalization | pinned behind it |
| Everything fetched is persisted | `poll_start − HEARTBEAT_WATERLINE_SAFETY_SEC` |
| Candidate ≤ current | unchanged (never moves backwards) |

A waterline read ahead of *now* is clamped. **The waterline must never pass a deposit that
is not durably recorded** — `_fetch_deposits_helius` stops at `ts <= since_ts`, so anything
left behind it is never seen again.

The Nexus poller applies the same proof rule:

| Nexus enumeration state | Waterline |
|---|---|
| CLI exception/non-zero exit, API error, malformed response | **held entirely** |
| Full page budget or processing budget exhausted | held (`pagination_truncated`), even when active rows exist |
| Unprocessed credits exist after a complete poll | poller may pin behind the oldest |
| Complete scan with persisted page data | may advance to the oldest scanned timestamp minus safety |
| Empty successful unfiltered response | **held** (`empty_result`); absence from a live endpoint is not proof of a complete stable range |
| Processing pass | always held; it has no scan evidence and never proposes a waterline |

A missing Nexus transaction or reference is **never** an automatic proof of
non-execution: the history endpoint is live and offset pagination has no snapshot
guarantee. Only a positive txid/reference match is actionable automatically. Negative,
error, and exhausted-pagination results remain `incomplete` and require manual
resolution rather than authorizing retry or refund.

---

## Code Locations

| Component | File | Function |
|-----------|------|----------|
| USDC→USDD polling | `src/swap_solana.py` | `poll_solana_deposits()` |
| Waterline advance | `src/swap_solana.py` | `_advance_solana_waterline()` |
| USDC→USDD processing | `src/solana_client.py` | `process_unprocessed_solana_deposits()` |
| Ambiguity resolution | `src/nexus_client.py` | `resolve_unverified_debits()`, `find_nexus_debit_by_reference()` |
| USDC refunds / quarantine | `src/solana_client.py` | `process_solana_deposits_refunding()`, `process_solana_deposits_quarantine()` |
| USDD→USDC polling | `src/swap_nexus.py` | `poll_nexus_deposits()` |
| USDD→USDC processing | `src/swap_nexus.py` | `process_unprocessed_txids()` |
| USDD quarantine transfer | `src/nexus_client.py` | `quarantine_nexus_token()` |
| Held-credit operator disposition | `nexus_transfer_operator.py` | `prepare`, `authorize`, `execute`, `resolve`, `finalize` |
| Alerting | `src/alerts.py` | `critical()`, `warning()`, `info()` |
| Startup recovery | `src/startup_recovery.py` | `perform_startup_recovery()` |

### Status Constants (`src/swap_nexus.py`)

```python
USDD_STATUS_PENDING          = "pending_receival"
USDD_STATUS_READY            = "ready for processing"
USDD_STATUS_SENDING          = "sending"
USDD_STATUS_AWAITING         = "sig created, awaiting confirmations"
USDD_STATUS_REFUNDED         = "refunded"
USDD_STATUS_PROCESSED        = "processed"
USDD_STATUS_FEES             = "processed as fees"
USDD_STATUS_REFUND_PENDING   = "refund pending"
USDD_STATUS_QUARANTINED      = "quarantined"
USDD_STATUS_TRADE_BAL_CHECK  = "trade balance to be checked"
USDD_STATUS_COLLECTING_REFUND = "collecting refund"
```

USDC-side statuses are string literals in `src/solana_client.py` / `src/nexus_client.py`
(listed in the table above) rather than named constants.

> **Known inconsistency:** `_process_stale_deposits()` also matches a `'memo unresolved'`
> status that no code path ever writes. Harmless, but it is dead.

---

## Monitoring

```sql
-- state distribution
SELECT status, COUNT(*) FROM unprocessed_sigs  GROUP BY status;
SELECT status, COUNT(*) FROM unprocessed_txids GROUP BY status;

-- ambiguous debits needing chain resolution (should drain quickly)
SELECT sig, reference, status FROM unprocessed_sigs
WHERE status IN ('debit in flight','debit unverified');

-- rolling 24h outbound USDC vs cap
SELECT COALESCE(SUM(amount_usdc_units),0) FROM payouts
WHERE timestamp >= strftime('%s','now') - 86400;

-- quarantined USDD actually moved?
SELECT txid, amount_usdd, status FROM quarantined_txids ORDER BY timestamp DESC;
```

Alerts (`ALERT_WEBHOOK_URL` / `ALERT_COMMAND`) fire on: `backing_deficit_pause`,
`unbacked_usdd_surplus`, `heartbeat_unreadable`, `heartbeat_asset_invalid`,
`insufficient_vault_liquidity`, `payout_cap_exceeded`, `swap_over_cap`, `usdd_quarantined`.

---

## References

- User-facing flow: [SWAP_INITIATOR_STATE_MACHINES.md](SWAP_INITIATOR_STATE_MACHINES.md)
- Configuration: [CONFIG.md](../CONFIG.md)
- Security hardening: [SECURITY.md](SECURITY.md)
- Operational setup: [SETUP.md](../SETUP.md)
- Risk assessment: [RISK_ASSESSMENT.md](RISK_ASSESSMENT.md)
