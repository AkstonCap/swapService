"""Real-worker regressions for typed durable refund/quarantine capacity holds.

Only Solana transport/address resolution is replaced. The workers, preparation state
machine, SQLite transactions, liability accounting, dashboard, and alert boundary are real.
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest

from src import alerts, config, dashboard, solana_client, state_db


@contextmanager
def isolated_state(tmp_path, *, cap_units=100, refund_fee_units=10):
    pair = replace(
        config.SWAP_PAIR,
        fees=replace(config.SWAP_PAIR.fees, refund_solana_units=refund_fee_units),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(state_db, "DB_PATH", str(tmp_path / "state.db")))
        stack.enter_context(patch.object(config, "SWAP_PAIR", pair))
        stack.enter_context(
            patch.object(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", cap_units, create=True)
        )
        state_db.init_db()
        yield tmp_path / "state.db"


def queue_disposition(kind: str, source_sig: str, *, timestamp=10, amount=60):
    status = "to be refunded" if kind == "refund" else "to be quarantined"
    state_db.add_unprocessed_sig(source_sig, timestamp, "source-memo", "sender", amount, status, None)


def run_worker(kind: str):
    worker = (
        solana_client.process_solana_deposits_refunding
        if kind == "refund"
        else solana_client.process_solana_deposits_quarantine
    )
    return worker(limit=100, timeout=10.0)


def prepare(
    kind: str, source_sig: str, *, destination="destination-token", cap=100, timestamp=10
):
    return state_db.prepare_solana_sig_disposition(
        source_sig=source_sig, kind=kind, timestamp=timestamp,
        from_address="sender", destination_address=destination,
        amount_usdc_units=60, memo="source-memo",
        payout_memo=f"swapService:v1:{kind}:{source_sig}",
        payout_units=50, cap_units=cap,
    )


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
def test_real_worker_cap_exhaustion_persists_typed_hold_without_send(tmp_path, kind):
    with isolated_state(tmp_path) as db_path, patch.object(
        state_db.time, "time", return_value=1_000
    ), patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ), patch.object(
        solana_client, "send_solana_token_to_account_with_sig"
    ) as send, patch.object(alerts, "warning") as warning:
        assert state_db.reserve_solana_payout_budget(
            obligation_id="existing:payout", kind="nexus_payout",
            amount_usdc_units=60, cap_units=100,
        )
        queue_disposition(kind, f"{kind}-source", amount=60)

        assert run_worker(kind) == 0

        send.assert_not_called()
        with sqlite3.connect(db_path) as conn:
            hold = conn.execute(
                """SELECT source_signature, kind, obligation_id, needed_units,
                          used_units, cap_units, first_held_timestamp, reason,
                          intent_evidence
                     FROM solana_payout_capacity_holds"""
            ).fetchone()
            source = conn.execute(
                "SELECT status, amount_usdc_units FROM unprocessed_sigs"
            ).fetchone()
            terminal_table = "refunded_sigs" if kind == "refund" else "quarantined_sigs"
            terminal = conn.execute(f"SELECT 1 FROM {terminal_table}").fetchone()

        assert hold[:8] == (
            f"{kind}-source", kind, f"{kind}:{kind}-source", 50,
            60, 100, 1_000, "rolling Solana payout cap exhausted",
        )
        assert isinstance(hold[8], str) and hold[8]
        assert source == (f"{kind} capacity held", 60)
        assert terminal is None
        assert state_db.get_unresolved_solana_liability_units() == 60
        warning.assert_called_once_with(
            "solana_disposition_capacity_held",
            "Solana rolling payout cap exhausted; disposition held until capacity is available",
            source_signature=f"{kind}-source",
            kind=kind,
            obligation_id=f"{kind}:{kind}-source",
            needed_units=50,
            used_units=60,
            cap_units=100,
        )

        result = state_db.prepare_solana_sig_disposition(
            source_sig=f"{kind}-source", kind=kind, timestamp=10,
            from_address="sender", destination_address="destination-token",
            amount_usdc_units=60, memo="source-memo",
            payout_memo=f"swapService:v1:{kind}:{kind}-source",
            payout_units=50, cap_units=100,
        )
        assert result.status is state_db.SolanaDispositionPrepareStatus.CAPACITY_HELD
        assert not result


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
def test_restart_retries_frozen_capacity_hold_after_confirmed_spend_ages_out_once(
    tmp_path, kind
):
    with isolated_state(tmp_path) as db_path, patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ) as resolve, patch.object(
        solana_client, "send_solana_token_to_account_with_sig",
        return_value=(True, f"{kind}-payout"),
    ) as send, patch.object(alerts, "warning"):
        with patch.object(state_db.time, "time", return_value=1_000):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="existing:payout", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
            assert state_db.record_solana_payout_submission(
                "existing:payout", "existing-signature"
            )
            assert state_db.settle_solana_payout_budget(
                "existing:payout", "existing-signature", 60
            )
            queue_disposition(kind, f"{kind}-restart", amount=60)
            assert run_worker(kind) == 0

        with sqlite3.connect(db_path) as conn:
            frozen_evidence = conn.execute(
                """SELECT intent_evidence FROM solana_payout_capacity_holds
                   WHERE source_signature = ?""",
                (f"{kind}-restart",),
            ).fetchone()[0]

        # Re-opening the schema models process restart. Fee, configured destination,
        # and ATA resolution may all drift, but a held retry must use only its frozen
        # validated transfer terms while enforcing the current rolling cap.
        state_db.init_db()
        config.SWAP_PAIR = replace(
            config.SWAP_PAIR,
            fees=replace(config.SWAP_PAIR.fees, refund_solana_units=20),
        )
        resolve.reset_mock()
        resolve.side_effect = AssertionError("held retry must not resolve a new destination")
        with patch.object(config, "USDC_QUARANTINE_ACCOUNT", "changed-quarantine-owner"), \
                patch.object(state_db.time, "time", return_value=87_401):
            assert run_worker(kind) == 1
            assert run_worker(kind) == 0

        resolve.assert_not_called()
        send.assert_called_once_with(
            "destination-token", 50,
            memo=f"swapService:v1:{kind}:{kind}-restart",
        )
        with sqlite3.connect(db_path) as conn:
            hold = conn.execute(
                "SELECT 1 FROM solana_payout_capacity_holds WHERE source_signature = ?",
                (f"{kind}-restart",),
            ).fetchone()
            events = conn.execute(
                """SELECT event FROM solana_payout_budget_events
                   WHERE obligation_id = ? ORDER BY id""",
                (f"{kind}:{kind}-restart",),
            ).fetchall()
            source_status = conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = ?",
                (f"{kind}-restart",),
            ).fetchone()
            terminal_table = "refunded_sigs" if kind == "refund" else "quarantined_sigs"
            terminal_evidence = conn.execute(
                f"SELECT intent_evidence FROM {terminal_table} WHERE sig = ?",
                (f"{kind}-restart",),
            ).fetchone()[0]

        assert hold is None
        assert events == [("reserved",), ("submitted",)]
        assert source_status == (f"{kind} sent, awaiting confirmation",)
        assert terminal_evidence == frozen_evidence


def test_released_capacity_is_admitted_in_global_oldest_hold_order(tmp_path):
    with isolated_state(tmp_path) as db_path, patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ), patch.object(
        solana_client, "send_solana_token_to_account_with_sig",
        return_value=(True, "oldest-payout"),
    ) as send, patch.object(alerts, "warning"):
        with patch.object(state_db.time, "time", return_value=1_000):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="blocking:reservation", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
            queue_disposition("quarantine", "oldest", amount=60)
            assert run_worker("quarantine") == 0
        with patch.object(state_db.time, "time", return_value=1_001):
            queue_disposition("refund", "younger", timestamp=11, amount=60)
            assert run_worker("refund") == 0

        with patch.object(state_db.time, "time", return_value=1_002):
            assert state_db.release_solana_payout_budget(
                "blocking:reservation", "operator cancelled before submission"
            )

        state_db.init_db()
        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 50
        with patch.object(state_db.time, "time", return_value=1_003):
            # The service normally invokes refunds first. A younger refund must not
            # jump an older quarantine merely because its worker happens to run first.
            assert run_worker("refund") == 0
            assert run_worker("quarantine") == 1
            assert run_worker("refund") == 0

        send.assert_called_once_with(
            "destination-token", 50,
            memo="swapService:v1:quarantine:oldest",
        )
        with sqlite3.connect(db_path) as conn:
            holds = conn.execute(
                """SELECT source_signature, kind FROM solana_payout_capacity_holds
                   ORDER BY first_held_timestamp, source_signature"""
            ).fetchall()
            source_statuses = dict(conn.execute(
                "SELECT sig, status FROM unprocessed_sigs"
            ).fetchall())
            released = conn.execute(
                """SELECT event, evidence FROM solana_payout_budget_events
                   WHERE obligation_id = 'blocking:reservation' ORDER BY id"""
            ).fetchall()

        assert holds == [("younger", "refund")]
        assert source_statuses == {
            "oldest": "quarantine sent, awaiting confirmation",
            "younger": "refund capacity held",
        }
        assert released == [
            ("reserved", None),
            ("released", "operator cancelled before submission"),
        ]


def test_changed_frozen_hold_terms_are_a_source_conflict_not_malformed_evidence(tmp_path):
    with isolated_state(tmp_path), patch.object(state_db.time, "time", return_value=1_000):
        assert state_db.reserve_solana_payout_budget(
            obligation_id="blocking", kind="nexus_payout",
            amount_usdc_units=60, cap_units=100,
        )
        queue_disposition("refund", "drifted", amount=60)
        assert prepare("refund", "drifted").status is (
            state_db.SolanaDispositionPrepareStatus.CAPACITY_HELD
        )

        result = prepare("refund", "drifted", destination="changed-destination")

        assert result.status is state_db.SolanaDispositionPrepareStatus.SOURCE_CONFLICT
        assert state_db.get_unprocessed_sig_status("drifted") == "refund capacity held"


@pytest.mark.parametrize(
    ("kind", "conflict"),
    [
        ("refund", "processed"),
        ("refund", "quarantine"),
        ("quarantine", "processed"),
        ("quarantine", "refund"),
    ],
)
def test_real_worker_capacity_hold_terminal_conflict_retains_full_liability(
    tmp_path, kind, conflict
):
    source_sig = f"{kind}-{conflict}-conflict"
    with isolated_state(tmp_path) as db_path, patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ) as resolve, patch.object(
        solana_client, "send_solana_token_to_account_with_sig"
    ) as send, patch.object(alerts, "warning"):
        with patch.object(state_db.time, "time", return_value=1_000):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="blocking", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
            queue_disposition(kind, source_sig, amount=60)
            assert run_worker(kind) == 0
            assert state_db.release_solana_payout_budget(
                "blocking", "operator released known-unsent blocker"
            )

        if conflict == "processed":
            state_db.mark_processed_sig(source_sig, 10, 60, None, 0, "processed", None)
        elif conflict == "quarantine":
            state_db.mark_quarantined_sig(
                source_sig, 10, "sender", 60, "source-memo",
                "conflicting-quarantine", 50, "quarantine_confirmed",
            )
        else:
            state_db.mark_refunded_sig(
                source_sig, 10, "sender", 60, "source-memo",
                "conflicting-refund", 50, "refund_confirmed",
            )

        with sqlite3.connect(db_path) as conn:
            before_terminal = {
                table: conn.execute(
                    f"SELECT * FROM {table} WHERE sig = ?", (source_sig,)
                ).fetchone()
                for table in ("processed_sigs", "refunded_sigs", "quarantined_sigs")
            }
            before_fees = conn.execute(
                "SELECT * FROM fee_entries WHERE sig = ?", (source_sig,)
            ).fetchall()

        resolve.reset_mock()
        resolve.side_effect = AssertionError("held conflict must not resolve a new destination")
        with patch.object(alerts, "critical") as critical, patch.object(
            state_db.time, "time", return_value=1_001
        ):
            assert run_worker(kind) == 0

        resolve.assert_not_called()
        send.assert_not_called()
        critical.assert_called_once()
        assert critical.call_args.kwargs["source_signature"] == source_sig
        assert critical.call_args.kwargs["kind"] == kind
        assert critical.call_args.kwargs["status"] == "source_conflict"
        assert state_db.get_unprocessed_sig_status(source_sig) == f"{kind} capacity held"
        assert state_db.get_unresolved_solana_liability_units() == 60

        issue = next(item for item in dashboard.api_issues()["issues"] if item["id"] == source_sig)
        assert issue["amount"] == pytest.approx(0.00006)
        assert issue["capacity_hold"]["source_signature"] == source_sig
        assert issue["capacity_hold"]["needed_units"] == 50

        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                """SELECT kind, needed_units FROM solana_payout_capacity_holds
                   WHERE source_signature = ?""",
                (source_sig,),
            ).fetchone() == (kind, 50)
            after_terminal = {
                table: conn.execute(
                    f"SELECT * FROM {table} WHERE sig = ?", (source_sig,)
                ).fetchone()
                for table in ("processed_sigs", "refunded_sigs", "quarantined_sigs")
            }
            after_fees = conn.execute(
                "SELECT * FROM fee_entries WHERE sig = ?", (source_sig,)
            ).fetchall()
        assert after_terminal == before_terminal
        assert after_fees == before_fees


def test_malformed_frozen_hold_is_typed_and_never_replaced(tmp_path):
    with isolated_state(tmp_path) as db_path, patch.object(
        state_db.time, "time", return_value=1_000
    ):
        assert state_db.reserve_solana_payout_budget(
            obligation_id="blocking", kind="nexus_payout",
            amount_usdc_units=60, cap_units=100,
        )
        queue_disposition("refund", "malformed", amount=60)
        assert prepare("refund", "malformed").status is (
            state_db.SolanaDispositionPrepareStatus.CAPACITY_HELD
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """UPDATE solana_payout_capacity_holds SET intent_evidence = '{'
                   WHERE source_signature = 'malformed'"""
            )

        result = prepare("refund", "malformed")

        assert result.status is state_db.SolanaDispositionPrepareStatus.MALFORMED_EVIDENCE
        with sqlite3.connect(db_path) as conn:
            evidence, attempts = conn.execute(
                """SELECT intent_evidence, attempt_count
                   FROM solana_payout_capacity_holds WHERE source_signature = 'malformed'"""
            ).fetchone()
        assert evidence == "{"
        assert attempts == 1


def test_capacity_hold_database_failure_rolls_back_source_and_hold(tmp_path):
    with isolated_state(tmp_path) as db_path, patch.object(
        state_db.time, "time", return_value=1_000
    ):
        assert state_db.reserve_solana_payout_budget(
            obligation_id="blocking", kind="nexus_payout",
            amount_usdc_units=60, cap_units=100,
        )
        queue_disposition("refund", "db-fault", amount=60)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TRIGGER reject_capacity_hold
                   BEFORE INSERT ON solana_payout_capacity_holds
                   BEGIN SELECT RAISE(ABORT, 'injected hold failure'); END"""
            )

        result = prepare("refund", "db-fault")

        assert result.status is state_db.SolanaDispositionPrepareStatus.DB_FAILURE
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = 'db-fault'"
            ).fetchone() == ("to be refunded",)
            assert conn.execute(
                "SELECT 1 FROM solana_payout_capacity_holds"
            ).fetchone() is None
            assert conn.execute(
                """SELECT 1 FROM solana_payout_budget_events
                   WHERE obligation_id = 'refund:db-fault'"""
            ).fetchone() is None


