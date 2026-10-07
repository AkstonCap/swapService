"""Source rediscovery after a partial restore is not historical authorization."""
import json
import sqlite3
from unittest.mock import Mock

import pytest

from src import config, dashboard, solana_client, startup_recovery, state_db, nexus_client
from test_empty_database_recovery import recovery_env, _configure_workers  # noqa: F401


def commit_page(provider, deposits, *, query="recovery-page", complete=True,
                previous=None, next_cursor=None):
    common = dict(
        vault_account=config.SWAP_PAIR.solana.vault_account,
        mint=config.SWAP_PAIR.solana.mint, network="devnet", commitment="finalized",
        query_identity=query, lower_timestamp=100, upper_timestamp=2000,
        previous_timestamp=previous, page_last_timestamp=deposits[-1][1],
        deposits=deposits, holds=[], complete=complete,
    )
    if provider == "helius":
        return state_db.commit_helius_deposit_scan_page(
            **common, request_pagination_token="next" if previous else None,
            next_pagination_token=next_cursor,
            scanned_signatures=[(row[0], row[1]) for row in deposits],
        )[0]
    return state_db.commit_solana_deposit_scan_page(
        **common, request_before_signature="next" if previous else None,
        next_before_signature=next_cursor, scanned_signature_count=len(deposits),
    )


