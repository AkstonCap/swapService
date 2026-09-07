"""Local DB-backed Nexus lifecycle regressions; all chain boundaries are mocked.

Reuse the existing suite's SDK-free fixture modules, not live credentials or nodes.
"""
import json
import pytest
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from unittest.mock import patch

from test_critical_safety import config, nexus_client, solana_client, state_db, swap_nexus
from src.nexus_memo import NexusPayoutEvidence


NEXUS_TXID = "ab" * 64
PAYOUT_SIGNATURE = "1" * 64


@contextmanager
def isolated_state(tmp_path):
    pair = replace(config.SWAP_PAIR,
                   nexus=replace(config.SWAP_PAIR.nexus, treasury_account="TREASURY"),
                   fees=replace(config.SWAP_PAIR.fees, flat_to_solana_units=100, basis_points=0))
    with ExitStack() as stack:
        stack.enter_context(patch.object(state_db, "DB_PATH", str(tmp_path / "state.db")))
        stack.enter_context(patch.object(config, "SWAP_PAIR", pair))
        stack.enter_context(patch.object(config, "DUST_CREDIT_NEXUS_UNITS", 1))
        stack.enter_context(patch.object(config, "MIN_CREDIT_NEXUS_UNITS", 50))
        stack.enter_context(patch.object(nexus_client, "get_account_info", return_value={"owner": "owner"}))
        stack.enter_context(patch.object(nexus_client, "_run", side_effect=AssertionError("unexpected Nexus transport")))
        state_db.init_db()
        yield


def credit_page(amount="0.000025"):
    return [{"txid": "fee-credit", "timestamp": 1000, "confirmations": 2,
             "contracts": [{"id": 0, "OP": "CREDIT", "from": "sender", "to": "TREASURY", "amount": amount}]}]


@pytest.mark.parametrize("amount, units", [("0.000025", 25), ("0.000075", 75)])
def test_fee_admission_rolls_back_journal_when_terminal_write_fails(tmp_path, amount, units):
    with isolated_state(tmp_path):
        with sqlite3.connect(state_db.DB_PATH) as conn:
            conn.execute("CREATE TRIGGER reject_terminal BEFORE INSERT ON processed_txids "
                         "BEGIN SELECT RAISE(ABORT, 'injected terminal failure'); END")
        with patch.object(nexus_client, "_run", return_value=(0, json.dumps(credit_page(amount)), "")):
            swap_nexus.poll_nexus_deposits()
        assert state_db.get_fee_entries() == []
        assert not state_db.is_processed_txid("fee-credit", 0)
        with sqlite3.connect(state_db.DB_PATH) as conn:
            conn.execute("DROP TRIGGER reject_terminal")
        with patch.object(nexus_client, "_run", return_value=(0, json.dumps(credit_page(amount)), "")):
            swap_nexus.poll_nexus_deposits()
        assert state_db.is_processed_txid("fee-credit", 0)
        assert [row[5] for row in state_db.get_fee_entries()] == [units]


def test_rejected_fee_finalization_does_not_advance_checkpoint(tmp_path):
    with isolated_state(tmp_path):
        with patch.object(nexus_client, "_run", return_value=(0, json.dumps(credit_page("0.000075")), "")), \
                patch.object(state_db, "finalize_nexus_credit", return_value=False), \
                patch.object(state_db, "propose_nexus_waterline") as checkpoint:
            swap_nexus.poll_nexus_deposits()
        checkpoint.assert_not_called()


def queue_ready(txid="payout-credit", contract_id=0):
    state_db.add_unprocessed_txid(
        txid=txid, contract_id=contract_id, timestamp=2_000_000_000,
        amount_usdd=0.001, amount_usdd_units=1000, from_address="sender",
        to_address="TREASURY", owner_from_address="owner", confirmations_credit=2,
        status=swap_nexus.NEXUS_STATUS_READY, receival_account="receiver",
    )


def test_fee_only_queued_credit_is_atomic_with_queue_removal(tmp_path):
    with isolated_state(tmp_path):
        queue_ready()
        pair = replace(config.SWAP_PAIR, fees=replace(config.SWAP_PAIR.fees, flat_to_solana_units=2000))
        with sqlite3.connect(state_db.DB_PATH) as conn:
            conn.execute("CREATE TRIGGER reject_terminal BEFORE INSERT ON processed_txids "
                         "BEGIN SELECT RAISE(ABORT, 'injected terminal failure'); END")
        with patch.object(config, "SWAP_PAIR", pair):
            swap_nexus.process_unprocessed_txids()
        assert state_db.get_fee_entries() == []
        assert state_db.is_unprocessed_txid("payout-credit", 0)
        with sqlite3.connect(state_db.DB_PATH) as conn:
            conn.execute("DROP TRIGGER reject_terminal")
        with patch.object(config, "SWAP_PAIR", pair):
            swap_nexus.process_unprocessed_txids()
        assert state_db.is_processed_txid("payout-credit", 0)
        assert not state_db.is_unprocessed_txid("payout-credit", 0)
        assert [row[5] for row in state_db.get_fee_entries()] == [1000]