def test_prepare_returns_typed_db_failure_when_database_cannot_open(tmp_path):
    with isolated_state(tmp_path):
        queue_disposition("refund", "db-open-fault", amount=60)
        with patch.object(
            state_db.sqlite3, "connect", side_effect=sqlite3.OperationalError("cannot open")
        ):
            result = prepare("refund", "db-open-fault")

        assert result.status is state_db.SolanaDispositionPrepareStatus.DB_FAILURE
        assert result.reason == "database failure: OperationalError"


def test_concurrent_real_workers_at_exact_cap_boundary_send_once(tmp_path):
    with isolated_state(tmp_path, cap_units=50), patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ), patch.object(
        solana_client, "send_solana_token_to_account_with_sig",
        return_value=(True, "one-signature"),
    ) as send:
        queue_disposition("refund", "concurrent", amount=60)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: run_worker("refund"), range(2)))

        assert sorted(results) == [0, 1]
        send.assert_called_once_with(
            "destination-token", 50,
            memo="swapService:v1:refund:concurrent",
        )
        assert state_db.payout_budget_used(86400) == 50


def test_alert_failure_cannot_erase_capacity_hold_or_trigger_send(tmp_path):
    with isolated_state(tmp_path) as db_path, patch.object(
        state_db.time, "time", return_value=1_000
    ), patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ), patch.object(
        solana_client, "send_solana_token_to_account_with_sig"
    ) as send, patch.object(
        alerts, "warning", side_effect=RuntimeError("alert transport failed")
    ):
        assert state_db.reserve_solana_payout_budget(
            obligation_id="blocking", kind="nexus_payout",
            amount_usdc_units=60, cap_units=100,
        )
        queue_disposition("refund", "alert-fault", amount=60)

        assert run_worker("refund") == 0

        send.assert_not_called()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                """SELECT kind, needed_units FROM solana_payout_capacity_holds
                   WHERE source_signature = 'alert-fault'"""
            ).fetchone() == ("refund", 50)
        assert state_db.get_unresolved_solana_liability_units() == 60


