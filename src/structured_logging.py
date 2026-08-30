"""Structured, secret-safe event logging for the Nexus ↔ Solana bridge.

Money-path events must be consumable by an operator's log collector and must never put
Nexus profile credentials, API credentials, or Solana signing material in log output.
The module deliberately uses only Python's standard ``logging`` facilities so it can run
before optional chain SDK dependencies are available.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "pin", "password", "session", "secret", "authorization", "api_key", "apikey",
    "keypair", "private_key", "access_token", "bearer",
)
_SECRET_ENV_NAMES = (
    "NEXUS_PIN", "NEXUS_SESSION", "NEXUS_API_PASSWORD", "VAULT_KEYPAIR",
    "ALERT_WEBHOOK_URL",
)


def _known_secrets() -> tuple[str, ...]:
    """Return currently configured raw secret values without retaining them globally."""
    return tuple(
        value for name in _SECRET_ENV_NAMES
        if (value := os.getenv(name))
    )


def redact(value: Any, *, field_name: str = "") -> Any:
    """Recursively remove credentials from a structured field or free-text message."""
    normalized_name = field_name.lower()
    if normalized_name and any(part in normalized_name for part in _SENSITIVE_KEY_PARTS):
        return "***"
    if isinstance(value, dict):
        return {str(key): redact(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item, field_name=field_name) for item in value]
    if isinstance(value, str):
        for secret in _known_secrets():
            value = value.replace(secret, "***")
    return value


class JsonFormatter(logging.Formatter):
    """Render bridge events as one JSON object per line for collectors and SIEMs."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "event_fields", {})
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "event": str(getattr(record, "event", record.getMessage())),
            "message": redact(str(getattr(record, "event_message", record.getMessage()))),
            "fields": redact(fields) if isinstance(fields, dict) else {},
        }
        return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def get_logger(name: str = "swapService") -> logging.Logger:
    """Get a configured, non-propagating JSON event logger exactly once per name."""
    logger = logging.getLogger(name)
    if not any(getattr(handler, "_swapservice_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler._swapservice_json = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def emit(logger: logging.Logger, level: int, event: str, message: str = "", **fields: Any) -> None:
    """Emit a named event with safe contextual fields.

    Event names remain stable machine identifiers; human text is supplementary.  This is
    important for correlating durable Nexus intent IDs with Solana signatures during a
    cross-chain incident without parsing console prose.
    """
    logger.log(
        level,
        redact(message or event),
        extra={
            "event": event,
            "event_message": redact(message or event),
            "event_fields": redact(fields),
        },
    )