def test_operator_hold_is_not_reopened_by_receival_lookup(tmp_path):
    with isolated_state(tmp_path):
        state_db.add_unprocessed_txid(
            txid="held-credit", contract_id=0, timestamp=2_000_000_000,
            amount_usdd=0.001, amount_usdd_units=1000, from_address="sender",
            to_address="TREASURY", owner_from_address="owner", confirmations_credit=2,
            status=swap_nexus.NEXUS_STATUS_REFUND_HOLD,
        )
        with patch.object(nexus_client, "find_asset_receival_account_by_txid_and_owner",
                          return_value=nexus_client.AssetLookup(
                              {"receival_account": "receiver", "owner": "owner"}, True)) as lookup, patch.object(
            solana_client, "is_valid_solana_token_account", return_value=True
        ):
            swap_nexus.process_unprocessed_txids(paused=True)
        lookup.assert_not_called()
        assert state_db.get_unprocessed_txids_as_dicts()[0]["comment"] == swap_nexus.NEXUS_STATUS_REFUND_HOLD


def test_legacy_quarantine_helper_cannot_move_or_archive_funds(tmp_path):
    with isolated_state(tmp_path):
        queue_ready()
        row = state_db.get_unprocessed_txids_as_dicts()[0]
        with patch.object(nexus_client, "quarantine_nexus_token", return_value=True) as move:
            swap_nexus._quarantine_txid(row, "test containment")
        move.assert_not_called()
        with sqlite3.connect(state_db.DB_PATH) as conn:
            assert conn.execute("SELECT COUNT(*) FROM quarantined_txids").fetchone()[0] == 0
        assert state_db.get_unprocessed_txids_as_dicts()[0]["comment"] == swap_nexus.NEXUS_STATUS_REFUND_HOLD


def test_failed_liquidity_read_cannot_submit_payout(tmp_path):
    with isolated_state(tmp_path):
        queue_ready()
        with patch.object(solana_client, "get_token_account_balance", side_effect=RuntimeError("RPC down")), \
                patch.object(solana_client, "send_solana_token_to_account_with_sig", return_value=(True, "sig")) as send, \
                patch.object(solana_client, "get_signatures_confirmation", return_value={}):
            swap_nexus.process_unprocessed_txids()
        send.assert_not_called()
        assert state_db.get_unprocessed_txids_as_dicts()[0]["comment"] == swap_nexus.NEXUS_STATUS_READY


def test_trade_balance_lookup_updates_only_selected_contract(tmp_path):
    with isolated_state(tmp_path):
        queue_ready(contract_id=0)
        queue_ready(contract_id=1)
        state_db.update_unprocessed_txid(txid="payout-credit", contract_id=0,
                                        status=swap_nexus.NEXUS_STATUS_TRADE_BAL_CHECK)
        state_db.update_unprocessed_txid(txid="payout-credit", contract_id=1,
                                        status=swap_nexus.NEXUS_STATUS_REFUND_HOLD)
        with patch.object(nexus_client, "find_asset_receival_account_by_txid_and_owner",
                          return_value=nexus_client.AssetLookup(
                              {"receival_account": "receiver", "owner": "owner"}, True)), patch.object(
            solana_client, "is_valid_solana_token_account", return_value=True
        ):
            swap_nexus.process_unprocessed_txids(paused=True)
        statuses = {row["contract_id"]: row["comment"] for row in state_db.get_unprocessed_txids_as_dicts()}
        assert statuses == {0: swap_nexus.NEXUS_STATUS_READY, 1: swap_nexus.NEXUS_STATUS_REFUND_HOLD}


def test_payout_does_not_book_fee_before_confirmation(tmp_path):
    with isolated_state(tmp_path):
        queue_ready()
        with patch.object(solana_client, "get_token_account_balance", return_value=1_000_000), patch.object(
            solana_client, "send_solana_token_to_account_with_sig", return_value=(True, "payout-signature")
        ) as send, patch.object(
            solana_client, "get_nexus_payout_evidence", return_value=None
        ) as evidence_lookup:
            swap_nexus.process_unprocessed_txids()
        send.assert_called_once_with("receiver", 1000 - 100, "nexus_txid:payout-credit:0")
        assert state_db.get_fee_entries() == []
        row = state_db.get_unprocessed_txids_as_dicts()[0]
        assert row["comment"] == swap_nexus.NEXUS_STATUS_AWAITING
        assert row["sig"] == "payout-signature"
        assert row["payout_fee_nexus_units"] == 100
        assert row["payout_solana_units"] == 1000 - 100
        evidence_lookup.assert_called_once_with("payout-signature", "payout-credit", 0)


