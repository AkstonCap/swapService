"""Canonical Provider Asset Standard v2 contract tests."""
from dataclasses import replace

import pytest


def _configure_v2(monkeypatch):
    from src import config

    pair = replace(
        config.SWAP_PAIR,
        solana=replace(
            config.SWAP_PAIR.solana,
            mint="SOL-MINT",
            symbol="S8",
            decimals=8,
            vault_account="SOL-VAULT",
            quarantine_account="SOL-QUARANTINE",
            fee_account="SOL-FEES",
        ),
        nexus=replace(
            config.SWAP_PAIR.nexus,
            register_address="NXS-TOKEN",
            symbol="N6",
            decimals=6,
            treasury_account="NXS-TREASURY",
            quarantine_account="NXS-QUARANTINE",
            fee_account="NXS-FEES",
        ),
        fees=replace(
            config.SWAP_PAIR.fees,
            flat_to_nexus_units=123_456,
            flat_to_solana_units=50_000_001,
            refund_solana_units=10_000_000,
            nexus_disposition_units=1,
            basis_points=17,
        ),
        deposit_memo_prefix="nexus-v2:",
    )
    values = {
        "SWAP_PAIR": pair,
        "NEXUS_SERVICE_ASSET_ADDRESS": "SERVICE-ASSET",
        "NEXUS_SERVICE_EXPECTED_OWNER": "OWNER",
        "SERVICE_ID": "provider:pair:one",
        "NEXUS_NETWORK": "mainnet",
        "SOLANA_NETWORK": "devnet",
        "RPC_URL": "https://api.devnet.solana.com",
        "SERVICE_PROVIDER": "provider",
        "SERVICE_CONTACT": "https://contact.invalid",
        "SERVICE_SOURCE_URL": "https://source.invalid/release",
        "SERVICE_TERMS_URL": "https://terms.invalid/v7",
        "SERVICE_VERSION": "7.2.1",
        "SERVICE_TERMS_VERSION": 7,
        "SERVICE_TERMS_EFFECTIVE_AT": 1_700_000_000,
        "MIN_DEPOSIT_SOLANA_UNITS": 20_000_001,
        "MIN_CREDIT_NEXUS_UNITS": 1_000_001,
        "DUST_CREDIT_NEXUS_UNITS": 10_001,
        "MAX_SWAP_SOLANA_UNITS": 100_000_000_001,
        "MAX_SWAP_NEXUS_UNITS": 2_000_001,
        "DAILY_PAYOUT_CAP_SOLANA_UNITS": 500_000_000_001,
        "MICRO_DEPOSIT_FEE_PCT": 99,
        "MICRO_CREDIT_FEE_PCT": 98,
        "NEXUS_SWAP_RECEIPTS_ENABLED": False,
    }
    for name, value in values.items():
        monkeypatch.setattr(config, name, value, raising=False)
    return pair


def test_provider_v2_contract_is_the_default_complete_schema():
    from src import service_record

    assert service_record.DISTORDIA_TYPE == "swapService"
    assert service_record.SCHEMA_VERSION == "2"
    assert "service_id" in service_record.IMMUTABLE_FIELDS
    assert "terms_hash" in service_record.MUTABLE_FIELDS
    assert "receipt_schema" in service_record.OPTIONAL_IMMUTABLE_FIELDS
    assert set(service_record.REQUIRED_FIELDS) == (
        set(service_record.IMMUTABLE_FIELDS) | set(service_record.MUTABLE_FIELDS)
    )


