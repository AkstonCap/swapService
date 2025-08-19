import json
import os
from typing import Dict, Any
from . import config
import datetime

# Load processed state
if os.path.exists(config.PROCESSED_SIG_FILE):
    with open(config.PROCESSED_SIG_FILE, "r") as f:
        _loaded = json.load(f)
        # Backward compatible: previously a list of keys; now a dict of key->timestamp
        if isinstance(_loaded, dict):
            processed_sigs: Dict[str, int] = {str(k): int(v or 0) for k, v in _loaded.items()}
        else:
            processed_sigs = {str(k): 0 for k in (_loaded or [])}
else:
    processed_sigs: Dict[str, int] = {}

if os.path.exists(config.PROCESSED_NEXUS_FILE):
    with open(config.PROCESSED_NEXUS_FILE, "r") as f:
        _loaded = json.load(f)
        if isinstance(_loaded, dict):
            processed_nexus_txs: Dict[str, int] = {str(k): int(v or 0) for k, v in _loaded.items()}
        else:
            processed_nexus_txs = {str(k): 0 for k in (_loaded or [])}
else:
    processed_nexus_txs: Dict[str, int] = {}

if os.path.exists(config.ATTEMPT_STATE_FILE):
    try:
        with open(config.ATTEMPT_STATE_FILE, "r") as f:
            attempt_state: Dict[str, Any] = json.load(f)
    except Exception:
        attempt_state = {}
else:
    attempt_state = {}


def save_state():
    with open(config.PROCESSED_SIG_FILE, "w") as f:
        json.dump(processed_sigs, f)
    with open(config.PROCESSED_NEXUS_FILE, "w") as f:
        json.dump(processed_nexus_txs, f)
    try:
        with open(config.ATTEMPT_STATE_FILE, "w") as f:
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
def mark_solana_processed(signature: str, ts: int | None = None):
    try:
        ts_int = int(ts or 0)
    except Exception:
        ts_int = 0
    processed_sigs[str(signature)] = ts_int

def mark_nexus_processed(key: str, ts: int | None = None):
    try:
        ts_int = int(ts or 0)
    except Exception:
        ts_int = 0
    processed_nexus_txs[str(key)] = ts_int

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
