import json
import os
import sqlite3
import inspect

import pytest

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
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPT_EXPECTED_COST_NXS_UNITS", 20, raising=False)
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPT_BUDGET_NXS_UNITS", 1_000, raising=False)
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


def receipt_capable_provider_record(owner="provider-genesis"):
    record = nexus_client.build_service_record(last_poll=0)
    record.update(owner=owner, receipt_schema=swap_receipts.SCHEMA)
    return record


def seed_confirmable_deposit():
    state_db.add_unprocessed_sig(
        "solana-signature-full", 100, "nexus:nexus-recipient", "sender", 2_000_000,
        "debited, awaiting confirmation", "nexus-output-txid",
    )
    state_db.set_unprocessed_sig_debit_intent(
        "solana-signature-full", 77, 1_898_000
    )


def enqueue_bound_receipt(payload, owner="provider-genesis"):
    source = payload["source_signature"]
    state_db.enqueue_swap_receipt(payload, swap_receipts.receipt_name(source))
    assert state_db.bind_swap_receipt_owner(source, owner)


def test_budget_bypassing_receipt_claim_helper_is_removed():
    assert not hasattr(state_db, "claim_swap_receipt")


def test_receipt_enqueue_is_ownerless_until_authenticated_binding(db):
    assert "expected_owner" not in inspect.signature(state_db.enqueue_swap_receipt).parameters
    payload = receipt_fields("ownerless-enqueue")
    saved = state_db.enqueue_swap_receipt(
        payload, swap_receipts.receipt_name(payload["source_signature"])
    )
    assert saved["status"] == "awaiting_owner"
    assert saved["expected_owner"] is None


