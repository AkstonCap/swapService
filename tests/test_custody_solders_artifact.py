"""Installed transaction SDK drift checks, not pre-execution attestation."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from importlib.machinery import ExtensionFileLoader, ModuleSpec, PathFinder, SourceFileLoader
from pathlib import Path

import pytest

from src.custody_witness import Certificate, Store, StoreClient

from src import custody_admission as admission


def repository_at(path):
    (path / "src").mkdir(parents=True)
    (path / "src" / "main.py").write_text("def run(): pass\n")
    (path / "swapService.py").write_text("from src.main import run\nrun()\n")
    (path / "requirements.txt").write_text("solders==0.26.0\n")
    return path


def extension_fixture(tmp_path, monkeypatch):
    package = tmp_path / "solders"
    package.mkdir()
    extension = package / "solders.abi3.so"
    extension.write_bytes(b"approved transaction SDK fixture")
    package_spec = ModuleSpec("solders", loader=None, is_package=True)
    package_spec.submodule_search_locations = [str(package)]
    original_find = PathFinder.find_spec

    def find_spec(name, path=None, target=None):
        if name == "solders":
            return package_spec
        if name != "solders.solders":
            return original_find(name, path, target)
        assert path == [str(package)]
        return ModuleSpec(name, ExtensionFileLoader(name, str(extension)),
                          origin=str(extension))

    monkeypatch.setattr(PathFinder, "find_spec", find_spec)
    return extension


def test_installed_solders_bytes_change_build_without_execution(tmp_path, monkeypatch):
    repository = repository_at(tmp_path / "runtime")
    extension = extension_fixture(tmp_path, monkeypatch)
    approved = admission.build_fingerprint(repository)
    assert admission.build_fingerprint(repository) == approved
    marker = tmp_path / "side-effect"
    extension.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")

    assert admission.build_fingerprint(repository) != approved
    assert not marker.exists()


def test_discovery_does_not_execute_package_or_extension(tmp_path, monkeypatch):
    package = tmp_path / "solders"
    package.mkdir()
    marker = tmp_path / "side-effect"
    package.joinpath("__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    extension = package / "solders.abi3.so"
    extension.write_bytes(b"not an executable library; only read as evidence")
    monkeypatch.syspath_prepend(str(tmp_path))

    expected = hashlib.sha256(admission._canonical([
        extension.name, hashlib.sha256(extension.read_bytes()).hexdigest(),
    ])).hexdigest()
    assert admission._solders_fingerprint() == expected
    assert not marker.exists()


@pytest.mark.parametrize("state", [
    "package_absent", "module_absent", "not_package", "source_loader", "origin_mismatch",
    "relative", "wrong_name", "missing", "empty", "directory", "fifo", "unreadable",
])
def test_invalid_solders_evidence_is_sanitized(tmp_path, monkeypatch, state):
    extension = extension_fixture(tmp_path, monkeypatch)
    original_find = PathFinder.find_spec
    original_open = os.open

    def find_spec(name, path=None, target=None):
        found = original_find(name, path, target)
        if name == "solders":
            if state == "package_absent":
                return None
            if state == "not_package":
                found.submodule_search_locations = None
        else:
            if state == "module_absent":
                return None
            if state == "source_loader":
                found.loader = SourceFileLoader(name, str(extension))
            elif state == "origin_mismatch":
                found.origin = str(extension) + ".other"
            elif state in {"relative", "wrong_name"}:
                found.origin = "solders.abi3.so" if state == "relative" else str(extension) + ".other"
                found.loader = ExtensionFileLoader(name, found.origin)
        return found

    monkeypatch.setattr(PathFinder, "find_spec", find_spec)
    if state in {"missing", "directory", "fifo"}:
        extension.unlink()
        if state == "directory":
            extension.mkdir()
        elif state == "fifo":
            os.mkfifo(extension)
    elif state == "empty":
        extension.write_bytes(b"")
    elif state == "unreadable":
        def opening(path, *args, **kwargs):
            if Path(path) == extension:
                raise PermissionError("private installation/credential detail")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(os, "open", opening)

    with pytest.raises(admission.AdmissionError) as rejected:
        admission._solders_fingerprint()
    assert str(rejected.value) == "runtime solders artifact evidence is unavailable"


@pytest.mark.parametrize("change", ["in_place", "replacement", "truncation", "growth", "discovery"])
def test_solders_change_during_read_is_rejected(tmp_path, monkeypatch, change):
    extension = extension_fixture(tmp_path, monkeypatch)
    original_fdopen = os.fdopen
    original_find = PathFinder.find_spec
    alternative = extension.parent / "solders.so"

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
                if change == "discovery":
                    alternative.write_bytes(b"replacement installed extension")
                    def find_spec(name, path=None, target=None):
                        found = original_find(name, path, target)
                        if name == "solders.solders":
                            found.origin = str(alternative)
                            found.loader = ExtensionFileLoader(name, found.origin)
                        return found
                    monkeypatch.setattr(PathFinder, "find_spec", find_spec)
                elif change == "replacement":
                    alternative.write_bytes(b"z" * len(chunk))
                    os.replace(alternative, extension)
                elif change == "growth":
                    with extension.open("ab") as writer:
                        writer.write(b"added bytes")
                else:
                    extension.write_bytes(b"z" * len(chunk) if change == "in_place" else b"")
            return chunk

    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs:
                        ChangingReader(original_fdopen(*args, **kwargs)))
    with pytest.raises(admission.AdmissionError, match="solders artifact evidence is unavailable"):
        admission._solders_fingerprint()


@pytest.mark.parametrize("change", ["mutation", "fifo"])
def test_solders_change_between_stat_and_open_is_rejected(tmp_path, monkeypatch, change):
    extension = extension_fixture(tmp_path, monkeypatch)
    original_open = os.open

    def opening(path, flags, *args, **kwargs):
        if Path(path) == extension:
            if change == "fifo":
                # Never actually open a potentially blocking FIFO in a broken build.
                assert flags & os.O_NONBLOCK
                extension.unlink()
                os.mkfifo(extension)
            else:
                extension.write_bytes(b"z" * extension.stat().st_size)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    with pytest.raises(admission.AdmissionError, match="solders artifact evidence is unavailable"):
        admission._solders_fingerprint()


@pytest.mark.parametrize("state", ["exact", "changed", "missing", "empty", "directory"])
def test_solders_admission_preserves_permit_until_exact_build(tmp_path, monkeypatch, state):
    repository = repository_at(tmp_path / "runtime")
    extension = extension_fixture(tmp_path, monkeypatch)
    original = extension.read_bytes()
    build = admission.build_fingerprint
    monkeypatch.setattr(admission, "build_fingerprint", lambda: build(repository))
    approved = admission.build_fingerprint()
    db = tmp_path / "custody.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE sources(id TEXT PRIMARY KEY, amount INTEGER NOT NULL)")
        conn.execute("INSERT INTO sources VALUES('source-1', 1100)")
    before = db.read_bytes()
    runtime, admin = "r" * 48, "a" * 48
    cert = Certificate.from_dict({
        "deployment_id": "solders-offline-fixture", "generation": 0,
        "permit_nonce": "1" * 64, **admission.inspect_image(db),
        "config_sha256": "3" * 64, "build_sha256": approved,
        "issued_at": 1_800_000_000, "approval_rationale": "offline fixture only",
    })
    store = Store(tmp_path / "witness.db", runtime_token=runtime, admin_token=admin)
    store.issue_initial(cert, actor="offline-reviewer", rationale="fixture only", admin_token=admin)
    client = StoreClient(store, runtime)
    head = client.get_head(cert.deployment_id)
    if state == "changed":
        extension.write_bytes(b"unapproved transaction builder")
    elif state == "empty":
        extension.write_bytes(b"")
    elif state in {"missing", "directory"}:
        extension.unlink()
        if state == "directory":
            extension.mkdir()

    def claim_current():
        return admission.claim(db_path=db, client=client, deployment_id=cert.deployment_id,
                               config_sha256=cert.config_sha256)

    if state != "exact":
        with pytest.raises(admission.AdmissionError):
            claim_current()
        assert client.get_head(cert.deployment_id) == head
        assert db.read_bytes() == before
        assert not Path(str(db) + ".admission.json").exists()
        assert not any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
        if extension.is_dir():
            extension.rmdir()
        extension.write_bytes(original)

    lease = claim_current()
    lease.complete()
    lease.assert_running()
    assert admission.dashboard_status(
        db_path=db, client=client, deployment_id=cert.deployment_id,
        config_sha256=cert.config_sha256,
    )["status"] == "not_held"
    sealed = lease.seal()
    assert sealed.generation == 1
    assert sealed.build_sha256 == approved
    assert client.get_head(cert.deployment_id)["status"] == "ready"