def test_dashboard_surfaces_complete_capacity_hold_evidence(tmp_path):
    with isolated_state(tmp_path), patch.object(state_db.time, "time", return_value=1_000):
        assert state_db.reserve_solana_payout_budget(
            obligation_id="blocking", kind="nexus_payout",
            amount_usdc_units=60, cap_units=100,
        )
        queue_disposition("refund", "visible", amount=60)
        assert prepare("refund", "visible").status is (
            state_db.SolanaDispositionPrepareStatus.CAPACITY_HELD
        )

        summary = dashboard.api_summary()
        issue = next(
            item for item in dashboard.api_issues()["issues"]
            if item["id"] == "visible"
        )

        assert summary["counts"]["solana_payout_capacity_holds"] == 1
        assert issue["status"] == "refund capacity held"
        assert issue["detail"] == "rolling Solana payout cap exhausted"
        assert issue["operator_action"] == (
            "wait for rolling capacity; automatic retry preserves the frozen intent"
        )
        assert issue["capacity_hold"] == {
            "source_signature": "visible",
            "kind": "refund",
            "obligation_id": "refund:visible",
            "needed_units": 50,
            "used_units": 60,
            "cap_units": 100,
            "first_held_timestamp": 1_000,
            "updated_timestamp": 1_000,
            "reason": "rolling Solana payout cap exhausted",
            "attempt_count": 1,
        }


