"""Regressions for payout proof and mutable live-pagination review gaps.

All chain boundaries are deterministic mocks; these tests never contact a live node.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest

from test_critical_safety import config, nexus_client, solana_client, state_db, swap_nexus
from src.nexus_memo import NexusPayoutEvidence


NEXUS_TXID = "ab" * 64
PAYOUT_SIGNATURE = "1" * 64


@contextmanager
def isolated_state(tmp_path):
    pair = replace(
        config.SWAP_PAIR,
        nexus=replace(config.SWAP_PAIR.nexus, treasury_account="TREASURY"),
        fees=replace(config.SWAP_PAIR.fees, flat_to_solana_units=100, basis_points=0),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(state_db, "DB_PATH", str(tmp_path / "state.db")))
        stack.enter_context(patch.object(config, "SWAP_PAIR", pair))
        stack.enter_context(patch.object(config, "DUST_CREDIT_NEXUS_UNITS", 1))
        stack.enter_context(patch.object(config, "MIN_CREDIT_NEXUS_UNITS", 50))
        stack.enter_context(patch.object(nexus_client, "get_account_info", return_value={"owner": "owner"}))
        stack.enter_context(patch.object(nexus_client, "_run", side_effect=AssertionError("unexpected Nexus transport")))
        state_db.init_db()
        yield


class _Keypair:
    def pubkey(self):
        return "OWNER"


def test_successful_submit_helper_does_not_archive_nexus_source(tmp_path):
    with isolated_state(tmp_path), patch.object(
        solana_client, "_is_token_account_for_mint", return_value=True
    ), patch.object(
        solana_client, "load_vault_keypair", return_value=_Keypair()
    ), patch.object(
        solana_client, "transfer_checked", return_value="transfer-instruction"
    ), patch.object(
        solana_client, "_memo_ix", return_value="memo-instruction"
    ), patch.object(
        solana_client, "_build_and_send_legacy_tx", return_value=PAYOUT_SIGNATURE
    ):
        result = solana_client.send_solana_token_to_account_with_sig(
            "receiver", 900, f"nexus_txid:{NEXUS_TXID}:7"
        )
        with sqlite3.connect(state_db.DB_PATH) as conn:
            processed = conn.execute("SELECT * FROM processed_txids").fetchall()

    assert result == (True, PAYOUT_SIGNATURE)
    assert processed == []


class _MemoRpcClient:
    def get_signatures_for_address(self, *args, **kwargs):
        raise AssertionError("patched through _rpc_call")

    def get_transaction(self, *args, **kwargs):
        raise AssertionError("patched through _rpc_call")


def payout_transaction(
    *,
    memo=f"nexus_txid:{NEXUS_TXID}:7",
    destination="receiver",
    amount=900,
    authority="OWNER",
    source="VAULT",
    mint="MINT",
    transaction_signature=PAYOUT_SIGNATURE,
):
    return {
        "transaction": {
            "signatures": [transaction_signature],
            "message": {
                "accountKeys": [{"pubkey": authority, "signer": True, "writable": True}],
                "instructions": [
                    {
                        "program": "spl-token",
                        "parsed": {
                            "type": "transferChecked",
                            "info": {
                                "source": source,
                                "destination": destination,
                                "mint": mint,
                                "tokenAmount": {"amount": str(amount)},
                            },
                        },
                    },
                    {
                        "programId": "Memo111111111111111111111111111111111111111",
                        "data": memo,
                    },
                ],
            }
        },
        "meta": {"err": None, "logMessages": []},
    }


def test_direct_signature_lookup_returns_exact_nexus_payout_evidence():
    with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
        solana_client, "_rpc_call", return_value=payout_transaction()
    ):
        evidence = solana_client.get_nexus_payout_evidence(
            PAYOUT_SIGNATURE, NEXUS_TXID, 7
        )

    assert evidence == NexusPayoutEvidence(
        txid=NEXUS_TXID,
        contract_id=7,
        solana_signature=PAYOUT_SIGNATURE,
        to_token_account="receiver",
        amount_solana_units=900,
    )


def queue_frozen_payout(*, contract_id=7, signature: str | None = PAYOUT_SIGNATURE):
    state_db.add_unprocessed_txid(
        txid=NEXUS_TXID,
        contract_id=contract_id,
        timestamp=2_000_000_000,
        amount_usdd=0.001,
        amount_usdd_units=1000,
        from_address="sender",
        to_address="TREASURY",
        owner_from_address="owner",
        confirmations_credit=2,
        status=swap_nexus.NEXUS_STATUS_READY,
        receival_account="receiver",
    )
    assert state_db.prepare_nexus_payout(
        txid=NEXUS_TXID,
        contract_id=contract_id,
        receival_account="receiver",
        amount_usdd_units=1000,
        payout_solana_units=900,
        payout_fee_nexus_units=100,
    )
    if signature is not None:
        state_db.update_unprocessed_txid(
            txid=NEXUS_TXID,
            contract_id=contract_id,
            status=swap_nexus.NEXUS_STATUS_AWAITING,
            sig=signature,
        )


def test_exact_direct_payout_proof_finalizes_source_once(tmp_path):
    with isolated_state(tmp_path):
        queue_frozen_payout()
        state_db.add_unprocessed_txid(
            txid=NEXUS_TXID,
            contract_id=8,
            timestamp=2_000_000_000,
            amount_usdd=0.001,
            amount_usdd_units=1000,
            from_address="sibling-sender",
            to_address="TREASURY",
            owner_from_address="sibling-owner",
            confirmations_credit=2,
            status=swap_nexus.NEXUS_STATUS_REFUND_HOLD,
        )
        with patch.object(
            solana_client, "_get_client", return_value=_MemoRpcClient()
        ), patch.object(
            solana_client, "_rpc_call", return_value=payout_transaction()
        ) as rpc, patch.object(
            solana_client,
            "get_signatures_confirmation",
            side_effect=AssertionError("status alone must not authorize finalization"),
        ):
            swap_nexus.process_unprocessed_txids(paused=True)
            swap_nexus.process_unprocessed_txids(paused=True)

        assert state_db.is_processed_txid(NEXUS_TXID, 7)
        assert not state_db.is_unprocessed_txid(NEXUS_TXID, 7)
        assert state_db.is_unprocessed_txid(NEXUS_TXID, 8)
        assert [entry[5] for entry in state_db.get_fee_entries()] == [100]
        assert rpc.call_count == 1


def test_exact_crash_recovered_proof_is_compared_before_signature_adoption(tmp_path):
    evidence = NexusPayoutEvidence(
        txid=NEXUS_TXID,
        contract_id=7,
        solana_signature=PAYOUT_SIGNATURE,
        to_token_account="receiver",
        amount_solana_units=900,
    )
    with isolated_state(tmp_path):
        queue_frozen_payout(signature=None)
        with patch.object(
            solana_client, "find_nexus_payout_evidence", return_value=evidence
        ) as lookup, patch.object(
            solana_client,
            "find_signature_with_memo",
            side_effect=AssertionError("money owner must consume full evidence"),
        ), patch.object(
            solana_client,
            "get_signatures_confirmation",
            side_effect=AssertionError("status alone must not authorize finalization"),
        ):
            swap_nexus.process_unprocessed_txids(paused=True)
            swap_nexus.process_unprocessed_txids(paused=True)

        assert state_db.is_processed_txid(NEXUS_TXID, 7)
        assert not state_db.is_unprocessed_txid(NEXUS_TXID, 7)
        assert [entry[5] for entry in state_db.get_fee_entries()] == [100]
        lookup.assert_called_once_with(f"nexus_txid:{NEXUS_TXID}:7")


INVALID_PAYOUT_TRANSACTIONS = [
    ("wrong output", payout_transaction(amount=899)),
    ("wrong destination", payout_transaction(destination="other-receiver")),
    ("wrong memo", payout_transaction(memo=f"nexus_txid:{'cd' * 64}:7")),
    ("wrong authority", payout_transaction(authority="OTHER-OWNER")),
    ("wrong source", payout_transaction(source="OTHER-VAULT")),
    ("wrong mint", payout_transaction(mint="OTHER-MINT")),
    ("unrelated signature", payout_transaction(transaction_signature="2" * 64)),
    ("missing success field", {**payout_transaction(), "meta": {}}),
]


@pytest.mark.parametrize("case, transaction", INVALID_PAYOUT_TRANSACTIONS)
def test_inexact_or_unattributable_direct_proof_never_archives(
    tmp_path, case, transaction
):
    del case
    with isolated_state(tmp_path):
        queue_frozen_payout()
        with patch.object(
            solana_client, "_get_client", return_value=_MemoRpcClient()
        ), patch.object(
            solana_client, "_rpc_call", return_value=transaction
        ), patch.object(
            solana_client,
            "get_signatures_confirmation",
            return_value={PAYOUT_SIGNATURE: True},
        ) as status_lookup:
            swap_nexus.process_unprocessed_txids(paused=True)

        assert not state_db.is_processed_txid(NEXUS_TXID, 7)
        row = state_db.get_unprocessed_txids_as_dicts()[0]
        assert row["comment"] == swap_nexus.NEXUS_STATUS_AWAITING
        assert row["sig"] == PAYOUT_SIGNATURE
        assert state_db.get_fee_entries() == []
        status_lookup.assert_not_called()


def test_recovered_mismatch_keeps_liability_and_never_resubmits(tmp_path):
    wrong_evidence = NexusPayoutEvidence(
        txid=NEXUS_TXID,
        contract_id=7,
        solana_signature=PAYOUT_SIGNATURE,
        to_token_account="wrong-receiver",
        amount_solana_units=900,
    )
    with isolated_state(tmp_path):
        queue_frozen_payout(signature=None)
        with patch.object(
            solana_client, "find_nexus_payout_evidence", return_value=wrong_evidence
        ), patch.object(
            solana_client, "send_solana_token_to_account_with_sig"
        ) as submit:
            swap_nexus.process_unprocessed_txids()

        submit.assert_not_called()
        assert not state_db.is_processed_txid(NEXUS_TXID, 7)
        row = state_db.get_unprocessed_txids_as_dicts()[0]
        assert row["comment"] == swap_nexus.NEXUS_STATUS_SENDING
        assert row["sig"] is None
        assert state_db.get_fee_entries() == []


def test_duplicate_same_source_payouts_are_ambiguous():
    other_signature = "2" * 64
    entries = [
        {"signature": PAYOUT_SIGNATURE, "confirmationStatus": "finalized", "err": None},
        {"signature": other_signature, "confirmationStatus": "finalized", "err": None},
    ]
    with patch.object(config, "VAULT_OWNER", None, create=True), patch.object(
        solana_client, "_get_client", return_value=_MemoRpcClient()
    ), patch.object(
        solana_client,
        "_rpc_call",
        side_effect=[
            entries,
            payout_transaction(),
            payout_transaction(transaction_signature=other_signature),
        ],
    ):
        evidence = solana_client.find_nexus_payout_evidence(
            f"nexus_txid:{NEXUS_TXID}:7"
        )

    assert evidence is None


def _history_tx(txid, timestamp, contracts):
    return {
        "txid": txid,
        "timestamp": timestamp,
        "confirmations": 2,
        "contracts": contracts,
    }


def _treasury_credit(txid, timestamp):
    return _history_tx(
        txid,
        timestamp,
        [{
            "id": 0,
            "OP": "CREDIT",
            "from": "sender",
            "to": "TREASURY",
            "amount": "0.001000",
        }],
    )


def test_live_multipage_offset_scan_preserves_credit_but_holds_checkpoint(tmp_path):
    first_page = [_treasury_credit("multipage-credit", 1_000)] + [
        _history_tx(f"debit-{index}", 999 - index, [{"id": 0, "OP": "DEBIT"}])
        for index in range(99)
    ]
    second_page = [_history_tx("older-debit", 800, [{"id": 0, "OP": "DEBIT"}])]
    with isolated_state(tmp_path), patch.object(
        nexus_client, "get_heartbeat_asset", return_value=None
    ), patch.object(
        nexus_client,
        "_run",
        side_effect=[
            (0, json.dumps(first_page), ""),
            (0, json.dumps(second_page), ""),
        ],
    ) as run, patch.object(
        state_db, "propose_nexus_waterline"
    ) as checkpoint:
        swap_nexus.poll_nexus_deposits()

        assert state_db.is_unprocessed_txid("multipage-credit", 0)
        assert run.call_count == 2
        checkpoint.assert_not_called()


def test_live_single_page_valid_scan_can_propose_checkpoint(tmp_path):
    with isolated_state(tmp_path), patch.object(
        nexus_client, "get_heartbeat_asset", return_value=None
    ), patch.object(
        config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0
    ), patch.object(
        nexus_client,
        "_run",
        return_value=(0, json.dumps([_treasury_credit("singlepage-credit", 1_000)]), ""),
    ), patch.object(
        state_db, "propose_nexus_waterline"
    ) as checkpoint:
        swap_nexus.poll_nexus_deposits()

        assert state_db.is_unprocessed_txid("singlepage-credit", 0)
        checkpoint.assert_called_once_with(1_000)


def test_memo_recovery_lookup_returns_full_evidence_not_a_signature():
    entry = {
        "signature": PAYOUT_SIGNATURE,
        "confirmationStatus": "finalized",
        "err": None,
    }
    with patch.object(config, "VAULT_OWNER", None, create=True), patch.object(
        solana_client, "_get_client", return_value=_MemoRpcClient()
    ), patch.object(
        solana_client, "_rpc_call", side_effect=[[entry], payout_transaction()]
    ):
        evidence = solana_client.find_nexus_payout_evidence(
            f"nexus_txid:{NEXUS_TXID}:7"
        )

    assert evidence == NexusPayoutEvidence(
        txid=NEXUS_TXID,
        contract_id=7,
        solana_signature=PAYOUT_SIGNATURE,
        to_token_account="receiver",
        amount_solana_units=900,
    )
