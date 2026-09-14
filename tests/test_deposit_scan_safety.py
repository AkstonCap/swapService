"""Offline tests of the live Solana scan -> queue -> waterline boundary."""
from __future__ import annotations

import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("SOLANA_RPC_URL", "http://127.0.0.1:1")
os.environ.setdefault("VAULT_KEYPAIR", "/nonexistent/offline-keypair.json")
os.environ.setdefault("VAULT_USDC_ACCOUNT", "11111111111111111111111111111111")
os.environ.setdefault("USDC_MINT", "11111111111111111111111111111111")
os.environ.setdefault("SOL_MAIN_ACCOUNT", "11111111111111111111111111111111")
os.environ.setdefault("NEXUS_PIN", "offline-fixture")
os.environ.setdefault("NEXUS_USDD_TREASURY_ACCOUNT", "OFFLINE-TREASURY")
os.environ.setdefault("NEXUS_TOKEN_REGISTER_ADDRESS", "OFFLINE-TOKEN")
os.environ.setdefault("NEXUS_CLI_PATH", "/bin/false")

from src import config, nexus_client, solana_client, state_db, swap_solana  # noqa: E402


def signature(n=1):
    return str(solana_client.Signature.from_bytes(bytes([n]) * 64))


def entry(n=1, timestamp=200):
    return {"signature": signature(n), "blockTime": timestamp,
            "err": None, "confirmationStatus": "finalized"}


def transaction(n=1):
    def balance(amount):
        return {"accountIndex": 0, "mint": str(config.USDC_MINT),
                "owner": str(config.SOL_MAIN_ACCOUNT),
                "uiTokenAmount": {"amount": str(amount)}}
    return {"blockTime": 200, "meta": {"err": None,
            "preTokenBalances": [balance(0)], "postTokenBalances": [balance(2000000)]},
            "transaction": {"signatures": [signature(n)], "message": {
                "accountKeys": [{"pubkey": str(config.VAULT_USDC_ACCOUNT)},
                                {"pubkey": "source-token-account"}],
                "instructions": [
                    {"program": "spl-token", "parsed": {"type": "transferChecked", "info": {
                        "source": "source-token-account",
                        "destination": str(config.VAULT_USDC_ACCOUNT),
                        "mint": str(config.USDC_MINT),
                        "tokenAmount": {"amount": "2000000"},
                    }}},
                    {"program": "spl-memo", "parsed": "nexus:recipient"},
                ]}}}


def full_transaction(n=1, timestamp=200, *, amount=2000000):
    """Helius `transactionDetails=full` omits the signatures-mode status field."""
    tx = transaction(n)
    tx["blockTime"] = timestamp
    tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = str(amount)
    tx["transaction"]["message"]["instructions"][0]["parsed"]["info"]["tokenAmount"]["amount"] = str(amount)
    return tx


def no_deposit_transaction(n=1, timestamp=200):
    tx = full_transaction(n, timestamp, amount=0)
    tx["transaction"]["message"]["instructions"] = []
    return tx


@pytest.fixture
def core(monkeypatch):
    client = SimpleNamespace(get_signatures_for_address=Mock(), get_transaction=Mock())
    monkeypatch.setattr(solana_client, "_get_client", lambda: client)
    # Exercise the scanner's SDK arguments; replace only outbound RPC execution.
    def rpc(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(solana_client, "_rpc_call", rpc)
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: None)
    client.get_signatures_for_address.return_value = {"result": [entry()]}
    client.get_transaction.return_value = {"result": transaction()}
    return client


@pytest.fixture(autouse=True)
def durable_scan_db(monkeypatch, tmp_path):
    """Use a fresh append-only cursor journal for each paginated-scan case."""
    monkeypatch.setattr(state_db, "DB_PATH", str(tmp_path / "state.db"))
    state_db.init_db()
    return tmp_path


def scan_core(limit=10):
    solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=limit,
    )
    return [tuple(row[:5]) for row in state_db.get_unprocessed_sigs()]


@pytest.mark.parametrize("response", [None, {}, {"result": None}, {"result": {}},
                                      {"error": {"code": -32000}}])
def test_missing_or_malformed_signature_page_is_not_an_empty_scan(core, response):
    core.get_signatures_for_address.return_value = response
    with pytest.raises(RuntimeError):
        scan_core()


def test_rpc_failure_is_not_an_empty_successful_scan(core):
    core.get_signatures_for_address.side_effect = TimeoutError("offline transport failure")
    with pytest.raises(RuntimeError):
        scan_core()


@pytest.mark.parametrize("bad_tx", [None, {}, {"meta": None}, {"meta": {}},
                                      {"meta": {"err": None}}])
def test_unreadable_transaction_holds_entire_scan(core, bad_tx):
    core.get_transaction.return_value = {"result": bad_tx}
    with pytest.raises(RuntimeError):
        scan_core()


