from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src import config, solana_client, state_db


def remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="swap-cap-wipe-") as tmp:
        db = Path(tmp) / "state.db"
        state_db.DB_PATH = str(db)
        pair = replace(
            config.SWAP_PAIR,
            fees=replace(
                config.SWAP_PAIR.fees,
                flat_to_nexus_units=0,
                basis_points=0,
                refund_solana_units=10,
            ),
        )
        config.SWAP_PAIR = pair
        config.MIN_DEPOSIT_SOLANA_UNITS = 1
        config.MAX_SWAP_SOLANA_UNITS = 1_000
        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 50
        source = ("cap-wipe-fee-drift", 1_000, "nexus:recipient", "source-token", 1_100)

        state_db.init_db()
        state_db.add_unprocessed_sig(*source, "ready for processing", None)
        solana_client.process_unprocessed_solana_deposits(1, 10)
        with patch.object(
            solana_client, "_resolve_solana_token_destination", return_value="original-destination"
        ), patch.object(solana_client, "send_solana_token_to_account_with_sig") as first_send:
            solana_client.process_solana_deposits_refunding(1, 10)

        with sqlite3.connect(db) as conn:
            before_blob = conn.execute(
                "SELECT intent_evidence FROM solana_payout_capacity_holds WHERE source_signature=?",
                (source[0],),
            ).fetchone()[0]
        before = state_db._parse_solana_sig_disposition_intent_evidence(before_blob)

        remove_sqlite(db)
        state_db.init_db()
        config.SWAP_PAIR = replace(
            pair, fees=replace(pair.fees, refund_solana_units=20)
        )
        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 2_000
        state_db.commit_solana_deposit_scan_page(
            vault_account="vault",
            mint="mint",
            network="mainnet",
            commitment="finalized",
            query_identity="cap-wipe-replay",
            lower_timestamp=879,
            request_before_signature=None,
            next_before_signature=None,
            upper_timestamp=1_900,
            previous_timestamp=None,
            page_last_timestamp=source[1],
            scanned_signature_count=1,
            deposits=[source],
            complete=True,
        )
        solana_client.process_unprocessed_solana_deposits(1, 10)
        with patch.object(
            solana_client, "_resolve_solana_token_destination", return_value="changed-destination"
        ), patch.object(
            solana_client,
            "send_solana_token_to_account_with_sig",
            return_value=(True, "changed-refund-signature"),
        ) as second_send:
            submitted = solana_client.process_solana_deposits_refunding(1, 10)

        with sqlite3.connect(db) as conn:
            after_blob = conn.execute(
                "SELECT intent_evidence FROM refunded_sigs WHERE sig=?", (source[0],)
            ).fetchone()[0]
            source_status = conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig=?", (source[0],)
            ).fetchone()[0]
        after = state_db._parse_solana_sig_disposition_intent_evidence(after_blob)

        print(json.dumps({
            "before_wipe": {
                "send_calls": first_send.call_count,
                "destination": before["destination_token_account"],
                "payout_units": before["payout_amount_solana_units"],
                "fee_units": before["fee_solana_units"],
                "refund_fee_term": before["refund_solana_fee_units"],
            },
            "after_total_db_loss_and_reingestion": {
                "worker_submitted": submitted,
                "send_calls": second_send.call_count,
                "send_args": second_send.call_args.args,
                "source_status": source_status,
                "destination": after["destination_token_account"],
                "payout_units": after["payout_amount_solana_units"],
                "fee_units": after["fee_solana_units"],
                "refund_fee_term": after["refund_solana_fee_units"],
            },
        }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
