"""Regressions for trusted Helius live deposit admission and durable holds."""
from __future__ import annotations

import json
import os
import sqlite3
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

from src import config, fees, nexus_client, solana_client, state_db, swap_solana  # noqa: E402


def signature(n: int) -> str:
    return str(solana_client.Signature.from_bytes(int(n).to_bytes(64, "little")))


def _balance(amount: int, *, index: int = 0) -> dict:
    return {
        "accountIndex": index,
        "mint": str(config.USDC_MINT),
        "owner": str(config.SOL_MAIN_ACCOUNT),
        "uiTokenAmount": {"amount": str(amount)},
    }


def full_transaction(n: int, timestamp: int, *, amount: int = 2_000_000) -> dict:
    return {
        "blockTime": timestamp,
        "meta": {
            "err": None,
            "preTokenBalances": [_balance(0)],
            "postTokenBalances": [_balance(amount)],
        },
        "transaction": {
            "signatures": [signature(n)],
            "message": {
                "accountKeys": [
                    {"pubkey": str(config.VAULT_USDC_ACCOUNT)},
                    {"pubkey": f"source-token-{n}"},
                ],
                "instructions": [
                    {
                        "program": "spl-token",
                        "parsed": {
                            "type": "transferChecked",
                            "info": {
                                "source": f"source-token-{n}",
                                "destination": str(config.VAULT_USDC_ACCOUNT),
                                "mint": str(config.USDC_MINT),
                                "tokenAmount": {"amount": str(amount)},
                            },
                        },
                    },
                    {"program": "spl-memo", "parsed": f"nexus:recipient-{n}"},
                ],
            },
        },
    }


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(state_db, "DB_PATH", str(tmp_path / "state.db"))
    state_db.init_db()
    return tmp_path / "state.db"


def _queue_rows() -> list[tuple]:
    with sqlite3.connect(state_db.DB_PATH) as conn:
        return conn.execute(
            "SELECT sig, timestamp, memo, from_address, amount_usdc_units "
            "FROM unprocessed_sigs ORDER BY timestamp"
        ).fetchall()


