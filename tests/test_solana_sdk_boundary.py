"""Offline regression checks for real solana-py/solders request boundaries.

The main safety suite installs process-global SDK stubs.  This file therefore runs the
real-SDK checks in a fresh interpreter and replaces only the Client provider transport;
no RPC endpoint, service, keypair, or production environment is used.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CHILD_ARGUMENT = "--real-sdk-child"
_SKIP_CODE = 77


def _run_real_sdk_checks() -> int:
    try:
        from unittest.mock import patch

        import dotenv
        from solana.rpc.api import Client
        from solders.signature import Signature
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == "solana" or exc.name.startswith("solders")):
            print(f"real Solana SDK unavailable: {exc}")
            return _SKIP_CODE
        raise

    fixture_environment = {
        "SOLANA_RPC_URL": "http://127.0.0.1:1",
        "VAULT_KEYPAIR": "/nonexistent/offline-keypair.json",
        "SOLANA_VAULT_ACCOUNT": "11111111111111111111111111111111",
        "SOLANA_TOKEN_MINT": "11111111111111111111111111111111",
        "SOL_MAIN_ACCOUNT": "11111111111111111111111111111111",
        "NEXUS_TREASURY_ACCOUNT": "OFFLINE-TREASURY",
        "NEXUS_TOKEN_REGISTER_ADDRESS": "OFFLINE-TOKEN",
        "NEXUS_PIN": "offline-fixture-not-a-credential",
        "NEXUS_CLI_PATH": "/bin/false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    with patch.dict(os.environ, fixture_environment, clear=True), patch.object(
        dotenv, "load_dotenv", return_value=False
    ):
        sys.path.insert(0, str(ROOT))
        from src import config, solana_client

    patch.object(
        socket.socket,
        "connect",
        side_effect=AssertionError("network access forbidden in SDK boundary test"),
    ).start()

    signature_a = str(Signature.default())
    signature_b = (
        "3PtGYH77LhhQqTXP4SmDVJ85hmDieWsgXCUbn14v7gYyVYPjZzygUQhTk3bSTYnf"
        "A48vCM1rmWY7zWL3j1EVKmEy"
    )
    assert isinstance(Signature.from_string(signature_a), Signature)
    assert isinstance(Signature.from_string(signature_b), Signature)

    def transaction(signature: str, memo: str | None = None) -> dict:
        instructions = []
        if memo is not None:
            instructions.append(
                {
                    "programId": "Memo111111111111111111111111111111111111111",
                    "data": memo,
                }
            )
        return {
            "slot": 1,
            "blockTime": 100,
            "version": "legacy",
            "meta": {
                "err": None,
                "status": {"Ok": None},
                "fee": 0,
                "preBalances": [1],
                "postBalances": [1],
                "preTokenBalances": [],
                "postTokenBalances": [],
                "logMessages": [],
            },
            "transaction": {
                "signatures": [signature],
                "message": {
                    "accountKeys": [
                        {
                            "pubkey": str(config.SOL_MAIN_ACCOUNT),
                            "signer": True,
                            "writable": True,
                            "source": "transaction",
                        }
                    ],
                    "recentBlockhash": "11111111111111111111111111111111",
                    "instructions": instructions,
                },
            },
        }

    def disposition_transactions() -> tuple[dict, dict]:
        source_token = "Vote111111111111111111111111111111111111111"
        memo = f"swapService:v1:refund:{signature_b}"
        payout = transaction(signature_a, memo)
        payout["transaction"]["message"]["instructions"].insert(0, {
            "program": "spl-token",
            "parsed": {"type": "transferChecked", "info": {
                "source": str(config.VAULT_USDC_ACCOUNT),
                "destination": source_token,
                "mint": str(config.USDC_MINT),
                "tokenAmount": {"amount": "100"},
            }},
        })
        source = transaction(signature_b, "nexus:recipient")
        source["blockTime"] = 90
        source["transaction"]["message"]["accountKeys"] = [
            {"pubkey": str(config.VAULT_USDC_ACCOUNT), "signer": False, "writable": True},
            {"pubkey": source_token, "signer": True, "writable": True},
        ]
        source["transaction"]["message"]["instructions"].insert(0, {
            "program": "spl-token",
            "parsed": {"type": "transferChecked", "info": {
                "source": source_token,
                "destination": str(config.VAULT_USDC_ACCOUNT),
                "mint": str(config.USDC_MINT),
                "tokenAmount": {"amount": "110"},
            }},
        })
        source["meta"]["preTokenBalances"] = [{
            "accountIndex": 0, "mint": str(config.USDC_MINT),
            "uiTokenAmount": {"amount": "1"},
        }]
        source["meta"]["postTokenBalances"] = [{
            "accountIndex": 0, "mint": str(config.USDC_MINT),
            "uiTokenAmount": {"amount": "111"},
        }]
        source["meta"]["innerInstructions"] = []
        return payout, source

    def entry(
        signature: str,
        *,
        block_time: int = 100,
        error: object = None,
    ) -> dict:
        return {
            "signature": signature,
            "blockTime": block_time,
            "confirmationStatus": "finalized",
            "err": error,
            "memo": None,
            "slot": 1,
        }

    class OfflineProvider:
        def __init__(self, signature_pages: list[list[dict]], transactions: dict[str, dict]):
            self.signature_pages = list(signature_pages)
            self.transactions = transactions
            self.requests: list[dict] = []

        def make_request(self, body, _response_type):
            request = __import__("json").loads(body.to_json())
            self.requests.append(request)
            if request["method"] == "getSignaturesForAddress":
                assert self.signature_pages, "unexpected signature page request"
                return {"jsonrpc": "2.0", "id": 1, "result": self.signature_pages.pop(0)}
            if request["method"] == "getTransaction":
                signature = request["params"][0]
                assert signature in self.transactions, f"unexpected transaction lookup: {signature}"
                return {"jsonrpc": "2.0", "id": 1, "result": self.transactions[signature]}
            raise AssertionError(f"unexpected SDK request: {request['method']}")

    def client_with(provider: OfflineProvider) -> Client:
        client = Client("http://127.0.0.1:1")
        client._provider = provider
        return client

    def requests_for(provider: OfflineProvider, method: str) -> list[dict]:
        return [request for request in provider.requests if request["method"] == method]

    # Mandatory startup scan: a non-empty first page must reach getTransaction through
    # the installed SDK request builder rather than being mislabeled as a fetch failure.
    provider = OfflineProvider(
        [[entry(signature_a)]],
        {signature_a: transaction(signature_a)},
    )
    with patch.object(solana_client, "_get_client", return_value=client_with(provider)):
        result = solana_client.scan_memos_since_timestamp(50, max_signatures=10)
    assert result["complete"] is True, result
    assert result["reason"] is None, result
    assert len(requests_for(provider, "getTransaction")) == 1

    # Current disposition recovery performs both transaction reads through the real SDK,
    # including the memo-named source signature and finalized commitment.
    payout_tx, source_tx = disposition_transactions()
    provider = OfflineProvider(
        [[entry(signature_a)]],
        {signature_a: payout_tx, signature_b: source_tx},
    )
    with patch.object(solana_client, "_get_client", return_value=client_with(provider)):
        result = solana_client.scan_memos_since_timestamp(50, max_signatures=10)
    assert result["complete"] is True, result
    disposition = result["solana_dispositions"][("refund", signature_b)]
    assert disposition["source_amount_solana_units"] == 110
    transaction_requests = requests_for(provider, "getTransaction")
    assert len(transaction_requests) == 2
    for request in transaction_requests:
        assert request["params"][1] == {
            "encoding": "jsonParsed",
            "commitment": "finalized",
            "maxSupportedTransactionVersion": 0,
        }

    # The second-page cursor must also pass through the real SDK as Signature, not str.
    first_page = [
        entry(signature_a, error="AccountInUse") for _ in range(999)
    ] + [entry(signature_b, error="AccountInUse")]
    provider = OfflineProvider(
        [first_page, [entry(signature_a, block_time=49)]],
        {},
    )
    with patch.object(solana_client, "_get_client", return_value=client_with(provider)):
        result = solana_client.scan_memos_since_timestamp(50, max_signatures=1001)
    assert result["complete"] is True, result
    signature_requests = requests_for(provider, "getSignaturesForAddress")
    assert len(signature_requests) == 2
    assert signature_requests[1]["params"][1]["before"] == signature_b

    # Malformed transaction signatures and pagination cursors fail closed with distinct,
    # explicit reasons.  Cursor failure also clears evidence accumulated on that page.
    provider = OfflineProvider([[entry("malformed-signature")]], {})
    with patch.object(solana_client, "_get_client", return_value=client_with(provider)):
        result = solana_client.scan_memos_since_timestamp(50, max_signatures=10)
    assert result["complete"] is False
    assert result["reason"] == "invalid_signature", result

    refund_memo = "refundSig:deposit-signature"
    cursor_page = [entry(signature_a)] + [
        entry(signature_a, error="AccountInUse") for _ in range(998)
    ] + [entry("malformed-cursor", error="AccountInUse")]
    provider = OfflineProvider(
        [cursor_page],
        {signature_a: transaction(signature_a, refund_memo)},
    )
    with patch.object(solana_client, "_get_client", return_value=client_with(provider)):
        result = solana_client.scan_memos_since_timestamp(50, max_signatures=1001)
    assert result["complete"] is False
    assert result["reason"] == "invalid_pagination_cursor", result
    assert result["refund_sigs"] == {}, result

    # Generic and bounded-recent memo lookups must build real getTransaction requests too.
    provider = OfflineProvider(
        [[entry(signature_a)]],
        {signature_a: transaction(signature_a, refund_memo)},
    )
    with patch.object(config, "VAULT_OWNER", None, create=True), patch.object(
        solana_client, "_get_client", return_value=client_with(provider)
    ):
        found = solana_client.find_signature_with_memo(refund_memo)
    assert found == signature_a
    assert len(requests_for(provider, "getTransaction")) == 1

    provider = OfflineProvider(
        [[entry(signature_a)]],
        {signature_a: transaction(signature_a, refund_memo)},
    )
    with patch.object(solana_client, "_get_client", return_value=client_with(provider)):
        recent = solana_client.scan_recent_memos()
    assert recent["refund_sigs"] == {"deposit-signature": signature_a}, recent
    assert len(requests_for(provider, "getTransaction")) == 1

    # The authoritative core scanner builds a typed getTransaction request too.
    provider = OfflineProvider(
        [[entry(signature_a)]],
        {signature_a: transaction(signature_a)},
    )
    with patch.object(solana_client, "_get_client", return_value=client_with(provider)), patch.object(
        solana_client.state_db, "get_solana_deposit_scan_cursor", return_value=None,
    ):
        try:
            solana_client._scan_incoming_deposits_core(
                str(config.VAULT_USDC_ACCOUNT), since_ts=50, page_size=2,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing exact vault evidence must hold the deposit scan")
    assert len(requests_for(provider, "getTransaction")) == 1

    print("real installed Solana SDK signature-boundary checks: PASS")
    return 0


def test_real_installed_sdk_signature_boundaries_are_offline_and_typed():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), _CHILD_ARGUMENT],
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode == _SKIP_CODE:
        pytest.skip(result.stdout.strip() or "real Solana SDK unavailable")
    assert result.returncode == 0, (
        f"real-SDK child failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
    assert "signature-boundary checks: PASS" in result.stdout


if __name__ == "__main__" and _CHILD_ARGUMENT in sys.argv:
    raise SystemExit(_run_real_sdk_checks())
