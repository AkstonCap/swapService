"""Sealed custody-image startup admission tests."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.custody_admission import (
    AdmissionError,
    build_fingerprint,
    claim,
    dashboard_status,
    inspect_image,
)
from src.custody_witness import Certificate, Store, StoreClient, WitnessError

RUNTIME = "r" * 48
ADMIN = "a" * 48
CONFIG = "3" * 64
BUILD = "4" * 64
DEPLOYMENT = "bridge-production"


def create_image(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sources(id TEXT PRIMARY KEY, amount INTEGER NOT NULL)")
        conn.execute("CREATE TABLE intents(id TEXT PRIMARY KEY, evidence TEXT NOT NULL)")
        conn.execute("INSERT INTO sources VALUES('source-1', 1100)")
        conn.execute("INSERT INTO intents VALUES('source-1', 'frozen-policy')")


def cert_for(path: Path, *, generation: int = 0, nonce: str = "1" * 64) -> Certificate:
    image = inspect_image(path)
    return Certificate.from_dict({
        "deployment_id": DEPLOYMENT,
        "generation": generation,
        "permit_nonce": nonce,
        "image_sha256": image["image_sha256"],
        "image_size": image["image_size"],
        "config_sha256": CONFIG,
        "build_sha256": BUILD,
        "schema_sha256": image["schema_sha256"],
        "issued_at": 1_800_000_000,
        "approval_rationale": "reviewed current runtime and coherent image",
    })


@pytest.fixture
def admitted(tmp_path):
    db = tmp_path / "custody.db"
    create_image(db)
    store = Store(tmp_path / "witness.db", runtime_token=RUNTIME, admin_token=ADMIN)
    cert = cert_for(db)
    store.issue_initial(cert, actor="independent-reviewer", rationale="release approved",
                        admin_token=ADMIN)
    return db, store, StoreClient(store, RUNTIME), cert


def do_claim(db: Path, client, **kwargs):
    return claim(db_path=db, client=client, deployment_id=DEPLOYMENT,
                 config_sha256=CONFIG, build_sha256=BUILD, **kwargs)


def test_build_fingerprint_binds_root_entrypoint_without_executing_it(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def run(): pass\n")
    (tmp_path / "requirements.txt").write_text("requests==2.33.0\n")
    entrypoint = tmp_path / "swapService.py"
    entrypoint.write_text("from src.main import run\nrun()\n")
    approved = build_fingerprint(tmp_path)
    assert approved == build_fingerprint(tmp_path)

    marker = tmp_path / "pre_admission_side_effect"
    entrypoint.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "from src.main import run\nrun()\n"
    )

    assert build_fingerprint(tmp_path) != approved
    assert not marker.exists()


@pytest.mark.parametrize("entrypoint_state", ["exact", "changed", "missing", "directory"])
def test_entrypoint_identity_controls_claim_without_consuming_changed_build(
    tmp_path, entrypoint_state,
):
    repository = tmp_path / "runtime"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "main.py").write_text("def run(): pass\n")
    (repository / "requirements.txt").write_text("requests==2.33.0\n")
    entrypoint = repository / "swapService.py"
    entrypoint.write_text("from src.main import run\nrun()\n")
    approved = build_fingerprint(repository)
    db = tmp_path / "custody.db"
    create_image(db)
    before = db.read_bytes()
    certificate = Certificate.from_dict({**cert_for(db).to_dict(), "build_sha256": approved})
    store = Store(tmp_path / "witness.db", runtime_token=RUNTIME, admin_token=ADMIN)
    store.issue_initial(certificate, actor="independent-reviewer",
                        rationale="exact fixture approved", admin_token=ADMIN)
    client = StoreClient(store, RUNTIME)
    marker = tmp_path / "pre_admission_side_effect"
    if entrypoint_state == "changed":
        entrypoint.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    elif entrypoint_state in {"missing", "directory"}:
        entrypoint.unlink()
        if entrypoint_state == "directory":
            entrypoint.mkdir()

    def claim_current_build():
        return claim(db_path=db, client=client, deployment_id=DEPLOYMENT,
                     config_sha256=CONFIG, build_sha256=build_fingerprint(repository))

    if entrypoint_state == "exact":
        lease = claim_current_build()
        lease.complete()
        lease.assert_running()
        next_certificate = lease.seal()
        assert next_certificate.generation == 1
        assert next_certificate.build_sha256 == approved
    else:
        with pytest.raises(AdmissionError, match=(
            "fingerprint is incomplete|sealed image deployment identity does not match"
        )):
            claim_current_build()
        assert db.read_bytes() == before
        assert not Path(str(db) + ".admission.json").exists()
        assert client.get_head(DEPLOYMENT)["status"] == "ready"
    assert not marker.exists()


def test_exact_sealed_image_claims_completes_and_reports_running(admitted):
    db, _store, client, _cert = admitted
    lease = do_claim(db, client)
    assert lease.phase == "claimed"
    lease.complete()
    lease.assert_running()
    assert json.loads(Path(str(db) + ".admission.json").read_text())["phase"] == "running"
    assert dashboard_status(db_path=db, client=client, deployment_id=DEPLOYMENT,
                            config_sha256=CONFIG, build_sha256=BUILD) == {
        "status": "not_held",
        "reason": None,
        "detail": "sealed custody lease is running",
        "operator_action": "none",
        "liabilities_complete": False,
        "lease_identity": {'generation': lease.certificate.generation,
                           'permit_nonce': lease.certificate.permit_nonce,
                           'claim_nonce': lease.claim_nonce},
    }


def test_mutation_of_every_dynamically_discovered_table_is_rejected(tmp_path):
    baseline = tmp_path / "baseline.db"
    create_image(baseline)
    with sqlite3.connect(baseline) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
    assert tables

    for index, table in enumerate(tables):
        db = tmp_path / f"mutated-{index}.db"
        shutil.copyfile(baseline, db)
        store = Store(tmp_path / f"witness-{index}.db", runtime_token=RUNTIME,
                      admin_token=ADMIN)
        store.issue_initial(cert_for(db), actor="independent-reviewer",
                            rationale="release approved", admin_token=ADMIN)
        client = StoreClient(store, RUNTIME)
        with sqlite3.connect(db) as conn:
            if table == "sources":
                conn.execute("UPDATE sources SET amount=amount+1")
            elif table == "intents":
                conn.execute("UPDATE intents SET evidence='replacement-policy'")
            else:  # future fixture tables must remain covered without a new parameter list
                conn.execute(f'DELETE FROM "{table}"')
        before = db.read_bytes()

        with pytest.raises(AdmissionError, match="sealed image does not match"):
            do_claim(db, client)

        assert db.read_bytes() == before
        assert not Path(str(db) + ".admission.json").exists()
        assert client.get_head(DEPLOYMENT)["status"] == "ready"


@pytest.mark.parametrize("failure", ["deleted", "wal", "journal", "shm"])
def test_missing_or_mixed_sqlite_components_block_before_permit_consumption(admitted, failure):
    db, _store, client, _cert = admitted
    if failure == "deleted":
        db.unlink()
    else:
        Path(str(db) + f"-{failure}").write_bytes(b"mixed generation")

    with pytest.raises(AdmissionError):
        do_claim(db, client)

    assert not Path(str(db) + ".admission.json").exists()
    assert client.get_head(DEPLOYMENT)["status"] == "ready"


def test_uncertified_existing_database_blocks_without_modification(tmp_path):
    db = tmp_path / "existing.db"
    create_image(db)
    before = db.read_bytes()
    store = Store(tmp_path / "witness.db", runtime_token=RUNTIME, admin_token=ADMIN)

    with pytest.raises(AdmissionError, match="witness admission failed"):
        do_claim(db, StoreClient(store, RUNTIME))

    assert db.read_bytes() == before
    assert not Path(str(db) + ".admission.json").exists()


def test_claim_network_ambiguity_accepts_only_exact_claimed_readback(admitted):
    db, _store, delegate, _cert = admitted

    class AmbiguousClient:
        def get_head(self, deployment_id):
            return delegate.get_head(deployment_id)

        def claim(self, *args):
            delegate.claim(*args)
            raise WitnessError("network response lost")

        def __getattr__(self, name):
            return getattr(delegate, name)

    lease = do_claim(db, AmbiguousClient())
    assert lease.phase == "claimed"
    assert delegate.get_head(DEPLOYMENT)["claim_nonce"] == lease.claim_nonce


def test_claim_ambiguity_with_nonmatching_readback_refuses(admitted):
    db, _store, delegate, _cert = admitted

    class FailedClient:
        def get_head(self, deployment_id):
            return delegate.get_head(deployment_id)

        def claim(self, *_args):
            raise WitnessError("network response lost")

    with pytest.raises(AdmissionError, match="claim outcome is not exact"):
        do_claim(db, FailedClient())
    assert not Path(str(db) + ".admission.json").exists()
    assert delegate.get_head(DEPLOYMENT)["status"] == "ready"


def test_unreadable_claim_outcome_best_effort_holds_consumed_permit(admitted):
    db, _store, delegate, _cert = admitted

    class LostReadback:
        reads = 0

        def get_head(self, deployment_id):
            self.reads += 1
            if self.reads > 1:
                raise WitnessError("readback unavailable")
            return delegate.get_head(deployment_id)

        def claim(self, *args):
            delegate.claim(*args)
            raise WitnessError("claim response lost")

        def hold(self, *args):
            return delegate.hold(*args)

    before = db.read_bytes()
    with pytest.raises(AdmissionError, match="claim outcome is not exact"):
        do_claim(db, LostReadback())
    assert db.read_bytes() == before
    assert not Path(str(db) + ".admission.json").exists()
    assert delegate.get_head(DEPLOYMENT)["status"] == "held"


def test_complete_failure_holds_consumed_generation_and_restart_refuses(admitted):
    db, _store, delegate, _cert = admitted

    class CompleteFailure:
        def __getattr__(self, name):
            return getattr(delegate, name)

        def complete(self, *_args):
            raise WitnessError("unavailable")

    lease = do_claim(db, CompleteFailure())
    with pytest.raises(AdmissionError, match="completion failed"):
        lease.complete()
    assert delegate.get_head(DEPLOYMENT)["status"] == "held"
    assert json.loads(Path(str(db) + ".admission.json").read_text())["phase"] == "held"
    with pytest.raises(AdmissionError):
        do_claim(db, delegate)


def test_exact_complete_readback_accepts_lost_response(admitted):
    db, _store, delegate, _cert = admitted

    class LostCompletionResponse:
        def __getattr__(self, name):
            return getattr(delegate, name)

        def complete(self, *args):
            delegate.complete(*args)
            raise WitnessError("completion response lost")

    lease = do_claim(db, LostCompletionResponse())
    lease.complete()
    assert lease.phase == "running"
    assert delegate.get_head(DEPLOYMENT)["status"] == "running"


def test_image_deletion_after_claim_holds_consumed_generation(admitted):
    db, _store, delegate, _cert = admitted

    class DeleteAfterClaim:
        def __getattr__(self, name):
            return getattr(delegate, name)

        def claim(self, *args):
            result = delegate.claim(*args)
            db.unlink()
            return result

    with pytest.raises(AdmissionError, match="sealed custody image is unavailable"):
        do_claim(db, DeleteAfterClaim())
    assert delegate.get_head(DEPLOYMENT)["status"] == "held"


def test_seal_transport_failure_holds_and_blocks_restart(admitted):
    db, _store, delegate, _cert = admitted

    class SealFailure:
        def __getattr__(self, name):
            return getattr(delegate, name)

        def seal(self, *_args):
            raise WitnessError("unavailable")

    lease = do_claim(db, SealFailure())
    lease.complete()
    with pytest.raises(AdmissionError, match="custody seal failed"):
        lease.seal()
    assert delegate.get_head(DEPLOYMENT)["status"] == "held"
    with pytest.raises(AdmissionError):
        do_claim(db, delegate)


def test_clean_quiescent_seal_creates_exact_next_generation(admitted):
    db, _store, client, cert = admitted
    lease = do_claim(db, client)
    lease.complete()
    next_certificate = lease.seal()
    assert next_certificate.generation == cert.generation + 1
    head = client.get_head(DEPLOYMENT)
    assert head["status"] == "ready"
    assert head["certificate"] == next_certificate.to_dict()
    assert json.loads(Path(str(db) + ".admission.json").read_text())["phase"] == "sealed"

    next_lease = do_claim(db, client)
    assert next_lease.generation == 1
    with pytest.raises(AdmissionError):
        do_claim(db, client)


def test_online_backup_clone_can_be_independently_certified(tmp_path):
    original = tmp_path / "original.db"
    clone = tmp_path / "clone.db"
    create_image(original)
    with sqlite3.connect(original) as source, sqlite3.connect(clone) as destination:
        source.backup(destination)
    store = Store(tmp_path / "witness.db", runtime_token=RUNTIME, admin_token=ADMIN)
    clone_cert = cert_for(clone)
    store.issue_initial(clone_cert, actor="independent-reviewer", rationale="clone reviewed",
                        admin_token=ADMIN)

    lease = do_claim(clone, StoreClient(store, RUNTIME))
    assert lease.generation == 0


def test_copied_receipt_cannot_grant_dashboard_health(admitted, tmp_path):
    db, _store, client, _cert = admitted
    lease = do_claim(db, client)
    lease.complete()
    copied_db = tmp_path / "copied.db"
    shutil.copyfile(db, copied_db)
    shutil.copyfile(str(db) + ".admission.json", str(copied_db) + ".admission.json")

    result = dashboard_status(db_path=copied_db, client=client, deployment_id="other-deployment",
                              config_sha256=CONFIG, build_sha256=BUILD)
    assert result["status"] == "unknown"
    assert result["liabilities_complete"] is False


def test_duplicate_receipt_keys_are_not_accepted_as_running(admitted):
    db, _store, client, _cert = admitted
    lease = do_claim(db, client)
    lease.complete()
    receipt_path = Path(str(db) + ".admission.json")
    valid = receipt_path.read_text().strip()
    receipt_path.write_text(valid[:-1] + ',"phase":"running"}')

    result = dashboard_status(db_path=db, client=client, deployment_id=DEPLOYMENT,
                              config_sha256=CONFIG, build_sha256=BUILD)
    assert result["status"] == "unknown"
    assert result["liabilities_complete"] is False


def test_missing_witness_configuration_always_fails(monkeypatch, tmp_path):
    db = tmp_path / "custody.db"
    create_image(db)
    for name in ("CUSTODY_WITNESS_URL", "CUSTODY_WITNESS_TOKEN", "CUSTODY_DEPLOYMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    with pytest.raises(AdmissionError, match="configuration missing"):
        claim(db_path=db, config_sha256=CONFIG, build_sha256=BUILD)
    status = dashboard_status(db_path=db, config_sha256=CONFIG, build_sha256=BUILD)

    assert status["status"] == "unknown"
    assert status["liabilities_complete"] is False
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_path_replaced_after_final_hash_cannot_become_trusted_identity(admitted, monkeypatch):
    from src import custody_admission as module
    db, _store, client, _cert = admitted
    original = module._schema_digest
    calls = []
    def replace_after_schema(path):
        result = original(path)
        replacement = db.with_name('replacement.db')
        create_image(replacement)
        with sqlite3.connect(replacement) as conn:
            conn.execute("UPDATE sources SET amount=9999")
        os.replace(replacement, db)
        calls.append(True)
        return result
    monkeypatch.setattr(module, '_schema_digest', replace_after_schema)
    with pytest.raises(AdmissionError):
        do_claim(db, client)
    assert calls
    assert client.get_head(DEPLOYMENT)['status'] == 'held'


def test_path_replaced_during_seal_cannot_be_certified(admitted, monkeypatch):
    from src import custody_admission as module
    db, _store, client, _cert = admitted
    lease = do_claim(db, client)
    lease.complete()
    original = module.inspect_image
    def replace_after_hash(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        replacement = db.with_name('replacement.db')
        create_image(replacement)
        os.replace(replacement, db)
        return result
    monkeypatch.setattr(module, 'inspect_image', replace_after_hash)
    with pytest.raises(AdmissionError):
        lease.seal()
    assert client.get_head(DEPLOYMENT)['status'] == 'held'


@pytest.mark.parametrize('field', ['deployment_id', 'config_sha256', 'build_sha256'])
def test_nonmatching_held_head_is_unknown_not_authoritative(admitted, field):
    db, _store, client, _cert = admitted
    lease = do_claim(db, client)
    lease.complete()
    lease.hold()
    head = client.get_head(DEPLOYMENT)
    head['certificate'][field] = 'other-deployment' if field == 'deployment_id' else '9' * 64
    class NonmatchingHead:
        def get_head(self, deployment):
            return head
    result = dashboard_status(db_path=db, client=NonmatchingHead(), deployment_id=DEPLOYMENT,
                              config_sha256=CONFIG, build_sha256=BUILD)
    assert result['status'] == 'unknown'
