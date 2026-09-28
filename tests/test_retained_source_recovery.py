"""Retained source/policy rows cannot replace lost historical authorization."""
from dataclasses import replace
import json
import sqlite3
from unittest.mock import Mock

import pytest

from src import (config, dashboard, nexus_client, solana_client,
                 solana_deposit_policy, startup_recovery, state_db)
from test_empty_database_recovery import recovery_env, _configure_workers  # noqa: F401
from test_partial_restore_deposit_recovery import commit_page


@pytest.mark.parametrize("source_status", [
    "ready for processing", "to be refunded", "to be quarantined", "quarantine failed",
])
@pytest.mark.parametrize("timestamp", [10, 110, 1001])
def test_startup_holds_retained_source_without_policy(
    recovery_env, monkeypatch, timestamp, source_status,
):
    path, *_ = recovery_env
    _configure_workers(monkeypatch, maximum=2000)
    monkeypatch.setattr(startup_recovery.time, "time", lambda: 1000)
    monkeypatch.setattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 10000)
    send = Mock(return_value=(True, "unauthorized-send"))
    debit = Mock(return_value=(True, "unauthorized-debit"))
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", debit)
    deposit = ("source-only", timestamp, "nexus:recipient", "sender", 1100)
    # Partial restore or interruption before the first durable authorization.
    state_db.add_unprocessed_sig(*deposit, source_status, None)

    for _ in range(2):
        assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
        solana_client.process_unprocessed_solana_deposits(limit=1)
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        debit.assert_not_called()
        send.assert_not_called()
        assert state_db.get_unprocessed_sig_status("source-only") == (
            state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
        )
        assert state_db.get_unresolved_solana_liability_units() == 1100
        with sqlite3.connect(path) as conn:
            assert conn.execute(
                "SELECT timestamp, memo, from_address, amount_usdc_units, policy_decision, "
                "policy_evidence FROM unprocessed_sigs WHERE sig = 'source-only'"
            ).fetchone() == (*deposit[1:], None, None)
            for table in ("fee_entries", "reservations", "solana_payout_budget_events",
                          "processed_sigs", "refunded_sigs", "quarantined_sigs"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        issue = next(row for row in dashboard.api_issues()["issues"] if row["id"] == "source-only")
        assert issue["status"] == state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
        assert "do not" in issue["operator_action"]
        # Repeated scan pages and schema initialization cannot promote a retained hold.
        assert commit_page("core", [deposit]) == 0
        assert commit_page("helius", [deposit]) == 0
        state_db.init_db()


@pytest.mark.parametrize("source_status", [
    "ready for processing", "to be refunded", "to be quarantined", "quarantine failed",
])
@pytest.mark.parametrize("damage", [
    "missing_decision", "missing_evidence", "malformed_json", "empty_evidence",
    "signature", "timestamp", "memo", "from_address", "input_units",
    "decision_conflict", "output_units", "nonpayable_ready",
])
def test_startup_holds_invalid_retained_policy_without_rewriting_evidence(
    recovery_env, monkeypatch, damage, source_status,
):
    path, *_ = recovery_env
    _configure_workers(monkeypatch, maximum=2000, flat=10)
    send, debit = Mock(), Mock()
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", debit)
    deposit = ("damaged-policy", 10, "nexus:recipient", "sender", 1100)
    state_db.add_unprocessed_sig(*deposit, source_status, None)
    evidence = solana_deposit_policy.freeze_evidence(
        signature=deposit[0], timestamp=deposit[1], memo=deposit[2],
        from_address=deposit[3], input_units=deposit[4],
        decision=solana_deposit_policy.classify(
            1100, solana_deposit_policy.terms_from_config(config)),
    )
    decision = solana_deposit_policy.PAYABLE
    if damage == "missing_decision":
        decision = None
    elif damage == "missing_evidence":
        evidence = None
    elif damage == "malformed_json":
        evidence = "{"
    elif damage == "empty_evidence":
        evidence = ""
    elif damage == "decision_conflict":
        decision = solana_deposit_policy.REFUND_OVERSIZED
    else:
        parsed = json.loads(evidence)
        if damage == "nonpayable_ready":
            # Internally valid evidence, but this decision cannot authorize a ready row.
            parsed["terms"]["minimum_input_units"] = 2000
            parsed["decision"] = decision = solana_deposit_policy.HOLD_BELOW_MINIMUM
        elif damage in {"timestamp", "input_units", "output_units"}:
            parsed[damage] += 1
            if damage == "input_units":
                parsed["output_units"] += 1  # Valid math, wrong source principal.
        else:
            parsed[damage] = "different-source"
        evidence = json.dumps(parsed)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE unprocessed_sigs SET policy_decision = ?, policy_evidence = ?",
                     (decision, evidence))
        before = dict(conn.execute("SELECT * FROM unprocessed_sigs").fetchone())
    _configure_workers(monkeypatch, maximum=3000, flat=100)

    for _ in range(2):
        assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
        assert state_db.get_unprocessed_sig_status(deposit[0]) == (
            state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
        )
        solana_client.process_unprocessed_solana_deposits(limit=1)
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        send.assert_not_called()
        debit.assert_not_called()
        assert state_db.get_unresolved_solana_liability_units() == 1100
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            assert dict(conn.execute("SELECT * FROM unprocessed_sigs").fetchone()) == {
                **before, "status": state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING,
            }
            for table in ("fee_entries", "reservations", "solana_payout_budget_events",
                          "processed_sigs", "refunded_sigs", "quarantined_sigs"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        issue = next(row for row in dashboard.api_issues()["issues"] if row["id"] == deposit[0])
        assert issue["status"] == state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
        assert "do not" in issue["operator_action"]
        assert commit_page("core", [deposit]) == 0
        assert commit_page("helius", [deposit]) == 0
        state_db.init_db()


@pytest.mark.parametrize("source_status", [
    "ready for processing", "to be refunded", "to be quarantined", "quarantine failed",
])
@pytest.mark.parametrize("evidence", [None, "{"])
@pytest.mark.parametrize("existing_boundary", [None, 100])
def test_failed_hold_update_rolls_back_boundary_and_refuses_scans(
    recovery_env, existing_boundary, evidence, source_status,
):
    path, scan, nexus_scan, reference = recovery_env
    if existing_boundary is not None:
        state_db.record_solana_recovery_boundary(existing_boundary)
    state_db.add_unprocessed_sig("source-only", 110, "memo", "sender", 1100,
                                 source_status, None)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE unprocessed_sigs SET policy_evidence = ?", (evidence,))
        conn.execute("""CREATE TRIGGER reject_source_hold BEFORE UPDATE ON unprocessed_sigs
            BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END""")
    result = startup_recovery.perform_startup_recovery()
    assert result["recovery_complete"] is False
    assert result["error"] == "solana_recovery_boundary_persistence_failed"
    scan.assert_not_called()
    nexus_scan.assert_not_called()
    reference.assert_not_called()
    assert state_db.get_unprocessed_sig_status("source-only") == source_status
    with sqlite3.connect(path, timeout=0) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        assert conn.execute("SELECT cutoff_timestamp FROM solana_recovery_boundary").fetchone() == (
            None if existing_boundary is None else (existing_boundary,)
        )


@pytest.mark.parametrize("evidence", [None, "{"])
def test_all_source_only_rows_are_held_without_starving_frozen_or_live_work(recovery_env, monkeypatch, evidence):
    path, *_ = recovery_env
    _configure_workers(monkeypatch, maximum=2000, flat=10)
    monkeypatch.setattr(startup_recovery.time, "time", lambda: 1000)
    send, debit = Mock(), Mock(return_value=(True, "original-debit"))
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", debit)
    for index in range(5):
        state_db.add_unprocessed_sig(f"source-{index}", 10 + index, "nexus:recipient",
                                     "sender", 1100, "ready for processing", None)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE unprocessed_sigs SET policy_evidence = ?", (evidence,))
    state_db.add_unprocessed_sig("frozen", 110, "nexus:recipient", "sender", 1100,
                                 "ready for processing", None)
    evidence = solana_deposit_policy.freeze_evidence(
        signature="frozen", timestamp=110, memo="nexus:recipient", from_address="sender",
        input_units=1100, decision=solana_deposit_policy.classify(
            1100, solana_deposit_policy.terms_from_config(config)),
    )
    state_db.freeze_solana_deposit_policy_decision("frozen", evidence)
    _configure_workers(monkeypatch, maximum=2000, flat=100)
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
    assert solana_client.process_unprocessed_solana_deposits(limit=1) == [1, 0, 0, 0]
    assert debit.call_args.args[:2] == ("recipient", 1090)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT policy_evidence FROM unprocessed_sigs WHERE sig = 'frozen'").fetchone() == (evidence,)
        assert conn.execute("SELECT COUNT(*) FROM unprocessed_sigs WHERE status = ?",
                            (state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING,)).fetchone() == (5,)
    # Fresh post-startup input can still freeze its first policy and execute once.
    assert commit_page("core", [("live", 1001, "nexus:recipient", "sender", 1100)]) == 1
    assert solana_client.process_unprocessed_solana_deposits(limit=1) == [1, 0, 0, 0]
    assert debit.call_args.args[:2] == ("recipient", 1000)
    state_db.init_db()
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
    solana_client.process_unprocessed_solana_deposits(limit=1)
    assert debit.call_count == 2
    send.assert_not_called()
    assert state_db.get_unresolved_solana_liability_units() == 7700


@pytest.mark.parametrize("source_status", [
    "ready for processing", "to be refunded", "to be quarantined", "quarantine failed",
])
@pytest.mark.parametrize("evidence", [None, "{"])
def test_recovery_hold_preserves_capacity_evidence_without_advertising_retry(
    recovery_env, monkeypatch, evidence, source_status,
):
    path, *_ = recovery_env
    _configure_workers(monkeypatch)
    send = Mock()
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    state_db.add_unprocessed_sig("partial", 110, "nexus:recipient", "sender", 1100,
                                 "ready for processing", None)
    solana_client.process_unprocessed_solana_deposits(limit=1)
    solana_client.process_solana_deposits_refunding(limit=1)
    assert state_db.get_unprocessed_sig_status("partial") == "refund capacity held"
    with sqlite3.connect(path) as conn:
        capacity_before = conn.execute("SELECT * FROM solana_payout_capacity_holds").fetchall()
        # Partial restore: stale source component alongside retained frozen capacity.
        conn.execute("UPDATE unprocessed_sigs SET status = ?, "
                     "policy_decision = NULL, policy_evidence = ? WHERE sig = 'partial'",
                     (source_status, evidence))
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
    issue = next(row for row in dashboard.api_issues()["issues"] if row["id"] == "partial")
    assert issue["status"] == state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
    assert issue["operator_action"] == dashboard.SIG_OPERATOR_ACTIONS[issue["status"]]
    for _ in range(2):
        solana_client.process_unprocessed_solana_deposits(limit=1)
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        state_db.init_db()
    send.assert_not_called()
    assert state_db.get_unresolved_solana_liability_units() == 1100
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM solana_payout_capacity_holds").fetchall() == capacity_before


@pytest.mark.parametrize("source_status", [
    "ready for processing", "to be refunded", "to be quarantined", "quarantine failed",
])
@pytest.mark.parametrize("evidence", [None, "{"])
def test_source_hold_changes_only_status_and_retains_existing_reservation(
    recovery_env, evidence, source_status,
):
    path, *_ = recovery_env
    state_db.add_unprocessed_sig("interrupted", 110, "nexus:recipient", "sender", 1100,
                                 source_status, "retained-remote-id")
    assert state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, "interrupted")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE unprocessed_sigs SET reference = 99, amount_usdd_units = 1090, "
                     "policy_evidence = ?", (evidence,))
        before = dict(conn.execute("SELECT * FROM unprocessed_sigs").fetchone())
        reservations = [tuple(row) for row in conn.execute("SELECT * FROM reservations")]
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        after = dict(conn.execute("SELECT * FROM unprocessed_sigs").fetchone())
        assert after == {**before, "status": state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING}
        assert [tuple(row) for row in conn.execute("SELECT * FROM reservations")] == reservations
    assert state_db.get_unresolved_solana_liability_units() == 1100


