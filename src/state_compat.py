"""Compatibility shim: state.py → state_db.py migration layer.

This module provides a backward-compatible API that translates legacy JSONL-based
`state` module calls into SQLite `state_db` operations.

Purpose: Allow gradual migration without rewriting 100+ call sites across solana_client,
nexus_client, swap_solana, swap_nexus, and fees modules.

Usage:
    # In files currently using `from . import state`:
    from . import state_compat as state
    
    # All existing code continues to work unchanged
    state.read_jsonl("unprocessed_sigs.json")  # → state_db.get_unprocessed_sigs()
    state.should_attempt(key)                   # → state_db.should_attempt(key)
    etc.

Migration path:
    1. Replace imports (this step)
    2. Test all flows
    3. Gradually replace state_compat.* with direct state_db.* calls
    4. Delete this file once migration complete
"""

from . import state_db, config
from typing import Dict, Any, List
import time

# ---------------------------------------------------------------------------
# JSONL → DB translation helpers
# ---------------------------------------------------------------------------

def _sig_tuple_to_dict(t: tuple) -> Dict[str, Any]:
    """Convert unprocessed_sigs tuple to dict matching old JSONL format."""
    if not t or len(t) < 7:
        return {}
    sig, timestamp, memo, from_address, amount_usdc_units, status, txid = t
    return {
        "sig": sig,
        "ts": timestamp,
        "memo": memo,
        "from": from_address,
        "amount_usdc_units": amount_usdc_units,
        "comment": status,
        "txid": txid,
    }


def _txid_tuple_to_dict(t: tuple) -> Dict[str, Any]:
    """Convert unprocessed_txids tuple to dict matching old JSONL format."""
    if not t or len(t) < 8:
        return {}
    txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status = t
    return {
        "txid": txid,
        "ts": timestamp,
        "amount_usdd": amount_usdd,
        "from": from_address,
        "to": to_address,
        "owner": owner_from_address,
        "confirmations": confirmations_credit,
        "comment": status,
    }


# ---------------------------------------------------------------------------
# JSONL operations (delegated to DB)
# ---------------------------------------------------------------------------

def read_jsonl(path: str) -> List[Dict[str, Any]]:
    """Read JSONL file → Query DB table."""
    if "unprocessed_sigs" in path.lower():
        tuples = state_db.get_unprocessed_sigs()
        return [_sig_tuple_to_dict(t) for t in tuples]
    elif "unprocessed_txids" in path.lower():
        tuples = state_db.get_unprocessed_txids()
        return [_txid_tuple_to_dict(t) for t in tuples]
    elif "processed" in path.lower() and "sig" in path.lower():
        # Legacy processed_sigs query (rarely used)
        return []
    elif "processed" in path.lower() and "txid" in path.lower():
        # Legacy processed_txids query
        return []
    else:
        # Unknown file → empty list (graceful degradation)
        return []


def append_jsonl(path: str, row: Dict[str, Any]):
    """Append to JSONL → Insert into DB table."""
    if "unprocessed_sigs" in path.lower():
        state_db.add_unprocessed_sig(
            sig=row.get("sig"),
            timestamp=int(row.get("ts") or time.time()),
            memo=row.get("memo"),
            from_address=row.get("from"),
            amount_usdc_units=int(row.get("amount_usdc_units") or 0),
            status=row.get("comment"),
            txid=row.get("txid"),
        )
    elif "unprocessed_txids" in path.lower():
        state_db.mark_unprocessed_txid(
            txid=row.get("txid"),
            timestamp=int(row.get("ts") or time.time()),
            amount_usdd=float(row.get("amount_usdd") or 0),
            from_address=row.get("from"),
            to_address=row.get("to"),
            owner_from_address=row.get("owner"),
            confirmations_credit=int(row.get("confirmations") or 0),
            status=row.get("comment"),
        )
    elif "processed" in path.lower():
        # Legacy append to processed files → use mark_processed_*
        if "sig" in path.lower():
            state_db.mark_processed_sig(
                sig=row.get("sig"),
                timestamp=int(row.get("ts") or time.time()),
                status=row.get("comment"),
            )
        elif "txid" in path.lower():
            state_db.mark_processed_txid(
                txid=row.get("txid"),
                timestamp=int(row.get("ts") or time.time()),
                amount_usdd=float(row.get("amount_usdd") or 0),
                from_address=row.get("from") or "",
                to_address=row.get("to") or "",
                owner=row.get("owner") or "",
                sig=row.get("sig") or "",
                status=row.get("comment"),
            )


