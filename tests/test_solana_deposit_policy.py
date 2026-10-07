from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock

import pytest

from src import config, dashboard, nexus_client, solana_client, solana_deposit_policy, state_db


@contextmanager
def isolated_policy_state(monkeypatch, tmp_path, *, minimum: int, maximum: int = 0):
    db_path = tmp_path / "policy-state.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(config, "MIN_DEPOSIT_SOLANA_UNITS", minimum)
    monkeypatch.setattr(config, "MAX_SWAP_SOLANA_UNITS", maximum)
    state_db.init_db()
    yield db_path


def test_real_worker_holds_positive_below_minimum_without_transport_or_fee(monkeypatch, tmp_path):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=1_000_001) as db_path:
        state_db.add_unprocessed_sig(
            "below-minimum", 100, "nexus:recipient", "source-account", 1_000_000,
            "ready for processing", None,
        )
        transport = Mock(return_value=(True, "must-not-send"))
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _address: True)
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", transport)

        result = solana_client.process_unprocessed_solana_deposits(limit=1, timeout=10)

        assert result == [0, 0, 0, 1]
        transport.assert_not_called()
        assert state_db.get_unprocessed_sig_status("below-minimum") == "policy held, non-sendable"
        assert state_db.get_unresolved_solana_liability_units() == 1_000_000
        issue = next(
            item for item in dashboard.api_issues()["issues"]
            if item["id"] == "below-minimum"
        )
        assert issue["status"] == "policy held, non-sendable"
        assert issue["amount"] == 1.0
        assert issue["operator_action"] == (
            "retain the full principal; review policy evidence before manual disposition"
        )
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM processed_sigs WHERE sig = 'below-minimum'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM fee_entries WHERE sig = 'below-minimum'"
            ).fetchone()[0] == 0


def test_public_minimum_uses_same_mixed_decimal_policy_terms(monkeypatch):
    pair = replace(
        config.SWAP_PAIR,
        solana=replace(config.SWAP_PAIR.solana, decimals=8),
        nexus=replace(config.SWAP_PAIR.nexus, decimals=6),
        fees=replace(
            config.SWAP_PAIR.fees,
            flat_to_nexus_units=123_456,
            basis_points=17,
        ),
    )
    monkeypatch.setattr(config, "SWAP_PAIR", pair)
    monkeypatch.setattr(config, "MIN_DEPOSIT_SOLANA_UNITS", 20_000_001)
    monkeypatch.setattr(config, "MAX_SWAP_SOLANA_UNITS", 0)
    monkeypatch.setattr(config, "USDC_DECIMALS", 6)
    monkeypatch.setattr(config, "MICRO_DEPOSIT_FEE_PCT", 99)

    record = nexus_client.build_service_record(last_poll=1)

    assert record["min_to_nexus"] == "0.20000001"
    assert record["fee_flat_to_nexus"] == "0.123456"
    assert record["fee_bps"] == "17"
    assert not any("micro" in field for field in record)


@pytest.mark.parametrize(
    ("input_decimals", "output_decimals", "amount", "minimum", "expected", "decision"),
    [
        (8, 6, 20_000_000, 20_000_001, 76_204, solana_deposit_policy.HOLD_BELOW_MINIMUM),
        (8, 6, 20_000_001, 20_000_001, 76_204, solana_deposit_policy.PAYABLE),
        (8, 6, 20_000_101, 20_000_001, 76_205, solana_deposit_policy.PAYABLE),
        (6, 8, 200_001, 200_001, 19_842_644, solana_deposit_policy.PAYABLE),
    ],
)
def test_exact_policy_boundaries_floor_rescaling_and_flat_bps(
    input_decimals, output_decimals, amount, minimum, expected, decision,
):
    terms = solana_deposit_policy.SolanaDepositTerms(
        minimum_input_units=minimum,
        maximum_input_units=0,
        input_decimals=input_decimals,
        output_decimals=output_decimals,
        flat_output_fee_units=123_456,
        fee_basis_points=17,
    )

    classified = solana_deposit_policy.classify(amount, terms)

    assert classified.output_units == expected
    assert classified.decision == decision


def test_below_minimum_hold_wins_when_configured_maximum_is_lower_than_minimum():
    terms = solana_deposit_policy.SolanaDepositTerms(
        minimum_input_units=1_000,
        maximum_input_units=100,
        input_decimals=6,
        output_decimals=6,
        flat_output_fee_units=0,
        fee_basis_points=0,
    )

    classified = solana_deposit_policy.classify(500, terms)

    assert classified.decision == solana_deposit_policy.HOLD_BELOW_MINIMUM
    assert classified.output_units == 500


