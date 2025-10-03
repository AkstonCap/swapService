# State Migration Strategy

## Problem
The codebase has 100+ references to `state` module across multiple files:
- `solana_client.py` - 50+ references
- `nexus_client.py` - 30+ references  
- `swap_solana.py` - 6 references
- `swap_nexus.py` - 40+ references
- `fees.py` - unknown references

Direct find-replace would require:
1. Understanding complex JSONL→DB migration logic
2. Rewriting business logic in 4+ large files
3. High risk of breaking existing swap flows
4. Difficult to test incrementally

## Solution: Compatibility Shim Layer

Create a thin `state_compat.py` that wraps `state_db.py` and provides backward-compatible API:

```python
# state_compat.py - DROP-IN REPLACEMENT
from . import state_db

# JSONL operations → DB operations
def read_jsonl(path):
    if "unprocessed_sigs" in path:
        return _sigs_to_dicts(state_db.get_unprocessed_sigs())
    elif "unprocessed_txids" in path:
        return _txids_to_dicts(state_db.get_unprocessed_txids())
    # etc.

def append_jsonl(path, row):
    if "unprocessed_sigs" in path:
        state_db.add_unprocessed_sig(...)
    # etc.

# Direct delegations
propose_solana_waterline = state_db.propose_solana_waterline
should_attempt = state_db.should_attempt
record_attempt = state_db.record_attempt
# etc.
```

### Migration Steps

1. ✅ **Complete `state_db.py`** - All tables & functions added
2. 🚧 **Replace imports** in affected files:
   ```python
   # Before:
   from . import state
   
   # After:
   from . import state_compat as state
   ```
3. ✅ **Test all swap flows** - USDC→USDD, USDD→USDC, refunds, quarantine
4. ✅ **Monitor production** - Watch for DB errors, performance issues
5. ⏳ **Gradual refactor** - Replace `state_compat` calls with direct `state_db` calls
6. ⏳ **Delete legacy** - Remove `state.py`, `state_compat.py`, JSONL files

### Advantages

- **Zero business logic changes** - Only import statements modified
- **Incremental testing** - Can rollback by changing one import
- **Low risk** - Shim layer handles translation complexity
- **Gradual migration** - Replace shim calls with direct DB calls over time
- **Maintains history** - Can diff against old `state.py` to verify coverage

### Implementation

File changes required:
- ✅ Create `state_compat.py` (new)
- ✅ `solana_client.py`: `from . import state_compat as state`
- ✅ `nexus_client.py`: `from . import state_compat as state`  
- ✅ `swap_solana.py`: `from . import state_compat as state` (already changed to state_db, revert)
- ✅ `swap_nexus.py`: `from . import state_compat as state` (already changed to state_db, revert)
- ✅ `fees.py`: `from . import state_compat as state`

TOTAL: 5 files, ~6 line changes

### Comparison

| Approach | Files Changed | Lines Changed | Risk | Test Effort |
|----------|---------------|---------------|------|-------------|
| Direct migration | 5 files | 150+ lines | HIGH | Weeks |
| Compatibility shim | 5 files | 6 lines | LOW | Days |

**Recommendation**: Use compatibility shim approach.