def test_init_db_migrates_bound_only_receipts_without_losing_obligations(tmp_path, monkeypatch):
    path = tmp_path / "legacy-receipts.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(path))
    payload = receipt_fields("legacy-bound-receipt")
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE swap_receipts (
                source_signature TEXT PRIMARY KEY,
                receipt_name TEXT NOT NULL UNIQUE,
                expected_owner TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                asset_address TEXT,
                created_timestamp INTEGER NOT NULL,
                updated_timestamp INTEGER NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO swap_receipts VALUES (?, ?, ?, ?, 'pending', NULL, 1, 1)",
            (
                payload["source_signature"], swap_receipts.receipt_name(payload["source_signature"]),
                "provider-genesis", json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )

    state_db.init_db()

    with sqlite3.connect(path) as conn:
        owner_column = next(
            row for row in conn.execute("PRAGMA table_info(swap_receipts)")
            if row[1] == "expected_owner"
        )
        assert owner_column[3] == 0
    saved = state_db.get_swap_receipt(payload["source_signature"])
    assert saved is not None
    assert saved["expected_owner"] == "provider-genesis"
    assert saved["status"] == "pending"


def test_finalization_atomically_archives_exact_payout_and_unbound_obligation(db):
    seed_confirmable_deposit()
    payload = receipt_fields()

    assert state_db.finalize_confirmed_solana_payout(
        sig="solana-signature-full", timestamp=100, amount_solana_units=2_000_000,
        output_txid="nexus-output-txid", output_units=1_898_000,
        nexus_destination="nexus-recipient", memo="nexus:nexus-recipient",
        reference=77, output_contract_id=7, fee_solana_units=102_000,
        receipt_payload=payload,
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
    assert obligation is not None
    assert obligation["status"] == "awaiting_owner"
    assert obligation["expected_owner"] is None
    assert json.loads(obligation["payload_json"]) == payload


def test_duplicate_finalization_preserves_later_owner_binding(db):
    seed_confirmable_deposit()
    payload = receipt_fields()
    arguments = dict(
        sig="solana-signature-full", timestamp=100, amount_solana_units=2_000_000,
        output_txid="nexus-output-txid", output_units=1_898_000,
        nexus_destination="nexus-recipient", memo="nexus:nexus-recipient",
        reference=77, output_contract_id=7, fee_solana_units=102_000,
        receipt_payload=payload,
        receipt_name=swap_receipts.receipt_name("solana-signature-full"),
    )

    assert state_db.finalize_confirmed_solana_payout(**arguments)
    assert state_db.bind_swap_receipt_owner("solana-signature-full", "provider-genesis")
    assert state_db.finalize_confirmed_solana_payout(**arguments)
    saved = state_db.get_swap_receipt("solana-signature-full")
    assert saved is not None
    assert saved["expected_owner"] == "provider-genesis"
    assert saved["status"] == "pending"


def test_restart_binds_authenticated_owner_then_creates_and_publishes(db, monkeypatch):
    payload = receipt_fields("owner-outage-restart")
    source = payload["source_signature"]
    name = swap_receipts.receipt_name(source)
    state_db.enqueue_swap_receipt(payload, name)
    monkeypatch.setattr(nexus_client, "read_service_record", receipt_capable_provider_record)
    calls = []

    def run(command, timeout=None):
        calls.append(command)
        if command[1] == "assets/create/asset":
            return (0, '{"txid":"create-txid","address":"receipt-register"}', "")
        return (0, json.dumps([
            dict(payload, owner="provider-genesis", address="receipt-register")
        ]), "")

    monkeypatch.setattr(nexus_client, "_run", run)

    assert swap_receipts.publish_pending_receipts() == 1
    receipt = state_db.get_swap_receipt(source)
    assert receipt is not None
    assert receipt["status"] == "published"
    assert receipt["expected_owner"] == "provider-genesis"
    assert receipt["asset_address"] == "receipt-register"
    assert len([call for call in calls if call[1] == "assets/create/asset"]) == 1


def test_invalid_registration_cannot_bind_or_publish_waiting_obligation(db, monkeypatch):
    payload = receipt_fields("invalid-registration")
    source = payload["source_signature"]
    state_db.enqueue_swap_receipt(payload, swap_receipts.receipt_name(source))
    invalid = receipt_capable_provider_record()
    invalid["receipt_schema"] = "unsupported"
    monkeypatch.setattr(nexus_client, "read_service_record", lambda: invalid)
    calls = []
    monkeypatch.setattr(nexus_client, "_run", lambda command, timeout=None: calls.append(command))

    assert swap_receipts.publish_pending_receipts() == 0
    saved = state_db.get_swap_receipt(source)
    assert saved is not None
    assert saved["status"] == "awaiting_owner"
    assert saved["expected_owner"] is None
    assert calls == []


def test_bound_owner_is_immutable_on_mismatch(db):
    payload = receipt_fields("owner-binding-mismatch")
    source = payload["source_signature"]
    state_db.enqueue_swap_receipt(payload, swap_receipts.receipt_name(source))

    assert state_db.bind_swap_receipt_owner(source, "provider-genesis")
    assert not state_db.bind_swap_receipt_owner(source, "different-owner")
    saved = state_db.get_swap_receipt(source)
    assert saved is not None
    assert saved["expected_owner"] == "provider-genesis"
    assert saved["status"] == "pending"


def test_full_source_signature_not_sequential_reference_is_receipt_identity(db):
    first = receipt_fields("full-source-signature-one")
    second = receipt_fields("full-source-signature-two")
    assert first["reference"] == second["reference"]
    assert first["output_txid"] == second["output_txid"]

    first_name = swap_receipts.receipt_name(first["source_signature"])
    second_name = swap_receipts.receipt_name(second["source_signature"])
    assert first_name != second_name
    enqueue_bound_receipt(first)
    enqueue_bound_receipt(second)

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
            receipt_payload=payload,
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
        receipt_payload=payload,
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


def test_create_uses_an_all_immutable_json_schema_and_profile_owner(db, monkeypatch):
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


def test_receipt_nxs_budget_reserves_before_create_and_holds_after_timeout(db, monkeypatch):
    """An uncertain create consumes the sole configured NXS allowance indefinitely."""
    first = receipt_fields("receipt-budget-first")
    second = receipt_fields("receipt-budget-second")
    for payload in (first, second):
        enqueue_bound_receipt(payload)
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPT_EXPECTED_COST_NXS_UNITS", 20)
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPT_BUDGET_NXS_UNITS", 20)
    monkeypatch.setattr(nexus_client, "read_service_record", receipt_capable_provider_record)
    creates = []

    def run(command, timeout=None):
        if command[1] == "assets/create/asset":
            creates.append(command)
            with sqlite3.connect(db) as conn:
                assert conn.execute(
                    "SELECT expected_cost_nxs_units FROM receipt_nxs_budget_events "
                    "WHERE source_signature=? AND event='reserved'",
                    (first["source_signature"],),
                ).fetchone() == (20,)
            raise TimeoutError("accepted but response lost")
        return (0, "[]", "")

    monkeypatch.setattr(nexus_client, "_run", run)
    assert swap_receipts.publish_pending_receipts() == 0
    assert swap_receipts.publish_pending_receipts() == 0
    assert len(creates) == 1
    assert state_db.get_swap_receipt(first["source_signature"])["status"] == "creating"
    assert state_db.get_swap_receipt(second["source_signature"])["status"] == "pending"


def test_parseable_receipt_create_response_persists_remote_identity_in_nxs_ledger(db, monkeypatch):
    payload = receipt_fields()
    source = payload["source_signature"]
    enqueue_bound_receipt(payload)
    assert state_db.claim_swap_receipt_with_nxs_budget(
        source, expected_cost_nxs_units=20, budget_nxs_units=20
    )
    monkeypatch.setattr(
        nexus_client, "_run",
        lambda command, timeout=None: (0, '{"txid":"create-txid","address":"asset-address"}', ""),
    )

    swap_receipts._create({"receipt_name": swap_receipts.receipt_name(source)}, payload)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            """SELECT event, expected_cost_nxs_units, create_txid, asset_address
               FROM receipt_nxs_budget_events WHERE source_signature=? ORDER BY id""",
            (source,),
        ).fetchall() == [
            ("reserved", 20, None, None),
            ("create_reported", 20, "create-txid", "asset-address"),
        ]


def test_timeout_after_create_acceptance_never_blindly_creates_again(db, monkeypatch):
    payload = receipt_fields()
    enqueue_bound_receipt(payload)
    calls = []

    def run(command, timeout=None):
        calls.append(command)
        if command[1] == "assets/create/asset":
            raise TimeoutError("accepted but response lost")
        return (0, "[]", "")

    monkeypatch.setattr(nexus_client, "_run", run)
    monkeypatch.setattr(nexus_client, "read_service_record", receipt_capable_provider_record)

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
    enqueue_bound_receipt(payload)
    assert state_db.claim_swap_receipt_with_nxs_budget(
        payload["source_signature"], expected_cost_nxs_units=20, budget_nxs_units=1_000
    )
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
    enqueue_bound_receipt(payload)
    assert state_db.claim_swap_receipt_with_nxs_budget(
        payload["source_signature"], expected_cost_nxs_units=20, budget_nxs_units=1_000
    )
    monkeypatch.setattr(nexus_client, "_run", lambda command, timeout=None: (0, json.dumps(records), ""))

    assert swap_receipts.publish_pending_receipts() == 0
    assert state_db.get_swap_receipt(payload["source_signature"])["status"] != "published"


def test_duplicate_exact_chain_receipts_keep_sqlite_obligation_unpublished(db, monkeypatch):
    payload = receipt_fields()
    source = payload["source_signature"]
    enqueue_bound_receipt(payload)
    assert state_db.claim_swap_receipt_with_nxs_budget(
        source, expected_cost_nxs_units=20, budget_nxs_units=1_000
    )
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
    enqueue_bound_receipt(payload)
    monkeypatch.setattr(
        nexus_client, "read_service_record", lambda: receipt_capable_provider_record("different-owner")
    )
    calls = []
    monkeypatch.setattr(nexus_client, "_run", lambda command, timeout=None: calls.append(command))

    assert swap_receipts.publish_pending_receipts() == 0
    assert state_db.get_swap_receipt(payload["source_signature"])["status"] == "pending"
    assert calls == []


def test_full_generic_query_page_is_not_treated_as_complete(db, monkeypatch):
    payload = receipt_fields()
    enqueue_bound_receipt(payload)
    assert state_db.claim_swap_receipt_with_nxs_budget(
        payload["source_signature"], expected_cost_nxs_units=20, budget_nxs_units=1_000
    )
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


def test_receipt_provider_registration_requires_schema_owner_and_exact_immutable_pair(monkeypatch):
    valid = receipt_capable_provider_record()
    monkeypatch.setattr(nexus_client, "read_service_record", lambda: valid)
    assert swap_receipts.receipt_provider_registration() == (
        "provider-genesis", "receipt-capable provider registration is valid"
    )

    for mutation, expected in (
        ({"receipt_schema": "other"}, "lacks receipt_schema"),
        ({"owner": ""}, "no authoritative owner"),
        ({"owner": "   "}, "no authoritative owner"),
        ({"solana_vault_address": "other-vault"}, "immutable pair/custody contract"),
    ):
        record = dict(valid, **mutation)
        monkeypatch.setattr(nexus_client, "read_service_record", lambda record=record: record)
        owner, reason = swap_receipts.receipt_provider_registration()
        assert owner is None
        assert expected in reason


def test_confirmation_path_freezes_chain_evidence_before_provider_owner_binding(db, monkeypatch):
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
    monkeypatch.setattr(nexus_client, "read_service_record", _raise_receipt_owner_lookup_error)

    assert nexus_client.check_unconfirmed_debits(10, 8) == 1
    obligation = state_db.get_swap_receipt("solana-signature-full")
    assert obligation is not None
    payload = json.loads(obligation["payload_json"])
    assert obligation["expected_owner"] is None
    assert obligation["status"] == "awaiting_owner"
    assert payload["source_signature"] == "solana-signature-full"
    assert payload["output_txid"] == "nexus-output-txid"
    assert payload["output_contract_id"] == "7"
    assert payload["output_units"] == "1898000"
    assert payload["reference"] == "77"


def _raise_receipt_owner_lookup_error():
    raise RuntimeError("provider registration unavailable")


def test_unavailable_receipt_owner_preserves_obligation_without_holding_payout(db, monkeypatch):
    """Transient provider lookup failure must lose neither payout nor receipt work."""
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
    monkeypatch.setattr(nexus_client, "read_service_record", _raise_receipt_owner_lookup_error)

    assert nexus_client.check_unconfirmed_debits(10, 8) == 1
    assert state_db.is_processed_sig("solana-signature-full")
    assert not state_db.is_unprocessed_sig("solana-signature-full")
    obligation = state_db.get_swap_receipt("solana-signature-full")
    assert obligation is not None
    assert obligation["status"] == "awaiting_owner"
    assert obligation["expected_owner"] is None
    payload = json.loads(obligation["payload_json"])
    assert payload == swap_receipts.build_receipt(
        source_signature="solana-signature-full",
        solana_mint=str(config.SWAP_PAIR.solana.mint),
        solana_vault=str(config.SWAP_PAIR.solana.vault_account),
        nexus_token=str(config.SWAP_PAIR.nexus.register_address),
        nexus_account="nexus-recipient", output_txid="nexus-output-txid",
        output_contract_id=7, output_units=1_898_000, reference=77,
    )


def test_receipt_builder_failure_archives_payout_with_manual_review_obligation(db, monkeypatch):
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
        swap_receipts, "build_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("injected canonical failure")),
    )

    assert nexus_client.check_unconfirmed_debits(10, 8) == 1
    obligation = state_db.get_swap_receipt("solana-signature-full")
    assert obligation is not None
    assert obligation["status"] == "manual_review"
    assert "injected canonical failure" in obligation["manual_review_error"]
    assert json.loads(obligation["payload_json"]) == {
        "source_signature": "solana-signature-full",
        "solana_mint": str(config.SWAP_PAIR.solana.mint),
        "solana_vault": str(config.SWAP_PAIR.solana.vault_account),
        "nexus_token": str(config.SWAP_PAIR.nexus.register_address),
        "nexus_account": "nexus-recipient",
        "output_txid": "nexus-output-txid",
        "output_contract_id": 7,
        "output_units": 1_898_000,
        "reference": 77,
    }


