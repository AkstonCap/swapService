import json
import os
import sqlite3
import sys
import types

import pytest

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
os.environ.setdefault("SOLANA_RPC_URL", "http://127.0.0.1:8899")
os.environ.setdefault("VAULT_KEYPAIR", "/tmp/nonexistent-keypair.json")
os.environ.setdefault("VAULT_USDC_ACCOUNT", "11111111111111111111111111111111")
os.environ.setdefault("USDC_MINT", "11111111111111111111111111111111")
os.environ.setdefault("NEXUS_PIN", "1234")
os.environ.setdefault("NEXUS_USDD_TREASURY_ACCOUNT", "TREASURY")
os.environ.setdefault("NEXUS_TOKEN_REGISTER_ADDRESS", "TOKEN-REGISTER")
os.environ.setdefault("SOL_MAIN_ACCOUNT", "11111111111111111111111111111111")

from src import config, nexus_client, state_db
from src import swap_receipts


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "receipts.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(path))
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPTS_ENABLED", True, raising=False)
    state_db.init_db()
    return path


def receipt_fields(signature="solana-signature-full"):
    return swap_receipts.build_receipt(
        source_signature=signature,
        solana_mint="sol-mint",
        solana_vault="sol-vault",
        nexus_token="nexus-token-register",
        nexus_account="nexus-recipient",
        output_txid="nexus-output-txid",
        output_contract_id=7,
        output_units=1_898_000,
        reference=77,
    )


def seed_confirmable_deposit():
    state_db.add_unprocessed_sig(
        "solana-signature-full", 100, "nexus:nexus-recipient", "sender", 2_000_000,
        "debited, awaiting confirmation", "nexus-output-txid",
    )
    state_db.set_unprocessed_sig_debit_intent(
        "solana-signature-full", 77, 1_898_000
    )


def test_finalization_atomically_binds_full_source_to_exact_payout_and_obligation(db):
    seed_confirmable_deposit()
    payload = receipt_fields()

    assert state_db.finalize_confirmed_solana_payout(
        sig="solana-signature-full", timestamp=100, amount_solana_units=2_000_000,
        output_txid="nexus-output-txid", output_units=1_898_000,
        nexus_destination="nexus-recipient", memo="nexus:nexus-recipient",
        reference=77, output_contract_id=7, fee_solana_units=102_000,
        receipt_payload=payload, expected_owner="provider-genesis",
        receipt_name=swap_receipts.receipt_name("solana-signature-full"),
    )

    assert not state_db.is_unprocessed_sig("solana-signature-full")
    with sqlite3.connect(db) as conn:
        completed = conn.execute(
            "SELECT txid, amount_usdd_units, nexus_destination, reference, contract_id "
            "FROM processed_sigs WHERE sig=?", ("solana-signature-full",)
        ).fetchone()
        fee = conn.execute(
            "SELECT amount_usdc_units FROM fee_entries WHERE sig=?",
            ("solana-signature-full",),
        ).fetchone()
    assert completed == ("nexus-output-txid", 1_898_000, "nexus-recipient", 77, 7)
    assert fee == (102_000,)
    obligation = state_db.get_swap_receipt("solana-signature-full")
    assert obligation["status"] == "pending"
    assert obligation["expected_owner"] == "provider-genesis"
    assert json.loads(obligation["payload_json"]) == payload


def test_full_source_signature_not_sequential_reference_is_receipt_identity(db):
    first = receipt_fields("full-source-signature-one")
    second = receipt_fields("full-source-signature-two")
    assert first["reference"] == second["reference"]
    assert first["output_txid"] == second["output_txid"]

    first_name = swap_receipts.receipt_name(first["source_signature"])
    second_name = swap_receipts.receipt_name(second["source_signature"])
    assert first_name != second_name
    state_db.enqueue_swap_receipt(first, "provider-genesis", first_name)
    state_db.enqueue_swap_receipt(second, "provider-genesis", second_name)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT source_signature, receipt_name FROM swap_receipts ORDER BY source_signature"
        ).fetchall() == [
            (first["source_signature"], first_name),
            (second["source_signature"], second_name),
        ]


