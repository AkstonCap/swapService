import json
import os
import fcntl
from typing import Dict, Any
from . import config
import datetime
from typing import Tuple

# Reservation file path (lazy)
_RESERVATIONS_FILE: str | None = None

def _reservations_path() -> str:
    global _RESERVATIONS_FILE
    if _RESERVATIONS_FILE:
        return _RESERVATIONS_FILE
    base_dir = os.path.dirname(config.PROCESSED_SIG_FILE)
    if not base_dir:
        base_dir = "."
    _RESERVATIONS_FILE = os.path.join(base_dir, "reservations.jsonl")
    return _RESERVATIONS_FILE

# Load processed state
processed_sigs: Dict[str, int] = {}
if os.path.exists(config.PROCESSED_SIG_FILE):
    try:
        # Try JSON object (legacy)
        with open(config.PROCESSED_SIG_FILE, "r", encoding="utf-8") as f:
            data = f.read()
            try:
                _loaded = json.loads(data)
                if isinstance(_loaded, dict):
                    processed_sigs = {str(k): int(v or 0) for k, v in _loaded.items()}
                else:
                    processed_sigs = {str(k): 0 for k in (_loaded or [])}
            except Exception:
                # Try JSONL lines
                processed_sigs = {}
                for line in data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        sig = str(row.get("sig") or row.get("signature") or "").strip()
                        ts = int(row.get("ts") or row.get("timestamp") or 0)
                        if sig:
                            processed_sigs[sig] = ts
                    except Exception:
                        continue
    except Exception:
        processed_sigs = {}

processed_nexus_txs: Dict[str, int] = {}
if os.path.exists(config.PROCESSED_NEXUS_FILE):
    try:
        with open(config.PROCESSED_NEXUS_FILE, "r", encoding="utf-8") as f:
            data = f.read()
            try:
                _loaded = json.loads(data)
                if isinstance(_loaded, dict):
                    processed_nexus_txs = {str(k): int(v or 0) for k, v in _loaded.items()}
                else:
                    processed_nexus_txs = {str(k): 0 for k in (_loaded or [])}
            except Exception:
                processed_nexus_txs = {}
                for line in data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        key = str(row.get("tx") or row.get("key") or row.get("txid") or "").strip()
                        ts = int(row.get("ts") or row.get("timestamp") or 0)
                        if key:
                            processed_nexus_txs[key] = ts
                    except Exception:
                        continue
    except Exception:
        processed_nexus_txs = {}

if os.path.exists(config.ATTEMPT_STATE_FILE):
    try:
        with open(config.ATTEMPT_STATE_FILE, "r") as f:
            attempt_state: Dict[str, Any] = json.load(f)
    except Exception:
        attempt_state = {}
else:
    attempt_state = {}