def test_typed_hold_reader_and_worker_candidates_use_first_held_fair_order(tmp_path):
    with isolated_state(tmp_path):
        with patch.object(state_db.time, "time", return_value=900):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="blocking", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
        with patch.object(state_db.time, "time", return_value=1_000):
            queue_disposition("refund", "old-hold", timestamp=20, amount=60)
            assert prepare("refund", "old-hold", timestamp=20).status is (
                state_db.SolanaDispositionPrepareStatus.CAPACITY_HELD
            )
        with patch.object(state_db.time, "time", return_value=1_001):
            queue_disposition("refund", "young-hold", timestamp=10, amount=60)
            assert prepare("refund", "young-hold", timestamp=10).status is (
                state_db.SolanaDispositionPrepareStatus.CAPACITY_HELD
            )

        holds = state_db.get_solana_payout_capacity_holds(kind="refund", limit=10)
        candidates = state_db.get_solana_sig_disposition_candidates("refund", limit=1)

        assert all(isinstance(hold, state_db.SolanaPayoutCapacityHold) for hold in holds)
        assert [hold.source_signature for hold in holds] == ["old-hold", "young-hold"]
        assert holds[0].obligation_id == "refund:old-hold"
        assert holds[0].needed_units == 50
        assert holds[0].used_units == 60
        assert holds[0].cap_units == 100
        assert holds[0].first_held_timestamp == 1_000
        assert holds[0].reason == "rolling Solana payout cap exhausted"
        assert candidates == [
            ("old-hold", 20, "source-memo", "sender", 60, "refund capacity held", None)
        ]


