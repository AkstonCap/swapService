"""Durable, opt-in publication of public Nexus payout receipt assets.

Receipt creation spends operator NXS even though it does not move bridged tokens. This
development-only extension consumes only obligations frozen by exact payout finalization
and treats every uncertain create result as accepted-until-proven-otherwise.
"""
from __future__ import annotations

import json
from typing import Any

from . import config, nexus_client, receipt_contract, state_db

SCHEMA = receipt_contract.SCHEMA
DISTORDIA_TYPE = receipt_contract.DISTORDIA_TYPE
REQUIRED_FIELDS = receipt_contract.REQUIRED_FIELDS
build_receipt = receipt_contract.build_receipt
receipt_name = receipt_contract.receipt_name


def receipt_provider_registration() -> tuple[str | None, str]:
    """Validate the immutable registration contract needed to publish receipts.

    ``format=basic`` fixes an asset's field set at creation. Merely turning on receipt
    publication for an older heartbeat would otherwise create public receipt assets that
    its provider record does not advertise. Compare immutable pair/custody fields as
    well as the receipt schema so a misnamed or unrelated asset cannot supply an owner.
    """
    try:
        record = nexus_client.read_service_record()
    except Exception:
        return None, "configured provider registration is unavailable"
    if not isinstance(record, dict):
        return None, "configured provider registration is not readable"
    if record.get("receipt_schema") != SCHEMA:
        return None, (
            f"configured provider registration lacks receipt_schema={SCHEMA!r}; "
            "create and migrate to a new receipt-capable format=basic registration"
        )
    expected = nexus_client.build_service_record(last_poll=0)
    mismatched = [
        field for field in nexus_client.SERVICE_RECORD_IMMUTABLE
        if record.get(field) != expected[field]
    ]
    if mismatched:
        return None, (
            "configured provider registration does not match this service's immutable "
            f"pair/custody contract: {', '.join(mismatched)}"
        )
    owner = record.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None, "configured receipt-capable provider registration has no authoritative owner"
    return owner.strip(), "receipt-capable provider registration is valid"


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
        asset_address = str(decoded.get("address") or "").strip() or None
        create_txid = str(decoded.get("txid") or "").strip() or None
        # Response identity is evidence only; it can never permit a second create.
        state_db.record_swap_receipt_create_report(
            payload["source_signature"], create_txid=create_txid, asset_address=asset_address,
        )
        state_db.update_swap_receipt_publication(
            payload["source_signature"], "verifying",
            asset_address,
        )


def publish_pending_receipts(limit: int = 100) -> int:
    """Create each asset at most once and mark done only after exact owner readback."""
    if not getattr(config, "NEXUS_SWAP_RECEIPTS_ENABLED", False):
        return 0

    # Validate the entire selected batch before any create can spend NXS. A corrupted
    # later row must become visible manual work even when an earlier row is publishable.
    validated: list[tuple[dict, dict[str, str]]] = []
    for row in state_db.list_swap_receipts_for_publication(limit):
        try:
            payload = json.loads(row["payload_json"])
            canonical = receipt_contract.canonicalize_payload(payload)
            if receipt_name(canonical["source_signature"]) != row["receipt_name"]:
                raise ValueError("receipt name does not match canonical source identity")
            if canonical["source_signature"] != row["source_signature"]:
                raise ValueError("receipt row source does not match canonical payload")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            reason = nexus_client.redact(f"{type(exc).__name__}: {exc}")
            state_db.mark_swap_receipt_manual_review(row["source_signature"], reason)
            continue
        validated.append((row, canonical))

    needs_registration = any(
        row["status"] in {"awaiting_owner", "pending"} for row, _payload in validated
    )
    authenticated_owner = None
    if needs_registration:
        # One authoritative registration snapshot governs the whole invocation. Re-reading
        # per row can mix owners/configurations within a single publication batch.
        authenticated_owner, _reason = receipt_provider_registration()

    published = 0
    for row, payload in validated:
        if row["status"] == "awaiting_owner":
            if not authenticated_owner or not state_db.bind_swap_receipt_owner(
                row["source_signature"], authenticated_owner
            ):
                continue
            row = state_db.get_swap_receipt(row["source_signature"])
            if row is None:
                continue

        if row["status"] == "pending":
            # The current authenticated registration must retain the immutable owner
            # before crossing the budgeted NXS-spending boundary.
            if authenticated_owner != row["expected_owner"]:
                continue
            if not state_db.claim_swap_receipt_with_nxs_budget(
                row["source_signature"],
                expected_cost_nxs_units=getattr(
                    config, "NEXUS_SWAP_RECEIPT_EXPECTED_COST_NXS_UNITS", 0
                ),
                budget_nxs_units=getattr(config, "NEXUS_SWAP_RECEIPT_BUDGET_NXS_UNITS", 0),
            ):
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