# Refunded signature index (prevent double refunds)
refunded_sigs: set[str] = set()
try:
    if os.path.exists(config.REFUNDED_SIGS_FILE):
        with open(config.REFUNDED_SIGS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    sg = str(row.get("sig") or "").strip()
                    if sg:
                        refunded_sigs.add(sg)
                except Exception:
                    continue
except Exception:
    refunded_sigs = set()


def save_state():
    # Do not rewrite processed files; they are append-only JSONL now.
    try:
        with open(config.ATTEMPT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(attempt_state, f)
    except Exception:
        pass


def _now() -> int:
    import time
    return int(time.time())


def should_attempt(action_key: str) -> bool:
    rec = attempt_state.get(action_key)
    if not rec:
        return True
    attempts = int(rec.get("attempts", 0))
    last = int(rec.get("last", 0))
    if attempts >= config.MAX_ACTION_ATTEMPTS:
        return False
    if (_now() - last) < config.ACTION_RETRY_COOLDOWN_SEC:
        return False
    return True


def record_attempt(action_key: str):
    rec = attempt_state.get(action_key, {"attempts": 0, "last": 0})
    rec["attempts"] = int(rec.get("attempts", 0)) + 1
    rec["last"] = _now()
    attempt_state[action_key] = rec
    save_state()

# --- Processed markers with timestamps ---
def _append_jsonl(path: str, row: Dict[str, Any]):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def mark_solana_processed(signature: str, ts: int | None = None, reason: str | None = None):
    atomic_mark_processed(signature, False, ts, reason)

def mark_nexus_processed(key: str, ts: int | None = None, reason: str | None = None):
    atomic_mark_processed(key, True, ts, reason)


def add_refunded_sig(sig: str):
    """Public wrapper for atomic refunded sig addition (defined once)."""
    atomic_add_refunded_sig(sig)

def prune_processed(solana_waterline: int | None = None, nexus_waterline: int | None = None):
    """Prune processed markers strictly older than the given waterlines minus safety.
    Uses HEARTBEAT_WATERLINE_SAFETY_SEC as an extra buffer.
    """
    try:
        safety = int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0))
    except Exception:
        safety = 0
    if solana_waterline:
        try:
            s_cut = int(solana_waterline) - max(0, safety)
            if s_cut > 0:
                to_del = [k for k, v in processed_sigs.items() if int(v or 0) and int(v) < s_cut]
                if to_del:
                    for k in to_del:
                        processed_sigs.pop(k, None)
        except Exception:
            pass
    if nexus_waterline:
        try:
            n_cut = int(nexus_waterline) - max(0, safety)
            if n_cut > 0:
                to_del = [k for k, v in processed_nexus_txs.items() if int(v or 0) and int(v) < n_cut]
                if to_del:
                    for k in to_del:
                        processed_nexus_txs.pop(k, None)
        except Exception:
            pass

# --- Ephemeral proposed waterlines (not persisted) ---
_proposed_solana_waterline: int | None = None
_proposed_nexus_waterline: int | None = None

def propose_solana_waterline(ts: int):
    """Propose a conservative Solana waterline timestamp (seconds). Keeps the minimum of proposals."""
    global _proposed_solana_waterline
    try:
        ts = int(ts)
        if ts <= 0:
            return
    except Exception:
        return
    if _proposed_solana_waterline is None:
        _proposed_solana_waterline = ts
    else:
        _proposed_solana_waterline = min(_proposed_solana_waterline, ts)

def propose_nexus_waterline(ts: int):
    """Propose a conservative Nexus waterline timestamp (seconds). Keeps the minimum of proposals."""
    global _proposed_nexus_waterline
    try:
        ts = int(ts)
        if ts <= 0:
            return
    except Exception:
        return
    if _proposed_nexus_waterline is None:
        _proposed_nexus_waterline = ts
    else:
        _proposed_nexus_waterline = min(_proposed_nexus_waterline, ts)

def get_and_clear_proposed_waterlines() -> tuple[int | None, int | None]:
    """Return (solana_ts, nexus_ts) proposals and clear them for the next loop."""
    global _proposed_solana_waterline, _proposed_nexus_waterline
    s, n = _proposed_solana_waterline, _proposed_nexus_waterline
    _proposed_solana_waterline = None
    _proposed_nexus_waterline = None
    return s, n

