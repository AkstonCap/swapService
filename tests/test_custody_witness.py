"""Independent one-use sealed-image witness tests."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.custody_witness import (
    Certificate,
    Client,
    HTTPApplication,
    Store,
    StoreClient,
    WitnessError,
    build_cli_parser,
    loads_object,
    make_http_server,
    run_cli,
)

RUNTIME = "r" * 48
ADMIN = "a" * 48


def certificate(*, generation: int = 7, nonce: str = "1" * 64) -> Certificate:
    return Certificate.from_dict({
        "deployment_id": "bridge-production",
        "generation": generation,
        "permit_nonce": nonce,
        "image_sha256": "2" * 64,
        "image_size": 4096,
        "config_sha256": "3" * 64,
        "build_sha256": "4" * 64,
        "schema_sha256": "5" * 64,
        "issued_at": 1_800_000_000,
        "approval_rationale": "reviewed coherent custody backup",
    })


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "independent-witness.db", runtime_token=RUNTIME, admin_token=ADMIN)


def test_certificate_requires_exact_fields_and_strict_scalar_types():
    valid = certificate().to_dict()
    for key in tuple(valid):
        malformed = dict(valid)
        malformed.pop(key)
        with pytest.raises(WitnessError, match="invalid certificate"):
            Certificate.from_dict(malformed)
    with pytest.raises(WitnessError, match="invalid certificate"):
        Certificate.from_dict({**valid, "extra": "not allowed"})
    for key, value in (("generation", True), ("image_size", "4096"),
                       ("issued_at", 1.5), ("permit_nonce", "A" * 64)):
        with pytest.raises(WitnessError, match="invalid certificate"):
            Certificate.from_dict({**valid, key: value})


def test_json_loader_rejects_duplicate_keys_and_non_object_payloads():
    with pytest.raises(WitnessError, match="invalid JSON"):
        loads_object('{"generation":1,"generation":2}')
    with pytest.raises(WitnessError, match="invalid JSON"):
        loads_object("[]")


def test_initial_issue_requires_distinct_admin_capability_and_is_one_time(store):
    cert = certificate()
    with pytest.raises(WitnessError, match="unauthorized"):
        store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=RUNTIME)
    head = store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)
    assert head["status"] == "ready"
    assert head["certificate"] == cert.to_dict()
    with pytest.raises(WitnessError, match="already exists"):
        store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)


def test_runtime_transition_consumes_permit_and_only_owned_running_can_seal(store):
    cert = certificate()
    store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)
    client = StoreClient(store, RUNTIME)
    claim_nonce = "6" * 64

    claimed = client.claim("bridge-production", 7, cert.permit_nonce, claim_nonce)
    assert claimed["status"] == "claimed"
    assert client.claim("bridge-production", 7, cert.permit_nonce, claim_nonce) == claimed
    with pytest.raises(WitnessError, match="stale or consumed"):
        client.claim("bridge-production", 7, cert.permit_nonce, "7" * 64)

    running = client.complete("bridge-production", 7, claim_nonce)
    assert running["status"] == "running"
    next_cert = certificate(generation=8, nonce="8" * 64)
    ready = client.seal("bridge-production", 7, claim_nonce, next_cert.to_dict())
    assert ready["status"] == "ready"
    assert ready["certificate"] == next_cert.to_dict()
    with pytest.raises(WitnessError, match="stale or consumed"):
        client.claim("bridge-production", 7, cert.permit_nonce, "9" * 64)


def test_stale_generation_certificate_or_claim_owner_is_refused(store):
    cert = certificate()
    store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)
    client = StoreClient(store, RUNTIME)
    with pytest.raises(WitnessError, match="stale or consumed"):
        client.claim("bridge-production", 6, cert.permit_nonce, "6" * 64)
    client.claim("bridge-production", 7, cert.permit_nonce, "6" * 64)
    with pytest.raises(WitnessError, match="claim ownership"):
        client.complete("bridge-production", 7, "7" * 64)
    with pytest.raises(WitnessError, match="next generation"):
        client.seal("bridge-production", 7, "6" * 64,
                    certificate(generation=9, nonce="9" * 64).to_dict())


def test_hold_is_permanent_and_cannot_be_reissued(store):
    cert = certificate()
    store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)
    client = StoreClient(store, RUNTIME)
    client.claim("bridge-production", 7, cert.permit_nonce, "6" * 64)
    held = client.hold("bridge-production", 7, "6" * 64, "ambiguous completion")
    assert held["status"] == "held"
    assert client.hold("bridge-production", 7, "6" * 64, "ambiguous completion") == held
    with pytest.raises(WitnessError, match="permanently held"):
        client.complete("bridge-production", 7, "6" * 64)
    with pytest.raises(WitnessError, match="already exists"):
        store.issue_initial(certificate(generation=8), actor="reviewer",
                            rationale="replace hold", admin_token=ADMIN)


def test_events_are_append_only_and_hash_linked(store):
    cert = certificate()
    store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)
    client = StoreClient(store, RUNTIME)
    client.claim("bridge-production", 7, cert.permit_nonce, "6" * 64)
    client.complete("bridge-production", 7, "6" * 64)

    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT sequence, prev_hash, event_hash, event_json FROM events ORDER BY sequence"
        ).fetchall()
    assert [row[0] for row in rows] == [1, 2, 3]
    assert rows[0][1] == "0" * 64
    assert rows[1][1] == rows[0][2]
    assert rows[2][1] == rows[1][2]
    assert [json.loads(row[3])["action"] for row in rows] == ["issue", "claim", "complete"]


def test_tokens_must_be_strong_distinct_and_errors_do_not_echo_them(tmp_path):
    with pytest.raises(WitnessError, match="strong distinct"):
        Store(tmp_path / "bad.db", runtime_token="short", admin_token=ADMIN)
    with pytest.raises(WitnessError, match="strong distinct"):
        Store(tmp_path / "bad.db", runtime_token=RUNTIME, admin_token=RUNTIME)
    store = Store(tmp_path / "ok.db", runtime_token=RUNTIME, admin_token=ADMIN)
    with pytest.raises(WitnessError) as excinfo:
        store.get_head("bridge-production", token="secret-that-must-not-leak")
    assert "secret-that-must-not-leak" not in str(excinfo.value)


def test_runtime_server_store_does_not_require_or_retain_admin_capability(tmp_path):
    store = Store(tmp_path / "runtime.db", runtime_token=RUNTIME, admin_token=None)
    with pytest.raises(WitnessError, match="unauthorized"):
        store.issue_initial(certificate(), actor="runtime", rationale="must fail",
                            admin_token=ADMIN)


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self.content = body
        self.headers = {"Content-Length": str(len(body))}

    def iter_content(self, chunk_size=1):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        pass


class _Session:
    def __init__(self, application):
        self.application = application
        self.calls = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = "/" + url.split("/", 3)[3]
        status, _headers, body = self.application.dispatch(
            method, path, kwargs.get("headers", {}), kwargs.get("data", b"")
        )
        return _Response(status, body)


def test_https_client_uses_authenticated_no_redirect_bounded_requests(store):
    cert = certificate()
    store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)
    session = _Session(HTTPApplication(store, max_body=4096))
    client = Client("https://witness.example", RUNTIME, session=session,
                    timeout=(1.0, 2.0), max_response=4096)

    assert client.get_head("bridge-production")["status"] == "ready"
    client.claim("bridge-production", 7, cert.permit_nonce, "6" * 64)

    for _method, _url, kwargs in session.calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (1.0, 2.0)
        assert kwargs["verify"] is True
        assert kwargs["stream"] is True
        assert kwargs["headers"]["Authorization"] == f"Bearer {RUNTIME}"
    assert session.trust_env is False
    with pytest.raises(WitnessError, match="HTTPS"):
        Client("http://witness.example", RUNTIME, session=session)


def test_reference_http_server_only_binds_loopback(store):
    application = HTTPApplication(store)
    server = make_http_server("127.0.0.1", 0, application)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
    with pytest.raises(WitnessError, match="loopback"):
        make_http_server("0.0.0.0", 0, application)


def test_admin_cli_only_issues_reviewed_certificate_or_serves(tmp_path):
    help_text = build_cli_parser().format_help()
    assert "issue" in help_text and "serve" in help_text
    assert "sign-current" not in help_text and "restore" not in help_text
    cert_path = tmp_path / "certificate.json"
    cert_path.write_text(json.dumps(certificate().to_dict()))
    store_path = tmp_path / "witness.db"

    assert run_cli([
        "issue", "--store", str(store_path), "--certificate", str(cert_path),
        "--actor", "independent-reviewer", "--rationale", "review board approval",
    ], environ={"CUSTODY_WITNESS_TOKEN": RUNTIME,
                "CUSTODY_WITNESS_ADMIN_TOKEN": ADMIN}) == 0
    assert Store(store_path, runtime_token=RUNTIME, admin_token=ADMIN).get_head(
        "bridge-production", token=RUNTIME
    )["status"] == "ready"


def test_http_application_rejects_extra_fields_duplicate_keys_and_bad_auth(store):
    cert = certificate()
    store.issue_initial(cert, actor="reviewer", rationale="approved", admin_token=ADMIN)
    application = HTTPApplication(store, max_body=4096)
    path = "/v1/deployments/bridge-production/claim"
    headers = {"Authorization": f"Bearer {RUNTIME}", "Content-Type": "application/json"}
    valid = {
        "generation": 7, "permit_nonce": cert.permit_nonce, "claim_nonce": "6" * 64,
    }
    for body in (
        json.dumps({**valid, "extra": 1}).encode(),
        b'{"generation":7,"generation":7,"permit_nonce":"' +
            cert.permit_nonce.encode() + b'","claim_nonce":"' + b"6" * 64 + b'"}',
    ):
        status, _response_headers, response = application.dispatch("POST", path, headers, body)
        assert status == 400
        assert RUNTIME.encode() not in response
    status, _response_headers, response = application.dispatch(
        "POST", path, {**headers, "Authorization": "Bearer wrong-secret"},
        json.dumps(valid).encode(),
    )
    assert status == 401
    assert b"wrong-secret" not in response


@pytest.mark.parametrize('action', ['complete', 'seal', 'hold'])
@pytest.mark.parametrize('generation', [7.0, True, '7', None, -1])
def test_all_transitions_reject_inexact_generation_without_changing_head(store, action, generation):
    cert = certificate()
    store.issue_initial(cert, actor='reviewer', rationale='approved', admin_token=ADMIN)
    client = StoreClient(store, RUNTIME)
    client.claim(cert.deployment_id, 7, cert.permit_nonce, '6' * 64)
    if action == 'seal':
        client.complete(cert.deployment_id, 7, '6' * 64)
    before = client.get_head(cert.deployment_id)
    extra = [certificate(generation=8, nonce='8' * 64).to_dict()] if action == 'seal' else []
    if action == 'hold':
        extra = ['operator review']
    with pytest.raises(WitnessError, match='invalid witness request'):
        getattr(client, action)(cert.deployment_id, generation, '6' * 64, *extra)
    assert client.get_head(cert.deployment_id) == before
