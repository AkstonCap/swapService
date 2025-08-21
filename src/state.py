import json
import os
from typing import Dict, Any
from . import config
import datetime

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
    try:
        ts_int = int(ts or 0)
    except Exception:
        ts_int = 0
    processed_sigs[str(signature)] = ts_int
    row = {
        "type": "solana",
        "sig": str(signature),
        "ts": ts_int,
        "ts_iso": datetime.datetime.utcfromtimestamp(ts_int).isoformat() + "Z" if ts_int else None,
        "reason": reason or "processed",
    }
    _append_jsonl(config.PROCESSED_SIG_FILE, row)
    try:
        print(f"PROCESSED SOLANA sig={row['sig']} ts={row['ts']} iso={row['ts_iso']} reason={row['reason']}")
    except Exception:
        pass

def mark_nexus_processed(key: str, ts: int | None = None, reason: str | None = None):
    try:
        ts_int = int(ts or 0)
    except Exception:
        ts_int = 0
    processed_nexus_txs[str(key)] = ts_int
    # Attempt to split txid and contract id if key is formatted as "<txid>:<cid>"
    txid = str(key)
    cid = None
    if ":" in txid:
        parts = txid.split(":", 1)
        txid, cid = parts[0], parts[1]
    row = {
        "type": "nexus",
        "tx": str(key),
        "txid": txid,
        "cid": cid,
        "ts": ts_int,
        "ts_iso": datetime.datetime.utcfromtimestamp(ts_int).isoformat() + "Z" if ts_int else None,
        "reason": reason or "processed",
    }
    _append_jsonl(config.PROCESSED_NEXUS_FILE, row)
    try:
        print(f"PROCESSED NEXUS tx={row['tx']} ts={row['ts']} iso={row['ts_iso']} reason={row['reason']}")
    except Exception:
        pass

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

# --- JSONL helpers for swap pipeline ---
def append_jsonl(path: str, row: Dict[str, Any]):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
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
    try:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass

def update_jsonl_row(path: str, predicate, update_fn) -> bool:
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
    path = config.REFERENCE_COUNTER_FILE
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                val = int(data.get("next", 1))
        else:
            val = 1
    except Exception:
        val = 1
    # persist next+1
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"next": val + 1}, f)
    except Exception:
        pass
    return val