# --- Failed refunds logging ---
def log_failed_refund(payload: Dict[str, Any]):
    try:
        row = dict(payload or {})
        row["ts"] = int(_now())
        row["ts_iso"] = datetime.datetime.utcfromtimestamp(row["ts"]).isoformat() + "Z"
        with open(config.FAILED_REFUNDS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        try:
            print(f"Failed to log refund: {e}")
        except Exception:
            pass

def is_refunded(sig: str) -> bool:
    return sig in refunded_sigs

# (duplicate add_refunded_sig removed)


# File locking context manager for atomic operations
class FileLock:
    def __init__(self, path: str):
        self.path = path
        self.lock_path = path + ".lock"
        self.file = None
    
    def __enter__(self):
        try:
            self.file = open(self.lock_path, 'w')
            try:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
            except (ImportError, AttributeError):
                # Windows fallback - use file existence as lock
                import time
                for _ in range(50):  # 5 second timeout
                    if not os.path.exists(self.lock_path + ".win"):
                        try:
                            with open(self.lock_path + ".win", 'x') as f:
                                f.write("lock")
                            break
                        except FileExistsError:
                            time.sleep(0.1)
                    else:
                        time.sleep(0.1)
        except Exception:
            if self.file:
                self.file.close()
            raise
        return self
    
    def __exit__(self, *args):
        if self.file:
            self.file.close()
        # Clean up Windows fallback lock
        try:
            os.remove(self.lock_path + ".win")
        except (FileNotFoundError, OSError):
            pass


def atomic_add_refunded_sig(sig: str):
    """Atomically add refunded signature with file lock."""
    if not sig:
        return
    
    with FileLock(config.REFUNDED_SIGS_FILE):
        # Re-read current state
        current_refunded = set()
        try:
            if os.path.exists(config.REFUNDED_SIGS_FILE):
                with open(config.REFUNDED_SIGS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                row = json.loads(line)
                                sg = str(row.get("sig") or "").strip()
                                if sg:
                                    current_refunded.add(sg)
                            except Exception:
                                continue
        except Exception:
            pass
        
        # Check if already refunded
        if sig in current_refunded:
            return  # Already refunded, skip
        
        # Add to both memory and disk atomically
        refunded_sigs.add(sig)
        row = {
            "sig": sig,
            "ts": _now(),
            "ts_iso": datetime.datetime.utcfromtimestamp(_now()).isoformat() + "Z"
        }
        try:
            with open(config.REFUNDED_SIGS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()  # Force write to disk
                try:
                    os.fsync(f.fileno())  # Force OS flush
                except (AttributeError, OSError):
                    pass  # Not available on all platforms
        except Exception:
            refunded_sigs.discard(sig)  # Rollback memory on disk failure
            raise


def finalize_refund(row: Dict[str, Any], reason: str | None = None):
    """Atomically finalize a refund across all tracking files.
    row should include at least: sig, amount_usdc_units, ts, from
    Steps:
      1. Append to REFUNDED_SIGS_FILE (detailed row) if not already refunded.
      2. Append to PROCESSED_SWAPS_FILE (if not already present).
      3. Remove any UNPROCESSED entry.
      4. Mark processed (processed_sigs) for idempotency.
    """
    sig = (row or {}).get("sig")
    if not sig:
        return
    ts = 0
    try:
        ts = int(row.get("ts") or 0)
    except Exception:
        ts = 0
    detailed = dict(row)
    detailed.setdefault("comment", "refunded")
    if reason:
        detailed["refund_reason"] = reason
    # 1 & 2 under lock to avoid races.
    with FileLock(config.REFUNDED_SIGS_FILE):
        # Build in-memory refunded set from file (lightweight; file usually small)
        existing_refunded = set()
        try:
            if os.path.exists(config.REFUNDED_SIGS_FILE):
                with open(config.REFUNDED_SIGS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line=line.strip()
                        if not line:
                            continue
                        try:
                            rj=json.loads(line)
                            sg=str(rj.get("sig") or "").strip()
                            if sg:
                                existing_refunded.add(sg)
                        except Exception:
                            continue
        except Exception:
            existing_refunded = set()
        new_refund = sig not in existing_refunded
        if new_refund:
            try:
                with open(config.REFUNDED_SIGS_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(detailed, ensure_ascii=False)+"\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
            except Exception:
                pass
        refunded_sigs.add(sig)
        # Append to processed swaps (outside REFUNDED file but still under same lock for simplicity)
        already_processed = False
        try:
            if os.path.exists(config.PROCESSED_SWAPS_FILE):
                with open(config.PROCESSED_SWAPS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        if f'"sig": "{sig}"' in line:
                            already_processed = True
                            break
        except Exception:
            pass
        if not already_processed:
            try:
                with open(config.PROCESSED_SWAPS_FILE, "a", encoding="utf-8") as f2:
                    f2.write(json.dumps(detailed, ensure_ascii=False)+"\n")
                    f2.flush()
                    try:
                        os.fsync(f2.fileno())
                    except Exception:
                        pass
            except Exception:
                pass
    # 3. Remove from unprocessed file
    try:
        with FileLock(config.UNPROCESSED_SIGS_FILE):
            if os.path.exists(config.UNPROCESSED_SIGS_FILE):
                rows = []
                changed=False
                with open(config.UNPROCESSED_SIGS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line=line.strip()
                        if not line:
                            continue
                        try:
                            rj=json.loads(line)
                        except Exception:
                            rows.append(line)
                            continue
                        if isinstance(rj, dict) and rj.get("sig") == sig:
                            changed=True
                            continue
                        rows.append(rj)
                if changed:
                    with open(config.UNPROCESSED_SIGS_FILE, "w", encoding="utf-8") as f:
                        for r in rows:
                            if isinstance(r, dict):
                                f.write(json.dumps(r, ensure_ascii=False)+"\n")
                            else:
                                f.write(str(r)+"\n")
    except Exception:
        pass
    # 4. Mark processed (atomic appends handled there)
    try:
        mark_solana_processed(sig, ts=ts, reason=f"refunded:{reason}" if reason else "refunded")
    except Exception:
        pass

# --- Reservation System ----------------------------------------------------
def reserve_action(kind: str, key: str, ttl_sec: int | None = None) -> bool:
    """Attempt to reserve (kind,key). Returns True if fresh reservation established.
    If an unexpired reservation exists returns False. Expired reservations are replaced.
    """
    if not kind or not key:
        return False
    if ttl_sec is None:
        ttl_sec = int(getattr(config, "RESERVATION_TTL_SEC", 300))
    path = _reservations_path()
    now = _now()
    new_rows: list[dict] = []
    claimed = True
    with FileLock(path):
        # Load existing
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line=line.strip()
                        if not line:
                            continue
                        try:
                            rj=json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(rj, dict):
                            continue
                        k = (rj.get("kind"), rj.get("key"))
                        ts = int(rj.get("ts") or 0)
                        if k == (kind, key):
                            # active?
                            if ts and now - ts < ttl_sec:
                                # still active -> cannot claim
                                claimed = False
                                new_rows.append(rj)  # preserve
                                continue
                            # expired -> drop old one, we'll add fresh below
                            continue
                        new_rows.append(rj)
        except Exception:
            new_rows = []
        if not claimed:
            # Write back untouched
            try:
                with open(path, "w", encoding="utf-8") as f:
                    for r in new_rows:
                        f.write(json.dumps(r, ensure_ascii=False)+"\n")
            except Exception:
                pass
            return False
        # Append reservation
        new_rows.append({"kind": kind, "key": key, "ts": now})
        try:
            with open(path, "w", encoding="utf-8") as f:
                for r in new_rows:
                    f.write(json.dumps(r, ensure_ascii=False)+"\n")
        except Exception:
            return False
    return True

def release_reservation(kind: str, key: str):
    if not kind or not key:
        return
    path = _reservations_path()
    with FileLock(path):
        if not os.path.exists(path):
            return
        try:
            rows=[]
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line=line.strip()
                    if not line:
                        continue
                    try:
                        rj=json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(rj, dict):
                        continue
                    if rj.get("kind") == kind and rj.get("key") == key:
                        continue
                    rows.append(rj)
            with open(path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False)+"\n")
        except Exception:
            pass

def has_active_reservation(kind: str, key: str, ttl_sec: int | None = None) -> bool:
    if not kind or not key:
        return False
    if ttl_sec is None:
        ttl_sec = int(getattr(config, "RESERVATION_TTL_SEC", 300))
    path = _reservations_path()
    now = _now()
    try:
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if not line:
                    continue
                try:
                    rj=json.loads(line)
                except Exception:
                    continue
                if not isinstance(rj, dict):
                    continue
                if rj.get("kind") == kind and rj.get("key") == key:
                    ts = int(rj.get("ts") or 0)
                    if ts and now - ts < ttl_sec:
                        return True
    except Exception:
        return False
    return False


def atomic_mark_processed(sig_or_tx: str, is_nexus: bool = False, ts: int | None = None, reason: str | None = None):
    """Atomically mark signature/tx as processed with file lock."""
    file_path = config.PROCESSED_NEXUS_FILE if is_nexus else config.PROCESSED_SIG_FILE
    memory_dict = processed_nexus_txs if is_nexus else processed_sigs
    
    with FileLock(file_path):
        # Double-check not already processed
        if sig_or_tx in memory_dict:
            return  # Already processed
        
        try:
            ts_int = int(ts or 0)
        except Exception:
            ts_int = 0
        
        # Add to memory
        memory_dict[sig_or_tx] = ts_int
        
        # Prepare row
        if is_nexus:
            txid, cid = (sig_or_tx.split(":", 1) if ":" in sig_or_tx else (sig_or_tx, None))
            row = {
                "type": "nexus",
                "tx": sig_or_tx,
                "txid": txid,
                "cid": cid,
                "ts": ts_int,
                "ts_iso": datetime.datetime.utcfromtimestamp(ts_int).isoformat() + "Z" if ts_int else None,
                "reason": reason or "processed",
            }
        else:
            row = {
                "type": "solana",
                "sig": sig_or_tx,
                "ts": ts_int,
                "ts_iso": datetime.datetime.utcfromtimestamp(ts_int).isoformat() + "Z" if ts_int else None,
                "reason": reason or "processed",
            }
        
        # Write atomically
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (AttributeError, OSError):
                    pass
        except Exception:
            memory_dict.pop(sig_or_tx, None)  # Rollback memory on disk failure
            raise
        
        # Log success
        try:
            if is_nexus:
                print(f"PROCESSED NEXUS tx={row['tx']} ts={row['ts']} iso={row['ts_iso']} reason={row['reason']}")
            else:
                print(f"PROCESSED SOLANA sig={row['sig']} ts={row['ts']} iso={row['ts_iso']} reason={row['reason']}")
        except Exception:
            pass

# --- JSONL helpers for swap pipeline ---
def append_jsonl(path: str, row: Dict[str, Any]):
    """Atomically append a JSONL row with file lock."""
    with FileLock(path):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (AttributeError, OSError):
                    pass
        except Exception:
            pass

def read_jsonl(path: str) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    continue
    except Exception:
        pass
    return out

def write_jsonl(path: str, rows: list[Dict[str, Any]]):
    """Atomically write entire JSONL file with file lock."""
    with FileLock(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (AttributeError, OSError):
                    pass
        except Exception:
            pass

def update_jsonl_row(path: str, predicate, update_fn) -> bool:
    """Atomically update a JSONL row matching predicate."""
    with FileLock(path):
        rows = read_jsonl(path)
        changed = False
        for i, r in enumerate(rows):
            try:
                if predicate(r):
                    nr = update_fn(dict(r))
                    rows[i] = nr
                    changed = True
            except Exception:
                continue
        if changed:
            write_jsonl(path, rows)
        return changed

# --- Unique integer reference counter ---
def next_reference() -> int:
    """Return next monotonically increasing integer reference.
    Used ONLY for: (1) Nexus USDD debit reference (USDC->USDD), (2) Solana USDC send memo (USDD->USDC).
    Implementation: atomic read-modify-write with file lock to avoid races across processes.
    If file missing or corrupt, starts at 1.
    """
    path = config.REFERENCE_COUNTER_FILE
    with FileLock(path):  # lock on counter file to serialize increments
        val = 1
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    cand = int(data.get("next", 1))
                    # Guard against zero/negative or absurd jumps (corruption)
                    if cand <= 0 or cand > 10**12:
                        cand = 1
                    val = cand
        except Exception:
            val = 1
        # persist next = val + 1
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"next": val + 1}, f)
        except Exception:
            pass
        return int(val)
