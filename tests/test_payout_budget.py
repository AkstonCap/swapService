"""Regression coverage for the durable Solana payout-cap reservation ledger.

These tests use a temporary SQLite database only; no chain RPC is invoked.
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from src import state_db


@contextmanager
def isolated_state(tmp_path):
    with patch.object(state_db, "DB_PATH", str(tmp_path / "state.db")):
        state_db.init_db()
        yield


def test_reservation_is_unique_and_counts_pending_capacity(tmp_path):
    with isolated_state(tmp_path):
        assert state_db.reserve_solana_payout_budget(
            obligation_id="nexus:credit-a:0", kind="nexus_payout", amount_usdc_units=60,
            cap_units=100,
        )
        assert not state_db.reserve_solana_payout_budget(
            obligation_id="nexus:credit-a:0", kind="nexus_payout", amount_usdc_units=60,
            cap_units=100,
        )
        assert not state_db.reserve_solana_payout_budget(
            obligation_id="nexus:credit-b:0", kind="nexus_payout", amount_usdc_units=50,
            cap_units=100,
        )

        assert state_db.payout_budget_used(86400) == 60
        with sqlite3.connect(state_db.DB_PATH) as conn:
            events = conn.execute(
                "SELECT obligation_id, event, amount_usdc_units FROM solana_payout_budget_events"
            ).fetchall()

    assert events == [("nexus:credit-a:0", "reserved", 60)]


def test_concurrent_reservations_cannot_oversubscribe_a_cap(tmp_path):
    with isolated_state(tmp_path):
        def reserve(index):
            return state_db.reserve_solana_payout_budget(
                obligation_id=f"nexus:credit-{index}:0", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(reserve, range(2)))

        assert sorted(results) == [False, True]
        assert state_db.payout_budget_used(86400) == 60


def test_confirmed_primary_payout_is_not_counted_again_through_legacy_payouts(tmp_path):
    """The primary durable event and compatibility record describe one Solana transfer."""
    with isolated_state(tmp_path):
        obligation_id = "nexus:credit-a:9"
        signature = "solana-signature"
        assert state_db.reserve_solana_payout_budget(
            obligation_id=obligation_id, kind="nexus_payout", amount_usdc_units=60,
            cap_units=100,
        )
        assert state_db.record_solana_payout_submission(obligation_id, signature)
        assert state_db.settle_solana_payout_budget(obligation_id, signature, 60)
        # The legacy helper still records every successful outbound send for the
        # unmigrated refund/quarantine paths. A primary send has both rows, but it
        # must consume its cap capacity exactly once.
        state_db.record_payout("solana_send", 60, signature)
        state_db.record_payout("solana_refund", 10, "legacy-refund-signature")

        # The separate, unmigrated legacy payment still occupies capacity.
        assert state_db.payout_budget_used(86400) == 70


def test_submitted_and_confirmed_events_preserve_exact_obligation_identity(tmp_path):
    with isolated_state(tmp_path):
        obligation_id = "nexus:credit-a:7"
        assert state_db.reserve_solana_payout_budget(
            obligation_id=obligation_id, kind="nexus_payout", amount_usdc_units=60,
            cap_units=100,
        )
        assert state_db.record_solana_payout_submission(obligation_id, "solana-signature")
        assert state_db.settle_solana_payout_budget(
            obligation_id, "solana-signature", 60
        )
        assert not state_db.settle_solana_payout_budget(
            obligation_id, "another-signature", 60
        )

        with sqlite3.connect(state_db.DB_PATH) as conn:
            events = conn.execute(
                """SELECT event, signature, amount_usdc_units
                   FROM solana_payout_budget_events
                   WHERE obligation_id = ? ORDER BY id""",
                (obligation_id,),
            ).fetchall()

    assert events == [
        ("reserved", None, 60),
        ("submitted", "solana-signature", 60),
        ("confirmed", "solana-signature", 60),
    ]


def test_recovery_completes_a_matching_preexisting_reservation(tmp_path):
    """A crash after an accepted send retains its cap claim and recovers exact finality."""
    with isolated_state(tmp_path), patch.object(state_db.time, "time", return_value=1_001):
        obligation_id = "nexus:credit-recovered:2"
        assert state_db.reserve_solana_payout_budget(
            obligation_id=obligation_id, kind="nexus_payout", amount_usdc_units=60,
            cap_units=100,
        )
        assert state_db.reconstruct_confirmed_solana_payout_budget(
            obligation_id=obligation_id,
            signature="recovered-signature",
            amount_usdc_units=60,
            chain_timestamp=1_000,
        )
        assert state_db.payout_budget_used(86400) == 60
        with sqlite3.connect(state_db.DB_PATH) as conn:
            events = conn.execute(
                """SELECT event, signature, amount_usdc_units
                   FROM solana_payout_budget_events
                   WHERE obligation_id = ? ORDER BY id""",
                (obligation_id,),
            ).fetchall()

    assert events == [
        ("reserved", None, 60),
        ("submitted", "recovered-signature", 60),
        ("confirmed", "recovered-signature", 60),
    ]


def test_refund_disposition_reserves_before_rpc_and_settles_on_confirmation(tmp_path):
    """Refunds use the durable cap ledger rather than the legacy read/send/write helper."""
    with isolated_state(tmp_path):
        state_db.add_unprocessed_sig(
            "deposit-a", 10, "original-memo", "recipient", 100,
            "to be refunded", None,
        )
        assert state_db.prepare_solana_sig_disposition(
            source_sig="deposit-a", kind="refund", timestamp=10,
            from_address="recipient", destination_address="recipient-token",
            amount_usdc_units=100, memo="original-memo",
            payout_memo="swapService:v1:refund:deposit-a", payout_units=90, cap_units=100,
        )
        assert state_db.payout_budget_used(86400) == 90
        assert not state_db.prepare_solana_sig_disposition(
            source_sig="deposit-a", kind="refund", timestamp=10,
            from_address="recipient", destination_address="recipient-token",
            amount_usdc_units=100, memo="original-memo",
            payout_memo="swapService:v1:refund:deposit-a", payout_units=90, cap_units=100,
        )

        assert state_db.record_solana_sig_disposition_submission(
            source_sig="deposit-a", kind="refund", payout_signature="refund-signature",
        )
        assert state_db.confirm_solana_sig_disposition(
            source_sig="deposit-a", kind="refund", payout_signature="refund-signature",
        )

        with sqlite3.connect(state_db.DB_PATH) as conn:
            refund = conn.execute(
                "SELECT refund_sig, refunded_units, status FROM refunded_sigs WHERE sig = 'deposit-a'"
            ).fetchone()
            events = conn.execute(
                """SELECT event, signature, amount_usdc_units
                   FROM solana_payout_budget_events
                   WHERE obligation_id = 'refund:deposit-a' ORDER BY id"""
            ).fetchall()
            pending = conn.execute(
                "SELECT 1 FROM unprocessed_sigs WHERE sig = 'deposit-a'"
            ).fetchone()
            fee = conn.execute(
                """SELECT kind, amount_usdc_units FROM fee_entries
                   WHERE sig = 'deposit-a'"""
            ).fetchone()

    assert refund == ("refund-signature", 90, "refund_confirmed")
    assert pending is None
    assert fee == ("refund_flat_fee", 10)
    assert events == [
        ("reserved", None, 90),
        ("submitted", "refund-signature", 90),
        ("confirmed", "refund-signature", 90),
    ]


def test_quarantine_disposition_cap_refusal_leaves_source_retryable(tmp_path):
    """A full cap must not create a partial quarantine intent or consume source state."""
    with isolated_state(tmp_path):
        state_db.add_unprocessed_sig(
            "deposit-b", 11, "original-memo", "sender", 100,
            "to be quarantined", None,
        )
        assert state_db.reserve_solana_payout_budget(
            obligation_id="other:obligation", kind="other", amount_usdc_units=60,
            cap_units=100,
        )
        assert not state_db.prepare_solana_sig_disposition(
            source_sig="deposit-b", kind="quarantine", timestamp=11,
            from_address="sender", destination_address="quarantine-token",
            amount_usdc_units=100, memo="original-memo",
            payout_memo="swapService:v1:quarantine:deposit-b",
            payout_units=50, cap_units=100,
        )
        with sqlite3.connect(state_db.DB_PATH) as conn:
            status = conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = 'deposit-b'"
            ).fetchone()
            quarantine = conn.execute(
                "SELECT 1 FROM quarantined_sigs WHERE sig = 'deposit-b'"
            ).fetchone()

    assert status == ("to be quarantined",)
    assert quarantine is None


def test_refund_submission_ledger_failure_keeps_reserved_capacity_held(tmp_path):
    """An accepted send cannot become retryable when its local signature write fails."""
    with isolated_state(tmp_path):
        state_db.add_unprocessed_sig(
            "deposit-ledger", 12, "original-memo", "recipient", 100,
            "to be refunded", None,
        )
        assert state_db.prepare_solana_sig_disposition(
            source_sig="deposit-ledger", kind="refund", timestamp=12,
            from_address="recipient", destination_address="recipient-token",
            amount_usdc_units=100, memo="original-memo",
            payout_memo="swapService:v1:refund:deposit-ledger",
            payout_units=90, cap_units=100,
        )
        with sqlite3.connect(state_db.DB_PATH) as conn:
            conn.execute(
                """CREATE TRIGGER reject_refund_submission
                   BEFORE UPDATE ON unprocessed_sigs
                   WHEN NEW.status = 'refund sent, awaiting confirmation'
                   BEGIN SELECT RAISE(ABORT, 'injected submission ledger failure'); END"""
            )
        with pytest.raises(sqlite3.DatabaseError, match="injected submission ledger failure"):
            state_db.record_solana_sig_disposition_submission(
                source_sig="deposit-ledger", kind="refund", payout_signature="refund-signature",
            )
        with sqlite3.connect(state_db.DB_PATH) as conn:
            status = conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = 'deposit-ledger'"
            ).fetchone()
            refund = conn.execute(
                "SELECT refund_sig, status FROM refunded_sigs WHERE sig = 'deposit-ledger'"
            ).fetchone()
            events = conn.execute(
                """SELECT event FROM solana_payout_budget_events
                   WHERE obligation_id = 'refund:deposit-ledger' ORDER BY id"""
            ).fetchall()

        assert status == ("refund submission held",)
        assert refund == (None, "submitting")
        assert events == [("reserved",)]
        assert state_db.payout_budget_used(86400) == 90


@pytest.mark.parametrize("amount, cap", [(True, 100), (0, 100), (1, -1), (1, "100")])
def test_reservation_rejects_non_exact_budget_terms(tmp_path, amount, cap):
    with isolated_state(tmp_path), pytest.raises(ValueError):
        state_db.reserve_solana_payout_budget(
            obligation_id="nexus:credit-a:0", kind="nexus_payout",
            amount_usdc_units=amount, cap_units=cap,
        )
