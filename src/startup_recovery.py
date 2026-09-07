"""Startup recovery & reconstruction utilities.

Responsibilities:
1. Fetch waterlines from Nexus heartbeat asset
2. Rebuild database from waterline timestamps (complete server wipeout recovery)
3. Reconstruct processed_txids markers for Nexus->Solana sends using on-chain memos
4. Reconstruct refunded_sigs for Solana refunds via refundSig:<deposit_sig> memos
5. Seed reference counter if missing
6. Provide summary of all actions taken

Design notes:
 - We intentionally do NOT mutate historical database records except to add missing processed markers;
   reconstruction is additive and idempotent.
 - Waterline-based scanning allows full recovery from complete database loss.
 - Falls back to recent-only scan only if waterlines are unavailable or unset.
 - Reference seeding heuristic: choose max(reference found in database OR Nexus) + 1.
"""
from __future__ import annotations
from decimal import Decimal
from . import config, solana_client, nexus_client, nexus_memo, state_db
import time

QUARANTINED_MEMO_PREFIX = "quarantinedSig:"


def _parse_decimal_amount(val) -> Decimal:
    """Parse a Nexus token amount into Decimal."""
    if val is None:
        return Decimal(0)
    try:
        return Decimal(str(val).strip())
    except Exception:
        try:
            return Decimal(float(val))
        except Exception:
            return Decimal(0)


