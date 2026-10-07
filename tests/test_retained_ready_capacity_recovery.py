"""A stale ready status cannot override a retained disposition-capacity intent."""
import sqlite3
from unittest.mock import Mock

import pytest

from src import config, dashboard, nexus_client, solana_client, startup_recovery, state_db
from test_empty_database_recovery import recovery_env, _configure_workers  # noqa: F401
from test_partial_restore_deposit_recovery import commit_page
from test_retained_ready_submission_recovery import _ready_source


def _capacity_source(sig, kind, timestamp=10):
    deposit = _ready_source(sig, timestamp)
    state_db.update_unprocessed_sig_status(
        sig, 'to be refunded' if kind == 'refund' else 'to be quarantined',
    )
    worker = (solana_client.process_solana_deposits_refunding if kind == 'refund'
              else solana_client.process_solana_deposits_quarantine)
    assert worker(limit=100) == 0
    assert state_db.get_unprocessed_sig_status(sig) == f'{kind} capacity held'
    return deposit


@pytest.mark.parametrize('kind', ['refund', 'quarantine'])
@pytest.mark.parametrize('damage', ['none', 'malformed', 'kind', 'obligation_id'])
@pytest.mark.parametrize('reservation', ['absent', 'expired', 'active'])
def test_ready_policy_cannot_override_retained_capacity_intent(
    recovery_env, monkeypatch, kind, damage, reservation,
):
    path, *_ = recovery_env
    _configure_workers(monkeypatch, maximum=2000, flat=10)
    deposit = _capacity_source('partial', kind)
    if reservation != 'absent':
        assert state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, deposit[0])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if reservation == 'expired':
            conn.execute('UPDATE reservations SET timestamp = 1')
        reservations = [tuple(row) for row in conn.execute('SELECT * FROM reservations')]
        # Partial restore combines an older ready source with newer disposition evidence.
        conn.execute("UPDATE unprocessed_sigs SET status = 'ready for processing'")
        if damage == 'malformed':
            conn.execute("UPDATE solana_payout_capacity_holds SET intent_evidence = '{'")
        elif damage == 'kind':
            conn.execute('UPDATE solana_payout_capacity_holds SET kind = ?',
                         ('quarantine' if kind == 'refund' else 'refund',))
        elif damage == 'obligation_id':
            conn.execute("UPDATE solana_payout_capacity_holds SET obligation_id = 'other-source'")
        before = dict(conn.execute('SELECT * FROM unprocessed_sigs').fetchone())
        capacity = [tuple(row) for row in conn.execute('SELECT * FROM solana_payout_capacity_holds')]
    send, debit = Mock(), Mock(return_value=(True, 'replacement-debit'))
    monkeypatch.setattr(solana_client, 'send_solana_token_to_account_with_sig', send)
    monkeypatch.setattr(nexus_client, 'debit_nexus_token_with_txid', debit)
    _configure_workers(monkeypatch, maximum=3000, flat=100)
    monkeypatch.setattr(config, 'DAILY_PAYOUT_CAP_SOLANA_UNITS', 10000)
    for _ in range(2):
        assert startup_recovery.perform_startup_recovery()['recovery_complete'] is True
        solana_client.process_unprocessed_solana_deposits(limit=1)
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        debit.assert_not_called()
        send.assert_not_called()
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            assert dict(conn.execute('SELECT * FROM unprocessed_sigs').fetchone()) == {
                **before, 'status': state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING,
            }
            assert [tuple(row) for row in conn.execute('SELECT * FROM solana_payout_capacity_holds')] == capacity
            assert [tuple(row) for row in conn.execute('SELECT * FROM reservations')] == reservations
            for table in ('fee_entries', 'solana_payout_budget_events',
                          'processed_sigs', 'refunded_sigs', 'quarantined_sigs'):
                assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert state_db.get_unresolved_solana_liability_units() == 1100
        issue = next(row for row in dashboard.api_issues()['issues'] if row['id'] == deposit[0])
        assert issue['status'] == state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING
        assert issue['operator_action'] == dashboard.SIG_OPERATOR_ACTIONS[issue['status']]
        assert commit_page('core', [deposit]) == 0
        assert commit_page('helius', [deposit]) == 0
        state_db.init_db()


