"""Selected installed SDK source drift is evidence, not execution attestation."""
from __future__ import annotations

import ast
import hashlib
import os
import sqlite3
from contextlib import closing
from importlib.machinery import PathFinder, SourceFileLoader, SourcelessFileLoader
from pathlib import Path

import pytest

from src import custody_admission as admission
from src.custody_witness import Certificate, Store, StoreClient


# Independent expected list: direct runtime imports plus mandatory top-level Python
# modules and token initializer imported by pinned solders. Other nested/optional
# imports remain outside this manifest, not proof of complete dependency closure.
INDIRECT_SOURCE_NAMES = (
    "account.py", "account_decoder.py", "address_lookup_table_account.py", "clock.py",
    "commitment_config.py", "compute_budget.py", "epoch_info.py", "epoch_rewards.py",
    "epoch_schedule.py", "errors.py", "null_signer.py", "presigner.py", "rent.py",
    "slot_history.py", "stake_history.py", "system_program.py", "sysvar.py",
    "transaction_status.py",
)
SOURCE_NAMES = (
    "__init__.py", "hash.py", "instruction.py", "keypair.py", "message.py",
    "pubkey.py", "signature.py", "transaction.py", *INDIRECT_SOURCE_NAMES,
    "token/__init__.py",
)


def source_fixture(tmp_path, monkeypatch):
    package = tmp_path / "installed" / "solders"
    package.mkdir(parents=True)
    for name in SOURCE_NAMES:
        package.joinpath(name).parent.mkdir(parents=True, exist_ok=True)
        package.joinpath(name).write_text("# approved SDK source fixture\n")
    package.joinpath("solders.abi3.so").write_bytes(b"offline native fixture")
    monkeypatch.syspath_prepend(str(package.parent))
    original_find = PathFinder.find_spec

    def find_spec(name, path=None, target=None):
        # Missing fixture initialization must not fall through to the real SDK.
        if name == "solders":
            return original_find(name, [str(package.parent)], target)
        return original_find(name, path, target)

    monkeypatch.setattr(PathFinder, "find_spec", find_spec)
    repository = tmp_path / "runtime"
    repository.joinpath("src").mkdir(parents=True)
    repository.joinpath("src", "main.py").write_text("def run(): pass\n")
    repository.joinpath("swapService.py").write_text("from src.main import run\nrun()\n")
    repository.joinpath("requirements.txt").write_text("solders==0.26.0\n")
    return repository, package


@pytest.mark.parametrize("name", SOURCE_NAMES)
def test_solders_source_drift_changes_build_without_execution(tmp_path, monkeypatch, name):
    repository, package = source_fixture(tmp_path, monkeypatch)
    approved = admission.build_fingerprint(repository)
    assert admission.build_fingerprint(repository) == approved
    marker = tmp_path / "package-side-effect"
    package.joinpath(name).write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )

    assert admission.build_fingerprint(repository) != approved
    assert not marker.exists()