def test_failure_after_one_valid_deposit_never_returns_partial_success(core):
    core.get_signatures_for_address.return_value = {"result": [entry(1), entry(2)]}
    core.get_transaction.side_effect = [{"result": transaction()}, TimeoutError("second read failed")]
    with pytest.raises(RuntimeError):
        scan_core()


def test_full_core_page_without_waterline_is_incomplete(core):
    core.get_signatures_for_address.return_value = {"result": [entry(1), entry(2)]}
    with pytest.raises(RuntimeError):
        scan_core(limit=1)


def test_core_valid_empty_page_and_complete_deposit_are_supported(core):
    assert scan_core() == [(signature(), 200, "nexus:recipient", "source-token-account", 2000000)]
    state_db.remove_unprocessed_sig(signature())
    core.get_signatures_for_address.return_value = {"result": []}
    assert scan_core() == []


def test_durable_cursor_resumes_saturated_history_without_advancing_waterline(core, durable_scan_db):
    """Each finalized page is queued before its exact `before` cursor is committed."""
    core.get_signatures_for_address.side_effect = [
        {"result": [entry(1, 300), entry(2, 299)]},
        {"result": [entry(3, 298), entry(4, 99)]},
    ]
    core.get_transaction.side_effect = [
        {"result": transaction(1)}, {"result": transaction(2)}, {"result": transaction(3)},
    ]

    first = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=2,
    )
    assert not first.complete
    assert first.next_before_signature == signature(2)
    cursor = state_db.get_solana_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT))
    assert cursor is not None
    assert cursor["before_signature"] == signature(2)
    assert cursor["upper_timestamp"] == 300

    second = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=2,
    )
    assert second.complete
    assert second.upper_timestamp == 300
    assert str(core.get_signatures_for_address.call_args_list[1].kwargs["before"]) == signature(2)
    assert state_db.get_solana_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT)) is None
    assert [row[0] for row in state_db.get_unprocessed_sigs()] == [signature(3), signature(2), signature(1)]
    with __import__("sqlite3").connect(state_db.DB_PATH) as conn:
        assert conn.execute(
            "SELECT event, request_before_signature, next_before_signature, admitted_count "
            "FROM solana_deposit_scan_events ORDER BY id"
        ).fetchall() == [
            ("page_committed", None, signature(2), 2),
            ("range_completed", signature(2), None, 1),
        ]


def test_core_cursor_rejects_cross_page_timestamp_reordering(core, durable_scan_db):
    core.get_signatures_for_address.side_effect = [
        {"result": [entry(1, 300), entry(2, 299)]},
        {"result": [entry(3, 301), entry(4, 99)]},
    ]
    core.get_transaction.side_effect = [
        {"result": transaction(1)}, {"result": transaction(2)},
    ]
    first = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=2,
    )
    assert not first.complete
    first_cursor = state_db.get_solana_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT))
    assert first_cursor is not None
    assert first_cursor["previous_timestamp"] == 299

    with pytest.raises(RuntimeError, match="incomplete"):
        solana_client.scan_incoming_deposits_with_durable_cursor(
            str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=2,
        )

    cursor = state_db.get_solana_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT))
    assert cursor is not None
    assert cursor["before_signature"] == signature(2)
    assert cursor["previous_timestamp"] == 299


def test_core_uses_configured_vault_index_not_first_same_owner_balance(core):
    tx = transaction()
    same_owner = {"accountIndex": 1, "mint": str(config.USDC_MINT),
                  "owner": str(config.SOL_MAIN_ACCOUNT),
                  "uiTokenAmount": {"amount": "9999999"}}
    tx["meta"]["preTokenBalances"].append({**same_owner, "uiTokenAmount": {"amount": "3"}})
    tx["meta"]["postTokenBalances"].append(same_owner)
    core.get_transaction.return_value = {"result": tx}
    assert scan_core() == [(signature(), 200, "nexus:recipient", "source-token-account", 2000000)]


@pytest.mark.parametrize("mutation", ["wrong_delta", "wrong_mint", "multiple_incoming", "multiple_memos"])
def test_core_requires_one_exact_classic_transfer_and_unambiguous_memo(core, mutation):
    tx = transaction()
    transfer = tx["transaction"]["message"]["instructions"][0]["parsed"]["info"]
    if mutation == "wrong_delta":
        transfer["tokenAmount"]["amount"] = "1999999"
    elif mutation == "wrong_mint":
        transfer["mint"] = "different-mint"
    elif mutation == "multiple_incoming":
        tx["transaction"]["message"]["instructions"].insert(
            1, {"program": "spl-token", "parsed": {"type": "transferChecked", "info": {
                **transfer, "source": "another-source", "tokenAmount": {"amount": "1"},
            }}}
        )
    else:
        tx["transaction"]["message"]["instructions"].append(
            {"program": "spl-memo", "parsed": "nexus:another-recipient"}
        )
    core.get_transaction.return_value = {"result": tx}
    if mutation == "wrong_mint":
        with pytest.raises(RuntimeError):
            scan_core()
        assert state_db.get_solana_deposit_holds() == []
    else:
        assert scan_core() == []
        assert state_db.get_solana_deposit_holds()[0]["signature"] == signature()


