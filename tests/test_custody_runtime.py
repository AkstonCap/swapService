"""The independent image permit precedes every mutable service startup action."""
import sys
import threading
import types
from unittest.mock import Mock

import pytest

import src
from src import alerts, custody_chain, main, startup_recovery, state_db


class AdmissionError(RuntimeError):
    pass


@pytest.fixture
def admission_boundary(monkeypatch):
    lease = Mock()
    admission = types.ModuleType('src.custody_admission')
    admission.AdmissionError = AdmissionError
    admission.claim = Mock(return_value=lease)
    monkeypatch.setitem(sys.modules, 'src.custody_admission', admission)
    monkeypatch.setattr(src, 'custody_admission', admission, raising=False)
    from src import custody_chain
    monkeypatch.setattr(custody_chain, 'verify', Mock())
    monkeypatch.setattr(main, 'validate_production_controls', Mock(return_value=True))
    monkeypatch.setattr(main, 'acquire_singleton_lock', Mock(return_value=True))
    monkeypatch.setattr(alerts, 'critical', Mock())
    monkeypatch.setattr(state_db, 'init_db', Mock())
    monkeypatch.setattr(startup_recovery, 'perform_startup_recovery',
                        Mock(return_value={'recovery_complete': False, 'error': 'offline-test'}))
    return admission, lease


def test_missing_image_permit_refuses_before_database_or_chain_access(admission_boundary):
    admission, lease = admission_boundary
    admission.claim.side_effect = AdmissionError('custody_witness_unavailable')
    assert main.run() is False
    state_db.init_db.assert_not_called()
    startup_recovery.perform_startup_recovery.assert_not_called()
    lease.complete.assert_not_called()
    lease.seal.assert_not_called()
    alerts.critical.assert_called_once()
    assert alerts.critical.call_args.kwargs['error'] == 'custody_witness_unavailable'


