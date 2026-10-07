"""Offline admission tests for immutable chain and effective config identity."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src import custody_admission as admission


SOLANA_GENESIS = "11111111111111111111111111111111"
NEXUS_GENESIS = "a" * 256


@pytest.fixture(autouse=True)
def expected_chain_identity(monkeypatch):
    monkeypatch.setenv("CUSTODY_SOLANA_GENESIS_HASH", SOLANA_GENESIS)
    monkeypatch.setenv("CUSTODY_NEXUS_GENESIS_HASH", NEXUS_GENESIS)


def test_configuration_fingerprint_binds_every_effective_uppercase_setting(monkeypatch):
    """Future uppercase safety settings are bound without another hand-maintained list."""
    excluded_credentials = {
        "NEXUS_API_PASSWORD", "NEXUS_API_USER", "NEXUS_PIN", "NEXUS_SESSION",
    }
    names = sorted(
        name for name in vars(config)
        if name.isupper() and not name.startswith("_") and name not in excluded_credentials
    )
    assert {
        "PRODUCTION_MODE",
        "MAX_SWAP_SOLANA_UNITS", "MAX_SWAP_NEXUS_UNITS",
        "DAILY_PAYOUT_CAP_SOLANA_UNITS",
        "MIN_DEPOSIT_SOLANA_UNITS", "MIN_CREDIT_NEXUS_UNITS",
        "DUST_CREDIT_NEXUS_UNITS", "SOLANA_FINALIZED_ABOVE_UNITS",
        "NEXUS_TRANSFER_MIN_CONFIRMATIONS",
        "HEARTBEAT_ENABLED", "HEARTBEAT_WATERLINE_ENABLED",
        "NEXUS_HEARTBEAT_ASSET_ADDRESS", "NEXUS_HEARTBEAT_ASSET_NAME",
        "HEARTBEAT_WATERLINE_SOLANA_FIELD", "HEARTBEAT_WATERLINE_NEXUS_FIELD",
        "HEARTBEAT_WATERLINE_SAFETY_SEC",
        "MAX_ACTION_ATTEMPTS", "ACTION_RETRY_COOLDOWN_SEC",
        "SOLANA_RPC_TIMEOUT_SEC", "SOLANA_TX_FETCH_TIMEOUT_SEC",
        "NEXUS_CLI_TIMEOUT_SEC", "REFUND_TIMEOUT_SEC",
        "FLAT_FEE_TO_NEXUS_UNITS", "FLAT_FEE_TO_SOLANA_UNITS",
        "FLAT_FEE_REFUND_SOLANA_UNITS", "FEE_NEXUS_DISPOSITION_UNITS", "FEE_BPS",
    } <= set(names)

    baseline = admission.configuration_fingerprint(config)
    for name in names:
        original = getattr(config, name)
        if type(original) is bool:
            changed = not original
        elif type(original) is int:
            changed = original + 1
        elif type(original) is float:
            changed = original + 0.5
        elif isinstance(original, str):
            changed = original + "-changed"
        elif original is None:
            changed = "configured"
        else:
            changed = {"changed": name}
        monkeypatch.setattr(config, name, changed)
        assert admission.configuration_fingerprint(config) != baseline, name
        monkeypatch.setattr(config, name, original)


def test_configuration_fingerprint_hashes_locations_and_excludes_credentials(monkeypatch):
    sentinel_url = "https://rpc.invalid/private-path?api-key=do-not-publish"
    sentinel_path = "/secret/operator/keypair.json"
    sentinel_pin = "987654"
    fake = SimpleNamespace(
        PRODUCTION_MODE=True,
        RPC_URL=sentinel_url,
        VAULT_KEYPAIR_PATH=sentinel_path,
        NEXUS_PIN=sentinel_pin,
        NEXUS_SESSION="transient-session",
        NEXUS_API_USER="api-user",
        NEXUS_API_PASSWORD="api-password",
        FUTURE_SAFETY_LIMIT=7,
    )
    payloads = []
    real_canonical = admission._canonical

    def capture(value):
        payloads.append(value)
        return real_canonical(value)

    monkeypatch.setattr(admission, "_canonical", capture)
    baseline = admission.configuration_fingerprint(fake)
    encoded = json.dumps(payloads[-1], sort_keys=True)
    assert sentinel_url not in encoded
    assert sentinel_path not in encoded
    assert sentinel_pin not in encoded
    assert "transient-session" not in encoded
    assert payloads[-1]["excluded_sensitive_settings"] == [
        "NEXUS_API_PASSWORD", "NEXUS_API_USER", "NEXUS_PIN", "NEXUS_SESSION",
    ]

    fake.NEXUS_PIN = "changed-without-recertification"
    fake.NEXUS_SESSION = "rotated-session"
    fake.NEXUS_API_PASSWORD = "rotated-password"
    assert admission.configuration_fingerprint(fake) == baseline

    fake.RPC_URL = "https://other-rpc.invalid"
    assert admission.configuration_fingerprint(fake) != baseline


def test_configuration_fingerprint_binds_both_independently_pinned_chain_ids(monkeypatch):
    fake = SimpleNamespace(PRODUCTION_MODE=True)
    baseline = admission.configuration_fingerprint(fake)
    monkeypatch.setenv("CUSTODY_SOLANA_GENESIS_HASH", "Sysvar1111111111111111111111111111111111111")
    assert admission.configuration_fingerprint(fake) != baseline
    monkeypatch.setenv("CUSTODY_SOLANA_GENESIS_HASH", SOLANA_GENESIS)
    monkeypatch.setenv("CUSTODY_NEXUS_GENESIS_HASH", "b" * 256)
    assert admission.configuration_fingerprint(fake) != baseline


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CUSTODY_SOLANA_GENESIS_HASH", None),
        ("CUSTODY_NEXUS_GENESIS_HASH", None),
        ("CUSTODY_SOLANA_GENESIS_HASH", "not-base58-0OIl"),
        ("CUSTODY_NEXUS_GENESIS_HASH", "abc"),
        ("CUSTODY_NEXUS_GENESIS_HASH", "A" * 256),
    ],
)
def test_configuration_fingerprint_rejects_missing_or_malformed_chain_identity(
    monkeypatch, name, value,
):
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(admission.AdmissionError, match="chain identity"):
        admission.configuration_fingerprint(SimpleNamespace(PRODUCTION_MODE=True))


def _response(payload):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def test_verify_accepts_only_exact_observed_genesis_hashes(monkeypatch):
    from src import custody_chain

    post = Mock(return_value=_response({
        "jsonrpc": "2.0", "id": "custody-chain-admission", "result": SOLANA_GENESIS,
    }))
    run = Mock(return_value=(0, json.dumps({"hash": NEXUS_GENESIS}) + "\n[Completed]", ""))
    monkeypatch.setattr(custody_chain.requests, "post", post)
    monkeypatch.setattr(custody_chain.nexus_client, "_run", run)

    assert custody_chain.verify() is None
    post.assert_called_once_with(
        config.RPC_URL,
        json={"jsonrpc": "2.0", "id": "custody-chain-admission", "method": "getGenesisHash"},
        timeout=config.SOLANA_RPC_TIMEOUT_SEC,
    )
    run.assert_called_once_with(
        [config.NEXUS_CLI, "ledger/get/blockhash", "height=0"],
        timeout=config.NEXUS_CLI_TIMEOUT_SEC,
    )


@pytest.mark.parametrize("chain", ["solana", "nexus"])
def test_verify_rejects_wrong_observed_chain(monkeypatch, chain):
    from src import custody_chain

    solana = SOLANA_GENESIS if chain != "solana" else "Sysvar1111111111111111111111111111111111111"
    nexus = NEXUS_GENESIS if chain != "nexus" else "b" * 256
    monkeypatch.setattr(custody_chain.requests, "post", Mock(return_value=_response({"result": solana})))
    monkeypatch.setattr(
        custody_chain.nexus_client, "_run",
        Mock(return_value=(0, json.dumps({"hash": nexus}), "")),
    )
    with pytest.raises(admission.AdmissionError, match="does not match"):
        custody_chain.verify()


@pytest.mark.parametrize(
    ("solana_payload", "nexus_payload"),
    [
        ({"result": 123}, {"hash": NEXUS_GENESIS}),
        ({"result": SOLANA_GENESIS}, {"hash": 123}),
        ({"result": SOLANA_GENESIS}, {"result": {"hash": NEXUS_GENESIS}}),
        ({"result": SOLANA_GENESIS}, {"error": {"code": -83}}),
    ],
)
def test_verify_rejects_malformed_query_results(monkeypatch, solana_payload, nexus_payload):
    from src import custody_chain

    monkeypatch.setattr(
        custody_chain.requests, "post", Mock(return_value=_response(solana_payload)),
    )
    monkeypatch.setattr(
        custody_chain.nexus_client, "_run",
        Mock(return_value=(0, json.dumps(nexus_payload), "")),
    )
    with pytest.raises(admission.AdmissionError, match="query failed"):
        custody_chain.verify()


@pytest.mark.parametrize("failure", ["solana", "nexus"])
def test_verify_rejects_unavailable_queries_without_leaking_transport_details(monkeypatch, failure):
    from src import custody_chain

    if failure == "solana":
        monkeypatch.setattr(
            custody_chain.requests, "post", Mock(side_effect=RuntimeError("secret rpc URL")),
        )
        monkeypatch.setattr(
            custody_chain.nexus_client, "_run",
            Mock(return_value=(0, json.dumps({"hash": NEXUS_GENESIS}), "")),
        )
    else:
        monkeypatch.setattr(
            custody_chain.requests, "post", Mock(return_value=_response({"result": SOLANA_GENESIS})),
        )
        monkeypatch.setattr(
            custody_chain.nexus_client, "_run",
            Mock(return_value=(1, "", "secret node detail")),
        )

    with pytest.raises(admission.AdmissionError) as caught:
        custody_chain.verify()
    assert "secret" not in str(caught.value)
