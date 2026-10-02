from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from src import (
    config,
    nexus_client,
    solana_client,
    startup_recovery,
    state_db,
    swap_solana,
)


def empty_solana_recovery_scan():
    return {
        "complete": True,
        "reason": None,
        "legacy_nexus_txids": {},
        "malformed_nexus_memos": [],
        "nexus_payouts": {},
        "nexus_payout_timestamps": {},
        "refund_sigs": {},
        "quarantined_sigs": {},
        "solana_dispositions": {},
    }


def remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def row(path: Path, sql: str, params=()):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, params).fetchone()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="swap-reeval-") as tmp:
        db = Path(tmp) / "state.db"
        state_db.DB_PATH = str(db)
        original_pair = config.SWAP_PAIR
        config.SWAP_PAIR = replace(
            original_pair,
            fees=replace(
                original_pair.fees,
                flat_to_nexus_units=0,
                basis_points=0,
                refund_solana_units=10,
            ),
        )
        config.MIN_DEPOSIT_SOLANA_UNITS = 1
        config.MAX_SWAP_SOLANA_UNITS = 1_000
        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 50
        source = ("wipe-capacity-source", 1_000, "nexus:recipient", "source-token", 1_100)
        state_db.init_db()
        state_db.add_unprocessed_sig(*source, "ready for processing", None)

        with patch.object(nexus_client, "is_valid_nexus_token_account", return_value=True), patch.object(
            solana_client, "_resolve_solana_token_destination", return_value="source-token"
        ), patch.object(solana_client, "send_solana_token_to_account_with_sig") as sol_send:
            classified = solana_client.process_unprocessed_solana_deposits(1, 10)
            held = solana_client.process_solana_deposits_refunding(1, 10)

        initial_policy = row(
            db,
            "SELECT status, policy_decision, amount_usdc_units FROM unprocessed_sigs WHERE sig=?",
            (source[0],),
        )
        initial_capacity = row(
            db,
            "SELECT kind, needed_units, cap_units, intent_evidence FROM solana_payout_capacity_holds WHERE source_signature=?",
            (source[0],),
        )
        initial_liability = state_db.get_unresolved_solana_liability_units()

        heartbeat_updates = []
        with patch.object(
            nexus_client,
            "update_heartbeat_asset",
            side_effect=lambda *args: heartbeat_updates.append(args) or True,
        ):
            swap_solana._advance_solana_waterline(
                current_wline=1,
                poll_start=2_000,
                fetch_ok=True,
                covered_through_ts=1_900,
            )
        externally_published_waterline = heartbeat_updates[-1][2]

        remove_sqlite(db)
        state_db.init_db()
        config.MAX_SWAP_SOLANA_UNITS = 2_000

        nexus_field, solana_field = nexus_client.heartbeat_waterline_field_names()
        heartbeat = {nexus_field: 1, solana_field: externally_published_waterline}
        with patch.object(nexus_client, "get_heartbeat_asset", return_value=heartbeat), patch.object(
            solana_client, "scan_memos_since_timestamp", side_effect=lambda _ts: empty_solana_recovery_scan()
        ), patch.object(
            nexus_client,
            "fetch_deposits_since",
            return_value=nexus_client.DepositScan([], True, None),
        ), patch.object(nexus_client, "get_last_reference", return_value=0):
            recovery = startup_recovery.perform_startup_recovery()

        admitted = state_db.commit_solana_deposit_scan_page(
            vault_account="vault",
            mint="mint",
            network="mainnet",
            commitment="finalized",
            query_identity="wipe-replay",
            lower_timestamp=externally_published_waterline,
            request_before_signature=None,
            next_before_signature=None,
            upper_timestamp=1_900,
            previous_timestamp=None,
            page_last_timestamp=source[1],
            scanned_signature_count=1,
            deposits=[source],
            complete=True,
        )

        nexus_send = Mock(return_value=(True, "nexus-debit-after-wipe"))
        with patch.object(nexus_client, "is_valid_nexus_token_account", return_value=True), patch.object(
            nexus_client, "debit_nexus_token_with_txid", nexus_send
        ):
            after_wipe_worker = solana_client.process_unprocessed_solana_deposits(1, 10)

        recovered_source = row(
            db,
            "SELECT status, policy_decision, amount_usdc_units, amount_usdd_units, txid FROM unprocessed_sigs WHERE sig=?",
            (source[0],),
        )
        recovered_capacity_count = row(
            db,
            "SELECT COUNT(*) FROM solana_payout_capacity_holds WHERE source_signature=?",
            (source[0],),
        )[0]
        output = {
            "before_wipe": {
                "deposit_worker": classified,
                "refund_worker": held,
                "solana_send_calls": sol_send.call_count,
                "source": initial_policy,
                "capacity_hold": initial_capacity[:3],
                "capacity_evidence_present": bool(initial_capacity[3]),
                "liability_units": initial_liability,
            },
            "waterline": {
                "source_timestamp": source[1],
                "published": externally_published_waterline,
                "behind_source": externally_published_waterline < source[1],
            },
            "after_total_db_loss": {
                "startup_recovery_complete": recovery.get("recovery_complete"),
                "startup_recovery_error": recovery.get("error"),
                "source_re_admitted": admitted,
                "deposit_worker": after_wipe_worker,
                "nexus_send_calls": nexus_send.call_count,
                "nexus_send_amount": nexus_send.call_args.args[1] if nexus_send.call_args else None,
                "source": recovered_source,
                "capacity_hold_count": recovered_capacity_count,
            },
        }
        print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