@pytest.mark.parametrize('kind', ['refund', 'quarantine'])
@pytest.mark.parametrize('existing_boundary', [None, 100])
def test_capacity_conflict_write_failure_rolls_back_startup(
    recovery_env, monkeypatch, kind, existing_boundary,
):
    path, scan, nexus_scan, reference = recovery_env
    _configure_workers(monkeypatch, maximum=2000, flat=10)
    if existing_boundary is not None:
        state_db.record_solana_recovery_boundary(existing_boundary)
    _capacity_source('partial', kind)
    state_db.update_unprocessed_sig_status('partial', 'ready for processing')
    state_db.add_unprocessed_sig('ordinary', 11, 'memo', 'sender', 1100,
                                 'to be refunded', None)
    with sqlite3.connect(path) as conn:
        before = conn.execute('SELECT * FROM unprocessed_sigs ORDER BY sig').fetchall()
        capacity = conn.execute('SELECT * FROM solana_payout_capacity_holds').fetchall()
        conn.execute("""CREATE TRIGGER reject_capacity_conflict BEFORE UPDATE ON unprocessed_sigs
            WHEN OLD.sig = 'partial'
            BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END""")
    result = startup_recovery.perform_startup_recovery()
    assert result['recovery_complete'] is False
    assert result['error'] == 'solana_recovery_boundary_persistence_failed'
    scan.assert_not_called()
    nexus_scan.assert_not_called()
    reference.assert_not_called()
    with sqlite3.connect(path, timeout=0) as conn:
        conn.execute('BEGIN EXCLUSIVE')
        assert conn.execute('SELECT * FROM unprocessed_sigs ORDER BY sig').fetchall() == before
        assert conn.execute('SELECT * FROM solana_payout_capacity_holds').fetchall() == capacity
        assert conn.execute('SELECT cutoff_timestamp FROM solana_recovery_boundary').fetchone() == (
            None if existing_boundary is None else (existing_boundary,)
        )


@pytest.mark.parametrize('kind', ['refund', 'quarantine'])
@pytest.mark.parametrize('timestamp', [10, 110, 1001])
def test_capacity_conflicts_do_not_consume_ready_worker_limit(
    recovery_env, monkeypatch, kind, timestamp,
):
    path, *_ = recovery_env
    _configure_workers(monkeypatch, maximum=2000, flat=10)
    monkeypatch.setattr(startup_recovery.time, 'time', lambda: 1000)
    for index in range(5):
        _capacity_source(f'conflict-{index}', kind, timestamp + index)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE unprocessed_sigs SET status = 'ready for processing'")
        capacity = conn.execute('SELECT * FROM solana_payout_capacity_holds ORDER BY source_signature').fetchall()
    _ready_source('valid-younger', timestamp + 10)
    send, debit = Mock(), Mock(return_value=(True, 'original-debit'))
    monkeypatch.setattr(solana_client, 'send_solana_token_to_account_with_sig', send)
    monkeypatch.setattr(nexus_client, 'debit_nexus_token_with_txid', debit)
    _configure_workers(monkeypatch, maximum=3000, flat=100)
    monkeypatch.setattr(config, 'DAILY_PAYOUT_CAP_SOLANA_UNITS', 10000)
    for iteration in range(2):
        assert startup_recovery.perform_startup_recovery()['recovery_complete'] is True
        assert solana_client.process_unprocessed_solana_deposits(limit=1) == (
            [1, 0, 0, 0] if iteration == 0 else 0
        )
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        debit.assert_called_once_with('recipient', 1090, 1)
        send.assert_not_called()
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM unprocessed_sigs WHERE status = ?',
                                (state_db.HISTORICAL_SOLANA_AUTHORIZATION_MISSING,)).fetchone() == (5,)
            assert conn.execute('SELECT * FROM solana_payout_capacity_holds ORDER BY source_signature').fetchall() == capacity
        assert state_db.get_unresolved_solana_liability_units() == 6600
        state_db.init_db()


@pytest.mark.parametrize('kind', ['refund', 'quarantine'])
def test_coherent_capacity_source_keeps_original_retry_intent(recovery_env, monkeypatch, kind):
    path, *_ = recovery_env
    _configure_workers(monkeypatch, maximum=2000, flat=10)
    _capacity_source('coherent', kind)
    with sqlite3.connect(path) as conn:
        before = conn.execute('SELECT * FROM unprocessed_sigs').fetchall()
    send, debit = Mock(return_value=(True, 'original-payout')), Mock()
    monkeypatch.setattr(solana_client, 'send_solana_token_to_account_with_sig', send)
    monkeypatch.setattr(nexus_client, 'debit_nexus_token_with_txid', debit)
    _configure_workers(monkeypatch, maximum=3000, flat=100)
    monkeypatch.setattr(config, 'DAILY_PAYOUT_CAP_SOLANA_UNITS', 10000)
    for iteration in range(2):
        assert startup_recovery.perform_startup_recovery()['recovery_complete'] is True
        if iteration == 0:
            with sqlite3.connect(path) as conn:
                assert conn.execute('SELECT * FROM unprocessed_sigs').fetchall() == before
        solana_client.process_unprocessed_solana_deposits(limit=1)
        solana_client.process_solana_deposits_refunding(limit=1)
        solana_client.process_solana_deposits_quarantine(limit=1)
        debit.assert_not_called()
        send.assert_called_once_with('original-destination', 1090,
                                     memo=f'swapService:v1:{kind}:coherent')
        assert state_db.get_unresolved_solana_liability_units() == 1100
        state_db.init_db()
