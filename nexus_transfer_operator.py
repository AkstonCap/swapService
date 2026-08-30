#!/usr/bin/env python3
"""Explicit, audited CLI for Nexus refund/quarantine intent disposition.

This tool is intentionally separate from the service loop. It never retries a Nexus
account debit and requires the operator to retype each immutable chain reference before
authorization or finalization.
"""
from __future__ import annotations

import argparse
import json
import sys

from src import state_db

HOLD_STATUS = "refund held for operator review"


def _nexus_modules():
    """Load chain dependencies only for a command that needs a Nexus node."""
    from src import config, nexus_client
    return config, nexus_client


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=str))


def _held_credit(txid: str) -> dict:
    matches = [row for row in state_db.get_unprocessed_txids_as_dicts() if row.get("txid") == txid]
    if len(matches) != 1:
        raise ValueError("held source credit was not found")
    row = matches[0]
    if row.get("comment") != HOLD_STATUS:
        raise ValueError("source credit is not in the explicit refund-hold state")
    if int(row.get("amount_usdd_units") or 0) <= 0:
        raise ValueError("held source credit lacks an exact positive base-unit amount")
    return row


def _prepare(args: argparse.Namespace) -> int:
    row = _held_credit(args.txid)
    config, _ = _nexus_modules()
    treasury = str(getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", "") or "")
    if not treasury:
        raise ValueError("NEXUS_USDD_TREASURY_ACCOUNT is required")
    if args.kind == "refund":
        destination = str(row.get("from") or "")
        if not destination:
            raise ValueError("held source credit has no sender address for a refund")
    else:
        destination = str(getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", "") or "")
        if not destination:
            raise ValueError("NEXUS_USDD_QUARANTINE_ACCOUNT is required for quarantine")
    intent = state_db.create_nexus_transfer_intent(
        kind=args.kind, source_txid=args.txid, from_address=treasury, to_address=destination,
        amount_usdd_units=int(row["amount_usdd_units"]),
    )
    state_db.record_nexus_transfer_preparation(intent["id"], actor=args.operator, rationale=args.reason)
    _emit({"intent": intent, "next": "review then authorize with --confirm-reference"})
    return 0


def _list(args: argparse.Namespace) -> int:
    statuses = tuple(args.status) if args.status else (
        "prepared", "authorized", "executing", "submitted", "outcome_unknown", "completed",
    )
    _emit(state_db.get_nexus_transfer_intents_by_status(statuses, limit=args.limit))
    return 0


def _show(args: argparse.Namespace) -> int:
    intent = state_db.get_nexus_transfer_intent(args.intent)
    if intent is None:
        raise ValueError("Nexus transfer intent does not exist")
    _emit({"intent": intent, "audit_events": state_db.get_nexus_transfer_audit_events(args.intent)})
    return 0


def _authorize(args: argparse.Namespace) -> int:
    intent = state_db.authorize_nexus_transfer_intent(
        args.intent, actor=args.operator, rationale=args.reason,
        expected_reference=args.confirm_reference,
    )
    _emit({"intent": intent, "next": "execute is a separate explicit command"})
    return 0


def _execute(args: argparse.Namespace) -> int:
    _, nexus_client = _nexus_modules()
    state_db.record_nexus_transfer_execution_request(
        args.intent, actor=args.operator, rationale=args.reason,
    )
    outcome = nexus_client.execute_nexus_transfer_intent(args.intent)
    _emit({"intent_id": args.intent, "executed": outcome.executed,
           "status": outcome.status, "remote_txid": outcome.remote_txid,
           "next": "resolve by positive chain-reference match; do not retry"})
    return 0


def _resolve(args: argparse.Namespace) -> int:
    _, nexus_client = _nexus_modules()
    count = nexus_client.resolve_nexus_transfer_intents(limit=args.limit)
    _emit({"resolved": count, "note": "only positive reference matches are completed"})
    return 0


def _finalize(args: argparse.Namespace) -> int:
    finalized = state_db.finalize_nexus_transfer_disposition(
        args.intent, actor=args.operator, rationale=args.reason,
        expected_remote_txid=args.confirm_remote_txid,
    )
    if not finalized:
        raise ValueError("refused: exact completed intent, remote txid, and held source evidence are required")
    _emit({"intent_id": args.intent, "finalized": True})
    return 0


def _operator_reason(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator", required=True, help="named human authorizing this action")
    parser.add_argument("--reason", required=True, help="durable human-readable rationale")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="state DB path (default: STATE_DB_PATH)")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="prepare refund/quarantine intent from a held credit")
    prepare.add_argument("--kind", required=True, choices=("refund", "quarantine"))
    prepare.add_argument("--txid", required=True, help="held Nexus credit txid")
    _operator_reason(prepare)
    prepare.set_defaults(handler=_prepare)

    listing = commands.add_parser("list", help="list transfer intents")
    listing.add_argument("--status", action="append", help="repeat to filter by status")
    listing.add_argument("--limit", type=int, default=200)
    listing.set_defaults(handler=_list)

    show = commands.add_parser("show", help="show one intent plus immutable audit events")
    show.add_argument("--intent", required=True)
    show.set_defaults(handler=_show)

    authorize = commands.add_parser("authorize", help="authorize one prepared intent")
    authorize.add_argument("--intent", required=True)
    authorize.add_argument("--confirm-reference", required=True, help="must exactly equal displayed reference")
    _operator_reason(authorize)
    authorize.set_defaults(handler=_authorize)

    execute = commands.add_parser("execute", help="consume one authorization and invoke Nexus CLI once")
    execute.add_argument("--intent", required=True)
    _operator_reason(execute)
    execute.set_defaults(handler=_execute)

    resolve = commands.add_parser("resolve", help="look up references; never invokes a debit")
    resolve.add_argument("--limit", type=int, default=200)
    resolve.set_defaults(handler=_resolve)

    finalize = commands.add_parser("finalize", help="archive source only after exact completed chain evidence")
    finalize.add_argument("--intent", required=True)
    finalize.add_argument("--confirm-remote-txid", required=True,
                          help="must exactly equal the completed intent remote txid")
    _operator_reason(finalize)
    finalize.set_defaults(handler=_finalize)

    args = parser.parse_args(argv)
    if args.db:
        state_db.DB_PATH = args.db
    state_db.init_db()
    try:
        return int(args.handler(args))
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
