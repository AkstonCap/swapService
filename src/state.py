import json
import os
from typing import Dict, Any
from . import config

# Load processed state
if os.path.exists(config.PROCESSED_SIG_FILE):
    with open(config.PROCESSED_SIG_FILE, "r") as f:
        processed_sigs = set(json.load(f))
else:
    processed_sigs = set()

if os.path.exists(config.PROCESSED_NEXUS_FILE):
    with open(config.PROCESSED_NEXUS_FILE, "r") as f:
        processed_nexus_txs = set(json.load(f))
else:
    processed_nexus_txs = set()

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
        json.dump(list(processed_sigs), f)
    with open(config.PROCESSED_NEXUS_FILE, "w") as f:
        json.dump(list(processed_nexus_txs), f)
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
