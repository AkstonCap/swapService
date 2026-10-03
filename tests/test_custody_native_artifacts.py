"""Loaded native-file drift containment, not trusted pre-execution attestation."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from src import custody_admission as admission
from src.custody_witness import Certificate, Store, StoreClient


def repository_at(path):
    (path / "src").mkdir(parents=True)
    (path / "src" / "main.py").write_text("def run(): pass\n")
    (path / "swapService.py").write_text("from src.main import run\nrun()\n")
    (path / "requirements.txt").write_text("requests==2.33.0\n")
    return path


def mapping(path, *, permissions="r-xp", address="1000-2000"):
    evidence = path.stat()
    device = f"{os.major(evidence.st_dev):02x}:{os.minor(evidence.st_dev):02x}"
    return f"{address} {permissions} 00000000 {device} {evidence.st_ino} {path}\n"


def native_fixture(tmp_path, monkeypatch):
    library = tmp_path / "libpython3.12.so.1.0"
    library.write_bytes(b"approved native runtime fixture")
    maps = tmp_path / "maps"
    maps.write_text(mapping(library))
    monkeypatch.setattr(admission, "_MAPS_PATH", maps, raising=False)
    return library, maps


def test_service_and_dashboard_processes_have_same_build_identity():
    # Different native extension import sets are normal, not a deployment upgrade.
    def fingerprint(module):
        result = subprocess.run([
            sys.executable, "-c",
            f"import {module}; from src.custody_admission import build_fingerprint; "
            "print(build_fingerprint())",
        ], check=True, capture_output=True, text=True, timeout=30)
        return result.stdout.strip().splitlines()[-1]

    assert fingerprint("src.main") == fingerprint("src.dashboard")


def test_loaded_native_bytes_change_build_without_execution(tmp_path, monkeypatch):
    repository = repository_at(tmp_path / "runtime")
    library, _maps = native_fixture(tmp_path, monkeypatch)
    approved = admission.build_fingerprint(repository)
    assert approved == admission.build_fingerprint(repository)
    marker = tmp_path / "side-effect"
    library.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    assert admission.build_fingerprint(repository) != approved
    assert not marker.exists()


@pytest.mark.parametrize("state", ["exact", "changed", "missing", "inode"])
def test_native_identity_preserves_permit_until_exact_build(tmp_path, monkeypatch, state):
    repository = repository_at(tmp_path / "runtime")
    library, maps = native_fixture(tmp_path, monkeypatch)
    original = library.read_bytes()
    approved = admission.build_fingerprint(repository)
    db = tmp_path / "custody.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE sources(id TEXT PRIMARY KEY, amount INTEGER NOT NULL)")
        conn.execute("INSERT INTO sources VALUES('source-1', 1100)")
    before = db.read_bytes()
    runtime, admin = "r" * 48, "a" * 48
    certificate = Certificate.from_dict({
        "deployment_id": "native-runtime-offline-fixture", "generation": 0,
        "permit_nonce": "1" * 64, **admission.inspect_image(db),
        "config_sha256": "3" * 64, "build_sha256": approved,
        "issued_at": 1_800_000_000, "approval_rationale": "offline fixture only",
    })
    store = Store(tmp_path / "witness.db", runtime_token=runtime, admin_token=admin)
    store.issue_initial(certificate, actor="offline-reviewer", rationale="fixture only",
                        admin_token=admin)
    client = StoreClient(store, runtime)
    head = client.get_head(certificate.deployment_id)
    if state == "changed":
        library.write_bytes(b"unapproved native artifact")
    elif state == "missing":
        library.unlink()
    elif state == "inode":
        fields = mapping(library).split(maxsplit=5)
        fields[4] = "1"
        maps.write_text(" ".join(fields))

    def claim_current():
        # Exercise the normal resolver, including fingerprint-before-image-open.
        monkeypatch.setattr(admission, "build_fingerprint",
                            lambda: build(repository))
        return admission.claim(db_path=db, client=client,
                               deployment_id=certificate.deployment_id,
                               config_sha256=certificate.config_sha256)

    build = admission.build_fingerprint
    if state != "exact":
        with pytest.raises(admission.AdmissionError):
            claim_current()
        assert client.get_head(certificate.deployment_id) == head
        assert db.read_bytes() == before
        assert not Path(str(db) + ".admission.json").exists()
        assert not any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm"))
        library.write_bytes(original)
        maps.write_text(mapping(library))
    lease = claim_current()
    lease.complete()
    lease.assert_running()
    assert admission.dashboard_status(
        db_path=db, client=client, deployment_id=certificate.deployment_id,
        config_sha256=certificate.config_sha256,
    )["status"] == "not_held"
    sealed = lease.seal()
    assert sealed.generation == 1
    assert sealed.build_sha256 == approved
    assert client.get_head(certificate.deployment_id)["status"] == "ready"


@pytest.mark.parametrize("name", [
    "libpython3.12.so.1.0", "libpython3.13t.so.1.0", "libpython3.12d.so.1.0",
    "libc.so.6", "libm.so.6", "libc-2.31.so", "ld-linux-aarch64.so.1",
    "ld-linux-x86-64.so.2", "ld-musl-aarch64.so.1", "ld-2.31.so", "ld64.so.1", "ld.so.1",
])
def test_foundational_library_names_are_bound(tmp_path, monkeypatch, name):
    library = tmp_path / name
    library.write_bytes(b"reviewed native artifact")
    maps = tmp_path / "maps"
    maps.write_text(mapping(library))
    monkeypatch.setattr(admission, "_MAPS_PATH", maps)
    digest = admission._native_fingerprint()
    library.write_bytes(b"changed artifact")
    assert admission._native_fingerprint() != digest


def test_mapping_order_addresses_and_unrelated_extensions_do_not_change_identity(
    tmp_path, monkeypatch,
):
    library, maps = native_fixture(tmp_path, monkeypatch)
    approved = admission._native_fingerprint()
    expected = [[str(library), hashlib.sha256(library.read_bytes()).hexdigest()]]
    assert approved == hashlib.sha256(admission._canonical(expected)).hexdigest()
    unrelated = tmp_path / "extension.so"
    unrelated.write_bytes(b"installed but outside this narrow manifest")
    maps.write_text(
        mapping(unrelated) + mapping(library, address="5000-6000")
        + mapping(library, address="7000-8000")
        + mapping(library, permissions="r--p", address="9000-a000")
        + "b000-c000 r-xp 00000000 00:00 0 [vdso]\n"
    )
    assert admission._native_fingerprint() == approved


@pytest.mark.parametrize("changed", ["libpython3.12.so.1.0", "libc.so.6", "libm.so.6", "ld.so.1"])
def test_each_foundational_artifact_changes_combined_manifest(tmp_path, monkeypatch, changed):
    names = ["libpython3.12.so.1.0", "libc.so.6", "libm.so.6", "ld.so.1"]
    libraries = [tmp_path / name for name in names]
    for library in libraries:
        library.write_bytes(library.name.encode())
    maps = tmp_path / "maps"
    maps.write_text("".join(mapping(library) for library in libraries))
    monkeypatch.setattr(admission, "_MAPS_PATH", maps)
    approved = admission._native_fingerprint()
    (tmp_path / changed).write_bytes(b"changed native artifact")
    assert admission._native_fingerprint() != approved


def test_real_native_snapshot_is_repeatable():
    digest = admission._native_fingerprint()
    assert len(digest) == 64
    assert admission._native_fingerprint() == digest



@pytest.mark.parametrize("state", [
    "maps_missing", "maps_empty", "malformed", "anonymous", "deleted", "inode",
    "device", "missing", "directory", "empty", "unreadable", "maps_unreadable",
])
def test_invalid_native_evidence_is_sanitized_and_refused(tmp_path, monkeypatch, state):
    repository = repository_at(tmp_path / "runtime")
    library, maps = native_fixture(tmp_path, monkeypatch)
    original_open = Path.open
    if state == "maps_missing":
        maps.unlink()
    elif state == "maps_empty":
        maps.write_text("")
    elif state == "malformed":
        maps.write_text("invalid mapping columns\n")
    elif state == "anonymous":
        maps.write_text("1000-2000 rwxp 00000000 00:00 0\n")
    elif state == "deleted":
        maps.write_text(mapping(library).rstrip() + " (deleted)\n")
    elif state in {"device", "inode"}:
        fields = mapping(library).split(maxsplit=5)
        fields[3 if state == "device" else 4] = "ff:ff" if state == "device" else "1"
        maps.write_text(" ".join(fields))
    elif state in {"missing", "directory"}:
        library.unlink()
        if state == "directory":
            library.mkdir()
    elif state == "empty":
        library.write_bytes(b"")
    elif state in {"unreadable", "maps_unreadable"}:
        unreadable = library if state == "unreadable" else maps

        def opening(path, *args, **kwargs):
            if path == unreadable:
                raise PermissionError("fixture private path and credential detail")
            return original_open(path, *args, **kwargs)

        original_os_open = os.open

        def opening_native(path, *args, **kwargs):
            if Path(path) == unreadable:
                raise PermissionError("fixture private path and credential detail")
            return original_os_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", opening)
        monkeypatch.setattr(os, "open", opening_native)

    with pytest.raises(admission.AdmissionError) as rejected:
        admission.build_fingerprint(repository)
    assert str(rejected.value) == "runtime native artifact evidence is unavailable"


@pytest.mark.parametrize("change", ["in_place", "replacement", "truncation", "mapping"])
def test_native_change_during_hash_is_rejected(tmp_path, monkeypatch, change):
    library, maps = native_fixture(tmp_path, monkeypatch)
    original_fdopen = os.fdopen

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
                if change == "mapping":
                    other = tmp_path / "libm.so.6"
                    other.write_bytes(b"newly loaded native artifact")
                    maps.write_text(mapping(library) + mapping(other, address="3000-4000"))
                elif change == "replacement":
                    other = tmp_path / "replacement"
                    other.write_bytes(b"z" * len(chunk))
                    os.replace(other, library)
                else:
                    library.write_bytes(b"z" * len(chunk) if change == "in_place" else b"")
            return chunk

    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs:
                        ChangingReader(original_fdopen(*args, **kwargs)))
    with pytest.raises(admission.AdmissionError, match="native artifact evidence is unavailable"):
        admission._native_fingerprint()


def test_native_change_between_stat_and_open_is_rejected(tmp_path, monkeypatch):
    library, _maps = native_fixture(tmp_path, monkeypatch)
    original_open = os.open

    def opening(path, *args, **kwargs):
        if Path(path) == library:
            library.write_bytes(b"z" * library.stat().st_size)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    with pytest.raises(admission.AdmissionError, match="native artifact evidence is unavailable"):
        admission._native_fingerprint()
