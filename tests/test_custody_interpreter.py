"""Interpreter-byte drift containment; not external executable attestation."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from src import custody_admission as admission
from src.custody_witness import Certificate, Store, StoreClient


def repository_at(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "src" / "main.py").write_text("def run(): pass\n")
    (path / "swapService.py").write_text("from src.main import run\nrun()\n")
    (path / "requirements.txt").write_text("requests==2.33.0\n")
    return path


def test_interpreter_bytes_change_build_without_executing_candidate(tmp_path, monkeypatch):
    repository = repository_at(tmp_path / "runtime")
    executable = tmp_path / "interpreter"
    executable.write_bytes(b"approved interpreter fixture")
    monkeypatch.setattr(admission, "_INTERPRETER_PATH", executable, raising=False)
    approved = admission.build_fingerprint(repository)
    assert approved == admission.build_fingerprint(repository)

    marker = tmp_path / "side-effect"
    executable.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")

    assert admission.build_fingerprint(repository) != approved
    assert not marker.exists()


@pytest.mark.parametrize("state", ["missing", "directory", "empty", "fifo", "unreadable"])
def test_invalid_interpreter_evidence_refuses_fingerprint(tmp_path, monkeypatch, state):
    repository = repository_at(tmp_path / "runtime")
    executable = tmp_path / "interpreter"
    if state == "directory":
        executable.mkdir()
    elif state == "empty":
        executable.touch()
    elif state == "fifo":
        os.mkfifo(executable)
    elif state == "unreadable":
        executable.write_bytes(b"approved interpreter fixture")
        original_open = Path.open

        def opening(path, *args, **kwargs):
            if path == executable:
                raise PermissionError("fixture private path and permission detail")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", opening)
    monkeypatch.setattr(admission, "_INTERPRETER_PATH", executable)

    with pytest.raises(admission.AdmissionError) as rejected:
        admission.build_fingerprint(repository)
    assert str(rejected.value) == "runtime interpreter evidence is unavailable"


@pytest.mark.parametrize("change", ["in_place", "replacement", "truncation"])
def test_interpreter_change_during_hash_is_rejected(tmp_path, monkeypatch, change):
    repository = repository_at(tmp_path / "runtime")
    executable = tmp_path / "interpreter"
    executable.write_bytes(b"original interpreter bytes")
    monkeypatch.setattr(admission, "_INTERPRETER_PATH", executable)
    original_open = Path.open

    class ChangingReader:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            chunk = self.stream.read(size)
            if chunk and not self.changed:
                self.changed = True
                data = b"z" * len(chunk) if change != "truncation" else b""
                target = executable if change != "replacement" else tmp_path / "replacement"
                with original_open(target, "wb") as writer:
                    writer.write(data)
                if change == "replacement":
                    os.replace(target, executable)
            return chunk

    def opening(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return ChangingReader(stream) if path == executable else stream

    monkeypatch.setattr(Path, "open", opening)
    with pytest.raises(admission.AdmissionError, match="runtime interpreter evidence is unavailable"):
        admission.build_fingerprint(repository)


@pytest.mark.parametrize("state", ["exact", "changed", "missing", "directory", "empty"])
def test_interpreter_identity_preserves_permit_until_exact_build(tmp_path, monkeypatch, state):
    repository = repository_at(tmp_path / "runtime")
    executable = tmp_path / "interpreter"
    original = b"approved interpreter fixture"
    executable.write_bytes(original)
    monkeypatch.setattr(admission, "_INTERPRETER_PATH", executable)
    approved = admission.build_fingerprint(repository)
    db = tmp_path / "custody.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE sources(id TEXT PRIMARY KEY, amount INTEGER NOT NULL)")
        conn.execute("INSERT INTO sources VALUES('source-1', 1100)")
    before = db.read_bytes()
    runtime_token, admin_token = "r" * 48, "a" * 48
    config_digest = "3" * 64
    cert = Certificate.from_dict({
        "deployment_id": "interpreter-offline-fixture", "generation": 0,
        "permit_nonce": "1" * 64, **admission.inspect_image(db),
        "config_sha256": config_digest, "build_sha256": approved,
        "issued_at": 1_800_000_000,
        "approval_rationale": "independently reviewed offline interpreter fixture",
    })
    store = Store(tmp_path / "witness.db", runtime_token=runtime_token, admin_token=admin_token)
    store.issue_initial(cert, actor="offline-reviewer", rationale="fixture only",
                        admin_token=admin_token)
    client = StoreClient(store, runtime_token)
    head = client.get_head(cert.deployment_id)
    marker = tmp_path / "side-effect"
    if state == "changed":
        executable.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    elif state == "empty":
        executable.write_bytes(b"")
    elif state in {"missing", "directory"}:
        executable.unlink()
        if state == "directory":
            executable.mkdir()

    def claim_current():
        return admission.claim(db_path=db, client=client, deployment_id=cert.deployment_id,
                               config_sha256=config_digest,
                               build_sha256=admission.build_fingerprint(repository))

    if state != "exact":
        with pytest.raises(admission.AdmissionError):
            claim_current()
        assert client.get_head(cert.deployment_id) == head
        assert db.read_bytes() == before
        assert not Path(str(db) + ".admission.json").exists()
        assert not marker.exists()
        if executable.is_dir():
            executable.rmdir()
        executable.write_bytes(original)

    lease = claim_current()
    lease.complete()
    lease.assert_running()
    sealed = lease.seal()
    assert sealed.generation == 1
    assert sealed.build_sha256 == approved
    assert client.get_head(cert.deployment_id)["status"] == "ready"
    assert not marker.exists()


def test_real_running_executable_not_sys_executable_or_path(tmp_path, monkeypatch):
    expected = hashlib.sha256(Path("/proc/self/exe").read_bytes()).hexdigest()
    spoofed = tmp_path / "not-the-running-interpreter"
    spoofed.write_bytes(b"unapproved binary")
    monkeypatch.setattr(sys, "executable", str(spoofed))
    monkeypatch.setenv("PATH", str(tmp_path))

    assert admission._interpreter_fingerprint() == expected