def test_provider_v2_config_defaults_and_explicit_environment(monkeypatch):
    import importlib

    from src import config

    names = {
        "ALLOW_LEGACY_PROVIDER_V1": "true",
        "NEXUS_SERVICE_ASSET_ADDRESS": "SERVICE-ASSET",
        "NEXUS_SERVICE_EXPECTED_OWNER": "OWNER",
        "SERVICE_ID": "provider:pair:one",
        "NEXUS_NETWORK": "devnet",
        "SERVICE_SOURCE_URL": "https://source.invalid/release",
        "SERVICE_TERMS_URL": "https://terms.invalid/v3",
        "SERVICE_TERMS_VERSION": "3",
        "SERVICE_TERMS_EFFECTIVE_AT": "1700000000",
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    try:
        loaded = importlib.reload(config)
        assert loaded.PROVIDER_RECORD_SCHEMA_VERSION == "2"
        assert loaded.ALLOW_LEGACY_PROVIDER_V1 is True
        assert loaded.NEXUS_SERVICE_ASSET_ADDRESS == "SERVICE-ASSET"
        assert loaded.NEXUS_SERVICE_EXPECTED_OWNER == "OWNER"
        assert loaded.SERVICE_ID == "provider:pair:one"
        assert loaded.NEXUS_NETWORK == "devnet"
        assert loaded.SERVICE_SOURCE_URL == "https://source.invalid/release"
        assert loaded.SERVICE_TERMS_URL == "https://terms.invalid/v3"
        assert loaded.SERVICE_TERMS_VERSION == 3
        assert loaded.SERVICE_TERMS_EFFECTIVE_AT == 1_700_000_000
    finally:
        for name in names:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(config)


def test_provider_v1_fallback_boolean_rejects_typos(monkeypatch):
    import importlib

    from src import config

    monkeypatch.setenv("ALLOW_LEGACY_PROVIDER_V1", "treu")
    try:
        with __import__("pytest").raises(ValueError, match="ALLOW_LEGACY_PROVIDER_V1.*treu"):
            importlib.reload(config)
    finally:
        monkeypatch.delenv("ALLOW_LEGACY_PROVIDER_V1", raising=False)
        importlib.reload(config)


def test_build_record_emits_complete_all_string_effective_terms_with_unequal_decimals(
    monkeypatch,
):
    from src import service_record

    _configure_v2(monkeypatch)
    monkeypatch.setattr(service_record.time, "time", lambda: 1_700_000_999)

    record = service_record.build_record(
        status="paused", last_poll=1_700_000_100, wline_sol=101, wline_nxs=202
    )

    assert set(record) == set(service_record.REQUIRED_FIELDS)
    assert all(type(value) is str for value in record.values())
    expected = {
        "owner": "OWNER",
        "address": "SERVICE-ASSET",
        "distordia-type": "swapService",
        "schema_version": "2",
        "service_id": "provider:pair:one",
        "nexus_network": "mainnet",
        "nexus_token_name": "N6",
        "nexus_token_address": "NXS-TOKEN",
        "nexus_token_decimals": "6",
        "nexus_treasury_address": "NXS-TREASURY",
        "nexus_quarantine_address": "NXS-QUARANTINE",
        "nexus_fee_address": "NXS-FEES",
        "solana_cluster": "devnet",
        "solana_token_symbol": "S8",
        "solana_token_mint": "SOL-MINT",
        "solana_token_decimals": "8",
        "solana_vault_address": "SOL-VAULT",
        "solana_quarantine_address": "SOL-QUARANTINE",
        "solana_fee_address": "SOL-FEES",
        "enabled_directions": "solana-to-nexus,nexus-to-solana",
        "user_mapping_type": "nexusBridge",
        "provider": "provider",
        "contact": "https://contact.invalid",
        "source_url": "https://source.invalid/release",
        "terms_url": "https://terms.invalid/v7",
        "software_version": "7.2.1",
        "memo_prefix": "nexus-v2:",
        "mapping_schema_version": "1",
        "fee_flat_to_nexus": "0.123456",
        "fee_bps_to_nexus": "17",
        "fee_flat_to_solana": "0.50000001",
        "fee_bps_to_solana": "17",
        "fee_refund_solana": "0.1",
        "fee_nexus_disposition": "0.000001",
        "micro_fee_pct_solana_input": "99",
        "micro_fee_pct_nexus_input": "98",
        "min_input_solana": "0.20000001",
        "dust_input_solana": "0",
        "min_input_nexus": "1.000001",
        "dust_input_nexus": "0.010001",
        "max_input_solana": "1000.00000001",
        "max_input_nexus": "2.000001",
        "daily_payout_cap_solana": "5000.00000001",
        "terms_version": "7",
        "terms_effective_at": "1700000000",
        "status": "paused",
        "pause_reason": "-",
        "last_poll_timestamp": "1700000100",
        "last_safe_timestamp_solana": "101",
        "last_safe_timestamp_nexus": "202",
        "record_updated_at": "1700000999",
    }
    assert record.items() >= expected.items()
    assert record["terms_hash"] == service_record.terms_hash(record)
    assert service_record.validate_record(record) is None


def test_terms_hash_rejects_noncanonical_all_string_infinity(monkeypatch):
    from src import service_record

    _configure_v2(monkeypatch)
    record = service_record.build_record(last_poll=1, wline_sol=2, wline_nxs=3)
    record["max_input_solana"] = "Infinity"

    with pytest.raises(ValueError, match="max_input_solana"):
        service_record.terms_hash(record)


def test_build_rejects_non_boolean_receipt_extension_flag(monkeypatch):
    from src import config, service_record

    _configure_v2(monkeypatch)
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPTS_ENABLED", "false")

    with pytest.raises(ValueError, match="NEXUS_SWAP_RECEIPTS_ENABLED.*boolean"):
        service_record.build_record(last_poll=1)


def test_micro_fee_percentages_cannot_exceed_one_hundred(monkeypatch):
    from src import service_record

    _configure_v2(monkeypatch)
    record = service_record.build_record(last_poll=1)
    record["micro_fee_pct_solana_input"] = "101"

    with pytest.raises(ValueError, match="micro_fee_pct_solana_input"):
        service_record.terms_hash(record)


def test_fingerprint_excludes_liveness_but_includes_policy_and_identity(monkeypatch):
    from src import service_record

    _configure_v2(monkeypatch)
    original = service_record.build_record(last_poll=10, wline_sol=20, wline_nxs=30)
    original_hash = original["terms_hash"]

    liveness = dict(original)
    liveness.update(
        status="maintenance",
        pause_reason="upgrade",
        last_poll_timestamp="11",
        last_safe_timestamp_solana="21",
        last_safe_timestamp_nexus="31",
        record_updated_at="40",
    )
    assert service_record.terms_hash(liveness) == original_hash
    service_record.validate_record(liveness)

    for field, changed in (
        ("service_id", "another-instance"),
        ("nexus_token_address", "ANOTHER-NXS-TOKEN"),
        ("fee_flat_to_solana", "0.50000002"),
        ("memo_prefix", "other:"),
    ):
        modified = dict(original)
        modified[field] = changed
        assert service_record.terms_hash(modified) != original_hash


def test_terms_hash_uses_sorted_compact_utf8_sha256(monkeypatch):
    import hashlib
    import json

    from src import service_record

    _configure_v2(monkeypatch)
    record = service_record.build_record(last_poll=1)
    projected = {field: record[field] for field in service_record.TERMS_HASH_FIELDS}
    encoded = json.dumps(
        projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert record["terms_hash"] == "sha256:" + hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("last_poll_timestamp", 1, "all be strings"),
        ("last_poll_timestamp", "-1", "last_poll_timestamp"),
        ("last_poll_timestamp", "01", "last_poll_timestamp"),
        ("terms_version", "1.0", "terms_version"),
        ("terms_effective_at", "Infinity", "terms_effective_at"),
        ("status", "ONLINE", "status"),
    ],
)
def test_validation_rejects_wrong_wire_types_revisions_timestamps_and_status(
    monkeypatch, field, bad_value, message
):
    from src import service_record

    _configure_v2(monkeypatch)
    record = service_record.build_record(last_poll=1)
    record[field] = bad_value
    with pytest.raises(ValueError, match=message):
        service_record.validate_record(record, require_config_match=False)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("fee_flat_to_nexus", "0.1234560"),
        ("fee_flat_to_nexus", "0.1234567"),
        ("fee_flat_to_solana", "5e-1"),
        ("min_input_solana", "00.2"),
        ("dust_input_nexus", "-0.01"),
        ("max_input_nexus", "NaN"),
    ],
)
def test_validation_rejects_noncanonical_or_unrepresentable_amounts(
    monkeypatch, field, bad_value
):
    from src import service_record

    _configure_v2(monkeypatch)
    record = service_record.build_record(last_poll=1)
    record[field] = bad_value
    with pytest.raises(ValueError, match=field):
        service_record.terms_hash(record)


