"""Durable, opt-in publication of public Nexus payout receipt assets.

This module owns no money movement.  It consumes only obligations frozen by exact payout
finalization and treats every uncertain create result as accepted-until-proven-otherwise.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import config, nexus_client, state_db

SCHEMA = "nexus-swap-receipt-v1"
DISTORDIA_TYPE = "nexusSwapReceipt"
REQUIRED_FIELDS = (
    "distordiaType", "schema", "source_signature", "solana_mint", "solana_vault",
    "nexus_token", "nexus_account", "output_txid", "output_contract_id",
    "output_units", "reference",
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
        "output_contract_id": str(_required_nonnegative_int(output_contract_id, "output_contract_id")),
        "output_units": str(_required_nonnegative_int(output_units, "output_units", positive=True)),
        "reference": str(_required_nonnegative_int(reference, "reference")),
    }


def receipt_name(source_signature: str) -> str:
    source = _required_text(source_signature, "source_signature")
    return "swap-receipt-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def expected_provider_owner() -> str | None:
    """Read owner from our provider registration; never accept caller-configured owner."""
    record = nexus_client.read_service_record()
    if not isinstance(record, dict):
        return None
    owner = record.get("owner")
    return owner.strip() if isinstance(owner, str) and owner.strip() else None


def _decode_list_output(output: str) -> list[dict[str, Any]] | None:
    decoded = nexus_client._parse_json_lenient(output)
    if isinstance(decoded, dict) and "result" in decoded:
        decoded = decoded["result"]
    if (not isinstance(decoded, list) or len(decoded) >= 100
            or any(not isinstance(item, dict) for item in decoded)):
        # A full page cannot prove there is no duplicate exact receipt on a later page.
        return None
    return decoded


def _query_exact_receipts(source_signature: str) -> list[dict[str, Any]] | None:
    fields = "owner,address," + ",".join(REQUIRED_FIELDS)
    command = [
        config.NEXUS_CLI,
        f"register/list/assets:asset/{fields}",
        f"where=results.source_signature={source_signature}",
        "limit=100",
    ]
    try:
        code, output, _error = nexus_client._run(
            command, timeout=getattr(config, "NEXUS_SWAP_RECEIPT_TIMEOUT_SEC", 20)
        )
    except Exception:
        return None
    if code != 0:
        return None
    return _decode_list_output(output)


def _exact_matches(row: dict, payload: dict[str, str], expected_owner: str) -> bool:
    if row.get("owner") != expected_owner:
        return False
    if any(row.get(field) != payload[field] for field in REQUIRED_FIELDS):
        return False
    # Require canonical wire integers, not signs, whitespace, bools or alternate spellings.
    for field, positive in (("output_contract_id", False), ("output_units", True), ("reference", False)):
        value = row.get(field)
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            return False
        if str(int(value)) != value or (positive and int(value) <= 0):
            return False
    return isinstance(row.get("address"), str) and bool(row["address"].strip())


def _readback(row: dict, payload: dict[str, str]) -> str | None:
    records = _query_exact_receipts(payload["source_signature"])
    if records is None:
        return None
    matches = [record for record in records
               if _exact_matches(record, payload, row["expected_owner"])]
    if len(matches) != 1:
        return None
    return matches[0]["address"].strip()


def _create(row: dict, payload: dict[str, str]) -> None:
    fields = [
        {"name": field, "type": "string", "value": payload[field], "mutable": False}
        for field in REQUIRED_FIELDS
    ]
    command = [
        config.NEXUS_CLI, "assets/create/asset", "format=JSON",
        f"name={row['receipt_name']}",
        "json=" + json.dumps(fields, separators=(",", ":")),
        f"pin={config.NEXUS_PIN}",
    ]
    # Deliberately no owner= argument: Nexus derives owner from the authenticated profile.
    code, output, _error = nexus_client._run(
        command, timeout=getattr(config, "NEXUS_SWAP_RECEIPT_TIMEOUT_SEC", 20)
    )
    if code != 0:
        return
    decoded = nexus_client._parse_json_lenient(output)
    if isinstance(decoded, dict) and not decoded.get("error"):
        state_db.update_swap_receipt_publication(
            payload["source_signature"], "verifying",
            str(decoded.get("address") or "").strip() or None,
        )


def publish_pending_receipts(limit: int = 100) -> int:
    """Create each asset at most once and mark done only after exact owner readback."""
    if not getattr(config, "NEXUS_SWAP_RECEIPTS_ENABLED", False):
        return 0
    published = 0
    for row in state_db.list_swap_receipts_for_publication(limit):
        try:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict) or set(payload) != set(REQUIRED_FIELDS):
                continue
            # Rebuild from typed values to reject malformed/corrupted durable payloads.
            canonical = build_receipt(
                source_signature=payload.get("source_signature"),
                solana_mint=payload.get("solana_mint"), solana_vault=payload.get("solana_vault"),
                nexus_token=payload.get("nexus_token"), nexus_account=payload.get("nexus_account"),
                output_txid=payload.get("output_txid"),
                output_contract_id=int(payload["output_contract_id"]),
                output_units=int(payload["output_units"]), reference=int(payload["reference"]),
            )
            if canonical != payload or receipt_name(payload["source_signature"]) != row["receipt_name"]:
                continue
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue

        if row["status"] == "pending":
            # Re-read authoritative profile-derived owner before crossing create boundary.
            if expected_provider_owner() != row["expected_owner"]:
                continue
            if not state_db.claim_swap_receipt(row["source_signature"]):
                continue
            row = state_db.get_swap_receipt(row["source_signature"])
            if row is None:
                continue
            try:
                _create(row, payload)
            except Exception:
                # Timeout may follow acceptance. Status remains creating forever unless
                # deterministic exact readback resolves it; never submit create again.
                continue

        address = _readback(row, payload)
        if address and state_db.update_swap_receipt_publication(
            row["source_signature"], "published", address
        ):
            published += 1
    return published