@pytest.mark.parametrize('outcome', [False, RuntimeError('init failure'), KeyboardInterrupt()])
def test_unsuccessful_runtime_consumes_permit_without_reissuing(
    admission_boundary, monkeypatch, outcome,
):
    admission, lease = admission_boundary
    admitted = Mock(return_value=outcome if not isinstance(outcome, BaseException) else None)
    if isinstance(outcome, BaseException):
        admitted.side_effect = outcome
    monkeypatch.setattr(main, '_run_admitted', admitted)
    if isinstance(outcome, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            main.run()
    else:
        assert main.run() is False
    admission.claim.assert_called_once()
    admitted.assert_called_once_with(lease)
    lease.hold.assert_called_once()
    lease.seal.assert_not_called()


def test_successful_shutdown_reissues_only_owned_permit(admission_boundary, monkeypatch):
    _, lease = admission_boundary
    monkeypatch.setattr(main, '_run_admitted', Mock(return_value=True))
    assert main.run() is True
    lease.seal.assert_called_once()
    lease.hold.assert_not_called()


@pytest.mark.parametrize('helper', ['safe_call', 'watchdog', 'rpc'])
def test_timed_out_worker_blocks_seal_until_it_really_exits(helper):
    entered, release = threading.Event(), threading.Event()

    def worker():
        entered.set()
        release.wait(2)

    try:
        if helper == 'safe_call':
            with pytest.raises(TimeoutError):
                main._safe_call(worker, timeout_sec=0.01)
        elif helper == 'rpc':
            from src import solana_client
            with pytest.raises(TimeoutError):
                solana_client._rpc_call(worker, timeout=0.01)
        else:
            main._run_with_watchdog(worker, 'custody-drain-test', 0.01)
        assert entered.is_set()
        assert main._drain_custody_workers(timeout_sec=0) is False
    finally:
        release.set()
        main._drain_custody_workers(timeout_sec=2)
        main._running_pollers.pop('custody-drain-test', None)
    assert main._drain_custody_workers(timeout_sec=0) is True


def test_alive_worker_refuses_clean_runtime_reissue(admission_boundary, monkeypatch):
    _, lease = admission_boundary
    monkeypatch.setattr(main, '_run_admitted', Mock(return_value=True))
    monkeypatch.setattr(main, '_drain_custody_workers', Mock(return_value=False))
    assert main.run() is False
    lease.seal.assert_not_called()
    lease.hold.assert_called_once()


def test_recovery_failure_never_completes_runtime_permit(admission_boundary):
    _, lease = admission_boundary
    assert main.run() is False
    state_db.init_db.assert_called_once()
    startup_recovery.perform_startup_recovery.assert_called_once()
    lease.complete.assert_not_called()
    lease.seal.assert_not_called()


def test_running_permit_required_before_worker_startup_checks(admission_boundary, monkeypatch):
    from src import balance_reconciler, nexus_client
    _, lease = admission_boundary
    startup_recovery.perform_startup_recovery.return_value = {'recovery_complete': True}
    lease.complete.side_effect = AdmissionError('witness offline')
    monkeypatch.setattr(nexus_client, 'validate_session_config', lambda: (True, 'offline'))
    monkeypatch.setattr(main, '_safe_call', lambda *args, **kwargs: (True, 'offline'))
    check = Mock()
    monkeypatch.setattr(balance_reconciler, 'run_balance_reconciliation', check)
    assert main.run() is False
    lease.complete.assert_called_once()
    check.assert_not_called()
    lease.seal.assert_not_called()


def test_lost_running_witness_blocks_loop_before_maintenance(admission_boundary, monkeypatch):
    from src import balance_reconciler, nexus_client
    _, lease = admission_boundary
    lease.assert_running = Mock(side_effect=[None, AdmissionError('witness offline')])
    startup_recovery.perform_startup_recovery.return_value = {'recovery_complete': True}
    monkeypatch.setattr(nexus_client, 'validate_session_config', lambda: (True, 'offline fixture'))
    monkeypatch.setattr(balance_reconciler, 'run_balance_reconciliation',
                        lambda **kwargs: {'healthy': True, 'discrepancies': []})
    def safe_call(fn, *args, **kwargs):
        if fn.__name__ == 'validate_heartbeat_asset':
            return True, 'offline fixture'
        return 0
    monkeypatch.setattr(main, '_safe_call', safe_call)
    poller = Mock()
    monkeypatch.setattr(main, '_run_with_watchdog', poller)
    # Stop independently if the expected gate is accidentally removed.
    class OneCycle:
        calls = 0
        def is_set(self):
            self.calls += 1
            return self.calls > 1
        def set(self):
            self.calls = 10
    monkeypatch.setattr(main.threading, 'Event', OneCycle)
    assert main.run() is False
    assert lease.assert_running.call_count == 2
    poller.assert_not_called()
    lease.seal.assert_not_called()
    lease.hold.assert_called_once()


@pytest.mark.parametrize('boundary', ['file', 'chain'])
def test_lost_file_or_chain_identity_stops_before_mutable_startup(admission_boundary, monkeypatch, boundary):
    _, lease = admission_boundary
    if boundary == 'file':
        lease.verify_image.side_effect = AdmissionError('custody file identity changed')
    else:
        custody_chain.verify.side_effect = AdmissionError('wrong chain identity')
    assert main.run() is False
    state_db.init_db.assert_not_called()
    startup_recovery.perform_startup_recovery.assert_not_called()
    lease.complete.assert_not_called()
    lease.seal.assert_not_called()
    lease.hold.assert_called_once()


@pytest.mark.parametrize('result', [(False, 'wrong asset owner'), RuntimeError('offline')])
def test_invalid_heartbeat_never_completes_or_starts_workers(admission_boundary, monkeypatch, result):
    from src import nexus_client
    _, lease = admission_boundary
    startup_recovery.perform_startup_recovery.return_value = {'recovery_complete': True}
    monkeypatch.setattr(nexus_client, 'validate_session_config', lambda: (True, 'offline'))
    checker = Mock(return_value=result)
    if isinstance(result, Exception):
        checker.side_effect = result
    monkeypatch.setattr(main, '_safe_call', checker)
    poller = Mock()
    monkeypatch.setattr(main, '_run_with_watchdog', poller)
    assert main.run() is False
    lease.complete.assert_not_called()
    poller.assert_not_called()
    lease.seal.assert_not_called()
