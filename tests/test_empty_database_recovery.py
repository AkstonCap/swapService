"""Empty-database startup must not turn source rediscovery into authorization."""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
from dataclasses import replace
from unittest.mock import Mock

import pytest

from src import alerts, config, main, nexus_client, solana_client, startup_recovery, state_db


@pytest.fixture
def recovery_env(monkeypatch, tmp_path):
    def no_network(*_args, **_kwargs):
        raise AssertionError("recovery regression must remain offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(alerts, "warning", Mock())
    monkeypatch.setattr(alerts, "critical", Mock())
    path = tmp_path / "custody.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(path))
    state_db.init_db()
    monkeypatch.setattr(nexus_client, "get_heartbeat_asset", lambda: {
        "last_safe_timestamp_nexus": "100",
        "last_safe_timestamp_solana": "100",
    })
    scan = Mock(return_value={
        "complete": True, "reason": None, "legacy_nexus_txids": {},
        "malformed_nexus_memos": [], "nexus_payouts": {},
        "nexus_payout_timestamps": {}, "refund_sigs": {},
        "quarantined_sigs": {}, "solana_dispositions": {},
    })
    nexus_scan = Mock(return_value=nexus_client.DepositScan([], True))
    reference = Mock(return_value=1)
    monkeypatch.setattr(solana_client, "scan_memos_since_timestamp", scan)
    monkeypatch.setattr(nexus_client, "fetch_deposits_since", nexus_scan)
    monkeypatch.setattr(nexus_client, "get_last_reference", reference)
    return path, scan, nexus_scan, reference


def test_empty_database_refuses_startup_before_reconstruction(recovery_env):
    path, scan, nexus_scan, reference = recovery_env

    result = startup_recovery.perform_startup_recovery()

    assert result["recovery_complete"] is False
    assert result["recovery_incomplete"] is True
    assert result["error"] == "empty_custody_database_recovery_held"
    scan.assert_not_called()
    nexus_scan.assert_not_called()
    reference.assert_not_called()
    with sqlite3.connect(path) as conn:
        for table in ("unprocessed_sigs", "unprocessed_txids", "fee_entries",
                      "solana_payout_budget_events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_hold_survives_reinitialization_and_later_source_insertion(recovery_env, monkeypatch):
    path, scan, nexus_scan, reference = recovery_env
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is False
    with sqlite3.connect(path) as conn:
        original = conn.execute("SELECT * FROM recovery_admission_holds").fetchall()

    # Neither incidental writes nor a later heartbeat can grant historical authority.
    state_db.add_unprocessed_sig(
        "rediscovered", 110, "nexus:recipient", "sender", 1100,
        "ready for processing", None,
    )
    monkeypatch.setattr(nexus_client, "get_heartbeat_asset", lambda: {
        "last_safe_timestamp_nexus": "200", "last_safe_timestamp_solana": "200",
    })
    for _ in range(2):
        state_db.init_db()
        assert startup_recovery.perform_startup_recovery()["error"] == (
            "empty_custody_database_recovery_held"
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM recovery_admission_holds").fetchall() == original
    assert state_db.get_unresolved_solana_liability_units() == 1100
    scan.assert_not_called()
    nexus_scan.assert_not_called()
    reference.assert_not_called()


@pytest.mark.parametrize("failure", ["missing_table", "write_rejected"])
def test_admission_database_failure_cannot_authorize_replay(recovery_env, failure):
    path, scan, nexus_scan, reference = recovery_env
    with sqlite3.connect(path) as conn:
        if failure == "missing_table":
            conn.execute("DROP TABLE recovery_admission_holds")
        else:
            conn.execute("""CREATE TRIGGER reject_hold BEFORE INSERT ON recovery_admission_holds
                BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END""")
    result = startup_recovery.perform_startup_recovery()
    assert result["recovery_complete"] is False
    assert result["error"] == "custody_recovery_admission_failed"
    scan.assert_not_called()
    nexus_scan.assert_not_called()
    reference.assert_not_called()
    with sqlite3.connect(path, timeout=0) as conn:
        conn.execute("BEGIN EXCLUSIVE")  # Failure releases both reader and writer locks.


def _configure_workers(monkeypatch, *, minimum=1, maximum=1000, flat=0):
    monkeypatch.setattr(config, "MIN_DEPOSIT_SOLANA_UNITS", minimum)
    monkeypatch.setattr(config, "MAX_SWAP_SOLANA_UNITS", maximum)
    monkeypatch.setattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 1)
    monkeypatch.setattr(config, "SWAP_PAIR", replace(
        config.SWAP_PAIR,
        fees=replace(config.SWAP_PAIR.fees, flat_to_nexus_units=flat,
                     basis_points=0, refund_solana_units=10),
    ))
    monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _: True)
    monkeypatch.setattr(solana_client, "_resolve_solana_token_destination",
                        Mock(return_value="original-destination"))


def test_existing_schema_upgrade_preserves_retained_source(recovery_env):
    path, _scan, _nexus_scan, _reference = recovery_env
    state_db.add_unprocessed_sig(
        "retained", 110, "nexus:recipient", "sender", 1100,
        "policy held, non-sendable", None,
    )
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE recovery_admission_holds")
        before = conn.execute("SELECT * FROM unprocessed_sigs").fetchall()
    state_db.init_db()
    state_db.init_db()
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM unprocessed_sigs").fetchall() == before
        assert conn.execute("SELECT * FROM recovery_admission_holds").fetchall() == []


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "100"])
def test_admission_rejects_inexact_or_nonpositive_checkpoints(recovery_env, invalid):
    with pytest.raises(ValueError, match="positive exact checkpoints"):
        state_db.latch_empty_custody_recovery(nexus_waterline=invalid, solana_waterline=100)
    with pytest.raises(ValueError, match="positive exact checkpoints"):
        state_db.latch_empty_custody_recovery(nexus_waterline=100, solana_waterline=invalid)