def test_validation_rejects_missing_extra_and_tampered_fields(monkeypatch):
    from src import service_record

    _configure_v2(monkeypatch)
    canonical = service_record.build_record(last_poll=1)

    missing = dict(canonical)
    del missing["daily_payout_cap_solana"]
    with pytest.raises(ValueError, match="missing=.*daily_payout_cap_solana"):
        service_record.validate_record(missing, require_config_match=False)

    extra = dict(canonical, NEXUS_PIN="not-even-a-real-pin")
    with pytest.raises(ValueError, match="extra=.*NEXUS_PIN"):
        service_record.validate_record(extra, require_config_match=False)

    tampered = dict(canonical, fee_bps_to_nexus="18")
    with pytest.raises(ValueError, match="terms_hash mismatch"):
        service_record.validate_record(tampered, require_config_match=False)


def test_structural_inspection_is_read_only_and_runtime_match_is_fail_closed(monkeypatch):
    from src import service_record

    _configure_v2(monkeypatch)
    other = service_record.build_record(last_poll=1)
    other["owner"] = "OTHER-OWNER"
    other["address"] = "OTHER-ASSET"
    other["service_id"] = "other:pair"
    other["nexus_token_address"] = "OTHER-TOKEN"
    other["nexus_treasury_address"] = "OTHER-TREASURY"
    other["terms_hash"] = service_record.terms_hash(other)

    assert service_record.validate_record(other, require_config_match=False) is None
    with pytest.raises(ValueError, match="does not match config"):
        service_record.validate_record(other)
    with pytest.raises(ValueError, match="expected_address"):
        service_record.validate_record(
            other, expected_address="SERVICE-ASSET", require_config_match=False
        )


