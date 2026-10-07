"""Unverified restores must never look healthy or create a replacement database."""
import sqlite3
from unittest.mock import Mock

from src import dashboard, state_db


def test_missing_dashboard_db_is_unknown_and_never_created(tmp_path, monkeypatch):
    path = tmp_path / 'missing.db'
    monkeypatch.setattr(state_db, 'DB_PATH', str(path))
    result = dashboard.api_summary()
    assert result['recovery_admission']['status'] == 'unknown'
    assert result['ratio'] is None
    assert result['payout_24h_solana'] is None
    assert result['paused'] is True
    assert not path.exists()


def test_empty_local_hold_table_cannot_replace_independent_witness(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(state_db, 'DB_PATH', str(path))
    state_db.init_db()
    state_db.save_metrics_snapshot(vault_usdc_units=100, circulating_usdd_units=100,
                                  paused=False, payouts_24h_units=3,
                                  fees_usdc_units=1, fees_usdd_units=2)
    before = path.read_bytes()
    result = dashboard.api_summary()
    assert result['recovery_admission']['status'] == 'unknown'
    assert result['ratio'] is None
    assert result['fees_solana'] is None
    assert path.read_bytes() == before


def test_witness_change_during_summary_invalidates_old_running_observation(tmp_path, monkeypatch, running_custody):
    from src import custody_admission
    path = tmp_path / 'state.db'
    monkeypatch.setattr(state_db, 'DB_PATH', str(path))
    state_db.init_db()
    state_db.save_metrics_snapshot(vault_usdc_units=100, circulating_usdd_units=100,
                                  paused=False, fees_usdc_units=1, fees_usdd_units=2)
    lease = running_custody()
    original = dashboard._rows
    def hold_during_read(sql, params=(), **kwargs):
        rows = original(sql, params, **kwargs)
        if 'metrics_snapshot' in sql:
            lease.hold()
        return rows
    monkeypatch.setattr(dashboard, '_rows', hold_during_read)
    summary = dashboard.api_summary()
    assert summary['recovery_admission']['status'] != 'not_held'
    assert summary['ratio'] is None
    assert summary['fees_solana'] is None
    assert summary['paused'] is True


def test_changed_running_generation_is_unknown_not_new_lease_green(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(state_db, 'DB_PATH', str(path))
    state_db.init_db()
    state_db.save_metrics_snapshot(vault_usdc_units=100, circulating_usdd_units=100, paused=False)
    status = Mock(side_effect=[
        {'status': 'not_held', 'liabilities_complete': False, 'lease_identity': 'old'},
        {'status': 'not_held', 'liabilities_complete': False, 'lease_identity': 'new'},
    ])
    monkeypatch.setattr(dashboard, '_recovery_admission_status', status)
    summary = dashboard.api_summary()
    assert summary['recovery_admission']['status'] == 'unknown'
    assert summary['ratio'] is None


def test_dashboard_collects_counts_and_metrics_from_one_snapshot(tmp_path, monkeypatch, running_custody):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(state_db, 'DB_PATH', str(path))
    state_db.init_db()
    lease = running_custody()
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.close()
    original = dashboard._rows
    changed = []
    def add_after_metrics(sql, params=(), **kwargs):
        result = original(sql, params, **kwargs)
        if 'metrics_snapshot' in sql:
            writer = sqlite3.connect(path)
            try:
                writer.execute("INSERT INTO processed_sigs (sig) VALUES ('later-write')")
                writer.commit()
                changed.append(True)
            finally:
                writer.close()
        return result
    monkeypatch.setattr(dashboard, '_rows', add_after_metrics)
    summary = dashboard.api_summary()
    assert changed
    assert summary['counts']['processed_sigs'] == 0
    assert summary['recovery_admission']['status'] == 'not_held'
    lease.hold()
