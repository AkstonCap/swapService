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
    "CUSTODY_SOLANA_GENESIS_HASH": "11111111111111111111111111111111",
    "CUSTODY_NEXUS_GENESIS_HASH": "a" * 256,
}

os.environ.update(_OFFLINE_ENV)


import pytest


@pytest.fixture
def running_custody(monkeypatch, tmp_path):
    """Issue a test-only independent permit for the current closed fixture image."""
    from src import custody_admission as admission, custody_witness as witness, state_db
    from pathlib import Path
    import secrets
    def admit(*, ready=False):
        token, admin = secrets.token_hex(32), secrets.token_hex(32)
        store = witness.Store(tmp_path / 'independent-witness.db', runtime_token=token,
                              admin_token=admin)
        cert = witness.Certificate.from_dict({
            'deployment_id': 'offline-test-deployment', 'generation': 0,
            'permit_nonce': secrets.token_hex(32),
            **admission.inspect_image(state_db.DB_PATH),
            'config_sha256': admission.configuration_fingerprint(),
            'build_sha256': admission.build_fingerprint(),
            'issued_at': 1,
            'approval_rationale': 'independently approved offline fixture only',
        })
        store.issue_initial(cert, actor='offline-independent-reviewer',
                            rationale='test fixture only', admin_token=admin)
        client = witness.StoreClient(store, token)
        monkeypatch.setenv('CUSTODY_WITNESS_URL', 'https://offline-witness.invalid')
        monkeypatch.setenv('CUSTODY_WITNESS_TOKEN', token)
        monkeypatch.setenv('CUSTODY_DEPLOYMENT_ID', cert.deployment_id)
        monkeypatch.setattr(admission, 'Client', lambda *args: client)
        if ready:
            return client
        lease = admission.claim()
        lease.complete()
        return lease
    return admit