def _seed_hold(
    n: int,
    *,
    timestamp: int,
    evidence: dict,
    reason: str,
    amount_units: int | None,
    network: str | None = "mainnet",
    vault_account: str | None = None,
    mint: str | None = None,
    observed_commitment: str | None = "confirmed",
    finality_required: int | None = 1,
) -> None:
    now = 10_000
    with sqlite3.connect(state_db.DB_PATH) as conn:
        conn.execute(
            """INSERT INTO solana_deposit_holds
               (signature, block_timestamp, memo, from_address, amount_units, reason,
                evidence_json, provider, query_identity, first_seen_timestamp,
                updated_timestamp, network, vault_account, mint, observed_commitment,
                finality_required)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'helius', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signature(n), timestamp, f"nexus:recipient-{n}", f"source-token-{n}",
                amount_units, reason,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                f"query-{n}", now, now, network,
                vault_account if vault_account is not None else str(config.VAULT_USDC_ACCOUNT),
                mint if mint is not None else str(config.USDC_MINT),
                observed_commitment, finality_required,
            ),
        )


def test_solana_rpc_helius_url_is_a_valid_helius_only_configuration(monkeypatch):
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.setattr(config, "RPC_URL", "https://mainnet.helius-rpc.com/?api-key=offline")

    assert solana_client._helius_rpc_url() == config.RPC_URL


def test_explicit_solana_rpc_url_precedes_helius_key_shorthand(monkeypatch):
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "offline-key")
    monkeypatch.setattr(config, "RPC_URL", "https://devnet.helius-rpc.com/?api-key=explicit")
    monkeypatch.setattr(config, "SOLANA_NETWORK", "devnet")

    assert solana_client._helius_rpc_url() == config.RPC_URL


def test_helius_key_shorthand_uses_configured_network(monkeypatch):
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "offline-key")
    monkeypatch.setattr(config, "RPC_URL", "http://127.0.0.1:8899")
    monkeypatch.setattr(config, "SOLANA_NETWORK", "devnet")

    assert solana_client._helius_rpc_url() == (
        "https://devnet.helius-rpc.com/?api-key=offline-key"
    )


@pytest.mark.parametrize(
    ("url", "configured"),
    [
        ("https://mainnet.helius-rpc.com/?api-key=x", "devnet"),
        ("https://devnet.helius-rpc.com/?api-key=x", "mainnet"),
        ("https://rpc.helius.xyz/?api-key=x", "devnet"),
    ],
)
def test_official_helius_hostname_cannot_contradict_network(monkeypatch, url, configured):
    monkeypatch.setattr(config, "SOLANA_NETWORK", configured)
    with pytest.raises(ValueError, match="conflicts"):
        solana_client._network_identity_for_url(url)


def test_helius_key_infers_official_core_network_instead_of_defaulting_mainnet(monkeypatch):
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "offline-key")
    monkeypatch.setattr(config, "RPC_URL", "https://api.devnet.solana.com")
    monkeypatch.setattr(config, "SOLANA_NETWORK", "")
    assert solana_client._helius_rpc_url().startswith("https://devnet.helius-rpc.com/")


def test_helius_key_without_identifiable_network_fails_closed(monkeypatch):
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "offline-key")
    monkeypatch.setattr(config, "RPC_URL", "https://private-rpc.example.invalid")
    monkeypatch.setattr(config, "SOLANA_NETWORK", "")
    with pytest.raises(ValueError, match="network"):
        solana_client._helius_rpc_url()


def test_explicit_helius_cannot_conflict_with_official_core_network(monkeypatch):
    monkeypatch.setenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(config, "RPC_URL", "https://api.devnet.solana.com")
    monkeypatch.setattr(config, "SOLANA_NETWORK", "")
    with pytest.raises(ValueError, match="network"):
        solana_client._helius_rpc_url()


def test_hold_liability_read_has_one_snapshot_during_concurrent_promotion(db, monkeypatch):
    _seed_hold(911, timestamp=200, evidence=full_transaction(911, 200),
               reason="awaiting_finalized", amount_units=2_000_000)
    connect = sqlite3.connect
    connections = 0
    promoted = False

    class InterleavedRead:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            nonlocal promoted
            cursor = self.conn.execute(sql, *args)
            if "SUM(amount_usdc_units)" in sql and "unprocessed_sigs" in sql:
                result = cursor.fetchone()
                cursor.close()
                promoted = state_db.promote_solana_deposit_hold(signature(911))
                return SimpleNamespace(fetchone=lambda: result)
            return cursor

        def close(self):
            self.conn.close()

    def interleaved_connect(*args, **kwargs):
        nonlocal connections
        connections += 1
        conn = connect(*args, **kwargs)
        return InterleavedRead(conn) if connections == 1 else conn

    monkeypatch.setattr(sqlite3, "connect", interleaved_connect)
    assert state_db.get_unresolved_solana_liability_units() == 2_000_000
    assert promoted
    assert state_db.get_unresolved_solana_liability_units() == 2_000_000


@pytest.mark.parametrize("endpoint_case", ["conflicting", "missing"])
def test_finalized_hold_replay_requires_valid_available_provider(db, monkeypatch, endpoint_case):
    monkeypatch.setattr(config, "SOLANA_NETWORK", "mainnet")
    monkeypatch.setattr(config, "HELIUS_RPC_URL", "", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "")
    monkeypatch.setenv("HELIUS_RPC_URL", (
        "https://devnet.helius-rpc.com/?api-key=offline"
        if endpoint_case == "conflicting" else ""
    ))
    monkeypatch.setattr(config, "RPC_URL", (
        "https://api.devnet.solana.com"
        if endpoint_case == "conflicting" else "https://api.mainnet-beta.solana.com"
    ))
    _seed_hold(
        91, timestamp=300, evidence=full_transaction(91, 300, amount=3_000_000), amount_units=3_000_000,
        reason="unsupported_vault_balance_change", network="mainnet",
        observed_commitment="finalized", finality_required=0,
    )
    with patch.object(solana_client, "_get_strictly_finalized_signatures",
                      side_effect=AssertionError("already finalized: no fresh status required")):
        assert solana_client.replay_solana_deposit_holds() == 0
        assert _queue_rows() == []
        assert len(state_db.get_solana_deposit_holds()) == 1
        assert state_db.get_unresolved_solana_liability_units() == 3_000_000
        monkeypatch.setattr(config, "RPC_URL", "https://api.mainnet-beta.solana.com")
        monkeypatch.setenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=offline")
        assert solana_client.replay_solana_deposit_holds() == 1
    assert [row[0] for row in _queue_rows()] == [signature(91)]
    assert state_db.get_solana_deposit_holds() == []
    assert state_db.get_unresolved_solana_liability_units() == 3_000_000


def test_helius_pages_persist_query_token_and_deposits_atomically(db, monkeypatch):
    provider = Mock(side_effect=[
        ([full_transaction(1, 300), full_transaction(2, 299)], "300:2"),
        ([full_transaction(3, 298)], None),
    ])
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(solana_client, "_helius_get_full_deposit_page", provider)
    monkeypatch.setattr(solana_client, "_get_client", Mock(side_effect=AssertionError("core RPC used")))
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized")

    first = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100,
        page_size=2, upper_timestamp=400,
    )

    assert not first.complete
    assert first.provider == "helius"
    assert first.next_pagination_token == "300:2"
    cursor = state_db.get_helius_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT))
    assert cursor["pagination_token"] == "300:2"
    assert cursor["lower_timestamp"] == 100
    assert cursor["upper_timestamp"] == 400
    assert cursor["commitment"] == "finalized"
    assert cursor["query_identity"]
    assert [row[0] for row in _queue_rows()] == [signature(2), signature(1)]

    second = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100,
        page_size=2, upper_timestamp=999,
    )

    assert second.complete
    assert second.upper_timestamp == 400
    assert state_db.get_helius_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT)) is None
    assert [row[0] for row in _queue_rows()] == [signature(3), signature(2), signature(1)]
    assert provider.call_args_list[0].kwargs["lower_timestamp"] == 100
    assert provider.call_args_list[0].kwargs["upper_timestamp"] == 400
    assert provider.call_args_list[1].kwargs["pagination_token"] == "300:2"


def test_helius_request_has_fixed_bounded_filters(monkeypatch):
    rpc = Mock(return_value={"data": [], "paginationToken": None})
    monkeypatch.setattr(solana_client, "_helius_rpc_call", rpc)

    assert solana_client._helius_get_full_deposit_page(
        "vault", limit=20, pagination_token=None, commitment="confirmed",
        lower_timestamp=100, upper_timestamp=400,
    ) == ([], None)
    options = rpc.call_args.args[1][1]
    assert options == {
        "limit": 20,
        "commitment": "confirmed",
        "transactionDetails": "full",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
        "sortOrder": "desc",
        "filters": {
            "blockTime": {"gte": 100, "lte": 400},
            "status": "succeeded",
        },
    }


def test_restart_reuses_helius_token_and_never_mixes_core_cursor(db, monkeypatch):
    provider = Mock(side_effect=[
        ([full_transaction(1, 300)], "300:1"),
        RuntimeError("temporary Helius outage"),
    ])
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(solana_client, "_helius_get_full_deposit_page", provider)
    core = Mock(side_effect=AssertionError("core fallback mixed a Helius cursor"))
    monkeypatch.setattr(solana_client, "_scan_incoming_deposits_core", core)

    first = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100,
        page_size=1, upper_timestamp=400,
    )
    assert not first.complete

    with pytest.raises(RuntimeError, match="Helius"):
        solana_client.scan_incoming_deposits_with_durable_cursor(
            str(config.VAULT_USDC_ACCOUNT), since_ts=100,
            page_size=1, upper_timestamp=999,
        )
    core.assert_not_called()
    assert provider.call_args_list[1].kwargs["pagination_token"] == "300:1"
    assert state_db.get_helius_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT))["pagination_token"] == "300:1"
    assert [row[0] for row in _queue_rows()] == [signature(1)]


def test_confirmed_large_helius_deposit_is_durably_held_then_promoted(db, monkeypatch):
    tx = full_transaction(4, 300, amount=2_000_000)
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(
        solana_client, "_helius_get_full_deposit_page",
        Mock(return_value=([tx], None)),
    )
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "confirmed")
    monkeypatch.setattr(config, "SOLANA_FINALIZED_ABOVE_UNITS", 1_000_000)
    finalized = Mock(return_value=set())
    monkeypatch.setattr(solana_client, "_get_strictly_finalized_signatures", finalized)

    progress = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100,
        page_size=10, upper_timestamp=400,
    )

    assert progress.complete
    assert _queue_rows() == []
    holds = state_db.get_solana_deposit_holds()
    assert [(row["signature"], row["reason"], row["amount_units"]) for row in holds] == [
        (signature(4), "awaiting_finalized", 2_000_000)
    ]

    finalized.return_value = {signature(4)}
    assert solana_client.replay_solana_deposit_holds() == 1
    assert state_db.get_solana_deposit_holds() == []
    assert [row[0] for row in _queue_rows()] == [signature(4)]


def test_confirmed_large_core_deposit_uses_same_durable_finality_gate(db, monkeypatch):
    client = SimpleNamespace(
        get_signatures_for_address=Mock(return_value={"result": [{
            "signature": signature(5), "blockTime": 300, "err": None,
            "confirmationStatus": "confirmed",
        }]}),
        get_transaction=Mock(return_value={"result": full_transaction(5, 300)}),
    )
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: None)
    monkeypatch.setattr(solana_client, "_get_client", lambda: client)
    monkeypatch.setattr(solana_client, "_rpc_call", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "confirmed")
    monkeypatch.setattr(config, "SOLANA_FINALIZED_ABOVE_UNITS", 1_000_000)

    progress = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100,
        page_size=10, upper_timestamp=400,
    )

    assert progress.complete
    assert _queue_rows() == []
    assert state_db.get_solana_deposit_holds()[0]["reason"] == "awaiting_finalized"


def test_classic_spl_transfer_is_admitted_from_exact_balance_metadata():
    tx = full_transaction(6, 300)
    info = tx["transaction"]["message"]["instructions"][0]["parsed"]["info"]
    amount = info.pop("tokenAmount")["amount"]
    info.pop("mint")
    info["amount"] = amount
    tx["transaction"]["message"]["instructions"][0]["parsed"]["type"] = "transfer"

    assert solana_client._extract_core_deposit_evidence(
        tx, signature=signature(6), vault_account=str(config.VAULT_USDC_ACCOUNT),
        mint=str(config.USDC_MINT),
    ) == ("nexus:recipient-6", "source-token-6", 2_000_000)


def test_vault_account_creation_with_transfer_is_admitted_from_exact_metadata():
    tx = full_transaction(7, 300)
    tx["meta"]["preTokenBalances"] = []
    tx["transaction"]["message"]["instructions"].insert(0, {
        "program": "spl-associated-token-account",
        "parsed": {"type": "create", "info": {
            "account": str(config.VAULT_USDC_ACCOUNT),
            "mint": str(config.USDC_MINT),
            "wallet": str(config.SOL_MAIN_ACCOUNT),
        }},
    })

    assert solana_client._extract_core_deposit_evidence(
        tx, signature=signature(7), vault_account=str(config.VAULT_USDC_ACCOUNT),
        mint=str(config.USDC_MINT),
    ) == ("nexus:recipient-7", "source-token-7", 2_000_000)


def test_ambiguous_multi_source_is_held_without_starving_unrelated_deposit(db, monkeypatch):
    ambiguous = full_transaction(8, 300, amount=3_000_000)
    first_info = ambiguous["transaction"]["message"]["instructions"][0]["parsed"]["info"]
    first_info["tokenAmount"]["amount"] = "2000000"
    ambiguous["transaction"]["message"]["instructions"].insert(1, {
        "program": "spl-token",
        "parsed": {"type": "transfer", "info": {
            "source": "another-source",
            "destination": str(config.VAULT_USDC_ACCOUNT),
            "amount": "1000000",
        }},
    })
    ordinary = full_transaction(9, 299)
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(
        solana_client, "_helius_get_full_deposit_page",
        Mock(return_value=([ambiguous, ordinary], None)),
    )
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized")

    progress = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100,
        page_size=10, upper_timestamp=400,
    )

    assert progress.complete
    assert [row[0] for row in _queue_rows()] == [signature(9)]
    holds = state_db.get_solana_deposit_holds()
    assert len(holds) == 1
    assert holds[0]["signature"] == signature(8)
    assert holds[0]["reason"] == "ambiguous_incoming_sources"
    assert json.loads(holds[0]["evidence_json"])["transaction"]["signatures"] == [signature(8)]


def test_malformed_helius_enumeration_never_advances_cursor_or_queues(db, monkeypatch):
    malformed = full_transaction(10, 300)
    del malformed["meta"]["postTokenBalances"]
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(
        solana_client, "_helius_get_full_deposit_page",
        Mock(return_value=([malformed], "next")),
    )

    with pytest.raises(RuntimeError, match="Helius"):
        solana_client.scan_incoming_deposits_with_durable_cursor(
            str(config.VAULT_USDC_ACCOUNT), since_ts=100,
            page_size=10, upper_timestamp=400,
        )

    assert _queue_rows() == []
    assert state_db.get_helius_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT)) is None
    assert state_db.get_solana_deposit_holds() == []


def test_real_poller_uses_helius_only_rpc_for_live_admission(db, monkeypatch):
    provider = Mock(return_value=([full_transaction(11, 300)], None))
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.setattr(config, "RPC_URL", "https://mainnet.helius-rpc.com/?api-key=offline")
    monkeypatch.setattr(config, "SOLANA_NETWORK", "")
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized")
    monkeypatch.setattr(solana_client, "_helius_get_full_deposit_page", provider)
    monkeypatch.setattr(solana_client, "_get_client", Mock(side_effect=AssertionError("core used")))

    with ExitStack() as stack:
        stack.enter_context(patch.object(nexus_client, "get_heartbeat_asset", return_value={"present": True}))
        stack.enter_context(patch.object(
            nexus_client, "parse_heartbeat_waterlines",
            return_value=SimpleNamespace(solana=100),
        ))
        heartbeat = stack.enter_context(patch.object(nexus_client, "update_heartbeat_asset"))
        stack.enter_context(patch.object(
            solana_client, "process_unprocessed_solana_deposits", return_value=[0] * 4,
        ))
        for method in (
            "process_solana_deposits_refunding", "process_solana_deposits_quarantine",
            "check_sig_confirmations", "check_quarantine_confirmations",
        ):
            stack.enter_context(patch.object(solana_client, method, return_value=0))
        stack.enter_context(patch.object(nexus_client, "resolve_unverified_debits", return_value=0))
        stack.enter_context(patch.object(nexus_client, "check_unconfirmed_debits", return_value=0))
        stack.enter_context(patch.object(nexus_client, "publish_service_record"))
        stack.enter_context(patch.object(solana_client, "get_token_account_balance", return_value=0))
        stack.enter_context(patch.object(state_db, "save_last_vault_balance"))
        stack.enter_context(patch.object(solana_client, "check_timestamp_unpr_sigs", return_value=None))
        swap_solana.poll_solana_deposits()

    assert [row[0] for row in _queue_rows()] == [signature(11)]
    assert provider.call_count == 1
    assert provider.call_args.kwargs["lower_timestamp"] == 100
    assert provider.call_args.kwargs["upper_timestamp"] > 300
    assert heartbeat.call_args.args[1] is None
    assert heartbeat.call_args.args[2] is not None


def test_positive_deposit_below_processing_minimum_still_enters_lifecycle(db, monkeypatch):
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(
        solana_client, "_helius_get_full_deposit_page",
        Mock(return_value=([full_transaction(12, 300, amount=7)], None)),
    )
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized")

    progress = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100,
        page_size=10, upper_timestamp=400,
    )

    assert progress.complete
    assert _queue_rows()[0][4] == 7


def test_multiple_classic_transfers_from_same_source_are_aggregated_exactly():
    tx = full_transaction(13, 300, amount=3_000_000)
    first = tx["transaction"]["message"]["instructions"][0]["parsed"]
    first["type"] = "transfer"
    first["info"] = {
        "source": "source-token-13",
        "destination": str(config.VAULT_USDC_ACCOUNT),
        "amount": "2000000",
    }
    tx["transaction"]["message"]["instructions"].insert(1, {
        "program": "spl-token",
        "parsed": {"type": "transfer", "info": {
            "source": "source-token-13",
            "destination": str(config.VAULT_USDC_ACCOUNT),
            "amount": "1000000",
        }},
    })

    assert solana_client._extract_core_deposit_evidence(
        tx, signature=signature(13), vault_account=str(config.VAULT_USDC_ACCOUNT),
        mint=str(config.USDC_MINT),
    ) == ("nexus:recipient-13", "source-token-13", 3_000_000)


def test_incomplete_finality_status_enumeration_holds_page(db, monkeypatch):
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(
        solana_client, "_helius_get_full_deposit_page",
        Mock(return_value=([full_transaction(14, 300)], None)),
    )
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "confirmed")
    monkeypatch.setattr(config, "SOLANA_FINALIZED_ABOVE_UNITS", 1)
    monkeypatch.setattr(solana_client, "_helius_rpc_call", Mock(return_value={"value": []}))

    with pytest.raises(RuntimeError, match="Helius"):
        solana_client.scan_incoming_deposits_with_durable_cursor(
            str(config.VAULT_USDC_ACCOUNT), since_ts=100,
            page_size=10, upper_timestamp=400,
        )

    assert _queue_rows() == []
    assert state_db.get_solana_deposit_holds() == []
    assert state_db.get_helius_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT)) is None


def test_unsupported_hold_freezes_principal_and_ingestion_provenance(db, monkeypatch):
    ambiguous = full_transaction(15, 300, amount=3_000_000)
    first = ambiguous["transaction"]["message"]["instructions"][0]["parsed"]["info"]
    first["tokenAmount"]["amount"] = "2000000"
    ambiguous["transaction"]["message"]["instructions"].insert(1, {
        "program": "spl-token",
        "parsed": {"type": "transfer", "info": {
            "source": "another-source",
            "destination": str(config.VAULT_USDC_ACCOUNT),
            "amount": "1000000",
        }},
    })
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(
        solana_client, "_helius_get_full_deposit_page", Mock(return_value=([ambiguous], None)),
    )
    monkeypatch.setattr(config, "SOLANA_NETWORK", "mainnet")
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "confirmed")

    progress = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=10, upper_timestamp=400,
    )
    hold = state_db.get_solana_deposit_holds()[0]

    assert progress.complete
    assert hold["amount_units"] == 3_000_000
    assert hold["network"] == "mainnet"
    assert hold["vault_account"] == str(config.VAULT_USDC_ACCOUNT)
    assert hold["mint"] == str(config.USDC_MINT)
    assert hold["observed_commitment"] == "confirmed"
    assert hold["finality_required"] == 1
    assert state_db.get_unresolved_solana_liability_units() == 3_000_000


def test_all_positive_holds_are_liabilities_without_double_count_after_promotion(db):
    _seed_hold(
        16, timestamp=300, evidence=full_transaction(16, 300, amount=7),
        reason="awaiting_finalized", amount_units=7,
    )
    state_db.add_unprocessed_sig(
        signature(17), 301, "nexus:recipient-17", "source-token-17", 11,
        "ready for processing", None,
    )
    assert state_db.get_unresolved_solana_liability_units() == 18

    assert state_db.promote_solana_deposit_hold(
        signature(16), memo="nexus:recipient-16", from_address="source-token-16",
        amount_units=7,
    )
    assert state_db.get_unresolved_solana_liability_units() == 18


def test_unknown_legacy_hold_amount_makes_liability_accounting_unhealthy(db):
    _seed_hold(
        18, timestamp=300, evidence=full_transaction(18, 300),
        reason="unsupported_vault_balance_change", amount_units=None,
        network=None, observed_commitment=None, finality_required=None,
    )
    with pytest.raises(RuntimeError, match="unquantified"):
        state_db.get_unresolved_solana_liability_units()
    with pytest.raises(RuntimeError, match="unquantified"):
        fees.available_backing_surplus_solana_units(10_000, 0)


def test_confirmed_hold_requires_fresh_finality_after_config_flips_to_finalized(db, monkeypatch):
    monkeypatch.setenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=offline")
    _seed_hold(
        19, timestamp=300, evidence=full_transaction(19, 300, amount=1),
        reason="awaiting_finalized", amount_units=1,
        observed_commitment="confirmed", finality_required=1,
    )
    monkeypatch.setattr(config, "SOLANA_NETWORK", "mainnet")
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized")
    statuses = Mock(return_value=set())
    monkeypatch.setattr(solana_client, "_get_strictly_finalized_signatures", statuses)

    assert solana_client.replay_solana_deposit_holds() == 0
    statuses.assert_called_once_with([signature(19)], provider="helius")
    assert not state_db.is_unprocessed_sig(signature(19))


def test_provider_network_mismatch_keeps_hold_without_finality_lookup(db, monkeypatch):
    _seed_hold(
        20, timestamp=300, evidence=full_transaction(20, 300, amount=1),
        reason="awaiting_finalized", amount_units=1, network="mainnet",
    )
    monkeypatch.setattr(config, "SOLANA_NETWORK", "devnet")
    monkeypatch.setattr(
        solana_client, "_helius_rpc_url", lambda: "https://devnet.helius-rpc.com/",
    )
    statuses = Mock(side_effect=AssertionError("wrong-network finality queried"))
    monkeypatch.setattr(solana_client, "_get_strictly_finalized_signatures", statuses)

    assert solana_client.replay_solana_deposit_holds() == 0
    statuses.assert_not_called()
    assert state_db.get_solana_deposit_holds()[0]["signature"] == signature(20)


def test_finality_statuses_are_batched_and_validated_before_any_promotion(db, monkeypatch):
    monkeypatch.setenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=offline")
    for n in range(21, 278):
        _seed_hold(
            n, timestamp=n, evidence=full_transaction(n, n, amount=1),
            reason="awaiting_finalized", amount_units=1,
        )
    monkeypatch.setattr(config, "SOLANA_NETWORK", "mainnet")
    calls: list[int] = []

    def rpc(method, params):
        assert method == "getSignatureStatuses"
        calls.append(len(params[0]))
        return {"value": [
            {"err": None, "confirmationStatus": "finalized"} for _ in params[0]
        ]}

    monkeypatch.setattr(solana_client, "_helius_rpc_call", rpc)
    assert solana_client.replay_solana_deposit_holds() == 257
    assert calls == [256, 1]
    assert state_db.get_solana_deposit_holds() == []

    for n in range(21, 278):
        with sqlite3.connect(state_db.DB_PATH) as conn:
            conn.execute("DELETE FROM unprocessed_sigs")
        _seed_hold(
            n + 300, timestamp=n, evidence=full_transaction(n + 300, n, amount=1),
            reason="awaiting_finalized", amount_units=1,
        )
    call_number = 0

    def malformed_second_batch(method, params):
        nonlocal call_number
        call_number += 1
        if call_number == 2:
            return {"value": []}
        return {"value": [
            {"err": None, "confirmationStatus": "finalized"} for _ in params[0]
        ]}

    monkeypatch.setattr(solana_client, "_helius_rpc_call", malformed_second_batch)
    with pytest.raises(RuntimeError, match="incomplete"):
        solana_client.replay_solana_deposit_holds()
    assert not any(state_db.is_unprocessed_sig(signature(n + 300)) for n in range(21, 278))


def test_permanent_old_parser_holds_do_not_starve_newer_finality_hold(db, monkeypatch):
    monkeypatch.setenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=offline")
    for n in range(1, 1001):
        tx = full_transaction(n, n, amount=2)
        tx["transaction"]["message"]["instructions"][0]["parsed"]["info"][
            "tokenAmount"
        ]["amount"] = "1"
        _seed_hold(
            n, timestamp=n, evidence=tx,
            reason="unsupported_vault_balance_change", amount_units=2,
            observed_commitment="finalized", finality_required=0,
        )
    _seed_hold(
        2000, timestamp=2000, evidence=full_transaction(2000, 2000, amount=1),
        reason="awaiting_finalized", amount_units=1,
    )
    monkeypatch.setattr(config, "SOLANA_NETWORK", "mainnet")
    monkeypatch.setattr(
        solana_client, "_get_strictly_finalized_signatures",
        lambda signatures, provider="helius": set(signatures),
    )

    assert solana_client.replay_solana_deposit_holds(limit=1000) >= 1
    assert state_db.is_unprocessed_sig(signature(2000))
    assert len(state_db.get_solana_deposit_holds(limit=2000)) == 1000


def test_outer_ata_create_and_matching_inner_initialize_are_one_creation():
    tx = full_transaction(251, 300)
    tx["meta"]["preTokenBalances"] = []
    tx["transaction"]["message"]["instructions"].insert(0, {
        "program": "spl-associated-token-account",
        "parsed": {"type": "create", "info": {
            "account": str(config.VAULT_USDC_ACCOUNT),
            "mint": str(config.USDC_MINT),
            "wallet": str(config.SOL_MAIN_ACCOUNT),
        }},
    })
    tx["meta"]["innerInstructions"] = [{
        "index": 0,
        "instructions": [{
            "program": "spl-token",
            "parsed": {"type": "initializeAccount3", "info": {
                "account": str(config.VAULT_USDC_ACCOUNT),
                "mint": str(config.USDC_MINT),
                "owner": str(config.SOL_MAIN_ACCOUNT),
            }},
        }],
    }]

    assert solana_client._extract_core_deposit_evidence(
        tx, signature=signature(251), vault_account=str(config.VAULT_USDC_ACCOUNT),
        mint=str(config.USDC_MINT),
    ) == ("nexus:recipient-251", "source-token-251", 2_000_000)


@pytest.mark.parametrize("conflict", ["mint", "owner", "independent_create"])
def test_conflicting_or_independent_vault_creation_evidence_is_rejected(conflict):
    tx = full_transaction(252, 300)
    tx["meta"]["preTokenBalances"] = []
    ata = {
        "program": "spl-associated-token-account",
        "parsed": {"type": "create", "info": {
            "account": str(config.VAULT_USDC_ACCOUNT),
            "mint": str(config.USDC_MINT),
            "wallet": str(config.SOL_MAIN_ACCOUNT),
        }},
    }
    initialize = {
        "program": "spl-token",
        "parsed": {"type": "initializeAccount3", "info": {
            "account": str(config.VAULT_USDC_ACCOUNT),
            "mint": str(config.USDC_MINT),
            "owner": str(config.SOL_MAIN_ACCOUNT),
        }},
    }
    tx["transaction"]["message"]["instructions"].insert(0, ata)
    tx["meta"]["innerInstructions"] = [{"index": 0, "instructions": [initialize]}]
    if conflict == "mint":
        initialize["parsed"]["info"]["mint"] = "different-mint"
    elif conflict == "owner":
        initialize["parsed"]["info"]["owner"] = "different-owner"
    else:
        tx["transaction"]["message"]["instructions"].insert(1, dict(ata))

    with pytest.raises(RuntimeError, match="creation evidence"):
        solana_client._extract_core_deposit_evidence(
            tx, signature=signature(252), vault_account=str(config.VAULT_USDC_ACCOUNT),
            mint=str(config.USDC_MINT),
        )


def test_completed_helius_query_deletes_seen_rows_but_keeps_audit_event(db, monkeypatch):
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(
        solana_client, "_helius_get_full_deposit_page",
        Mock(return_value=([full_transaction(253, 300)], None)),
    )
    monkeypatch.setattr(config, "SOLANA_NETWORK", "mainnet")
    solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=10, upper_timestamp=400,
    )

    with sqlite3.connect(state_db.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM solana_deposit_scan_seen").fetchone()[0] == 0
        assert conn.execute(
            "SELECT event, signature_count FROM helius_deposit_scan_events"
        ).fetchall() == [("range_completed", 1)]


def test_preupgrade_core_cursor_reenumerates_from_unchanged_checkpoint(db, monkeypatch):
    with sqlite3.connect(state_db.DB_PATH) as conn:
        conn.execute(
            """INSERT INTO solana_deposit_scan_cursor
               (vault_account, mint, lower_timestamp, before_signature, upper_timestamp,
                started_timestamp, network, commitment, query_identity, previous_timestamp)
               VALUES (?, ?, 100, ?, 300, 1, NULL, NULL, NULL, NULL)""",
            (str(config.VAULT_USDC_ACCOUNT), str(config.USDC_MINT), signature(254)),
        )
    client = SimpleNamespace(
        get_signatures_for_address=Mock(return_value={"result": []}),
        get_transaction=Mock(),
    )
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: None)
    monkeypatch.setattr(solana_client, "_get_client", lambda: client)
    monkeypatch.setattr(solana_client, "_rpc_call", lambda fn, *args, **kwargs: fn(*args, **kwargs))

    progress = solana_client.scan_incoming_deposits_with_durable_cursor(
        str(config.VAULT_USDC_ACCOUNT), since_ts=100, page_size=10, upper_timestamp=400,
    )

    assert progress.complete
    assert "before" not in client.get_signatures_for_address.call_args.kwargs
    assert state_db.get_solana_deposit_scan_cursor(str(config.VAULT_USDC_ACCOUNT)) is None


def test_real_poll_pins_public_waterline_so_held_source_is_rediscovered_after_db_wipe(
    db, monkeypatch,
):
    ambiguous = full_transaction(255, 300, amount=3)
    ambiguous["transaction"]["message"]["instructions"][0]["parsed"]["info"][
        "tokenAmount"
    ]["amount"] = "2"
    provider = Mock(return_value=([ambiguous], None))
    monkeypatch.setattr(solana_client, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/")
    monkeypatch.setattr(solana_client, "_helius_get_full_deposit_page", provider)
    monkeypatch.setattr(config, "SOLANA_NETWORK", "mainnet")
    monkeypatch.setattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized")
    monkeypatch.setattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 120)
    waterline = 100
    published: list[int | None] = []

    def heartbeat_update(_poll, _nexus, solana):
        nonlocal waterline
        published.append(solana)
        if solana is not None:
            waterline = solana

    monkeypatch.setattr(nexus_client, "get_heartbeat_asset", lambda: {"present": True})
    monkeypatch.setattr(
        nexus_client, "parse_heartbeat_waterlines",
        lambda _heartbeat: SimpleNamespace(solana=waterline),
    )
    monkeypatch.setattr(nexus_client, "update_heartbeat_asset", heartbeat_update)
    monkeypatch.setattr(
        solana_client, "process_unprocessed_solana_deposits", lambda *_args: [0] * 4,
    )
    for method in (
        "process_solana_deposits_refunding", "process_solana_deposits_quarantine",
        "check_sig_confirmations", "check_quarantine_confirmations",
    ):
        monkeypatch.setattr(solana_client, method, lambda *_args: 0)
    monkeypatch.setattr(nexus_client, "resolve_unverified_debits", lambda: 0)
    monkeypatch.setattr(nexus_client, "check_unconfirmed_debits", lambda *_args: 0)
    monkeypatch.setattr(nexus_client, "publish_service_record", lambda **_kwargs: None)
    monkeypatch.setattr(solana_client, "get_token_account_balance", lambda *_args: 0)
    monkeypatch.setattr(state_db, "save_last_vault_balance", lambda *_args: None)
    monkeypatch.setattr(solana_client, "check_timestamp_unpr_sigs", lambda: None)

    swap_solana.poll_solana_deposits()
    assert state_db.get_solana_deposit_holds()[0]["signature"] == signature(255)
    assert published[-1] is not None and published[-1] < 300

    for suffix in ("", "-wal", "-shm"):
        candidate = __import__("pathlib").Path(str(db) + suffix)
        if candidate.exists():
            candidate.unlink()
    state_db.init_db()

    swap_solana.poll_solana_deposits()
    assert provider.call_count == 2
    assert provider.call_args_list[1].kwargs["lower_timestamp"] == waterline
    assert state_db.get_solana_deposit_holds()[0]["signature"] == signature(255)