@pytest.mark.parametrize("kind", ["below_minimum", "nonpositive", "refund", "quarantine"])
def test_real_main_refuses_lost_unsent_authorization(recovery_env, monkeypatch, kind):
    path, scan, nexus_scan, reference = recovery_env
    _configure_workers(monkeypatch, minimum=2000 if kind == "below_minimum" else 1,
                       flat=1100 if kind == "nonpositive" else 0,
                       maximum=2000 if kind == "nonpositive" else 1000)
    send = Mock(return_value=(True, "must-not-send"))
    debit = Mock(return_value=(True, "must-not-debit"))
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", debit)
    state_db.add_unprocessed_sig(
        "lost-source", 110, "nexus:recipient", "sender", 1100,
        "to be quarantined" if kind == "quarantine" else "ready for processing", None,
    )
    solana_client.process_unprocessed_solana_deposits(limit=1)
    solana_client.process_solana_deposits_refunding(limit=1)
    solana_client.process_solana_deposits_quarantine(limit=1)
    assert state_db.get_unprocessed_sig_status("lost-source") == (
        f"{kind} capacity held" if kind in {"refund", "quarantine"}
        else "policy held, non-sendable"
    )
    assert state_db.get_unresolved_solana_liability_units() == 1100
    # Lose DB plus any WAL/SHM, not merely a table or an in-memory mock.
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    _configure_workers(monkeypatch, maximum=2000)
    monkeypatch.setattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 10000)
    monkeypatch.setattr(main, "validate_production_controls", lambda: True)
    monkeypatch.setattr(main, "acquire_singleton_lock", lambda: True)
    poller = Mock()
    critical = Mock()
    monkeypatch.setattr(main, "_run_with_watchdog", poller)
    monkeypatch.setattr(alerts, "critical", critical)

    assert main.run() is False
    assert main.run() is False

    poller.assert_not_called()
    send.assert_not_called()
    debit.assert_not_called()
    scan.assert_not_called()
    nexus_scan.assert_not_called()
    reference.assert_not_called()
    assert critical.call_count == 2
    # Independent admission now precedes even schema creation and the legacy latch.
    assert critical.call_args.args[0] == "custody_admission_refused"
    assert not path.exists()