def test_eager_token_initializer_drift_changes_build_without_execution(tmp_path, monkeypatch):
    repository, package = source_fixture(tmp_path, monkeypatch)
    approved = admission.build_fingerprint(repository)
    marker = tmp_path / "token-side-effect"
    package.joinpath("token", "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )

    assert admission.build_fingerprint(repository) != approved
    assert not marker.exists()


def test_solders_source_manifest_covers_direct_runtime_imports():
    imported = set()
    for path in Path(admission.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("solders."):
                imported.add(node.module.removeprefix("solders."))
    manifest = set(admission._SOLDERS_SOURCE_MODULES)
    assert imported <= manifest
    assert manifest == {
        Path(name).stem for name in SOURCE_NAMES if name != "__init__.py" and "/" not in name
    }
    assert admission._SOLDERS_SOURCE_PACKAGES == ("token",)


def test_source_manifest_covers_installed_mandatory_flat_initializer_imports():
    # Inspect the pinned package without execution. Optional imports and token's
    # not-eagerly-imported submodules remain outside this finite manifest.
    package = PathFinder.find_spec("solders")
    assert package is not None and isinstance(package.origin, str)
    tree = ast.parse(Path(package.origin).read_text())
    eager = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
        for alias in node.names
    }
    assert eager == set(admission._SOLDERS_SOURCE_MODULES) | {"token"}
    token = Path(package.origin).parent / "token" / "__init__.py"
    token_imports = [
        node for node in ast.walk(ast.parse(token.read_text()))
        if isinstance(node, ast.ImportFrom) and node.level
    ]
    assert {(node.level, node.module) for node in token_imports} == {(2, "solders")}


def test_source_discovery_reads_manifest_without_execution(tmp_path, monkeypatch):
    _repository, package = source_fixture(tmp_path, monkeypatch)
    marker = tmp_path / "side-effect"
    for name in SOURCE_NAMES:
        package.joinpath(name).write_text(f"raise RuntimeError({str(marker)!r})\n")
    expected = hashlib.sha256(admission._canonical([
        [name, hashlib.sha256(package.joinpath(name).read_bytes()).hexdigest()]
        for name in sorted(SOURCE_NAMES)
    ])).hexdigest()
    assert admission._solders_sources_fingerprint() == expected
    assert not marker.exists()


@pytest.mark.parametrize("name", SOURCE_NAMES)
@pytest.mark.parametrize("state", ["missing", "empty", "directory", "fifo", "unreadable"])
def test_invalid_source_evidence_is_sanitized(tmp_path, monkeypatch, name, state):
    _repository, package = source_fixture(tmp_path, monkeypatch)
    source = package / name
    if state in {"missing", "directory", "fifo"}:
        source.unlink()
        if state == "directory":
            source.mkdir()
        elif state == "fifo":
            os.mkfifo(source)
    elif state == "empty":
        source.write_bytes(b"")
    else:
        original_open = os.open

        def opening(path, *args, **kwargs):
            if Path(path) == source:
                raise PermissionError("private installation detail")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", opening)
    with pytest.raises(admission.AdmissionError) as rejected:
        admission._solders_sources_fingerprint()
    assert str(rejected.value) == "runtime solders source evidence is unavailable"


@pytest.mark.parametrize("module", [
    "solders", "solders.transaction", "solders.system_program", "solders.sysvar",
    "solders.token",
])
@pytest.mark.parametrize("state", ["absent", "bytecode", "origin_mismatch", "relative", "package_paths"])
def test_invalid_source_discovery_is_rejected(tmp_path, monkeypatch, module, state):
    source_fixture(tmp_path, monkeypatch)
    original_find = PathFinder.find_spec

    def find_spec(name, path=None, target=None):
        found = original_find(name, path, target)
        if name != module:
            return found
        if state == "absent":
            return None
        assert found is not None and isinstance(found.origin, str)
        if state == "bytecode":
            found.loader = SourcelessFileLoader(name, found.origin)
        elif state == "origin_mismatch":
            found.origin += ".other"
        elif state == "relative":
            found.origin = Path(found.origin).name
            found.loader = SourceFileLoader(name, found.origin)
        elif state == "package_paths":
            found.submodule_search_locations = ["/unapproved"]
        return found

    monkeypatch.setattr(PathFinder, "find_spec", find_spec)
    with pytest.raises(admission.AdmissionError, match="solders source evidence is unavailable"):
        admission._solders_sources_fingerprint()


@pytest.mark.parametrize("state", [
    "not_package", "extra_search_path", "loader_path_mismatch", "flat_module",
])
def test_token_package_discovery_requires_exact_initializer(tmp_path, monkeypatch, state):
    source_fixture(tmp_path, monkeypatch)
    original_find = PathFinder.find_spec

    def find_spec(name, path=None, target=None):
        found = original_find(name, path, target)
        if name != "solders.token":
            return found
        assert found is not None and isinstance(found.origin, str)
        assert found.submodule_search_locations is not None
        if state == "not_package":
            found.submodule_search_locations = None
        elif state == "extra_search_path":
            found.submodule_search_locations.append("/unapproved")
        elif state == "loader_path_mismatch":
            found.loader = SourceFileLoader(name, found.origin + ".other")
        else:
            found.origin = str(Path(found.origin).parent.with_suffix(".py"))
            found.loader = SourceFileLoader(name, found.origin)
            found.submodule_search_locations = None
        return found

    monkeypatch.setattr(PathFinder, "find_spec", find_spec)
    with pytest.raises(admission.AdmissionError, match="solders source evidence is unavailable"):
        admission._solders_sources_fingerprint()


@pytest.mark.parametrize("change", [
    "in_place", "replacement", "truncation", "growth", "earlier_source", "discovery",
])
@pytest.mark.parametrize("name", [
    "transaction.py", "system_program.py", "transaction_status.py", "token/__init__.py",
])
def test_source_change_during_read_is_rejected(tmp_path, monkeypatch, change, name):
    _repository, package = source_fixture(tmp_path, monkeypatch)
    source = package / name
    original_fdopen = os.fdopen
    original_find = PathFinder.find_spec

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
                if change == "earlier_source":
                    package.joinpath("hash.py").write_bytes(b"changed after it was hashed")
                elif change == "discovery":
                    module_name = name.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
                    def find_spec(name, path=None, target=None):
                        return None if name == "solders." + module_name else original_find(name, path, target)
                    monkeypatch.setattr(PathFinder, "find_spec", find_spec)
                elif change == "replacement":
                    replacement = source.with_suffix(".new")
                    replacement.write_bytes(b"z" * len(chunk))
                    os.replace(replacement, source)
                elif change == "growth":
                    with source.open("ab") as writer:
                        writer.write(b"added bytes")
                else:
                    source.write_bytes(b"z" * len(chunk) if change == "in_place" else b"")
            return chunk

    def fdopen(descriptor, *args, **kwargs):
        stream = original_fdopen(descriptor, *args, **kwargs)
        if os.fstat(descriptor).st_ino == source.stat().st_ino:
            return ChangingReader(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", fdopen)
    with pytest.raises(admission.AdmissionError, match="solders source evidence is unavailable"):
        admission._solders_sources_fingerprint()


@pytest.mark.parametrize("change", ["mutation", "fifo"])
@pytest.mark.parametrize("name", ["transaction.py", "system_program.py", "token/__init__.py"])
def test_source_change_between_stat_and_open_is_rejected(tmp_path, monkeypatch, change, name):
    _repository, package = source_fixture(tmp_path, monkeypatch)
    source = package / name
    original_open = os.open

    def opening(path, flags, *args, **kwargs):
        if Path(path) == source:
            if change == "fifo":
                assert flags & os.O_NONBLOCK
                source.unlink()
                os.mkfifo(source)
            else:
                source.write_bytes(b"z" * source.stat().st_size)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    with pytest.raises(admission.AdmissionError, match="solders source evidence is unavailable"):
        admission._solders_sources_fingerprint()


@pytest.mark.parametrize("state", ["exact", "changed", "missing", "empty", "directory"])
@pytest.mark.parametrize("name", ["transaction.py", *INDIRECT_SOURCE_NAMES, "token/__init__.py"])
def test_source_admission_preserves_permit_until_exact_build(tmp_path, monkeypatch, state, name):
    repository, package = source_fixture(tmp_path, monkeypatch)
    source = package / name
    original = source.read_bytes()
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
        "deployment_id": "solders-source-offline", "generation": 0,
        "permit_nonce": "1" * 64, **admission.inspect_image(db),
        "config_sha256": "3" * 64, "build_sha256": approved,
        "issued_at": 1_800_000_000, "approval_rationale": "offline fixture only",
    })
    store = Store(tmp_path / "witness.db", runtime_token=runtime, admin_token=admin)
    store.issue_initial(cert, actor="offline-reviewer", rationale="fixture only", admin_token=admin)
    client = StoreClient(store, runtime)
    head = client.get_head(cert.deployment_id)
    if state == "changed":
        source.write_bytes(b"unapproved SDK wrapper")
    elif state == "empty":
        source.write_bytes(b"")
    elif state in {"missing", "directory"}:
        source.unlink()
        if state == "directory":
            source.mkdir()

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
        if source.is_dir():
            source.rmdir()
        source.write_bytes(original)

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