@pytest.mark.parametrize("amount", [True, 1.0, "1", 0, -1])
def test_policy_rejects_non_exact_or_nonpositive_input(amount):
    terms = solana_deposit_policy.SolanaDepositTerms(1, 0, 6, 6, 0, 0)
    with pytest.raises(ValueError):
        solana_deposit_policy.classify(amount, terms)


def _set_pair(monkeypatch, *, input_decimals=6, output_decimals=6, flat=100, bps=0):
    pair = replace(
        config.SWAP_PAIR,
        solana=replace(config.SWAP_PAIR.solana, decimals=input_decimals),
        nexus=replace(config.SWAP_PAIR.nexus, decimals=output_decimals),
        fees=replace(config.SWAP_PAIR.fees, flat_to_nexus_units=flat, basis_points=bps),
    )
    monkeypatch.setattr(config, "SWAP_PAIR", pair)
    return pair


@pytest.mark.parametrize(
    ("memo", "amount", "minimum", "expected_decision"),
    [
        ("wrong:recipient", 9, 1_000_000, solana_deposit_policy.HOLD_BELOW_MINIMUM),
        ("nexus:invalid-account", 10, 1_000_000, solana_deposit_policy.HOLD_BELOW_MINIMUM),
        ("wrong:recipient", 100, 100, solana_deposit_policy.HOLD_NONPOSITIVE_OUTPUT),
        ("nexus:invalid-account", 100, 100, solana_deposit_policy.HOLD_NONPOSITIVE_OUTPUT),
    ],
)
def test_low_invalid_memo_or_account_stays_full_principal_hold_through_all_workers(
    monkeypatch, tmp_path, memo, amount, minimum, expected_decision,
):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=minimum) as db_path:
        pair = _set_pair(monkeypatch, flat=100)
        monkeypatch.setattr(
            config, "SWAP_PAIR",
            replace(pair, fees=replace(pair.fees, refund_solana_units=10)),
        )
        state_db.add_unprocessed_sig(
            "invalid-source", 100, memo, "source-account", amount,
            "ready for processing", None,
        )
        validator = Mock(return_value=False)
        nexus_transport = Mock()
        destination_resolver = Mock(return_value="must-not-resolve")
        solana_transport = Mock(return_value=(True, "must-not-send"))
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", validator)
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", nexus_transport)
        monkeypatch.setattr(
            solana_client, "_resolve_solana_token_destination", destination_resolver,
        )
        monkeypatch.setattr(
            solana_client, "send_solana_token_to_account_with_sig", solana_transport,
        )

        first = solana_client.process_unprocessed_solana_deposits(limit=1, timeout=10)
        refunded = solana_client.process_solana_deposits_refunding(limit=1, timeout=10)
        quarantined = solana_client.process_solana_deposits_quarantine(limit=1, timeout=10)

        assert first == [0, 0, 0, 1]
        assert refunded == quarantined == 0
        assert state_db.get_unprocessed_sig_status("invalid-source") == (
            "policy held, non-sendable"
        )
        assert state_db.get_unresolved_solana_liability_units() == amount
        validator.assert_not_called()
        nexus_transport.assert_not_called()
        destination_resolver.assert_not_called()
        solana_transport.assert_not_called()
        with sqlite3.connect(db_path) as conn:
            decision, evidence = conn.execute(
                "SELECT policy_decision, policy_evidence FROM unprocessed_sigs WHERE sig=?",
                ("invalid-source",),
            ).fetchone()
            assert decision == expected_decision
            assert solana_deposit_policy.parse_frozen_evidence(evidence)["input_units"] == amount
            assert conn.execute(
                "SELECT COUNT(*) FROM fee_entries WHERE sig='invalid-source'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM processed_sigs WHERE sig='invalid-source'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM refunded_sigs WHERE sig='invalid-source'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM quarantined_sigs WHERE sig='invalid-source'"
            ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("memo", "validator_calls"),
    [("wrong:recipient", 0), ("nexus:invalid-account", 1)],
)
def test_payable_invalid_memo_or_account_keeps_normal_refund_route(
    monkeypatch, tmp_path, memo, validator_calls,
):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=100) as db_path:
        pair = _set_pair(monkeypatch, flat=0)
        monkeypatch.setattr(
            config, "SWAP_PAIR",
            replace(pair, fees=replace(pair.fees, refund_solana_units=10)),
        )
        state_db.add_unprocessed_sig(
            "invalid-payable", 100, memo, "source-account", 1_000,
            "ready for processing", None,
        )
        validator = Mock(return_value=False)
        nexus_transport = Mock()
        solana_transport = Mock(return_value=(True, "refund-signature"))
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", validator)
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", nexus_transport)
        monkeypatch.setattr(
            solana_client, "_resolve_solana_token_destination",
            Mock(return_value="source-token-account"),
        )
        monkeypatch.setattr(
            solana_client, "send_solana_token_to_account_with_sig", solana_transport,
        )

        first = solana_client.process_unprocessed_solana_deposits(limit=1, timeout=10)
        refunded = solana_client.process_solana_deposits_refunding(limit=1, timeout=10)

        assert first == [0, 1, 0, 0]
        assert refunded == 1
        assert validator.call_count == validator_calls
        nexus_transport.assert_not_called()
        solana_transport.assert_called_once()
        assert solana_transport.call_args.args[1] == 990
        assert state_db.get_unprocessed_sig_status("invalid-payable") == (
            "refund sent, awaiting confirmation"
        )
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT policy_decision FROM unprocessed_sigs WHERE sig='invalid-payable'"
            ).fetchone()[0] == solana_deposit_policy.PAYABLE
            assert conn.execute(
                "SELECT refunded_units, status FROM refunded_sigs "
                "WHERE sig='invalid-payable'"
            ).fetchone() == (990, "awaiting confirmation")