def test_memo_recovery_cannot_finalize_an_unconfirmed_payout(tmp_path):
    with isolated_state(tmp_path):
        queue_ready()
        assert state_db.prepare_nexus_payout(
            txid="payout-credit", contract_id=0, receival_account="receiver", amount_usdd_units=1000,
            payout_solana_units=1000 - 100, payout_fee_nexus_units=100,
        )
        state_db.update_unprocessed_txid(txid="payout-credit", contract_id=0,
                                        status=swap_nexus.NEXUS_STATUS_AWAITING)
        with patch.object(
            solana_client, "find_nexus_payout_evidence", return_value=None
        ) as evidence_lookup:
            swap_nexus.process_unprocessed_txids(paused=True)
        assert not state_db.is_processed_txid("payout-credit", 0)
        assert state_db.is_unprocessed_txid("payout-credit", 0)
        assert state_db.get_fee_entries() == []
        evidence_lookup.assert_called_once_with("nexus_txid:payout-credit:0")


def test_confirmed_payout_uses_frozen_fee_and_finalizes_atomically(tmp_path):
    with isolated_state(tmp_path):
        queue_ready(txid=NEXUS_TXID)
        queue_ready(txid=NEXUS_TXID, contract_id=1)
        assert state_db.prepare_nexus_payout(
            txid=NEXUS_TXID, contract_id=0, receival_account="receiver", amount_usdd_units=1000,
            payout_solana_units=1000 - 100, payout_fee_nexus_units=100,
        )
        state_db.update_unprocessed_txid(txid=NEXUS_TXID, contract_id=0,
                                        status=swap_nexus.NEXUS_STATUS_AWAITING, sig=PAYOUT_SIGNATURE)
        with sqlite3.connect(state_db.DB_PATH) as conn:
            conn.execute("CREATE TRIGGER reject_removal BEFORE DELETE ON unprocessed_txids "
                         "BEGIN SELECT RAISE(ABORT, 'injected removal failure'); END")
        changed_policy = replace(config.SWAP_PAIR, fees=replace(config.SWAP_PAIR.fees, flat_to_solana_units=999))
        evidence = NexusPayoutEvidence(
            txid=NEXUS_TXID, contract_id=0, solana_signature=PAYOUT_SIGNATURE,
            to_token_account="receiver", amount_solana_units=900,
        )
        with patch.object(config, "SWAP_PAIR", changed_policy), patch.object(
            solana_client, "get_nexus_payout_evidence", return_value=evidence
        ) as evidence_lookup:
            swap_nexus.process_unprocessed_txids(paused=True)
            assert not state_db.is_processed_txid(NEXUS_TXID, 0)
            assert state_db.get_fee_entries() == []
            assert state_db.is_unprocessed_txid(NEXUS_TXID, 0)
            with sqlite3.connect(state_db.DB_PATH) as conn:
                conn.execute("DROP TRIGGER reject_removal")
            swap_nexus.process_unprocessed_txids(paused=True)
            swap_nexus.process_unprocessed_txids(paused=True)
        assert state_db.is_processed_txid(NEXUS_TXID, 0)
        assert not state_db.is_unprocessed_txid(NEXUS_TXID, 0)
        assert state_db.is_unprocessed_txid(NEXUS_TXID, 1)
        assert [row[5] for row in state_db.get_fee_entries()] == [100]
        assert evidence_lookup.call_count == 2


def test_confirmed_legacy_payout_without_frozen_terms_remains_a_liability(tmp_path):
    with isolated_state(tmp_path):
        queue_ready()
        state_db.update_unprocessed_txid(txid="payout-credit", contract_id=0,
                                        status=swap_nexus.NEXUS_STATUS_AWAITING, sig="legacy-signature")
        evidence = NexusPayoutEvidence(
            txid="payout-credit", contract_id=0, solana_signature="legacy-signature",
            to_token_account="receiver", amount_solana_units=900,
        )
        with patch.object(
            solana_client, "get_nexus_payout_evidence", return_value=evidence
        ) as evidence_lookup:
            swap_nexus.process_unprocessed_txids(paused=True)
        assert not state_db.is_processed_txid("payout-credit", 0)
        assert state_db.is_unprocessed_txid("payout-credit", 0)
        assert state_db.get_fee_entries() == []
        evidence_lookup.assert_called_once_with("legacy-signature", "payout-credit", 0)