def test_runtime_rejects_public_metadata_mismatch_but_explicit_terms_update_can_inspect(
    monkeypatch,
):
    from src import service_record

    _configure_v2(monkeypatch)
    changed = service_record.build_record(last_poll=1)
    changed["provider"] = "new-provider"
    changed["fee_flat_to_nexus"] = "0.123455"
    changed["terms_version"] = "8"
    changed["terms_hash"] = service_record.terms_hash(changed)

    with pytest.raises(ValueError, match="terms field|metadata field"):
        service_record.validate_record(changed)
    assert service_record.validate_record(changed, require_terms_match=False) is None

    changed["solana_vault_address"] = "OTHER-VAULT"
    changed["terms_hash"] = service_record.terms_hash(changed)
    with pytest.raises(ValueError, match="immutable field solana_vault_address"):
        service_record.validate_record(changed, require_terms_match=False)


def test_onchain_validation_requires_owner_and_address_metadata(monkeypatch):
    from src import service_record

    _configure_v2(monkeypatch)
    record = service_record.build_record(last_poll=1)
    for field in ("owner", "address"):
        missing = dict(record)
        del missing[field]
        with pytest.raises(ValueError, match=field):
            service_record.validate_record(missing, require_config_match=False)


def test_receipt_schema_is_the_only_fixed_optional_extension(monkeypatch):
    from src import config, service_record
    from src.receipt_contract import SCHEMA

    _configure_v2(monkeypatch)
    monkeypatch.setattr(config, "NEXUS_SWAP_RECEIPTS_ENABLED", True)
    record = service_record.build_record(last_poll=1)
    assert record["receipt_schema"] == SCHEMA
    assert record["schema_version"] == "2"
    service_record.validate_record(record)

    wrong = dict(record, receipt_schema="nexus-swap-receipt-v2")
    with pytest.raises(ValueError, match="receipt_schema"):
        service_record.validate_record(wrong, require_config_match=False)


def test_builder_requires_explicit_registration_identity_only_when_called(monkeypatch):
    from src import config, service_record

    _configure_v2(monkeypatch)
    for field in (
        "NEXUS_SERVICE_ASSET_ADDRESS",
        "NEXUS_SERVICE_EXPECTED_OWNER",
        "SERVICE_ID",
        "NEXUS_NETWORK",
    ):
        prior = getattr(config, field)
        monkeypatch.setattr(config, field, "")
        with pytest.raises(ValueError):
            service_record.build_record(last_poll=1)
        monkeypatch.setattr(config, field, prior)


def test_v2_uses_only_canonical_waterline_names_and_never_publicizes_config_dict(
    monkeypatch,
):
    from src import config, service_record

    _configure_v2(monkeypatch)
    monkeypatch.setattr(config, "HEARTBEAT_WATERLINE_SOLANA_FIELD", "custom_sol")
    monkeypatch.setattr(config, "HEARTBEAT_WATERLINE_NEXUS_FIELD", "custom_nxs")
    monkeypatch.setattr(config, "ARBITRARY_CONFIG", {"password": "secret"}, raising=False)
    record = service_record.build_record(last_poll=1, wline_sol=2, wline_nxs=3)

    assert record["last_safe_timestamp_solana"] == "2"
    assert record["last_safe_timestamp_nexus"] == "3"
    assert "custom_sol" not in record and "custom_nxs" not in record
    assert "ARBITRARY_CONFIG" not in record
    assert "password" not in "".join(record)


def test_builder_rejects_secret_value_collision_in_public_metadata(monkeypatch):
    from src import config, service_record

    _configure_v2(monkeypatch)
    monkeypatch.setattr(config, "NEXUS_API_PASSWORD", "leaked-password")
    monkeypatch.setattr(config, "SERVICE_CONTACT", "leaked-password")
    with pytest.raises(ValueError, match="must not expose.*NEXUS_API_PASSWORD"):
        service_record.build_record(last_poll=1)
