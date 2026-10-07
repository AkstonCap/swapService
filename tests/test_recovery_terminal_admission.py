"""Startup must audit persisted terminal provenance outside bounded chain scans."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from src import nexus_client, solana_client, startup_recovery, state_db


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
@pytest.mark.parametrize("provenance", [None, "legacy_unknown", "recovery_only"])
def test_startup_refuses_unproven_terminal_before_remote_recovery(tmp_path, kind, provenance):
    db_path = tmp_path / "restored.db"
    with patch.object(state_db, "DB_PATH", str(db_path)):
        state_db.init_db()
        details = state_db._SOLANA_SIG_DISPOSITION[kind]
        # This shape was emitted by legacy chain-only recovery. Its timestamp is
        # older than the heartbeat/rolling window, so an empty complete scan would
        # otherwise allow startup with an inferred fee and no pending liability.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"""INSERT INTO {details['table']}
                    (sig, timestamp, from_address, destination_address,
                     amount_usdc_units, memo, payout_memo,
                     {details['signature_column']}, {details['units_column']},
                     status, intent_provenance)
                    VALUES ('old-source', 10, 'source', 'destination', 10000,
                            'nexus:recipient', ?, 'old-payout', 1, ?, ?)""",
                (f"swapService:v1:{kind}:old-source", details["terminal_status"], provenance),
            )
            conn.execute(
                """INSERT INTO fee_entries
                    (sig, kind, amount_usdc_units, timestamp)
                    VALUES ('old-source', ?, 9999, 11)""",
                (f"{kind}_flat_fee",),
            )
            before = list(conn.iterdump())
        with patch.object(nexus_client, "get_heartbeat_asset", return_value={}) as heartbeat, patch.object(
            solana_client, "scan_memos_since_timestamp"
        ) as scan:
            result = startup_recovery.perform_startup_recovery()
        with sqlite3.connect(db_path) as conn:
            after = list(conn.iterdump())

    assert result["recovery_complete"] is False
    assert result["recovery_incomplete"] is True
    assert result["error"] == "solana_terminal_provenance_unresolved"
    assert before == after  # Refusal must not invent cap spend, fees or authorization.
    heartbeat.assert_not_called()
    scan.assert_not_called()


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
@pytest.mark.parametrize("legacy", [False, True])
def test_startup_allows_proven_terminal(tmp_path, kind, legacy):
    db_path = tmp_path / "proven.db"
    with patch.object(state_db, "DB_PATH", str(db_path)):
        state_db.init_db()
        details = state_db._SOLANA_SIG_DISPOSITION[kind]
        state_db.add_unprocessed_sig(
            "source", 10, "nexus:recipient", "sender", 110,
            details["ready_statuses"][0], None,
        )
        assert state_db.prepare_solana_sig_disposition(
            source_sig="source", kind=kind, timestamp=10, from_address="sender",
            destination_address="destination", amount_usdc_units=110,
            memo="nexus:recipient", payout_memo=f"swapService:v1:{kind}:source",
            payout_units=100, cap_units=1000,
        )
        if legacy:
            # Establish provenance from a real in-flight journal before finality.
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    f"UPDATE {details['table']} SET intent_provenance = NULL, intent_evidence = NULL"
                )
            state_db.init_db()
        assert state_db.record_solana_sig_disposition_submission(
            source_sig="source", kind=kind, payout_signature="payout"
        )
        assert state_db.confirm_solana_sig_disposition(
            source_sig="source", kind=kind, payout_signature="payout"
        )
        with patch.object(nexus_client, "get_heartbeat_asset", return_value={}) as heartbeat:
            result = startup_recovery.perform_startup_recovery()
        assert result["error"] == "heartbeat_missing"  # Reached the next gate.
        heartbeat.assert_called_once_with()
        assert state_db.get_unresolved_solana_liability_units() == 0


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
def test_startup_rejects_current_marker_without_matching_evidence(tmp_path, kind):
    db_path = tmp_path / "corrupted.db"
    with patch.object(state_db, "DB_PATH", str(db_path)):
        state_db.init_db()
        details = state_db._SOLANA_SIG_DISPOSITION[kind]
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"""INSERT INTO {details['table']}
                    (sig, timestamp, from_address, destination_address,
                     amount_usdc_units, memo, payout_memo,
                     {details['signature_column']}, {details['units_column']},
                     status, intent_provenance, intent_evidence)
                    VALUES ('old-source', 10, 'source', 'destination', 10000,
                            'nexus:recipient', ?, 'old-payout', 1, ?,
                            'pre_submission_v1', '{{}}')""",
                (f"swapService:v1:{kind}:old-source", details["terminal_status"]),
            )
        with patch.object(nexus_client, "get_heartbeat_asset") as heartbeat:
            result = startup_recovery.perform_startup_recovery()
        assert result["error"] == "solana_terminal_provenance_unresolved"
        heartbeat.assert_not_called()


def test_startup_provenance_read_failure_is_explicit_and_fail_closed(tmp_path):
    db_path = tmp_path / "damaged.db"
    with patch.object(state_db, "DB_PATH", str(db_path)):
        state_db.init_db()
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE refunded_sigs")
        with patch.object(nexus_client, "get_heartbeat_asset") as heartbeat:
            result = startup_recovery.perform_startup_recovery()
        assert result == {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "error": "solana_terminal_provenance_audit_failed",
        }
        heartbeat.assert_not_called()
        # The failed audit must release its reader transaction.
        with sqlite3.connect(db_path, timeout=0) as conn:
            conn.execute("BEGIN EXCLUSIVE")
