"""Stable offline environment shared by the composable pytest suite.

Individual legacy modules use ``setdefault`` at import time.  Establish one canonical
fixture before collection so module order and a developer's local ``.env`` cannot change
the token/custody identities exercised by tests.
"""
from __future__ import annotations

import os

_OFFLINE_ENV = {
    "SOLANA_RPC_URL": "http://127.0.0.1:1",
    "VAULT_KEYPAIR": "/nonexistent/offline-keypair.json",
    "VAULT_USDC_ACCOUNT": "11111111111111111111111111111111",
    "USDC_MINT": "11111111111111111111111111111111",
    "SOL_MAIN_ACCOUNT": "11111111111111111111111111111111",
    "NEXUS_PIN": "offline-fixture-not-a-credential",
    "NEXUS_USDD_TREASURY_ACCOUNT": "TREASURY",
    "NEXUS_TOKEN_REGISTER_ADDRESS": "TOKEN-REGISTER",
    "NEXUS_CLI_PATH": "/bin/false",
}

os.environ.update(_OFFLINE_ENV)
