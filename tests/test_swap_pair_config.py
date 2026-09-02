"""Regression coverage for Batch 7's canonical pair-configuration foundation."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
import os
import sys
import types

import pytest


def _load_config(monkeypatch: pytest.MonkeyPatch, **values: str):
    """Load ``src.config`` under a minimal isolated operator environment."""
    for key in list(os.environ):
        if key.startswith((
            "SOLANA_", "NEXUS_", "USDC", "USDD", "SOL_", "FLAT_", "DYNAMIC_",
            "FEE_", "MIN_", "DEPOSIT_", "SERVICE_", "VAULT_",
        )):
            monkeypatch.delenv(key, raising=False)

    dotenv = types.ModuleType("dotenv")
    setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    solders = types.ModuleType("solders")
    solders_pubkey = types.ModuleType("solders.pubkey")

    class PublicKey:
        @staticmethod
        def from_string(value: str) -> str:
            return value

    setattr(solders_pubkey, "Pubkey", PublicKey)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    monkeypatch.setitem(sys.modules, "solders", solders)
    monkeypatch.setitem(sys.modules, "solders.pubkey", solders_pubkey)
    monkeypatch.delitem(sys.modules, "src.config", raising=False)

    baseline = {
        "SOLANA_RPC_URL": "https://rpc.example.invalid",
        "VAULT_KEYPAIR": "/tmp/vault.json",
        "SOLANA_VAULT_ACCOUNT": "solana-vault",
        "SOLANA_TOKEN_MINT": "solana-mint",
        "SOL_MAIN_ACCOUNT": "solana-owner",
        "NEXUS_PIN": "test-pin",
        "NEXUS_TREASURY_ACCOUNT": "nexus-treasury",
    }
    baseline.update(values)
    for key, value in baseline.items():
        monkeypatch.setenv(key, value)

    return importlib.import_module("src.config")


def test_canonical_swap_pair_groups_exact_identities_custody_and_fee_terms(monkeypatch):
    config = _load_config(
        monkeypatch,
        SOLANA_TOKEN_SYMBOL="wBTC",
        SOLANA_TOKEN_DECIMALS="8",
        NEXUS_TOKEN_NAME="BTCn",
        NEXUS_TOKEN_REGISTER_ADDRESS="nexus-token-register",
        NEXUS_TOKEN_DECIMALS="8",
        SOLANA_QUARANTINE_ACCOUNT="solana-quarantine",
        SOLANA_FEE_ACCOUNT="solana-fee",
        NEXUS_QUARANTINE_ACCOUNT="nexus-quarantine",
        NEXUS_FEE_ACCOUNT="nexus-fee",
        FEE_FLAT_TO_NEXUS="0.0001",
        FEE_FLAT_TO_SOLANA="0.0002",
        FEE_REFUND_SOLANA="0.0003",
        FEE_BPS="15",
    )

    pair = config.SWAP_PAIR
    assert pair.solana.mint == "solana-mint"
    assert pair.solana.vault_account == "solana-vault"
    assert pair.solana.quarantine_account == "solana-quarantine"
    assert pair.nexus.register_address == "nexus-token-register"
    assert pair.nexus.treasury_account == "nexus-treasury"
    assert pair.nexus.quarantine_account == "nexus-quarantine"
    assert pair.fees.flat_to_nexus_units == 10_000
    assert pair.fees.flat_to_solana_units == 20_000
    assert pair.fees.refund_solana_units == 30_000
    assert pair.fees.basis_points == 15

    with pytest.raises(FrozenInstanceError):
        pair.fees.basis_points = 20


def test_canonical_fee_and_legacy_alias_conflict_fails_closed(monkeypatch):
    with pytest.raises(ValueError, match=r"FEE_FLAT_TO_NEXUS.*FLAT_FEE_USDD"):
        _load_config(
            monkeypatch,
            FEE_FLAT_TO_NEXUS="0.1",
            FLAT_FEE_USDD="0.2",
        )