def test_worker_alerts_on_malformed_hold_without_send_or_evidence_rewrite(tmp_path):
    with isolated_state(tmp_path) as db_path, patch.object(
        state_db.time, "time", return_value=1_000
    ), patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ) as resolve, patch.object(
        solana_client, "send_solana_token_to_account_with_sig"
    ) as send, patch.object(alerts, "critical") as critical:
        assert state_db.reserve_solana_payout_budget(
            obligation_id="blocking", kind="nexus_payout",
            amount_usdc_units=60, cap_units=100,
        )
        queue_disposition("refund", "bad-held-json", amount=60)
        assert run_worker("refund") == 0
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """UPDATE solana_payout_capacity_holds SET intent_evidence = '{'
                   WHERE source_signature = 'bad-held-json'"""
            )

        resolve.reset_mock()
        resolve.side_effect = AssertionError("malformed held intent must not resolve destination")
        assert run_worker("refund") == 0

        resolve.assert_not_called()
        send.assert_not_called()
        critical.assert_called_once_with(
            "solana_disposition_preparation_held",
            "Solana disposition preparation failed closed",
            source_signature="bad-held-json",
            kind="refund",
            obligation_id="refund:bad-held-json",
            status="malformed_evidence",
            reason="capacity hold has malformed frozen intent evidence",
        )
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                """SELECT intent_evidence FROM solana_payout_capacity_holds
                   WHERE source_signature = 'bad-held-json'"""
            ).fetchone() == ("{",)
        assert state_db.get_unprocessed_sig_status("bad-held-json") == "refund capacity held"
        assert state_db.get_unresolved_solana_liability_units() == 60
        issue = next(
            item for item in dashboard.api_issues()["issues"]
            if item["id"] == "bad-held-json"
        )
        assert issue["capacity_hold"]["source_signature"] == "bad-held-json"