def _rebuild_nexus_from_waterline(
    waterline_timestamp: int,
    *,
    paid_nexus_payouts: dict[
        tuple[str, int], nexus_memo.NexusPayoutEvidence
    ] | None = None,
) -> dict:
    """Rebuild exact Nexus CREDIT states from a complete scan and payout evidence."""
    treasury_addr = config.SWAP_PAIR.nexus.treasury_account
    if not treasury_addr:
        return {
            "recovery_complete": False,
            "nexus_deposits_added": 0,
            "error": "no_treasury_configured",
        }

    payouts = paid_nexus_payouts or {}
    if not isinstance(payouts, dict):
        return {
            "recovery_complete": False,
            "nexus_deposits_added": 0,
            "error": "invalid_nexus_payout_evidence",
        }
    for identity, evidence in payouts.items():
        if (not isinstance(identity, tuple) or len(identity) != 2
                or not nexus_memo.is_canonical_nexus_txid(identity[0])
                or type(identity[1]) is not int or identity[1] < 0
                or not isinstance(evidence, nexus_memo.NexusPayoutEvidence)
                or evidence.txid != identity[0]
                or evidence.contract_id != identity[1]
                or not evidence.solana_signature
                or not evidence.to_token_account
                or type(evidence.amount_solana_units) is not int
                or evidence.amount_solana_units <= 0):
            return {
                "recovery_complete": False,
                "nexus_deposits_added": 0,
                "error": "invalid_nexus_payout_evidence",
            }

    print(f"   Rebuilding Nexus deposits from waterline {waterline_timestamp}...")
    scan = nexus_client.fetch_deposits_since(treasury_addr, waterline_timestamp)
    if not isinstance(scan, nexus_client.DepositScan) or not scan.complete:
        reason = scan.reason if isinstance(scan, nexus_client.DepositScan) else "invalid_result"
        return {
            "recovery_complete": False,
            "nexus_deposits_added": 0,
            "nexus_deposits_scanned": 0,
            "error": f"nexus_deposit_scan_incomplete:{reason or 'unknown'}",
        }
    if not isinstance(scan.deposits, list):
        return {
            "recovery_complete": False,
            "nexus_deposits_added": 0,
            "nexus_deposits_scanned": 0,
            "error": "nexus_deposit_scan_incomplete:invalid_deposits",
        }

    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    try:
        processed = set(conn.execute("SELECT txid, contract_id FROM processed_txids"))
        refunded = set(conn.execute("SELECT txid, contract_id FROM refunded_txids"))
        quarantined = set(conn.execute("SELECT txid, contract_id FROM quarantined_txids"))
        unprocessed_rows = {
            (row[0], row[1]): {
                "timestamp": row[2],
                "amount_usdd": row[3],
                "from_address": row[4],
                "to_address": row[5],
                "owner": row[6],
                "status": row[7],
                "receival_account": row[8],
                "amount_usdd_units": row[9],
                "payout_solana_units": row[10],
                "payout_fee_nexus_units": row[11],
            }
            for row in conn.execute(
                """SELECT txid, contract_id, timestamp, amount_usdd, from_address,
                          to_address, owner_from_address, status, receival_account,
                          amount_usdd_units, payout_solana_units, payout_fee_nexus_units
                   FROM unprocessed_txids"""
            )
        }
        unprocessed = set(unprocessed_rows)
    finally:
        conn.close()

    credits: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for tx in scan.deposits:
        if not isinstance(tx, dict):
            return {"recovery_complete": False, "error": "nexus_deposit_scan_incomplete:invalid_transaction"}
        txid = tx.get("txid")
        timestamp = tx.get("timestamp")
        confirmations = tx.get("confirmations")
        contracts = tx.get("contracts")
        if (not nexus_memo.is_canonical_nexus_txid(txid)
                or isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0
                or isinstance(confirmations, bool) or not isinstance(confirmations, int)
                or confirmations < 0 or not isinstance(contracts, list)):
            return {"recovery_complete": False, "error": "nexus_deposit_scan_incomplete:malformed_evidence"}
        for contract in contracts:
            if not isinstance(contract, dict):
                return {"recovery_complete": False, "error": "nexus_deposit_scan_incomplete:invalid_contract"}
            if str(contract.get("OP") or "").upper() != "CREDIT":
                continue
            to_address = nexus_client._parse_nexus_contract_address(contract.get("to"))
            if to_address != treasury_addr:
                continue
            contract_id = contract.get("id")
            sender = nexus_client._parse_nexus_contract_address(contract.get("from"))
            if (type(contract_id) is not int or contract_id < 0 or not sender):
                return {"recovery_complete": False, "error": "nexus_deposit_scan_incomplete:malformed_credit"}
            identity = (txid, contract_id)
            if identity in seen:
                return {"recovery_complete": False, "error": "nexus_deposit_scan_incomplete:duplicate_credit_identity"}
            seen.add(identity)
            amount_dec = _parse_decimal_amount(contract.get("amount"))
            classification = nexus_client.classify_nexus_credit(contract.get("amount"))
            if classification.disposition == "invalid" and not (
                amount_dec.is_finite() and amount_dec > 0
            ):
                return {"recovery_complete": False, "error": "nexus_deposit_scan_incomplete:invalid_credit_amount"}
            owner = ""
            if classification.disposition != "dust":
                account = nexus_client.get_account_info(sender)
                if not isinstance(account, dict) or not str(account.get("owner") or "").strip():
                    return {"recovery_complete": False, "error": "nexus_credit_owner_evidence_unavailable"}
                owner = str(account["owner"])
            credits.append({
                "identity": identity,
                "txid": txid,
                "contract_id": contract_id,
                "timestamp": timestamp,
                "confirmations": confirmations,
                "amount_dec": amount_dec,
                "classification": classification,
                "sender": sender,
                "to_address": to_address,
                "owner": owner,
            })

    matched_payouts = {identity for identity in payouts if identity in processed}
    for credit in credits:
        identity = credit["identity"]
        if identity not in payouts:
            continue
        if identity in refunded or identity in quarantined:
            return {"recovery_complete": False, "error": "nexus_payout_conflicts_with_terminal_state"}
        if credit["classification"].disposition != "payable":
            return {"recovery_complete": False, "error": "nexus_payout_source_not_payable"}
        matched_payouts.add(identity)
    if matched_payouts != set(payouts):
        return {"recovery_complete": False, "error": "nexus_payout_source_evidence_missing"}

    added_count = 0
    skipped_processed = 0
    skipped_fees = 0
    reconstructed_payouts = 0
    for credit in credits:
        identity = credit["identity"]
        classification = credit["classification"]
        if identity in processed or identity in refunded or identity in quarantined:
            skipped_processed += 1
            continue
        if classification.disposition == "dust":
            continue
        payout_evidence = payouts.get(identity)
        if payout_evidence is not None:
            payout_fee_units = classification.amount_nexus_units - config.solana_units_to_nexus(
                payout_evidence.amount_solana_units, round_up=False
            )
            if payout_fee_units < 0 or payout_fee_units > classification.amount_nexus_units:
                return {"recovery_complete": False, "error": "invalid_nexus_payout_amount_evidence"}

            if identity not in unprocessed:
                state_db.add_unprocessed_txid(
                    txid=credit["txid"], contract_id=credit["contract_id"],
                    timestamp=credit["timestamp"], amount_usdd=float(credit["amount_dec"]),
                    from_address=credit["sender"], to_address=credit["to_address"],
                    owner_from_address=credit["owner"],
                    confirmations_credit=credit["confirmations"], status="ready for processing",
                    receival_account=payout_evidence.to_token_account,
                    amount_usdd_units=classification.amount_nexus_units,
                )
                unprocessed.add(identity)
                unprocessed_rows[identity] = {
                    "timestamp": credit["timestamp"],
                    "amount_usdd": float(credit["amount_dec"]),
                    "from_address": credit["sender"],
                    "to_address": credit["to_address"],
                    "owner": credit["owner"],
                    "status": "ready for processing",
                    "receival_account": payout_evidence.to_token_account,
                    "amount_usdd_units": classification.amount_nexus_units,
                    "payout_solana_units": None,
                    "payout_fee_nexus_units": None,
                }
            row = unprocessed_rows[identity]
            expected_source = (
                credit["timestamp"], float(credit["amount_dec"]),
                credit["sender"], credit["to_address"], credit["owner"],
                payout_evidence.to_token_account, classification.amount_nexus_units,
            )
            actual_source = (
                row["timestamp"], row["amount_usdd"], row["from_address"],
                row["to_address"], row["owner"], row["receival_account"],
                row["amount_usdd_units"],
            )
            if actual_source != expected_source:
                return {"recovery_complete": False, "error": "nexus_paid_source_state_conflict"}
            if row["status"] == "ready for processing":
                prepared = state_db.prepare_nexus_payout(
                    txid=credit["txid"], contract_id=credit["contract_id"],
                    receival_account=payout_evidence.to_token_account,
                    amount_usdd_units=classification.amount_nexus_units,
                    payout_solana_units=payout_evidence.amount_solana_units,
                    payout_fee_nexus_units=payout_fee_units,
                )
                if not prepared:
                    return {"recovery_complete": False, "error": "nexus_payout_term_persistence_failed"}
                state_db.update_unprocessed_txid(
                    credit["txid"], contract_id=credit["contract_id"],
                    sig=payout_evidence.solana_signature,
                )
            elif (row["status"] not in {"sending", "sig created, awaiting confirmations"}
                    or row["payout_solana_units"] != payout_evidence.amount_solana_units
                    or row["payout_fee_nexus_units"] != payout_fee_units):
                return {"recovery_complete": False, "error": "nexus_paid_source_held_incomplete"}

            finalized = state_db.finalize_nexus_credit(
                txid=credit["txid"], contract_id=credit["contract_id"],
                timestamp=credit["timestamp"], amount_usdd=float(credit["amount_dec"]),
                amount_usdd_units=classification.amount_nexus_units,
                from_address=credit["sender"], to_address=credit["to_address"],
                owner=credit["owner"], sig=payout_evidence.solana_signature,
                status="processed from attributable composite payout evidence",
                fee_kind="swap_nexus_to_solana" if payout_fee_units else None,
                fee_nexus_units=payout_fee_units,
            )
            if not finalized:
                return {"recovery_complete": False, "error": "nexus_payout_finalization_failed"}
            processed.add(identity)
            unprocessed.discard(identity)
            reconstructed_payouts += 1
            continue

        if identity not in unprocessed:
            if classification.disposition == "invalid":
                state_db.add_unprocessed_txid(
                    txid=credit["txid"], contract_id=credit["contract_id"],
                    timestamp=credit["timestamp"], amount_usdd=float(credit["amount_dec"]),
                    from_address=credit["sender"], to_address=credit["to_address"],
                    owner_from_address=credit["owner"],
                    confirmations_credit=credit["confirmations"], status="quarantined",
                    amount_usdd_units=None, hold_reason="invalid_exact_nexus_amount",
                )
                unprocessed.add(identity)
                continue
            status = "refund pending" if classification.disposition == "over_cap" else "pending_receival"
            state_db.add_unprocessed_txid(
                txid=credit["txid"], contract_id=credit["contract_id"],
                timestamp=credit["timestamp"], amount_usdd=float(credit["amount_dec"]),
                from_address=credit["sender"], to_address=credit["to_address"],
                owner_from_address=credit["owner"],
                confirmations_credit=credit["confirmations"], status=status,
                amount_usdd_units=classification.amount_nexus_units,
            )
            unprocessed.add(identity)

        if classification.disposition in {"below_minimum", "fee_only"}:
            kind = (
                "below_min_credit_nexus"
                if classification.disposition == "below_minimum"
                else "fee_only_nexus_credit"
            )
            finalized = state_db.finalize_nexus_credit(
                txid=credit["txid"], contract_id=credit["contract_id"],
                timestamp=credit["timestamp"], amount_usdd=float(credit["amount_dec"]),
                amount_usdd_units=classification.amount_nexus_units,
                from_address=credit["sender"], to_address=credit["to_address"],
                owner=credit["owner"], sig="", status="processed as fees",
                fee_kind=kind, fee_nexus_units=classification.amount_nexus_units,
            )
            if not finalized:
                return {"recovery_complete": False, "error": "nexus_fee_finalization_failed"}
            processed.add(identity)
            unprocessed.discard(identity)
            skipped_fees += 1
            continue
        added_count += 1

    return {
        "recovery_complete": True,
        "nexus_deposits_added": added_count,
        "nexus_from_timestamp": waterline_timestamp,
        "nexus_deposits_scanned": len(scan.deposits),
        "nexus_skipped_processed": skipped_processed,
        "nexus_skipped_fees": skipped_fees,
        "nexus_payouts_reconstructed": reconstructed_payouts,
    }


