# state_db.py Migration Guide

## Overview
Enhanced `state_db.py` to support complete migration from legacy `state.py` JSONL-based persistence to SQLite.

## New Tables Added

### 1. **reservations**
Prevents duplicate processing with TTL-based locking.
```sql
CREATE TABLE reservations (
    kind TEXT NOT NULL,      -- 'debit', 'credit', 'refund', etc.
    key TEXT NOT NULL,       -- signature or txid
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
)
```

**Functions:**
- `reserve_action(kind, key, ttl_sec=300) -> bool`
- `release_reservation(kind, key)`
- `is_reserved(kind, key, ttl_sec=300) -> bool`
- `cleanup_expired_reservations(ttl_sec=300) -> int`

---

### 2. **attempts**
Tracks retry attempts for failed operations.
```sql
CREATE TABLE attempts (
    action_key TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0,
    last_timestamp INTEGER
)
```

**Functions:**
- `should_attempt(action_key, max_attempts=3) -> bool`
- `record_attempt(action_key)`
- `get_attempt_count(action_key) -> int`
- `reset_attempts(action_key)`

---

### 3. **waterline_proposals**
Stores proposed waterline timestamps before applying to heartbeat.
```sql
CREATE TABLE waterline_proposals (
    chain TEXT PRIMARY KEY,  -- 'solana' or 'nexus'
    proposed_timestamp INTEGER NOT NULL
)
```

**Functions:**
- `propose_solana_waterline(ts)`
- `propose_nexus_waterline(ts)`
- `get_proposed_solana_waterline() -> int | None`
- `get_proposed_nexus_waterline() -> int | None`
- `get_and_clear_proposed_waterlines() -> tuple[int | None, int | None]`
- `clear_waterline_proposals()`

---

### 4. **fee_entries**
Journal of all fee collections.
```sql
CREATE TABLE fee_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sig TEXT,                    -- Solana signature (USDC->USDD)
    txid TEXT,                   -- Nexus txid (USDD->USDC)
    kind TEXT NOT NULL,          -- 'flat', 'dynamic', 'swap'
    amount_usdc_units INTEGER,
    amount_usdd_units INTEGER,
    timestamp INTEGER NOT NULL
)
```

**Functions:**
- `add_fee_entry(sig, txid, kind, amount_usdc_units, amount_usdd_units)`
- `get_fee_entries(limit=1000, kind=None) -> List[Tuple]`
- `get_total_fees_collected() -> Tuple[int, int]`
- `update_fee_summary()`

---

### 5. **fee_summary**
Aggregated fee totals (updated periodically).
```sql
CREATE TABLE fee_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_collected_usdc INTEGER DEFAULT 0,
    total_collected_usdd INTEGER DEFAULT 0,
    last_updated INTEGER
)
```

---

## Schema Updates

### Updated **refunded_txids** table
Added `sig` column to store refund transfer signature:
```sql
ALTER TABLE refunded_txids ADD COLUMN sig TEXT;
```

---

## New Helper Functions

### Reference Management
- `next_reference() -> int` — Get next unique reference number for Nexus debits

### Refund Finalization
- `finalize_refund(sig, reason="refunded")` — Move sig from unprocessed to refunded
- `is_refunded(sig) -> bool` — Convenience wrapper for `is_refunded_sig`

### Unprocessed Txids
- `get_unprocessed_txids(limit=1000) -> List[Tuple]`
- `update_unprocessed_txid(txid, **kwargs)`
- `remove_unprocessed_txid(txid)`
- `is_processed_txid(txid) -> bool`

---

## Migration Checklist

### Phase 1: Database Setup ✅
- [x] Create new tables (reservations, attempts, waterline_proposals, fee_entries, fee_summary)
- [x] Add helper functions
- [x] Fix duplicate `conn.close()` statements

### Phase 2: Update Pollers (TODO)
- [ ] **swap_solana.py**: Replace `state.*` calls with `state_db.*`
- [ ] **swap_nexus.py**: Replace `state.*` calls with `state_db.*`
- [ ] **fees.py**: Migrate fee tracking to DB

### Phase 3: Main Loop (TODO)
- [ ] Add waterline application logic in main.py
- [ ] Call `cleanup_expired_reservations()` periodically
- [ ] Call `update_fee_summary()` periodically