def test_finalization_without_receipt_is_backward_compatible(db):
    seed_confirmable_deposit()
    assert state_db.finalize_confirmed_solana_payout(
        sig="solana-signature-full", timestamp=100, amount_solana_units=2_000_000,
        output_txid="nexus-output-txid", output_units=1_898_000,
        nexus_destination="nexus-recipient", memo="nexus:nexus-recipient",
        reference=77, output_contract_id=7, fee_solana_units=0,
    )
    assert state_db.get_swap_receipt("solana-signature-full") is None


def test_finalization_rejects_receipt_not_bound_to_exact_confirmed_payout(db):
    seed_confirmable_deposit()
    payload = receipt_fields()
    payload["output_txid"] = "different-payout"
    with pytest.raises(ValueError, match="does not match"):
        state_db.finalize_confirmed_solana_payout(
            sig="solana-signature-full", timestamp=100, amount_solana_units=2_000_000,
            output_txid="nexus-output-txid", output_units=1_898_000,
            nexus_destination="nexus-recipient", memo="nexus:nexus-recipient",
            reference=77, output_contract_id=7, fee_solana_units=0,
            receipt_payload=payload, expected_owner="provider-genesis",
            receipt_name=swap_receipts.receipt_name("solana-signature-full"),
        )
    assert state_db.is_unprocessed_sig("solana-signature-full")
    assert not state_db.is_processed_sig("solana-signature-full")


def test_legacy_completed_row_never_gets_a_fabricated_receipt_obligation(db):
    state_db.mark_processed_sig(
        "solana-signature-full", 100, 2_000_000, "nexus-output-txid", 1.898,
        "debit_confirmed", 77, amount_usdd_units=1_898_000,
        nexus_destination="nexus-recipient", memo="nexus:nexus-recipient", contract_id=7,
    )
    payload = receipt_fields()
    assert not state_db.finalize_confirmed_solana_payout(
        sig="solana-signature-full", timestamp=100, amount_solana_units=2_000_000,
        output_txid="nexus-output-txid", output_units=1_898_000,
        nexus_destination="nexus-recipient", memo="nexus:nexus-recipient",
        reference=77, output_contract_id=7, fee_solana_units=0,
        receipt_payload=payload, expected_owner="provider-genesis",
        receipt_name=swap_receipts.receipt_name("solana-signature-full"),
    )
    assert state_db.get_swap_receipt("solana-signature-full") is None


@pytest.mark.parametrize("change", [
    {"source_signature": ""}, {"output_units": 0}, {"output_units": "1"},
    {"output_contract_id": -1}, {"output_contract_id": "7"},
    {"reference": True}, {"nexus_token": ""},
])
def test_receipt_builder_rejects_absent_or_malformed_exact_evidence(change):
    args = dict(
        source_signature="full-signature", solana_mint="mint", solana_vault="vault",
        nexus_token="token", nexus_account="account", output_txid="txid",
        output_contract_id=7, output_units=1, reference=9,
    )
    args.update(change)
    with pytest.raises(ValueError):
        swap_receipts.build_receipt(**args)


def test_create_uses_an_all_immutable_json_schema_and_profile_owner(monkeypatch):
    payload = receipt_fields()
    calls = []
    monkeypatch.setattr(
        nexus_client, "_run",
        lambda command, timeout=None: (calls.append(command) or (1, "", "create rejected")),
    )

    swap_receipts._create({"receipt_name": "receipt-name"}, payload)

    assert len(calls) == 1
    command = calls[0]
    assert command[:4] == [
        config.NEXUS_CLI, "assets/create/asset", "format=JSON", "name=receipt-name",
    ]
    definitions = [arg for arg in command if arg.startswith("json=")]
    assert len(definitions) == 1
    assert json.loads(definitions[0].removeprefix("json=")) == [
        {"name": field, "type": "string", "value": payload[field], "mutable": False}
        for field in swap_receipts.REQUIRED_FIELDS
    ]
    assert not any(arg.startswith("owner=") for arg in command)
    assert not any(
        arg.startswith(f"{field}=")
        for field in swap_receipts.REQUIRED_FIELDS
        for arg in command
    )