def _rebuild_solana_from_waterline(waterline_timestamp: int) -> dict:
    """Collect complete Solana payout evidence without creating sparse terminal rows."""
    print(f"   Rebuilding Solana markers from waterline {waterline_timestamp}...")
    memo_map = solana_client.scan_memos_since_timestamp(waterline_timestamp)
    if not isinstance(memo_map, dict):
        return {
            "recovery_complete": False,
            "error": "solana_memo_scan_incomplete:invalid_result",
        }
    if memo_map.get("complete") is not True:
        return {
            "recovery_complete": False,
            "error": f"solana_memo_scan_incomplete:{memo_map.get('reason') or 'unknown'}",
        }
    legacy_payouts = memo_map.get("legacy_nexus_txids")
    if not isinstance(legacy_payouts, dict):
        return {
            "recovery_complete": False,
            "error": "solana_memo_scan_incomplete:invalid_legacy_payout_evidence",
        }
    if legacy_payouts:
        return {
            "recovery_complete": False,
            "error": "legacy_nexus_payout_identity_unresolved",
            "legacy_nexus_payouts_unresolved": len(legacy_payouts),
        }

    malformed = memo_map.get("malformed_nexus_memos")
    payouts = memo_map.get("nexus_payouts")
    refunds = memo_map.get("refund_sigs")
    quarantines = memo_map.get("quarantined_sigs")
    if not isinstance(malformed, list) or not isinstance(payouts, dict):
        return {
            "recovery_complete": False,
            "error": "solana_memo_scan_incomplete:invalid_payout_evidence",
        }
    if malformed:
        return {
            "recovery_complete": False,
            "error": "solana_memo_scan_incomplete:malformed_nexus_payout_memo",
        }
    if not isinstance(refunds, dict) or not isinstance(quarantines, dict):
        return {
            "recovery_complete": False,
            "error": "solana_memo_scan_incomplete:invalid_marker_evidence",
        }
    # A refund/quarantine memo is positive chain evidence, but it does not contain the
    # original deposit amount/source needed for an authoritative terminal DB row. Hold
    # startup rather than manufacturing amount=0 placeholders that look settled.
    if refunds or quarantines:
        return {
            "recovery_complete": False,
            "error": "sparse_solana_marker_evidence_unresolved",
            "unresolved_refund_memos": len(refunds),
            "unresolved_quarantine_memos": len(quarantines),
        }

    validated_payouts: dict[tuple[str, int], nexus_memo.NexusPayoutEvidence] = {}
    for identity, evidence in payouts.items():
        if (not isinstance(identity, tuple) or len(identity) != 2
                or not nexus_memo.is_canonical_nexus_txid(identity[0])
                or type(identity[1]) is not int or identity[1] < 0
                or not isinstance(evidence, nexus_memo.NexusPayoutEvidence)
                or evidence.txid != identity[0]
                or evidence.contract_id != identity[1]
                or not isinstance(evidence.solana_signature, str)
                or not evidence.solana_signature.strip()
                or not isinstance(evidence.to_token_account, str)
                or not evidence.to_token_account.strip()
                or type(evidence.amount_solana_units) is not int
                or evidence.amount_solana_units <= 0):
            return {
                "recovery_complete": False,
                "error": "solana_memo_scan_incomplete:invalid_payout_evidence",
            }
        validated_payouts[(identity[0], identity[1])] = evidence

    return {
        "recovery_complete": True,
        "solana_from_timestamp": waterline_timestamp,
        "solana_found_processed_memos": len(validated_payouts),
        "solana_found_refund_memos": 0,
        "solana_found_quarantined_memos": 0,
        "_nexus_payouts": validated_payouts,
    }