def test_equal_maximum_is_payable_and_above_maximum_keeps_refund_disposition(
    monkeypatch, tmp_path,
):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=1, maximum=1_000) as db_path:
        _set_pair(monkeypatch, flat=0)
        for sig, amount in (("at-max", 1_000), ("above-max", 1_001)):
            state_db.add_unprocessed_sig(
                sig, 100, "nexus:recipient", "source-account", amount,
                "ready for processing", None,
            )
        transport = Mock(return_value=(True, "nexus-tx"))
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _address: True)
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", transport)

        result = solana_client.process_unprocessed_solana_deposits(limit=2, timeout=10)

        assert result == [1, 1, 0, 0]
        transport.assert_called_once()
        assert transport.call_args.args[1] == 1_000
        assert state_db.get_unprocessed_sig_status("at-max") == "debited, awaiting confirmation"
        assert state_db.get_unprocessed_sig_status("above-max") == "to be refunded"
        with sqlite3.connect(db_path) as conn:
            rows = dict(conn.execute(
                "SELECT sig, policy_decision FROM unprocessed_sigs ORDER BY sig"
            ).fetchall())
        assert rows == {
            "above-max": solana_deposit_policy.REFUND_OVERSIZED,
            "at-max": solana_deposit_policy.PAYABLE,
        }


def test_nonpositive_output_is_full_principal_hold_without_fee(monkeypatch, tmp_path):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=100) as db_path:
        _set_pair(monkeypatch, flat=101)
        state_db.add_unprocessed_sig(
            "zero-output", 100, "nexus:recipient", "source-account", 100,
            "ready for processing", None,
        )
        transport = Mock()
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _address: True)
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", transport)

        assert solana_client.process_unprocessed_solana_deposits(1, 10) == [0, 0, 0, 1]

        transport.assert_not_called()
        assert state_db.get_unprocessed_sig_status("zero-output") == "policy held, non-sendable"
        assert state_db.get_unresolved_solana_liability_units() == 100
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT policy_decision FROM unprocessed_sigs WHERE sig='zero-output'"
            ).fetchone()[0] == solana_deposit_policy.HOLD_NONPOSITIVE_OUTPUT
            assert conn.execute(
                "SELECT COUNT(*) FROM fee_entries WHERE sig='zero-output'"
            ).fetchone()[0] == 0


def test_below_minimum_with_unavailable_source_account_still_retains_principal(
    monkeypatch, tmp_path,
):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=101):
        _set_pair(monkeypatch, flat=0)
        state_db.commit_solana_deposit_scan_page(
            vault_account="vault", mint="mint", network="mainnet",
            commitment="finalized", query_identity="nullable-source-query",
            lower_timestamp=1, request_before_signature=None,
            next_before_signature=None, upper_timestamp=200, previous_timestamp=None,
            page_last_timestamp=100, scanned_signature_count=1,
            deposits=[("source-unavailable", 100, "nexus:recipient", None, 100)],
            complete=True,
        )
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _address: True)
        transport = Mock()
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", transport)

        assert solana_client.process_unprocessed_solana_deposits(1, 10) == [0, 0, 0, 1]
        assert state_db.get_unprocessed_sig_status("source-unavailable") == (
            "policy held, non-sendable"
        )
        assert state_db.get_unresolved_solana_liability_units() == 100
        transport.assert_not_called()