@pytest.mark.parametrize("provider", ["core", "helius"])
@pytest.mark.parametrize("kind", ["below_minimum", "nonpositive", "refund", "quarantine"])
def test_partial_restore_rediscovery_cannot_replace_lost_intent(
    recovery_env, monkeypatch, tmp_path, provider, kind,
):
    path, *_ = recovery_env
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO processed_sigs (sig, timestamp) VALUES ('unrelated', 101)")
        conn.commit()
        with sqlite3.connect(tmp_path / "stale.db") as backup:
            conn.backup(backup)
    _configure_workers(monkeypatch, minimum=2000 if kind == "below_minimum" else 1,
                       flat=1100 if kind == "nonpositive" else 0,
                       maximum=2000 if kind == "nonpositive" else 1000)
    send, debit = Mock(), Mock(return_value=(True, "unauthorized-debit"))
    monkeypatch.setattr(solana_client, "send_solana_token_to_account_with_sig", send)
    monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", debit)
    deposit = ("lost-source", 110, "nexus:recipient", "sender", 1100)
    state_db.add_unprocessed_sig(
        *deposit, "to be quarantined" if kind == "quarantine" else "ready for processing", None,
    )
    solana_client.process_unprocessed_solana_deposits(limit=1)
    solana_client.process_solana_deposits_refunding(limit=1)
    solana_client.process_solana_deposits_quarantine(limit=1)
    assert state_db.get_unresolved_solana_liability_units() == 1100
    assert state_db.get_unprocessed_sig_status("lost-source") == (
        f"{kind} capacity held" if kind in {"refund", "quarantine"}
        else "policy held, non-sendable"
    )
    monkeypatch.setattr(state_db, "DB_PATH", str(tmp_path / "stale.db"))
    _configure_workers(monkeypatch, maximum=2000)
    monkeypatch.setattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 10000)
    monkeypatch.setattr(startup_recovery.time, "time", lambda: 1000)
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is True

    assert commit_page(provider, [deposit]) == 0
    hold = state_db.get_solana_deposit_holds()[0]
    assert hold["reason"] == "historical_solana_authorization_missing"
    assert (hold["signature"], hold["block_timestamp"], hold["memo"],
            hold["from_address"], hold["amount_units"]) == deposit
    assert hold["network"] == "devnet"
    assert hold["vault_account"] == config.SWAP_PAIR.solana.vault_account
    assert hold["mint"] == config.SWAP_PAIR.solana.mint
    assert hold["observed_commitment"] == "finalized"
    assert json.loads(hold["evidence_json"])["token_program"] == (
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    )
    issue = next(item for item in dashboard.api_issues()["issues"] if item["id"] == "lost-source")
    assert issue["status"] == hold["reason"]
    assert issue["amount_units"] == 1100
    assert "do not" in issue["operator_action"]
    for _ in range(2):
        state_db.init_db()
        startup_recovery.perform_startup_recovery()
        assert commit_page(provider, [deposit]) == 0
        assert state_db.promote_solana_deposit_hold("lost-source") is False
        assert solana_client.replay_solana_deposit_holds(limit=1) == 0
        solana_client.process_unprocessed_solana_deposits(limit=1)
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        assert state_db.get_unresolved_solana_liability_units() == 1100
        assert state_db.get_earliest_solana_deposit_hold_timestamp() == 110
    send.assert_not_called()
    debit.assert_not_called()
    with sqlite3.connect(state_db.DB_PATH) as conn:
        for table in ("unprocessed_sigs", "fee_entries", "reservations",
                      "solana_payout_budget_events", "refunded_sigs", "quarantined_sigs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_retained_finality_hold_does_not_lose_replay_eligibility(recovery_env):
    path, *_ = recovery_env
    with sqlite3.connect(path) as conn:
        state_db._insert_solana_deposit_hold(conn, (
            "retained", 110, "memo", "sender", 100, "awaiting_finalized", "{}",
            "core", "query", "devnet", "vault", "mint", "confirmed", 1,
        ))
    state_db.record_solana_recovery_boundary(1000)
    assert state_db.promote_solana_deposit_hold("retained") is True
    assert state_db.get_unresolved_solana_liability_units() == 100


def test_historical_holds_are_excluded_before_replay_limit(recovery_env):
    state_db.record_solana_recovery_boundary(1000)
    commit_page("core", [("historical", 110, "memo", "sender", 100)])
    with sqlite3.connect(state_db.DB_PATH) as conn:
        state_db._insert_solana_deposit_hold(conn, (
            "live", 1001, "memo", "sender", 100, "awaiting_finalized", "{}",
            "core", "query", "devnet", "vault", "mint", "confirmed", 1,
        ))
    rows = state_db.get_solana_deposit_holds(limit=1, include_historical=False)
    assert [row["signature"] for row in rows] == ["live"]


def test_scanner_holds_receive_stable_historical_reason(recovery_env):
    state_db.record_solana_recovery_boundary(1000)
    hold = ("unseen", 110, "memo", "sender", 100, "awaiting_finalized", "{}",
            "core", "query", "devnet", "vault", "mint", "confirmed", 1)
    for _ in range(2):
        with sqlite3.connect(state_db.DB_PATH) as conn:
            state_db._insert_solana_deposit_hold(conn, hold)
        assert state_db.get_solana_deposit_holds()[0]["reason"] == (
            state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
        )
        assert state_db.promote_solana_deposit_hold("unseen") is False
    assert state_db.get_unresolved_solana_liability_units() == 100


@pytest.mark.parametrize("provider", ["core", "helius"])
def test_boundary_is_inclusive_and_paged_replay_preserves_retained_rows(recovery_env, provider):
    state_db.record_solana_recovery_boundary(1000)
    retained = ("retained", 110, "memo", "sender", 123)
    state_db.add_unprocessed_sig(*retained, "policy held, non-sendable", None)
    assert commit_page(provider, [("live", 1001, "memo", "sender", 20),
                                  ("equal", 1000, "memo", "sender", 30)],
                       complete=False, next_cursor="next") == 1
    state_db.init_db()
    state_db.record_solana_recovery_boundary(900)  # Clock reversal cannot relax the gate.
    assert commit_page(provider, [("older", 999, "memo", "sender", 40), retained],
                       previous=1000) == 0
    assert {row["signature"] for row in state_db.get_solana_deposit_holds()} == {"equal", "older"}
    assert state_db.get_unprocessed_sig_status("retained") == "policy held, non-sendable"
    # A restart cannot certify this retained source-only row's missing policy,
    # even when its timestamp exceeds the clock/replay boundary.
    assert state_db.get_unprocessed_sig_status("live") == (
        state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
    )
    assert state_db.get_unresolved_solana_liability_units() == 213


def test_additive_boundary_upgrade_and_reinitialization_preserve_state(recovery_env):
    path, *_ = recovery_env
    state_db.add_unprocessed_sig("retained", 110, "memo", "sender", 123, "policy held, non-sendable")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE solana_recovery_boundary")
        before = conn.execute("SELECT * FROM unprocessed_sigs").fetchall()
    state_db.init_db()
    state_db.record_solana_recovery_boundary(1000)
    state_db.init_db()
    state_db.record_solana_recovery_boundary(900)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM unprocessed_sigs").fetchall() == before
        assert conn.execute("SELECT * FROM solana_recovery_boundary").fetchall() == [(1, 1000)]


@pytest.mark.parametrize("failure", ["missing_table", "write_rejected", "malformed"])
def test_boundary_failure_refuses_startup_before_scans(recovery_env, failure):
    path, scan, nexus_scan, reference = recovery_env
    state_db.add_unprocessed_sig("retained", 110, "memo", "sender", 100, "policy held, non-sendable")
    with sqlite3.connect(path) as conn:
        if failure == "missing_table":
            conn.execute("DROP TABLE solana_recovery_boundary")
        elif failure == "write_rejected":
            conn.execute("""CREATE TRIGGER reject_boundary BEFORE INSERT ON solana_recovery_boundary
                BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END""")
        else:
            conn.execute("INSERT INTO solana_recovery_boundary VALUES (1, 'invalid')")
    result = startup_recovery.perform_startup_recovery()
    assert result["recovery_complete"] is False
    assert result["error"] == "solana_recovery_boundary_persistence_failed"
    scan.assert_not_called()
    nexus_scan.assert_not_called()
    reference.assert_not_called()
    with sqlite3.connect(path, timeout=0) as conn:
        conn.execute("BEGIN EXCLUSIVE")


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "100"])
def test_boundary_rejects_invalid_timestamps(recovery_env, invalid):
    with pytest.raises(ValueError, match="positive exact timestamp"):
        state_db.record_solana_recovery_boundary(invalid)


@pytest.mark.parametrize("provider", ["core", "helius"])
def test_replay_page_rolls_back_hold_and_cursor_on_conflicting_source(recovery_env, provider):
    state_db.record_solana_recovery_boundary(1000)
    state_db.add_unprocessed_sig("retained", 110, "memo", "sender", 100, "policy held, non-sendable")
    with pytest.raises(ValueError, match="conflicts with retained"):
        commit_page(provider, [("unknown", 200, "memo", "sender", 100),
                               ("retained", 110, "memo", "sender", 101)])
    assert state_db.get_solana_deposit_holds() == []
    assert state_db.get_solana_deposit_scan_cursor(config.SWAP_PAIR.solana.vault_account) is None
    assert state_db.get_helius_deposit_scan_cursor(config.SWAP_PAIR.solana.vault_account) is None
    assert state_db.get_unresolved_solana_liability_units() == 100