def test_prepare_result_distinguishes_conflicting_lifecycle_and_already_submitted(tmp_path):
    with isolated_state(tmp_path) as db_path:
        queue_disposition("refund", "conflicting", amount=60)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """INSERT INTO processed_sigs
                   (sig, timestamp, amount_usdc_units, status)
                   VALUES ('conflicting', 10, 60, 'processed')"""
            )
        conflict = prepare("refund", "conflicting")
        assert conflict.status is state_db.SolanaDispositionPrepareStatus.SOURCE_CONFLICT

        queue_disposition("refund", "claimed", timestamp=11, amount=60)
        first = state_db.prepare_solana_sig_disposition(
            source_sig="claimed", kind="refund", timestamp=11,
            from_address="sender", destination_address="destination-token",
            amount_usdc_units=60, memo="source-memo",
            payout_memo="swapService:v1:refund:claimed", payout_units=50, cap_units=100,
        )
        second = state_db.prepare_solana_sig_disposition(
            source_sig="claimed", kind="refund", timestamp=11,
            from_address="sender", destination_address="destination-token",
            amount_usdc_units=60, memo="source-memo",
            payout_memo="swapService:v1:refund:claimed", payout_units=50, cap_units=100,
        )
        assert first.status is state_db.SolanaDispositionPrepareStatus.PREPARED
        assert second.status is state_db.SolanaDispositionPrepareStatus.ALREADY_SUBMITTED
        assert not second


def test_malformed_proposed_terms_are_typed_without_any_database_write(tmp_path):
    with isolated_state(tmp_path) as db_path:
        queue_disposition("refund", "bad-input", amount=60)
        result = state_db.prepare_solana_sig_disposition(
            source_sig="bad-input", kind="refund", timestamp=10,
            from_address="sender", destination_address="destination-token",
            amount_usdc_units=60, memo="source-memo",
            payout_memo="swapService:v1:refund:bad-input",
            payout_units=True, cap_units=100,
        )

        assert result.status is state_db.SolanaDispositionPrepareStatus.MALFORMED_EVIDENCE
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = 'bad-input'"
            ).fetchone() == ("to be refunded",)
            assert conn.execute(
                "SELECT 1 FROM solana_payout_capacity_holds"
            ).fetchone() is None
            assert conn.execute(
                "SELECT 1 FROM refunded_sigs"
            ).fetchone() is None


def test_malformed_persisted_source_evidence_is_typed_and_left_unchanged(tmp_path):
    with isolated_state(tmp_path) as db_path:
        queue_disposition("refund", "bad-source", amount=60)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE unprocessed_sigs SET memo = ? WHERE sig = 'bad-source'",
                (sqlite3.Binary(b"\xff"),),
            )

        result = prepare("refund", "bad-source")

        assert result.status is state_db.SolanaDispositionPrepareStatus.MALFORMED_EVIDENCE
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT memo, status FROM unprocessed_sigs WHERE sig = 'bad-source'"
            ).fetchone() == (b"\xff", "to be refunded")
            assert conn.execute(
                "SELECT 1 FROM solana_payout_capacity_holds"
            ).fetchone() is None


def test_release_refuses_pre_rpc_disposition_intent_and_submitted_transfer(tmp_path):
    with isolated_state(tmp_path):
        queue_disposition("refund", "unknown-outcome", amount=60)
        assert prepare("refund", "unknown-outcome").status is (
            state_db.SolanaDispositionPrepareStatus.PREPARED
        )
        assert not state_db.release_solana_payout_budget(
            "refund:unknown-outcome", "must not release a possibly submitted transfer"
        )

        assert state_db.reserve_solana_payout_budget(
            obligation_id="submitted:other", kind="nexus_payout",
            amount_usdc_units=10, cap_units=100,
        )
        assert state_db.record_solana_payout_submission(
            "submitted:other", "remote-signature"
        )
        assert not state_db.release_solana_payout_budget(
            "submitted:other", "must not release returned remote identity"
        )
        assert state_db.payout_budget_used(86400) == 60


