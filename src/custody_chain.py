"""Fail-closed admission against independently pinned immutable chain identities."""
from __future__ import annotations

import os

import requests
from solders.hash import Hash

from . import config, nexus_client
from .custody_admission import AdmissionError


_HEX = frozenset("0123456789abcdef")


def _expected_identities() -> tuple[str, str]:
    solana = os.getenv("CUSTODY_SOLANA_GENESIS_HASH")
    nexus = os.getenv("CUSTODY_NEXUS_GENESIS_HASH")
    if not solana or not nexus:
        raise AdmissionError("custody chain identity configuration missing")
    try:
        valid_solana = solana == solana.strip() and str(Hash.from_string(solana)) == solana
    except Exception:
        valid_solana = False
    valid_nexus = (
        nexus == nexus.strip()
        and len(nexus) == 256
        and all(character in _HEX for character in nexus)
    )
    if not valid_solana or not valid_nexus:
        raise AdmissionError("custody chain identity configuration invalid")
    return solana, nexus


def _solana_genesis_hash() -> str:
    try:
        response = requests.post(
            config.RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": "custody-chain-admission",
                "method": "getGenesisHash",
            },
            timeout=config.SOLANA_RPC_TIMEOUT_SEC,
        )
        response.raise_for_status()
        body = response.json()
        observed = body.get("result") if isinstance(body, dict) and "error" not in body else None
        if type(observed) is not str or str(Hash.from_string(observed)) != observed:
            raise ValueError
        return observed
    except Exception as exc:
        raise AdmissionError("Solana custody chain identity query failed") from exc


def _nexus_height_zero_hash() -> str:
    try:
        code, output, _error = nexus_client._run(
            [config.NEXUS_CLI, "ledger/get/blockhash", "height=0"],
            timeout=config.NEXUS_CLI_TIMEOUT_SEC,
        )
        if code != 0:
            raise ValueError
        body = nexus_client._parse_json_lenient(output)
        if not isinstance(body, dict) or set(body) != {"hash"}:
            raise ValueError
        observed = body["hash"]
        if (type(observed) is not str or len(observed) != 256
                or any(character not in _HEX for character in observed)):
            raise ValueError
        return observed
    except Exception as exc:
        raise AdmissionError("Nexus custody chain identity query failed") from exc


def verify() -> None:
    """Require both configured endpoints to report the independently pinned chains."""
    expected_solana, expected_nexus = _expected_identities()
    observed_solana = _solana_genesis_hash()
    observed_nexus = _nexus_height_zero_hash()
    if observed_solana != expected_solana or observed_nexus != expected_nexus:
        raise AdmissionError("custody chain identity does not match approved deployment")