def _fallback_recent_scan() -> dict:
    """Report that a bounded recent scan cannot authorize exposure."""
    return {
        "recovery_complete": False,
        "recovery_incomplete": True,
        "fallback_mode": True,
        "error": "bounded_recent_scan_not_authoritative",
    }


def perform_startup_recovery() -> dict:
    """Recover both chains from nonzero checkpoints or return an explicit latch."""
    print("🔧 Starting recovery...")

    try:
        interrupted_nexus_transfers_held = state_db.recover_interrupted_nexus_transfer_intents()
    except Exception as exc:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "error": f"interrupted_transfer_hold_failed:{exc}",
        }

    try:
        heartbeat = nexus_client.get_heartbeat_asset()
    except Exception as exc:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "interrupted_nexus_transfers_held": interrupted_nexus_transfers_held,
            "error": f"heartbeat_lookup_failed:{exc}",
        }
    if not isinstance(heartbeat, dict) or not heartbeat:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "interrupted_nexus_transfers_held": interrupted_nexus_transfers_held,
            "error": "heartbeat_missing",
        }

    try:
        waterlines = nexus_client.parse_heartbeat_waterlines(heartbeat)
    except ValueError as exc:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "interrupted_nexus_transfers_held": interrupted_nexus_transfers_held,
            "error": f"heartbeat_waterline_schema_incompatible:{exc}",
        }
    nexus_waterline = waterlines.nexus
    solana_waterline = waterlines.solana
    missing = [
        chain for chain, checkpoint in (
            ("nexus", nexus_waterline), ("solana", solana_waterline)
        ) if type(checkpoint) is not int or checkpoint <= 0
    ]
    if missing:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "interrupted_nexus_transfers_held": interrupted_nexus_transfers_held,
            "error": "zero_or_missing_checkpoint:" + ",".join(missing),
        }

    print(f"   Waterlines: Nexus={nexus_waterline}, Solana={solana_waterline}")

    try:
        solana_stats = _rebuild_solana_from_waterline(solana_waterline)
    except Exception as exc:
        solana_stats = {"recovery_complete": False, "error": f"solana_rebuild_exception:{exc}"}
    if solana_stats.get("recovery_complete") is not True:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "waterline_mode": True,
            "nexus_waterline": nexus_waterline,
            "solana_waterline": solana_waterline,
            "interrupted_nexus_transfers_held": interrupted_nexus_transfers_held,
            **solana_stats,
        }

    payouts = solana_stats.pop("_nexus_payouts", {})
    try:
        nexus_stats = _rebuild_nexus_from_waterline(
            nexus_waterline, paid_nexus_payouts=payouts
        )
    except Exception as exc:
        nexus_stats = {"recovery_complete": False, "error": f"nexus_rebuild_exception:{exc}"}
    if nexus_stats.get("recovery_complete") is not True:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "waterline_mode": True,
            "nexus_waterline": nexus_waterline,
            "solana_waterline": solana_waterline,
            "interrupted_nexus_transfers_held": interrupted_nexus_transfers_held,
            **solana_stats,
            **nexus_stats,
        }

    try:
        seeded = nexus_client.get_last_reference()
    except Exception as exc:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "error": f"reference_lookup_failed:{exc}",
        }
    if type(seeded) is not int or seeded < 0:
        return {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "error": "reference_seed_unavailable",
        }

    return {
        "recovery_complete": True,
        "recovery_incomplete": False,
        "waterline_mode": True,
        "nexus_waterline": nexus_waterline,
        "solana_waterline": solana_waterline,
        "reference_seeded": seeded,
        "interrupted_nexus_transfers_held": interrupted_nexus_transfers_held,
        **solana_stats,
        **nexus_stats,
    }