@pytest.mark.parametrize(
    ("older_kind", "younger_kind"),
    [
        ("refund", "refund"),
        ("quarantine", "quarantine"),
        ("refund", "quarantine"),
        ("quarantine", "refund"),
    ],
)
def test_real_workers_skip_older_currently_impossible_hold_for_younger_fitting_payout(
    tmp_path, older_kind, younger_kind
):
    old_sig = f"old-{older_kind}-before-{younger_kind}"
    young_sig = f"young-{younger_kind}-after-{older_kind}"
    with isolated_state(tmp_path) as db_path, patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ), patch.object(
        solana_client, "send_solana_token_to_account_with_sig",
        return_value=(True, "younger-payout"),
    ) as send, patch.object(alerts, "warning") as warning, patch.object(
        alerts, "critical"
    ) as critical:
        with patch.object(state_db.time, "time", return_value=1_000):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="blocking", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
            queue_disposition(older_kind, old_sig, amount=60)
            assert run_worker(older_kind) == 0
            assert state_db.release_solana_payout_budget("blocking", "known unsent")

        with sqlite3.connect(db_path) as conn:
            frozen_old_intent = conn.execute(
                """SELECT intent_evidence FROM solana_payout_capacity_holds
                   WHERE source_signature = ?""",
                (old_sig,),
            ).fetchone()[0]

        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 40
        with patch.object(state_db.time, "time", return_value=2_000):
            assert run_worker(older_kind) == 0

        # A restart at the unchanged cap must neither retry nor re-alert this
        # non-sendable hold. It remains eligible if an operator raises the cap later.
        state_db.init_db()
        with patch.object(state_db.time, "time", return_value=2_001):
            assert run_worker(older_kind) == 0
            queue_disposition(younger_kind, young_sig, timestamp=11, amount=40)
            assert run_worker(younger_kind) == 1

        send.assert_called_once_with(
            "destination-token", 30,
            memo=f"swapService:v1:{younger_kind}:{young_sig}",
        )
        assert state_db.payout_budget_used(86400) == 30
        assert critical.call_count == 1
        assert critical.call_args.args[:2] == (
            "solana_disposition_current_cap_too_low",
            "Frozen Solana disposition exceeds the current nonzero payout cap",
        )
        assert critical.call_args.kwargs == {
            "source_signature": old_sig,
            "kind": older_kind,
            "obligation_id": f"{older_kind}:{old_sig}",
            "needed_units": 50,
            "used_units": 0,
            "cap_units": 40,
            "operator_action": (
                "raise the payout cap to at least needed_units; do not send manually"
            ),
        }
        # The initial rolling hold warns once; unchanged impossible-cap retries are
        # filtered before the worker limit and cannot create an alert storm.
        assert warning.call_count == 1

        with sqlite3.connect(db_path) as conn:
            old_hold = conn.execute(
                """SELECT needed_units, used_units, cap_units, reason,
                          intent_evidence, attempt_count
                   FROM solana_payout_capacity_holds WHERE source_signature = ?""",
                (old_sig,),
            ).fetchone()
            sources = dict(conn.execute(
                "SELECT sig, (status || ':' || amount_usdc_units) FROM unprocessed_sigs"
            ).fetchall())
            young_table = "refunded_sigs" if younger_kind == "refund" else "quarantined_sigs"
            young_terminal = conn.execute(
                f"""SELECT destination_address, amount_usdc_units,
                           {('refunded_units' if younger_kind == 'refund' else 'quarantined_units')},
                           payout_memo
                    FROM {young_table} WHERE sig = ?""",
                (young_sig,),
            ).fetchone()

        assert old_hold == (
            50, 0, 40,
            "frozen Solana payout exceeds current nonzero cap",
            frozen_old_intent, 2,
        )
        assert sources[old_sig] == f"{older_kind} capacity held:60"
        assert sources[young_sig] == f"{younger_kind} sent, awaiting confirmation:40"
        assert young_terminal == (
            "destination-token", 40, 30,
            f"swapService:v1:{younger_kind}:{young_sig}",
        )
        assert state_db.get_unresolved_solana_liability_units() == 100

        issue = next(
            item for item in dashboard.api_issues()["issues"] if item["id"] == old_sig
        )
        assert issue["detail"] == "frozen Solana payout exceeds current nonzero cap"
        assert issue["operator_action"] == (
            "raise the payout cap to at least the frozen needed units; "
            "do not send manually; automatic retry resumes after a safe cap increase"
        )


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
@pytest.mark.parametrize("recovery_cap", [50, 0])
def test_restart_reenables_impossible_hold_with_original_frozen_terms_after_cap_change(
    tmp_path, kind, recovery_cap
):
    source_sig = f"{kind}-cap-recovery-{recovery_cap}"
    with isolated_state(tmp_path) as db_path, patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="frozen-destination"
    ) as resolve, patch.object(
        solana_client, "send_solana_token_to_account_with_sig",
        return_value=(True, "recovered-payout"),
    ) as send, patch.object(alerts, "warning"), patch.object(
        alerts, "critical"
    ) as critical:
        with patch.object(state_db.time, "time", return_value=1_000):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="blocking", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
            queue_disposition(kind, source_sig, amount=60)
            assert run_worker(kind) == 0
            assert state_db.release_solana_payout_budget("blocking", "known unsent")

        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 40
        with patch.object(state_db.time, "time", return_value=2_000):
            assert run_worker(kind) == 0
        with sqlite3.connect(db_path) as conn:
            frozen_evidence = conn.execute(
                """SELECT intent_evidence FROM solana_payout_capacity_holds
                   WHERE source_signature = ?""",
                (source_sig,),
            ).fetchone()[0]

        state_db.init_db()
        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = recovery_cap
        config.SWAP_PAIR = replace(
            config.SWAP_PAIR,
            fees=replace(config.SWAP_PAIR.fees, refund_solana_units=20),
        )
        resolve.reset_mock()
        resolve.side_effect = AssertionError("held retry must use its frozen destination")
        recoverable_issue = next(
            item for item in dashboard.api_issues()["issues"]
            if item["id"] == source_sig
        )
        assert recoverable_issue["detail"] == (
            "current payout cap admits the frozen payout; automatic retry pending"
        )
        assert recoverable_issue["operator_action"] == (
            "allow automatic retry; inspect if stale; do not send manually"
        )
        with patch.object(config, "USDC_QUARANTINE_ACCOUNT", "changed-owner"), patch.object(
            state_db.time, "time", return_value=2_001
        ):
            assert run_worker(kind) == 1
            assert run_worker(kind) == 0

        resolve.assert_not_called()
        send.assert_called_once_with(
            "frozen-destination", 50,
            memo=f"swapService:v1:{kind}:{source_sig}",
        )
        assert critical.call_count == 1
        assert state_db.payout_budget_used(86400) == 50
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT 1 FROM solana_payout_capacity_holds WHERE source_signature = ?",
                (source_sig,),
            ).fetchone() is None
            terminal_table = "refunded_sigs" if kind == "refund" else "quarantined_sigs"
            terminal = conn.execute(
                f"""SELECT destination_address,
                           {('refunded_units' if kind == 'refund' else 'quarantined_units')},
                           payout_memo, intent_evidence
                    FROM {terminal_table} WHERE sig = ?""",
                (source_sig,),
            ).fetchone()
        assert terminal == (
            "frozen-destination", 50,
            f"swapService:v1:{kind}:{source_sig}", frozen_evidence,
        )


