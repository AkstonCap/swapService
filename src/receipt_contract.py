"""Pure canonical contract for optional Nexus swap receipt assets.

This module has no database, configuration, or chain-client imports so persistence and
publication can share one wire contract without an import cycle.
"""
from __future__ import annotations

import hashlib

SCHEMA = "nexus-swap-receipt-v1"
DISTORDIA_TYPE = "nexusSwapReceipt"
REQUIRED_FIELDS = (
    "distordiaType", "schema", "source_signature", "solana_mint", "solana_vault",
    "nexus_token", "nexus_account", "output_txid", "output_contract_id",
    "output_units", "reference",
)
EVIDENCE_FIELDS = (
    "source_signature", "solana_mint", "solana_vault", "nexus_token",
    "nexus_account", "output_txid", "output_contract_id", "output_units", "reference",
)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"swap receipt {field} must be a non-empty string")
    return value


def _required_nonnegative_int(value: object, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"swap receipt {field} must be a {qualifier} integer")
    return value


def build_receipt(
    *, source_signature: str, solana_mint: str, solana_vault: str,
    nexus_token: str, nexus_account: str, output_txid: str,
    output_contract_id: int, output_units: int, reference: int,
) -> dict[str, str]:
    """Build the canonical all-string receipt payload from exact evidence."""
    return {
        "distordiaType": DISTORDIA_TYPE,
        "schema": SCHEMA,
        "source_signature": _required_text(source_signature, "source_signature"),
        "solana_mint": _required_text(solana_mint, "solana_mint"),
        "solana_vault": _required_text(solana_vault, "solana_vault"),
        "nexus_token": _required_text(nexus_token, "nexus_token"),
        "nexus_account": _required_text(nexus_account, "nexus_account"),
        "output_txid": _required_text(output_txid, "output_txid"),
        "output_contract_id": str(
            _required_nonnegative_int(output_contract_id, "output_contract_id")
        ),
        "output_units": str(
            _required_nonnegative_int(output_units, "output_units", positive=True)
        ),
        "reference": str(_required_nonnegative_int(reference, "reference")),
    }


def canonicalize_payload(payload: object) -> dict[str, str]:
    """Validate and return one exact canonical persisted/wire payload."""
    if (not isinstance(payload, dict) or set(payload) != set(REQUIRED_FIELDS)
            or any(not isinstance(value, str) for value in payload.values())):
        raise ValueError("swap receipt payload must contain exactly the canonical string fields")
    try:
        canonical = build_receipt(
            source_signature=payload["source_signature"],
            solana_mint=payload["solana_mint"],
            solana_vault=payload["solana_vault"],
            nexus_token=payload["nexus_token"],
            nexus_account=payload["nexus_account"],
            output_txid=payload["output_txid"],
            output_contract_id=int(payload["output_contract_id"]),
            output_units=int(payload["output_units"]),
            reference=int(payload["reference"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("swap receipt payload is not canonical") from exc
    if canonical != payload:
        raise ValueError("swap receipt payload is not canonical")
    return canonical


def receipt_name(source_signature: str) -> str:
    source = _required_text(source_signature, "source_signature")
    return "swap-receipt-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
