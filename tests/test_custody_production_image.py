"""Whole production schema, not a selected status/table allowlist, is witnessed."""
import secrets
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from src import alerts, config, custody_admission as admission, custody_witness as witness
from src import main, state_db, startup_recovery, solana_client, nexus_client


def issue(db, store_path):
    token, admin = secrets.token_hex(32), secrets.token_hex(32)
    store = witness.Store(store_path, runtime_token=token, admin_token=admin)
    cert = witness.Certificate.from_dict({
        'deployment_id': 'production-schema-fixture', 'generation': 0,
        'permit_nonce': secrets.token_hex(32), **admission.inspect_image(db),
        'config_sha256': admission.configuration_fingerprint(),
        'build_sha256': admission.build_fingerprint(),
        'issued_at': 1, 'approval_rationale': 'offline reviewed fixture',
    })
    store.issue_initial(cert, actor='independent-fixture-reviewer',
                        rationale='offline test only', admin_token=admin)
    return witness.StoreClient(store, token), cert


def test_all_production_tables_are_covered_before_real_main_mutation(tmp_path, monkeypatch):
    baseline = tmp_path / 'baseline.db'
    monkeypatch.setattr(state_db, 'DB_PATH', str(baseline))
    state_db.init_db()
    with sqlite3.connect(baseline) as conn:
        tables = sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    assert len(tables) > 30  # Detect accidental toy-fixture coverage.
    original_claim = admission.claim
    initialize, recover, scan, reference, send, debit = [Mock() for _ in range(6)]
    monkeypatch.setattr(main, 'validate_production_controls', lambda: True)
    monkeypatch.setattr(main, 'acquire_singleton_lock', lambda: True)
    monkeypatch.setattr(state_db, 'init_db', initialize)
    monkeypatch.setattr(startup_recovery, 'perform_startup_recovery', recover)
    monkeypatch.setattr(startup_recovery, '_rebuild_solana_from_waterline', scan)
    monkeypatch.setattr(nexus_client, 'get_last_reference', reference)
    monkeypatch.setattr(solana_client, 'send_solana_token_to_account_with_sig', send)
    monkeypatch.setattr(nexus_client, 'debit_nexus_token_with_txid', debit)
    monkeypatch.setattr(alerts, 'critical', Mock())
    for index, table in enumerate(tables):
        candidate = tmp_path / f'partial-{index}.db'
        shutil.copyfile(baseline, candidate)
        client, cert = issue(candidate, tmp_path / f'witness-{index}.db')
        with sqlite3.connect(candidate) as conn:
            conn.execute('DROP TABLE "' + table.replace('"', '""') + '"')
        before = candidate.read_bytes()
        monkeypatch.setattr(state_db, 'DB_PATH', str(candidate))
        monkeypatch.setattr(admission, 'claim', lambda: original_claim(
            client=client, deployment_id=cert.deployment_id))
        assert main.run() is False, table
        assert candidate.read_bytes() == before, table
        assert client.get_head(cert.deployment_id)['status'] == 'ready', table
    for blocked in (initialize, recover, scan, reference, send, debit):
        blocked.assert_not_called()


def test_copied_running_receipt_same_deployment_is_not_live_database(tmp_path, monkeypatch):
    db = tmp_path / 'original.db'
    monkeypatch.setattr(state_db, 'DB_PATH', str(db))
    state_db.init_db()
    client, cert = issue(db, tmp_path / 'witness.db')
    lease = admission.claim(client=client, deployment_id=cert.deployment_id)
    lease.complete()
    copied = tmp_path / 'copied.db'
    shutil.copyfile(db, copied)
    shutil.copyfile(str(db) + '.admission.json', str(copied) + '.admission.json')
    with sqlite3.connect(copied) as conn:
        conn.execute('DELETE FROM processed_sigs')
        conn.execute('DROP TABLE solana_payout_budget_events')
    result = admission.dashboard_status(db_path=copied, client=client,
                                        deployment_id=cert.deployment_id)
    assert result['status'] == 'unknown'