def test_timeout_after_create_acceptance_never_blindly_creates_again(db, monkeypatch):
    payload = receipt_fields()
    state_db.enqueue_swap_receipt(payload, "provider-genesis", swap_receipts.receipt_name(payload["source_signature"]))
    calls = []

    def run(command, timeout=None):
        calls.append(command)
        if command[1] == "assets/create/asset":
            raise TimeoutError("accepted but response lost")
        return (0, "[]", "")

    monkeypatch.setattr(nexus_client, "_run", run)
    monkeypatch.setattr(nexus_client, "read_service_record", lambda: {"owner": "provider-genesis"})

    assert swap_receipts.publish_pending_receipts() == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status, COUNT(*) FROM swap_receipts WHERE source_signature=?",
            (payload["source_signature"],),
        ).fetchone() == ("creating", 1)

    # A fresh publication pass reopens the SQLite journal as a restarted process would.
    assert swap_receipts.publish_pending_receipts() == 0
    creates = [c for c in calls if c[1] == "assets/create/asset"]
    assert len(creates) == 1
    assert not any(str(arg).startswith("owner=") for arg in creates[0])


def test_exact_receipt_query_uses_explicit_where_cli_parameter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        nexus_client, "_run",
        lambda command, timeout=None: (calls.append(command) or (0, "[]", "")),
    )

    assert swap_receipts._query_exact_receipts("full-source-signature") == []

    assert len(calls) == 1
    assert "where=results.source_signature=full-source-signature" in calls[0]
    assert "results.source_signature=full-source-signature" not in calls[0]


def test_restart_recovers_by_exact_generic_readback_before_done(db, monkeypatch):
    payload = receipt_fields()
    name = swap_receipts.receipt_name(payload["source_signature"])
    state_db.enqueue_swap_receipt(payload, "provider-genesis", name)
    assert state_db.claim_swap_receipt(payload["source_signature"])
    observed = dict(payload, owner="provider-genesis", address="receipt-register")
    calls = []

    def run(command, timeout=None):
        calls.append(command)
        assert command[1].startswith("register/list/assets:asset/")
        return (0, json.dumps([observed]), "")

    monkeypatch.setattr(nexus_client, "_run", run)
    assert swap_receipts.publish_pending_receipts() == 1
    saved = state_db.get_swap_receipt(payload["source_signature"])
    assert saved["status"] == "published"
    assert saved["asset_address"] == "receipt-register"
    assert calls and all(c[1] != "assets/create/asset" for c in calls)


@pytest.mark.parametrize("records", [
    [],
    [{"owner": "provider-genesis"}],
    [dict(receipt_fields(), owner="attacker", address="x")],
])
def test_absent_malformed_or_wrong_owner_readback_fails_closed(db, monkeypatch, records):
    payload = receipt_fields()
    state_db.enqueue_swap_receipt(payload, "provider-genesis", swap_receipts.receipt_name(payload["source_signature"]))
    assert state_db.claim_swap_receipt(payload["source_signature"])
    monkeypatch.setattr(nexus_client, "_run", lambda command, timeout=None: (0, json.dumps(records), ""))

    assert swap_receipts.publish_pending_receipts() == 0
    assert state_db.get_swap_receipt(payload["source_signature"])["status"] != "published"