@pytest.mark.parametrize("where", ["signature", "transaction"])
def test_core_failed_transaction_cannot_create_a_deposit(core, where):
    if where == "signature":
        row = entry()
        row["err"] = {"InstructionError": [1, "Custom"]}
        core.get_signatures_for_address.return_value = {"result": [row]}
    else:
        tx = transaction()
        tx["meta"]["err"] = {"InstructionError": [1, "Custom"]}
        core.get_transaction.return_value = {"result": tx}
    if where == "signature":
        assert scan_core() == []
    else:
        with pytest.raises(RuntimeError):
            scan_core()


@pytest.mark.parametrize("bad_field", ["signature", "blockTime", "err", "confirmationStatus"])
def test_core_incomplete_signature_identity_or_outcome_holds(core, bad_field):
    row = entry()
    del row[bad_field]
    core.get_signatures_for_address.return_value = {"result": [row]}
    with pytest.raises(RuntimeError):
        scan_core()


def test_helius_full_page_requests_json_parsed_evidence_and_pagination(monkeypatch):
    rpc = Mock(return_value={"data": [full_transaction()], "paginationToken": "next"})
    monkeypatch.setattr(solana_client, "_helius_rpc_call", rpc)

    rows, continuation = solana_client._helius_get_full_deposit_page(
        "vault", limit=20, pagination_token="previous", commitment="finalized",
        lower_timestamp=100, upper_timestamp=400,
    )

    assert rows == [full_transaction()]
    assert continuation == "next"
    assert rpc.call_args.args == ("getTransactionsForAddress", ["vault", {
        "limit": 20,
        "commitment": "finalized",
        "transactionDetails": "full",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
        "sortOrder": "desc",
        "filters": {
            "blockTime": {"gte": 100, "lte": 400},
            "status": "succeeded",
        },
        "paginationToken": "previous",
    }])


def test_core_scan_through_real_installed_sdk_request_builders(monkeypatch):
    import json
    from solana.rpc.api import Client
    client = Client("http://127.0.0.1:1")
    requests = []
    def transport(body, parser):
        request = json.loads(body.to_json())
        requests.append(request)
        if request["method"] == "getSignaturesForAddress":
            return {"result": [entry()]}
        assert request["method"] == "getTransaction"
        return {"result": transaction()}
    monkeypatch.setattr(client._provider, "make_request", transport)
    monkeypatch.setattr(solana_client, "_get_client", lambda: client)
    # Only the provider is replaced: production _rpc_call and solders encoders run.
    assert scan_core()[0][0] == signature()
    assert requests[0]["params"][1]["commitment"] == "finalized"
    assert requests[1]["params"] == [signature(), {
        "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}]


@pytest.mark.parametrize("mode", ["rpc_failure", "out_of_order"])
def test_real_poller_holds_waterline_and_queue_on_incomplete_scan(core, durable_scan_db, mode):
    if mode == "rpc_failure":
        core.get_signatures_for_address.side_effect = TimeoutError("offline RPC failure")
    else:
        core.get_signatures_for_address.return_value = {"result": [entry(1, 90), entry(2, 200)]}
    with ExitStack() as stack:
        stack.enter_context(patch.object(solana_client, "_helius_rpc_url", return_value=None))
        stack.enter_context(patch.object(nexus_client, "get_heartbeat_asset", return_value={"present": True}))
        stack.enter_context(patch.object(nexus_client, "parse_heartbeat_waterlines",
                                        return_value=SimpleNamespace(solana=100)))
        heartbeat = stack.enter_context(patch.object(nexus_client, "update_heartbeat_asset"))
        enqueue = stack.enter_context(patch.object(state_db, "add_unprocessed_sig"))
        stack.enter_context(patch.object(solana_client, "process_unprocessed_solana_deposits", return_value=[0]*4))
        for method in ("process_solana_deposits_refunding", "process_solana_deposits_quarantine",
                       "check_sig_confirmations", "check_quarantine_confirmations"):
            stack.enter_context(patch.object(solana_client, method, return_value=0))
        stack.enter_context(patch.object(nexus_client, "publish_service_record"))
        stack.enter_context(patch.object(solana_client, "get_token_account_balance", return_value=0))
        stack.enter_context(patch.object(state_db, "save_last_vault_balance"))
        stack.enter_context(patch.object(nexus_client, "check_unconfirmed_debits", return_value=0))
        stack.enter_context(patch.object(nexus_client, "resolve_unverified_debits", return_value=0))
        stack.enter_context(patch.object(solana_client, "check_timestamp_unpr_sigs", return_value=None))
        swap_solana.poll_solana_deposits()
    enqueue.assert_not_called()
    heartbeat.assert_called_once()
    assert heartbeat.call_args.args[1:] == (None, None)