### Phase 4: Testing (TODO)
- [ ] Test USDC->USDD swap end-to-end
- [ ] Test USDD->USDC swap end-to-end
- [ ] Test refund flows
- [ ] Test reservation locking
- [ ] Test waterline advancement
- [ ] Verify fee tracking accuracy

### Phase 5: Cleanup (TODO)
- [ ] Backup existing JSONL files
- [ ] Remove `state.py` imports from all files
- [ ] Delete `state.py`
- [ ] Delete legacy JSONL files

---

## Key Differences from state.py

| Feature | state.py (JSONL) | state_db.py (SQLite) |
|---------|------------------|----------------------|
| Reservations | In-memory dict | DB table with TTL |
| Attempts | JSONL file | DB table with counters |
| Waterlines | In-memory ephemeral | DB table (persistent) |
| Fees | JSONL journal | DB journal + summary |
| References | Max from JSONL scan | SQL MAX query |
| Concurrency | File locks | SQLite transactions |
| Crash recovery | Manual JSONL repair | Automatic DB integrity |
| Query performance | Full file scan | Indexed SQL queries |

---

## Example Usage

### Reservation Pattern
```python
# Before processing
if not state_db.reserve_action("debit", sig, ttl_sec=300):
    print(f"Signature {sig} already reserved")
    return

try:
    # Process debit...
    result = nexus_client.debit_usdd(...)
    state_db.mark_processed_sig(sig, ts, status="debit_confirmed")
finally:
    state_db.release_reservation("debit", sig)
```

### Retry Logic
```python
refund_key = f"refund_{sig}"
if not state_db.should_attempt(refund_key, max_attempts=3):
    print(f"Max refund attempts reached for {sig}")
    state_db.finalize_refund(sig, reason="refund_failed")
    return

state_db.record_attempt(refund_key)
# Attempt refund...
```

### Waterline Management
```python
# After processing sigs
state_db.propose_solana_waterline(latest_processed_timestamp)

# Later, in main loop
sol_wl, nxs_wl = state_db.get_and_clear_proposed_waterlines()
if sol_wl or nxs_wl:
    update_heartbeat_asset(
        set_solana_waterline=sol_wl,
        set_nexus_waterline=nxs_wl
    )
```

### Fee Tracking
```python
# Record fee
state_db.add_fee_entry(
    sig=sig,
    txid=None,
    kind="swap",
    amount_usdc_units=total_fee_usdc,
    amount_usdd_units=None
)

# Get totals
usdc_total, usdd_total = state_db.get_total_fees_collected()
print(f"Total fees: {usdc_total} USDC, {usdd_total} USDD")
```

---

## Performance Notes

- **Indexes**: Consider adding indexes on frequently queried columns:
  ```sql
  CREATE INDEX idx_unprocessed_sigs_status ON unprocessed_sigs(status);
  CREATE INDEX idx_processed_sigs_reference ON processed_sigs(reference);
  CREATE INDEX idx_fee_entries_timestamp ON fee_entries(timestamp);
  ```

- **Cleanup**: Run periodic cleanup to prevent table bloat:
  ```python
  # Every 5 minutes in main loop
  state_db.cleanup_expired_reservations(ttl_sec=300)
  ```

- **Transactions**: SQLite handles transactions automatically for single operations. For multi-step operations, use explicit transactions in calling code.

---

## Backward Compatibility

All functions maintain backward compatibility with existing call signatures where possible:
- `mark_processed_sig()` accepts old 3-arg form: `(sig, timestamp, status_string)`
- `mark_unprocessed_txid()` accepts legacy `sig` param (ignored)
- `mark_quarantined_sig()` accepts legacy columns (stored in DB)

---

## Next Steps

1. **Test DB initialization**: Run `state_db.init_db()` and verify all tables created
2. **Migrate one poller**: Start with `swap_solana.py` as proof-of-concept
3. **Run integration tests**: Verify no regressions
4. **Migrate remaining files**: `swap_nexus.py`, `fees.py`, `main.py`
5. **Deploy & monitor**: Watch for SQL errors, reservation deadlocks, etc.
6. **Clean up**: Remove `state.py` once fully migrated

---

**Status**: Database layer complete ✅ | Poller migration pending 🚧