def write_jsonl(path: str, rows: List[Dict[str, Any]]):
    """Overwrite JSONL → Clear and bulk insert into DB."""
    # WARNING: This is destructive. Legacy code uses this to filter/update unprocessed lists.
    # We need to be careful here.
    if "unprocessed_sigs" in path.lower():
        # Get current sigs
        current = state_db.get_unprocessed_sigs()
        current_sigs = {t[0] for t in current}
        new_sigs = {r.get("sig") for r in rows if r.get("sig")}
        
        # Remove sigs not in new list
        for sig in (current_sigs - new_sigs):
            state_db.remove_unprocessed_sig(sig)
            
        # Update/add sigs in new list
        for row in rows:
            if row.get("sig"):
                append_jsonl(path, row)
                
    elif "unprocessed_txids" in path.lower():
        # Similar logic for txids
        current = state_db.get_unprocessed_txids()
        current_txids = {t[0] for t in current}
        new_txids = {r.get("txid") for r in rows if r.get("txid")}
        
        for txid in (current_txids - new_txids):
            state_db.remove_unprocessed_txid(txid)
            
        for row in rows:
            if row.get("txid"):
                append_jsonl(path, row)


def update_jsonl_row(path: str, predicate, updater) -> bool:
    """Update matching row in JSONL → Update DB record."""
    rows = read_jsonl(path)
    found = False
    for row in rows:
        if predicate(row):
            updated = updater(row)
            append_jsonl(path, updated)  # Uses INSERT OR REPLACE
            found = True
            break
    return found


# ---------------------------------------------------------------------------
# Direct delegations to state_db
# ---------------------------------------------------------------------------

# Waterlines
propose_solana_waterline = state_db.propose_solana_waterline
propose_nexus_waterline = state_db.propose_nexus_waterline

# Attempts
should_attempt = state_db.should_attempt
record_attempt = state_db.record_attempt
get_attempt_count = state_db.get_attempt_count
reset_attempts = state_db.reset_attempts

# Reservations
reserve_action = state_db.reserve_action
release_reservation = state_db.release_reservation
is_reserved = state_db.is_reserved

# References
next_reference = state_db.next_reference

# Refunds
finalize_refund = state_db.finalize_refund
is_refunded = state_db.is_refunded

# Vault balance
load_last_vault_balance = state_db.load_last_vault_balance
save_last_vault_balance = state_db.save_last_vault_balance

# Processed markers
def mark_solana_processed(signature: str, ts: int | None = None, reason: str | None = None):
    """Mark signature as processed."""
    state_db.mark_processed_sig(
        sig=signature,
        timestamp=ts or int(time.time()),
        status=reason or "processed",
    )


def mark_nexus_processed(key: str, ts: int | None = None, reason: str | None = None):
    """Mark Nexus txid as processed."""
    state_db.mark_processed_txid(
        txid=key,
        timestamp=ts or int(time.time()),
        amount_usdd=0.0,  # Legacy didn't track amount
        from_address="",
        to_address="",
        owner="",
        sig="",
        status=reason or "processed",
    )


def add_refunded_sig(sig: str):
    """Add signature to refunded list."""
    state_db.mark_refunded_sig(
        sig=sig,
        timestamp=int(time.time()),
        from_address="",
        amount_usdc_units=0,
        memo=None,
        refund_sig=None,
        refunded_units=None,
        status="refunded",
    )


# ---------------------------------------------------------------------------
# Legacy state that doesn't need DB (no-ops or compatibility stubs)
# ---------------------------------------------------------------------------

def save_state():
    """Legacy save_state() → no-op (DB auto-commits)."""
    pass


# Backward compatibility: legacy code might access attempt_state dict directly
class AttemptStateProxy:
    """Proxy for legacy state.attempt_state dict access."""
    def get(self, key, default=None):
        count = state_db.get_attempt_count(key)
        if count == 0:
            return default
        return {"attempts": count, "last_ts": int(time.time())}


attempt_state = AttemptStateProxy()


# Legacy processed state dicts (deprecated, use DB queries)
processed_sigs: Dict[str, int] = {}
processed_nexus_txs: Dict[str, int] = {}
refunded_sigs: set[str] = set()


def prune_processed(solana_waterline: int | None = None, nexus_waterline: int | None = None):
    """Legacy prune (no-op - DB handles this via waterline logic)."""
    pass