@pytest.mark.parametrize("entrypoint", ["library", "main"])
@pytest.mark.parametrize("restore_mode", ["online_backup", "copied_db_wal"])
@pytest.mark.parametrize("kind", ["refund", "quarantine"])
def test_restored_capacity_intent_keeps_original_terms_through_startup(
    recovery_env, monkeypatch, tmp_path, restore_mode, kind, entrypoint, running_custody,
):
    path, _scan, _nexus_scan, _reference = recovery_env
    _configure_workers(monkeypatch)
    send = Mock(return_value=(True, "original-payout"))
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    state_db.add_unprocessed_sig(
        "restored-source", 110, "original-memo", "sender", 1100,
        "to be refunded" if kind == "refund" else "to be quarantined", None,
    )
    worker = (solana_client.process_solana_deposits_refunding if kind == "refund"
              else solana_client.process_solana_deposits_quarantine)
    assert worker(limit=1) == 0
    restored = tmp_path / "restored.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        # Put actual frozen evidence into WAL, requiring a coherent DB+WAL restore.
        conn.execute("UPDATE solana_payout_capacity_holds SET first_held_timestamp = 123")
        conn.commit()
        before = conn.execute("SELECT intent_evidence FROM solana_payout_capacity_holds").fetchone()[0]
        if restore_mode == "online_backup":
            with sqlite3.connect(restored) as destination:
                conn.backup(destination)
            destination.close()
        else:
            shutil.copyfile(path, restored)
            shutil.copyfile(str(path) + "-wal", str(restored) + "-wal")
    finally:
        conn.close()
    monkeypatch.setattr(state_db, "DB_PATH", str(restored))
    state_db.init_db()
    with sqlite3.connect(restored) as conn:
        assert conn.execute(
            "SELECT first_held_timestamp FROM solana_payout_capacity_holds"
        ).fetchone()[0] == 123
    conn.close()
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
    monkeypatch.setattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 10000)
    monkeypatch.setattr(config, "SWAP_PAIR", replace(
        config.SWAP_PAIR, fees=replace(config.SWAP_PAIR.fees, refund_solana_units=20),
    ))
    resolver = Mock(side_effect=AssertionError("must reuse original destination"))
    monkeypatch.setattr(solana_client, "_resolve_solana_token_destination", resolver)
    if entrypoint == "library":
        assert worker(limit=1) == 1
    else:
        from src import balance_reconciler, fees, custody_chain
        monkeypatch.setattr(custody_chain, '_solana_genesis_hash',
                            lambda: os.environ['CUSTODY_SOLANA_GENESIS_HASH'])
        monkeypatch.setattr(custody_chain, '_nexus_height_zero_hash',
                            lambda: os.environ['CUSTODY_NEXUS_GENESIS_HASH'])
        client = running_custody(ready=True)
        monkeypatch.setattr(main, "validate_production_controls", lambda: True)
        monkeypatch.setattr(main, "acquire_singleton_lock", lambda: True)
        monkeypatch.setattr(nexus_client, "validate_session_config", lambda: (True, "offline"))
        monkeypatch.setattr(balance_reconciler, "run_balance_reconciliation",
                            lambda **kwargs: {"healthy": True, "discrepancies": []})
        monkeypatch.setattr(fees, "maintain_backing_and_bounds", lambda: False)
        def safe_call(fn, *args, **kwargs):
            if fn.__name__ == "validate_heartbeat_asset":
                return True, "offline"
            if fn.__name__ == "maintain_backing_and_bounds":
                return False
            return 0
        monkeypatch.setattr(main, "_safe_call", safe_call)
        monkeypatch.setattr(main, "poll_solana_deposits", lambda **kwargs: worker(limit=1))
        def one_poller(fn, label, budget):
            assert label == "solana"
            fn()
            main._stop_event.set()
        monkeypatch.setattr(main, "_run_with_watchdog", one_poller)
        assert main.run() is True
        head = client.get_head("offline-test-deployment")
        assert head["status"] == "ready"
        assert head["certificate"]["generation"] == 1
    assert worker(limit=1) == 0
    resolver.assert_not_called()
    send.assert_called_once_with(
        "original-destination", 1090, memo=f"swapService:v1:{kind}:restored-source",
    )
    with sqlite3.connect(restored) as conn:
        table = "refunded_sigs" if kind == "refund" else "quarantined_sigs"
        assert conn.execute(f"SELECT intent_evidence FROM {table}").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM recovery_admission_holds").fetchone()[0] == 0