def test_payable_decision_survives_restart_and_current_config_drift(monkeypatch, tmp_path):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=100) as db_path:
        _set_pair(monkeypatch, flat=10, bps=100)
        state_db.add_unprocessed_sig(
            "frozen-payable", 100, "nexus:recipient", "source-account", 1_000,
            "ready for processing", None,
        )
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _address: True)
        monkeypatch.setattr(state_db, "reserve_action", lambda *_args, **_kwargs: False)

        assert solana_client.process_unprocessed_solana_deposits(1, 10) == [0, 0, 0, 0]
        with sqlite3.connect(db_path) as conn:
            first_evidence = conn.execute(
                "SELECT policy_evidence FROM unprocessed_sigs WHERE sig='frozen-payable'"
            ).fetchone()[0]

        monkeypatch.setattr(config, "MIN_DEPOSIT_SOLANA_UNITS", 2_000)
        _set_pair(monkeypatch, flat=999, bps=9_999)
        monkeypatch.setattr(state_db, "reserve_action", lambda *_args, **_kwargs: True)
        transport = Mock(return_value=(True, "nexus-tx"))
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", transport)

        assert solana_client.process_unprocessed_solana_deposits(1, 10) == [1, 0, 0, 0]

        transport.assert_called_once()
        assert transport.call_args.args[1] == 980
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT policy_evidence, amount_usdd_units, status FROM unprocessed_sigs "
                "WHERE sig='frozen-payable'"
            ).fetchone()
        assert row == (first_evidence, 980, "debited, awaiting confirmation")


def test_policy_freeze_is_idempotent_and_conflicting_evidence_fails_closed(
    monkeypatch, tmp_path,
):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=100) as db_path:
        _set_pair(monkeypatch, flat=10)
        state_db.add_unprocessed_sig(
            "idempotent", 100, "nexus:recipient", "source-account", 1_000,
            "ready for processing", None,
        )
        decision = solana_deposit_policy.classify(
            1_000, solana_deposit_policy.terms_from_config(config),
        )
        evidence = solana_deposit_policy.freeze_evidence(
            signature="idempotent", timestamp=100, memo="nexus:recipient",
            from_address="source-account", input_units=1_000, decision=decision,
        )

        first = state_db.freeze_solana_deposit_policy_decision("idempotent", evidence)
        second = state_db.freeze_solana_deposit_policy_decision("idempotent", evidence)
        assert first["decision"] == second["decision"] == solana_deposit_policy.PAYABLE

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE unprocessed_sigs SET policy_evidence='{}' WHERE sig='idempotent'"
            )
            conn.commit()
        with pytest.raises(ValueError, match="policy evidence"):
            state_db.freeze_solana_deposit_policy_decision("idempotent", evidence)
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT status, policy_decision, policy_evidence FROM unprocessed_sigs "
                "WHERE sig='idempotent'"
            ).fetchone() == ("ready for processing", solana_deposit_policy.PAYABLE, "{}")