def test_worker_limit_skips_already_diagnosed_impossible_holds_and_admits_fitting_work(
    tmp_path
):
    with isolated_state(tmp_path) as db_path, patch.object(
        solana_client, "_resolve_solana_token_destination", return_value="destination-token"
    ), patch.object(
        solana_client, "send_solana_token_to_account_with_sig",
        return_value=(True, "fitting-payout"),
    ) as send, patch.object(alerts, "warning") as warning, patch.object(
        alerts, "critical"
    ) as critical:
        with patch.object(state_db.time, "time", return_value=1_000):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="blocking", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
            for index in range(4):
                queue_disposition("refund", f"impossible-{index}", timestamp=10 + index, amount=60)
            assert run_worker("refund") == 0
            assert state_db.release_solana_payout_budget("blocking", "known unsent")

        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 40
        with patch.object(state_db.time, "time", return_value=2_000):
            # Only two stale holds fit the worker limit. They are diagnosed once.
            assert solana_client.process_solana_deposits_refunding(limit=2, timeout=10) == 0
            # Even a not-yet-retried hold is diagnosed from its frozen needed units and
            # the live cap, so bounded worker scans cannot hide the operator action.
            stale_issue = next(
                item for item in dashboard.api_issues()["issues"]
                if item["id"] == "impossible-3"
            )
            assert stale_issue["detail"] == "frozen Solana payout exceeds current nonzero cap"
            assert stale_issue["operator_action"] == (
                "raise the payout cap to at least the frozen needed units; "
                "do not send manually; automatic retry resumes after a safe cap increase"
            )
            queue_disposition("refund", "fits-beyond-limit", timestamp=20, amount=40)
            # The fresh fitting row sorts before remaining stale-impossible diagnostics.
            assert solana_client.process_solana_deposits_refunding(limit=1, timeout=10) == 1

        send.assert_called_once_with(
            "destination-token", 30,
            memo="swapService:v1:refund:fits-beyond-limit",
        )
        assert warning.call_count == 4
        assert critical.call_count == 2
        assert state_db.payout_budget_used(86400) == 30
        with sqlite3.connect(db_path) as conn:
            holds = conn.execute(
                """SELECT source_signature, reason, attempt_count
                   FROM solana_payout_capacity_holds ORDER BY source_signature"""
            ).fetchall()
            fitting_status = conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = 'fits-beyond-limit'"
            ).fetchone()
        assert holds == [
            (
                f"impossible-{index}",
                (
                    "frozen Solana payout exceeds current nonzero cap"
                    if index < 2 else "waiting behind older Solana payout capacity hold"
                ),
                2 if index < 2 else 1,
            )
            for index in range(4)
        ]
        assert fitting_status == ("refund sent, awaiting confirmation",)