def test_manual_review_receipt_rejects_boolean_integer_evidence_atomically(db):
    seed_confirmable_deposit()
    evidence = {
        "source_signature": "solana-signature-full",
        "solana_mint": "sol-mint",
        "solana_vault": "sol-vault",
        "nexus_token": "nexus-token-register",
        "nexus_account": "nexus-recipient",
        "output_txid": "nexus-output-txid",
        "output_contract_id": True,
        "output_units": 1_898_000,
        "reference": 77,
    }
    with pytest.raises(ValueError, match="exact confirmed payout"):
        state_db.finalize_confirmed_solana_payout(
            sig="solana-signature-full", timestamp=100,
            amount_solana_units=2_000_000, output_txid="nexus-output-txid",
            output_units=1_898_000, nexus_destination="nexus-recipient",
            memo="nexus:nexus-recipient", reference=77, output_contract_id=1,
            fee_solana_units=102_000, receipt_evidence=evidence,
            receipt_error="ValueError: injected", receipt_name=swap_receipts.receipt_name(
                "solana-signature-full"
            ),
        )

    assert state_db.is_unprocessed_sig("solana-signature-full")
    assert not state_db.is_processed_sig("solana-signature-full")
    assert state_db.get_swap_receipt("solana-signature-full") is None


@pytest.mark.parametrize("field", ["output_units", "output_contract_id", "reference"])
def test_overflowing_json_receipt_number_is_held_without_blocking_batch(db, monkeypatch, field):
    bad = receipt_fields("overflowing-json-number")
    good = receipt_fields("later-valid-receipt")
    for payload in (bad, good):
        state_db.enqueue_swap_receipt(
            payload, swap_receipts.receipt_name(payload["source_signature"])
        )
    encoded = json.dumps(bad, separators=(",", ":"))
    encoded = encoded.replace(f'"{field}":"{bad[field]}"', f'"{field}":1e999')
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE swap_receipts SET payload_json=? WHERE source_signature=?",
            (encoded, bad["source_signature"]),
        )
    monkeypatch.setattr(
        swap_receipts, "receipt_provider_registration", lambda: ("provider-genesis", "ok")
    )
    creates = []
    monkeypatch.setattr(swap_receipts, "_create", lambda row, payload: creates.append(payload))
    monkeypatch.setattr(swap_receipts, "_readback", lambda row, payload: "exact-address")

    assert swap_receipts.publish_pending_receipts() == 1
    held = state_db.get_swap_receipt(bad["source_signature"])
    assert held is not None
    assert held["status"] == "manual_review"
    assert held["manual_review_error"]
    assert creates == [good]
    published = state_db.get_swap_receipt(good["source_signature"])
    assert published is not None
    assert published["status"] == "published"