def test_policy_freeze_rolls_back_status_and_evidence_on_database_failure(monkeypatch, tmp_path):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=100) as db_path:
        _set_pair(monkeypatch, flat=0)
        state_db.add_unprocessed_sig(
            "rollback", 100, "nexus:recipient", "source-account", 99,
            "ready for processing", None,
        )
        decision = solana_deposit_policy.classify(
            99, solana_deposit_policy.terms_from_config(config),
        )
        evidence = solana_deposit_policy.freeze_evidence(
            signature="rollback", timestamp=100, memo="nexus:recipient",
            from_address="source-account", input_units=99, decision=decision,
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TRIGGER force_policy_rollback AFTER UPDATE OF policy_decision
                   ON unprocessed_sigs BEGIN SELECT RAISE(ABORT, 'forced rollback'); END"""
            )
            conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced rollback"):
            state_db.freeze_solana_deposit_policy_decision("rollback", evidence)

        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT status, policy_decision, policy_evidence FROM unprocessed_sigs "
                "WHERE sig='rollback'"
            ).fetchone() == ("ready for processing", None, None)


def test_historic_submitted_intent_is_not_reclassified_by_new_minimum(monkeypatch, tmp_path):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=10_000) as db_path:
        state_db.add_unprocessed_sig(
            "already-submitted", 100, "nexus:recipient", "source-account", 1_000,
            "debited, awaiting confirmation", "nexus-tx",
        )
        state_db.set_unprocessed_sig_debit_intent("already-submitted", 77, 900)
        transport = Mock()
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", transport)

        assert solana_client.process_unprocessed_solana_deposits(1, 10) == 0

        transport.assert_not_called()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT status, txid, reference, amount_usdd_units, policy_decision "
                "FROM unprocessed_sigs WHERE sig='already-submitted'"
            ).fetchone() == ("debited, awaiting confirmation", "nexus-tx", 77, 900, None)


@pytest.mark.parametrize("ingestion", ["live_helius", "recovery_core"])
def test_live_and_recovery_ingestion_reach_same_real_worker_hold(
    monkeypatch, tmp_path, ingestion,
):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=1_001) as db_path:
        _set_pair(monkeypatch, flat=1)
        deposit = (f"{ingestion}-sig", 101, "nexus:recipient", "source-account", 1_000)
        if ingestion == "live_helius":
            admitted, held = state_db.commit_helius_deposit_scan_page(
                vault_account="vault", network="mainnet", mint="mint",
                commitment="finalized", lower_timestamp=100, upper_timestamp=200,
                query_identity="live-query", request_pagination_token=None,
                next_pagination_token=None, previous_timestamp=None,
                page_last_timestamp=101, scanned_signatures=[(deposit[0], 101)],
                deposits=[deposit], holds=[], complete=True,
            )
            assert (admitted, held) == (1, 0)
        else:
            admitted = state_db.commit_solana_deposit_scan_page(
                vault_account="vault", mint="mint", network="mainnet",
                commitment="finalized", query_identity="recovery-query",
                lower_timestamp=100, request_before_signature=None,
                next_before_signature=None, upper_timestamp=200,
                previous_timestamp=None, page_last_timestamp=101,
                scanned_signature_count=1, deposits=[deposit], complete=True,
            )
            assert admitted == 1
        transport = Mock()
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _address: True)
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", transport)

        assert solana_client.process_unprocessed_solana_deposits(1, 10) == [0, 0, 0, 1]

        transport.assert_not_called()
        assert state_db.get_unprocessed_sig_status(deposit[0]) == "policy held, non-sendable"
        assert state_db.get_unresolved_solana_liability_units() == 1_000
        with sqlite3.connect(db_path) as conn:
            decision, evidence = conn.execute(
                "SELECT policy_decision, policy_evidence FROM unprocessed_sigs WHERE sig=?",
                (deposit[0],),
            ).fetchone()
        assert decision == solana_deposit_policy.HOLD_BELOW_MINIMUM
        assert solana_deposit_policy.parse_frozen_evidence(evidence)["input_units"] == 1_000


def test_recovery_replay_conflicting_with_retained_policy_source_fails_closed(
    monkeypatch, tmp_path,
):
    with isolated_policy_state(monkeypatch, tmp_path, minimum=1_001) as db_path:
        _set_pair(monkeypatch, flat=1)
        original = ("replayed-sig", 101, "nexus:recipient", "source-account", 1_000)
        state_db.commit_solana_deposit_scan_page(
            vault_account="live-vault", mint="mint", network="mainnet",
            commitment="finalized", query_identity="live-query", lower_timestamp=100,
            request_before_signature=None, next_before_signature=None, upper_timestamp=200,
            previous_timestamp=None, page_last_timestamp=101, scanned_signature_count=1,
            deposits=[original], complete=True,
        )
        monkeypatch.setattr(nexus_client, "is_valid_nexus_token_account", lambda _address: True)
        monkeypatch.setattr(nexus_client, "debit_nexus_token_with_txid", Mock())
        assert solana_client.process_unprocessed_solana_deposits(1, 10) == [0, 0, 0, 1]

        conflicting = (*original[:4], 999)
        with pytest.raises(ValueError, match="conflict"):
            state_db.commit_solana_deposit_scan_page(
                vault_account="recovery-vault", mint="mint", network="mainnet",
                commitment="finalized", query_identity="recovery-query", lower_timestamp=100,
                request_before_signature=None, next_before_signature=None, upper_timestamp=200,
                previous_timestamp=None, page_last_timestamp=101, scanned_signature_count=1,
                deposits=[conflicting], complete=True,
            )

        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT amount_usdc_units, status, policy_decision FROM unprocessed_sigs "
                "WHERE sig='replayed-sig'"
            ).fetchone() == (
                1_000, "policy held, non-sendable",
                solana_deposit_policy.HOLD_BELOW_MINIMUM,
            )
            assert conn.execute(
                "SELECT COUNT(*) FROM solana_deposit_scan_cursor "
                "WHERE vault_account='recovery-vault'"
            ).fetchone()[0] == 0