@pytest.mark.parametrize("source_status", [
    "to be refunded", "to be quarantined", "quarantine failed",
])
@pytest.mark.parametrize("policy", ["payable", "oversized", "below_minimum", "nonpositive"])
@pytest.mark.parametrize("refund_fee", [100, 1100])
def test_policy_alone_cannot_authorize_retained_disposition_or_starve_frozen_retry(
    recovery_env, monkeypatch, source_status, policy, refund_fee,
):
    path, *_ = recovery_env
    _configure_workers(monkeypatch)
    monkeypatch.setattr(startup_recovery.time, "time", lambda: 1000)
    send = Mock(return_value=(True, "original-payout"))
    debit = Mock()
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", debit)
    kind = "refund" if source_status == "to be refunded" else "quarantine"
    worker = (solana_client.process_solana_deposits_refunding if kind == "refund"
              else solana_client.process_solana_deposits_quarantine)
    # Positive control: retain a younger complete capacity intent under original terms.
    state_db.add_unprocessed_sig("frozen", 110, "memo", "sender", 1100,
                                 source_status, None)
    assert worker(limit=1) == 0
    assert state_db.get_unprocessed_sig_status("frozen") == f"{kind} capacity held"
    _configure_workers(monkeypatch, minimum=2000 if policy == "below_minimum" else 1,
                       maximum=1000 if policy == "oversized" else 2000,
                       flat=1100 if policy == "nonpositive" else 0)
    for index in range(5):
        sig = f"policy-only-{index}"
        state_db.add_unprocessed_sig(sig, 10 + index, "nexus:recipient", "sender", 1100,
                                     "ready for processing", None)
        evidence = solana_deposit_policy.freeze_evidence(
            signature=sig, timestamp=10 + index, memo="nexus:recipient",
            from_address="sender", input_units=1100,
            decision=solana_deposit_policy.classify(
                1100, solana_deposit_policy.terms_from_config(config)),
        )
        state_db.freeze_solana_deposit_policy_decision(sig, evidence)
        state_db.update_unprocessed_sig_status(sig, source_status)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        before = [dict(row) for row in conn.execute(
            "SELECT * FROM unprocessed_sigs WHERE sig LIKE 'policy-only-%' ORDER BY sig")]
    _configure_workers(monkeypatch, maximum=3000, flat=100)
    monkeypatch.setattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 10000)
    monkeypatch.setattr(config, "SWAP_PAIR", replace(
        config.SWAP_PAIR, fees=replace(config.SWAP_PAIR.fees, refund_solana_units=refund_fee),
    ))
    resolver = Mock(side_effect=AssertionError("must not derive a new destination"))
    monkeypatch.setattr(solana_client, "_resolve_solana_token_destination", resolver)

    for iteration in range(2):
        assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True
        assert worker(limit=1) == (1 if iteration == 0 else 0)
        solana_client.process_unprocessed_solana_deposits(limit=1)
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        debit.assert_not_called()
        resolver.assert_not_called()
        send.assert_called_once_with(
            "original-destination", 1090, memo=f"swapService:v1:{kind}:frozen",
        )
        assert state_db.get_unresolved_solana_liability_units() == 6600
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            after = [dict(row) for row in conn.execute(
                "SELECT * FROM unprocessed_sigs WHERE sig LIKE 'policy-only-%' ORDER BY sig")]
            assert after == [
                {**row, "status": state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING}
                for row in before
            ]
            assert conn.execute("SELECT COUNT(*) FROM fee_entries").fetchone()[0] == 0
        state_db.init_db()