def test_duplicate_exact_chain_receipts_keep_sqlite_obligation_unpublished(db, monkeypatch):
    payload = receipt_fields()
    source = payload["source_signature"]
    state_db.enqueue_swap_receipt(
        payload, "provider-genesis", swap_receipts.receipt_name(source)
    )
    assert state_db.claim_swap_receipt(source)
    records = [
        dict(payload, owner="provider-genesis", address="receipt-one"),
        dict(payload, owner="provider-genesis", address="receipt-two"),
    ]
    monkeypatch.setattr(
        nexus_client, "_run",
        lambda command, timeout=None: (0, json.dumps(records), ""),
    )

    assert swap_receipts.publish_pending_receipts() == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status, asset_address, COUNT(*) FROM swap_receipts "
            "WHERE source_signature=?", (source,),
        ).fetchone() == ("creating", None, 1)


def test_provider_owner_must_come_from_own_registration_before_create(db, monkeypatch):
    payload = receipt_fields()
    state_db.enqueue_swap_receipt(payload, "provider-genesis", swap_receipts.receipt_name(payload["source_signature"]))
    monkeypatch.setattr(nexus_client, "read_service_record", lambda: {"owner": "different-owner"})
    calls = []
    monkeypatch.setattr(nexus_client, "_run", lambda command, timeout=None: calls.append(command))

    assert swap_receipts.publish_pending_receipts() == 0
    assert state_db.get_swap_receipt(payload["source_signature"])["status"] == "pending"
    assert calls == []


def test_full_generic_query_page_is_not_treated_as_complete(db, monkeypatch):
    payload = receipt_fields()
    state_db.enqueue_swap_receipt(payload, "provider-genesis", swap_receipts.receipt_name(payload["source_signature"]))
    assert state_db.claim_swap_receipt(payload["source_signature"])
    records = [dict(payload, owner="provider-genesis", address="exact")]
    records.extend(
        dict(payload, source_signature=f"other-{index}", owner="provider-genesis", address=f"x-{index}")
        for index in range(99)
    )
    monkeypatch.setattr(
        nexus_client, "_run", lambda command, timeout=None: (0, json.dumps(records), "")
    )
    assert swap_receipts.publish_pending_receipts() == 0
    assert state_db.get_swap_receipt(payload["source_signature"])["status"] != "published"


def test_registration_advertises_only_enabled_known_v1_extension(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPTS_ENABLED", False, raising=False)
    assert "receipt_schema" not in nexus_client.build_service_record(last_poll=1)
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPTS_ENABLED", True, raising=False)
    assert nexus_client.build_service_record(last_poll=1)["receipt_schema"] == "nexus-swap-receipt-v1"


def test_confirmation_path_freezes_chain_evidence_and_authoritative_provider_owner(db, monkeypatch):
    seed_confirmable_deposit()
    evidence = nexus_client.TransferDebitEvidence(
        remote_txid="nexus-output-txid", contract_id=7,
        from_address=str(config.NEXUS_TOKEN_REGISTER_ADDRESS),
        to_address="nexus-recipient", amount_usdd_units=1_898_000, reference="77",
    )
    monkeypatch.setattr(
        nexus_client, "get_transactions_confirmations",
        lambda txids: nexus_client.BatchLookup({"nexus-output-txid": 10}, True),
    )
    monkeypatch.setattr(
        nexus_client, "get_nexus_transfer_debits_by_txid",
        lambda txid: nexus_client.BatchLookup({txid: [evidence]}, True),
    )
    monkeypatch.setattr(
        nexus_client, "read_service_record", lambda: {"owner": "provider-genesis"}
    )

    assert nexus_client.check_unconfirmed_debits(10, 8) == 1
    obligation = state_db.get_swap_receipt("solana-signature-full")
    payload = json.loads(obligation["payload_json"])
    assert obligation["expected_owner"] == "provider-genesis"
    assert payload["source_signature"] == "solana-signature-full"
    assert payload["output_txid"] == "nexus-output-txid"
    assert payload["output_contract_id"] == "7"
    assert payload["output_units"] == "1898000"
    assert payload["reference"] == "77"
