"""Operator alerting.

Before this existed, every safety signal the service produced - backing deficit pause,
unbacked-mint discrepancy, halted poller, exhausted refund, breached cap - was a bare
`print()` to stdout. Nothing reached a human unless somebody happened to be watching a
terminal, which for a custodial bridge means the detection logic was effectively
decorative.

Delivery is best-effort and never raises: an alerting failure must not take down the
swap loop. Configure at least one channel:

    ALERT_WEBHOOK_URL   POST a JSON body
    ALERT_COMMAND       executable; receives the same JSON on stdin

Identical events are rate-limited to one per ALERT_MIN_INTERVAL_SEC so a stuck condition
cannot flood the channel.
"""
import json
import subprocess
import threading
import time
from typing import Any, Dict

from . import config

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

_lock = threading.Lock()
_last_sent: Dict[str, float] = {}


def _redact(value: Any) -> Any:
    """Never let the Nexus PIN reach an external channel."""
    pin = getattr(config, "NEXUS_PIN", "") or ""
    if pin and isinstance(value, str) and pin in value:
        return value.replace(pin, "***")
    return value


def _deliver(payload: dict) -> None:
    body = json.dumps(payload, default=str)

    url = getattr(config, "ALERT_WEBHOOK_URL", None)
    if url:
        try:
            import requests
            requests.post(url, data=body,
                          headers={"Content-Type": "application/json"},
                          timeout=10)
        except Exception as e:
            print(f"[alert] webhook delivery failed: {e}")

    cmd = getattr(config, "ALERT_COMMAND", None)
    if cmd:
        try:
            subprocess.run([cmd], input=body, text=True, timeout=15,
                           capture_output=True)
        except Exception as e:
            print(f"[alert] command delivery failed: {e}")


def alert(level: str, event: str, message: str = "", **fields) -> None:
    """Emit an operator alert. Always logs; delivers if a channel is configured."""
    try:
        safe_fields = {k: _redact(v) for k, v in fields.items()}
        detail = " ".join(f"{k}={v}" for k, v in safe_fields.items() if v is not None)
        print(f"[ALERT:{level}] {event} {_redact(message)} {detail}".rstrip())

        # Rate-limit identical events so a persistent condition cannot flood the channel.
        now = time.monotonic()
        interval = int(getattr(config, "ALERT_MIN_INTERVAL_SEC", 300))
        with _lock:
            last = _last_sent.get(event)
            if last is not None and (now - last) < interval:
                return
            _last_sent[event] = now

        if not (getattr(config, "ALERT_WEBHOOK_URL", None) or getattr(config, "ALERT_COMMAND", None)):
            return

        payload = {
            "service": "swapService",
            "level": level,
            "event": event,
            "message": _redact(message),
            "timestamp": int(time.time()),
            "fields": safe_fields,
        }
        # Deliver off the hot path; a slow webhook must not stall the swap loop.
        threading.Thread(target=_deliver, args=(payload,), daemon=True).start()
    except Exception as e:
        print(f"[alert] error emitting alert: {e}")


def critical(event: str, message: str = "", **fields) -> None:
    alert(CRITICAL, event, message, **fields)


def warning(event: str, message: str = "", **fields) -> None:
    alert(WARNING, event, message, **fields)


def info(event: str, message: str = "", **fields) -> None:
    alert(INFO, event, message, **fields)