def test_malformed_stored_receipt_transitions_to_explicit_manual_review(db, monkeypatch):
    payload = receipt_fields("malformed-durable-payload")
    source = payload["source_signature"]
    enqueue_bound_receipt(payload)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE swap_receipts SET payload_json=? WHERE source_signature=?",
            ('{"output_units":"not-an-integer"}', source),
        )
    calls = []
    monkeypatch.setattr(nexus_client, "read_service_record", receipt_capable_provider_record)
    monkeypatch.setattr(nexus_client, "_run", lambda command, timeout=None: calls.append(command))

    assert swap_receipts.publish_pending_receipts() == 0
    saved = state_db.get_swap_receipt(source)
    assert saved is not None
    assert saved["status"] == "manual_review"
    assert saved["manual_review_error"]
    assert calls == []


def test_receipt_batch_validates_all_rows_and_registers_provider_once(db, monkeypatch):
    valid = receipt_fields("batch-valid")
    malformed = receipt_fields("batch-malformed")
    for payload in (valid, malformed):
        enqueue_bound_receipt(payload)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE swap_receipts SET payload_json='{}', created_timestamp=2 "
            "WHERE source_signature=?",
            (malformed["source_signature"],),
        )
        conn.execute(
            "UPDATE swap_receipts SET created_timestamp=1 WHERE source_signature=?",
            (valid["source_signature"],),
        )

    registration_calls = []
    monkeypatch.setattr(
        nexus_client, "read_service_record",
        lambda: (registration_calls.append(True) or receipt_capable_provider_record()),
    )
    creates = []

    def run(command, timeout=None):
        if command[1] == "assets/create/asset":
            creates.append(command)
            malformed_row = state_db.get_swap_receipt(malformed["source_signature"])
            assert malformed_row is not None
            assert malformed_row["status"] == "manual_review"
            return (1, "", "rejected")
        return (0, "[]", "")

    monkeypatch.setattr(nexus_client, "_run", run)

    assert swap_receipts.publish_pending_receipts() == 0
    assert len(registration_calls) == 1
    assert len(creates) == 1
    malformed_saved = state_db.get_swap_receipt(malformed["source_signature"])
    assert malformed_saved is not None
    assert malformed_saved["status"] == "manual_review"


def test_receipt_capable_provider_registration_is_loaded_once_for_multirow_batch(db, monkeypatch):
    payloads = [receipt_fields("batch-one"), receipt_fields("batch-two")]
    for payload in payloads:
        enqueue_bound_receipt(payload)
    registration_calls = []
    monkeypatch.setattr(
        nexus_client, "read_service_record",
        lambda: (registration_calls.append(True) or receipt_capable_provider_record()),
    )
    monkeypatch.setattr(
        nexus_client, "_run",
        lambda command, timeout=None: (1, "", "create rejected")
        if command[1] == "assets/create/asset" else (0, "[]", ""),
    )

    assert swap_receipts.publish_pending_receipts() == 0
    assert len(registration_calls) == 1


def test_receipt_contract_is_neutral_single_source_for_schema_fields_and_name():
    from src import receipt_contract

    assert swap_receipts.SCHEMA == receipt_contract.SCHEMA
    assert swap_receipts.DISTORDIA_TYPE == receipt_contract.DISTORDIA_TYPE
    assert swap_receipts.REQUIRED_FIELDS == receipt_contract.REQUIRED_FIELDS
    assert swap_receipts.receipt_name("source") == receipt_contract.receipt_name("source")
