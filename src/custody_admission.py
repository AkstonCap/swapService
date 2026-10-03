"""Fail-closed admission for an independently witnessed sealed SQLite image."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .custody_witness import Certificate, Client, WitnessError, loads_object


class AdmissionError(Exception):
    """Sanitized startup admission error."""


_HEX = frozenset("0123456789abcdef")
# Linux already supplies the live-process identity used by custody receipts. Unlike
# sys.executable or PATH, this identifies the executable actually running the check.
_INTERPRETER_PATH = Path("/proc/self/exe")
_MAPS_PATH = Path("/proc/self/maps")
_HEAD_FIELDS = frozenset({"status", "certificate", "claim_nonce", "event_hash", "hold_reason"})
_RECEIPT_FIELDS = frozenset({
    "phase", "deployment_id", "generation", "permit_nonce", "claim_nonce",
    "config_sha256", "build_sha256", "runtime_identity",
})


def _runtime_identity(path: Path, pid: int | None = None) -> dict[str, Any]:
    """Bind a running receipt to this file and a live writer, not a copied backup."""
    pid = os.getpid() if pid is None else pid
    try:
        stat = path.stat()
        # Split after comm's final ')' because a process name may contain spaces.
        process = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if process[0] == "Z":
            raise ValueError
        start_ticks = int(process[19])  # /proc stat field 22, relative to field 3
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"path_sha256": hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
                "device": stat.st_dev, "inode": stat.st_ino,
                "writer_pid": pid, "writer_start_ticks": start_ticks, "boot_id": boot}
    except (OSError, ValueError, IndexError) as exc:
        raise AdmissionError("live custody file identity is unavailable") from exc


def _identity_matches(path: Path, value: object) -> bool:
    try:
        if not isinstance(value, dict) or type(value.get("writer_pid")) is not int:
            return False
        return value == _runtime_identity(path, value["writer_pid"])
    except AdmissionError:
        return False


def _is_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _sidecars(path: Path) -> list[Path]:
    return [Path(str(path) + suffix) for suffix in ("-wal", "-journal", "-shm")]


def _require_sealed_file(path: Path) -> None:
    if not path.is_file():
        raise AdmissionError("sealed custody image is unavailable")
    if any(sidecar.exists() for sidecar in _sidecars(path)):
        raise AdmissionError("sealed custody image has ambiguous SQLite sidecars")


def _file_evidence(path: Path) -> tuple[str, int]:
    _require_sealed_file(path)
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb", buffering=0) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise AdmissionError("sealed custody image cannot be read") from exc
    if size <= 0:
        raise AdmissionError("sealed custody image is empty")
    return digest.hexdigest(), size


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
                               timeout=2)
    except sqlite3.Error as exc:
        raise AdmissionError("sealed custody image is not readable SQLite") from exc


def _schema_digest(path: Path) -> str:
    try:
        with closing(_readonly_connection(path)) as conn:
            rows = conn.execute("""
                SELECT type,name,tbl_name,sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type,name,tbl_name,sql
            """).fetchall()
    except sqlite3.Error as exc:
        raise AdmissionError("sealed custody schema cannot be verified") from exc
    return hashlib.sha256(_canonical([list(row) for row in rows])).hexdigest()


def _integrity_check(path: Path) -> None:
    try:
        with closing(_readonly_connection(path)) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise AdmissionError("sealed custody integrity check failed") from exc
    if rows != [("ok",)]:
        raise AdmissionError("sealed custody integrity check failed")


def inspect_image(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return the exact file and logical schema evidence used by certificates."""
    target = Path(path)
    image_sha256, image_size = _file_evidence(target)
    schema_sha256 = _schema_digest(target)
    return {
        "image_sha256": image_sha256,
        "image_size": image_size,
        "schema_sha256": schema_sha256,
    }


def _interpreter_fingerprint() -> str:
    """Hash running Linux interpreter bytes without executing a candidate binary."""
    digest = hashlib.sha256()
    size = 0

    def identity(evidence: os.stat_result) -> tuple[int, ...]:
        return (evidence.st_dev, evidence.st_ino, evidence.st_size,
                evidence.st_mtime_ns, evidence.st_ctime_ns)

    try:
        initial = _INTERPRETER_PATH.stat()
        if not stat.S_ISREG(initial.st_mode) or initial.st_size <= 0:
            raise AdmissionError("runtime interpreter evidence is unavailable")
        with _INTERPRETER_PATH.open("rb", buffering=0) as stream:
            if identity(os.fstat(stream.fileno())) != identity(initial):
                raise AdmissionError("runtime interpreter evidence is unavailable")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            if (size != initial.st_size
                    or identity(os.fstat(stream.fileno())) != identity(initial)
                    or identity(_INTERPRETER_PATH.stat()) != identity(initial)):
                raise AdmissionError("runtime interpreter evidence is unavailable")
    except OSError as exc:
        raise AdmissionError("runtime interpreter evidence is unavailable") from exc
    return digest.hexdigest()


def _native_fingerprint() -> str:
    """Hash conventional Linux interpreter/loader/libc/libm mapped files.

    Restrict this increment to foundational runtime libraries: service/dashboard
    extension import sets normally differ. Other libraries, kernel-provided pages,
    mapped-memory identity and not-yet-loaded code still require external attestation.
    """
    def mapped_paths() -> dict[Path, tuple[int, int, int]]:
        paths: dict[Path, tuple[int, int, int]] = {}
        for line in _MAPS_PATH.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=5)
            if (len(fields) < 5
                    or re.fullmatch(r"[0-9a-f]+-[0-9a-f]+", fields[0]) is None
                    or re.fullmatch(r"[r-][w-][x-][ps]", fields[1]) is None
                    or re.fullmatch(r"[0-9a-f]+", fields[2]) is None
                    or re.fullmatch(r"[0-9a-f]+:[0-9a-f]+", fields[3]) is None
                    or re.fullmatch(r"[0-9]+", fields[4]) is None):
                raise ValueError
            if "x" not in fields[1]:
                continue
            name = fields[5] if len(fields) == 6 else ""
            inode = int(fields[4])
            if name in {"[vdso]", "[vsyscall]"} and inode == 0:
                continue
            # Procfs escapes newline as \\012 ambiguously with literal backslashes;
            # unusual filenames require a separately reviewed immutable deployment.
            if (not name.startswith("/") or name.endswith(" (deleted)")
                    or "\\" in name or inode <= 0):
                raise ValueError
            major, minor = (int(part, 16) for part in fields[3].split(":"))
            path = Path(name)
            if re.fullmatch(
                r"(?:libpython[0-9]+\.[0-9]+[dt]?|lib[cm](?:-[0-9.]+)?|"
                r"ld(?:64|-linux[-\w]*|-musl[-\w]*|-[0-9.]+)?)\.so(?:\.[0-9]+)*",
                path.name,
            ) is None:
                continue
            expected = (major, minor, inode)
            if path in paths and paths[path] != expected:
                raise ValueError
            paths[path] = expected
        if not paths:
            raise ValueError
        return paths

    def identity(evidence: os.stat_result) -> tuple[int, ...]:
        return (evidence.st_dev, evidence.st_ino, evidence.st_mode, evidence.st_size,
                evidence.st_mtime_ns, evidence.st_ctime_ns)

    try:
        paths = mapped_paths()
        artifacts = []
        observed = {}
        for path, expected in sorted(paths.items()):
            initial = path.stat()
            if (not stat.S_ISREG(initial.st_mode) or initial.st_size <= 0
                    or (os.major(initial.st_dev), os.minor(initial.st_dev), initial.st_ino)
                    != expected):
                raise ValueError
            # Nonblocking open also refuses a raced-in FIFO instead of hanging.
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb", buffering=0) as stream:
                if identity(os.fstat(stream.fileno())) != identity(initial):
                    raise ValueError
                digest = hashlib.sha256()
                size = 0
                while chunk := stream.read(min(1024 * 1024, initial.st_size - size + 1)):
                    size += len(chunk)
                    if size > initial.st_size:
                        raise ValueError
                    digest.update(chunk)
                if (size != initial.st_size
                        or identity(os.fstat(stream.fileno())) != identity(initial)
                        or identity(path.stat()) != identity(initial)):
                    raise ValueError
            observed[path] = identity(initial)
            artifacts.append([str(path), digest.hexdigest()])
        if (mapped_paths() != paths
                or any(identity(path.stat()) != value for path, value in observed.items())):
            raise ValueError
        return hashlib.sha256(_canonical(artifacts)).hexdigest()
    except (OSError, UnicodeError, ValueError) as exc:
        raise AdmissionError("runtime native artifact evidence is unavailable") from exc


def build_fingerprint(root: str | os.PathLike[str] | None = None) -> str:
    """Bind service sources, requirements, interpreter and foundational native files.

    This in-process digest is a drift check, not pre-execution attestation. An
    independently trusted launcher must still verify every installed artifact,
    other shared library and executable byte before repository code executes.
    Conventional libpython/libc/libm/loader file hashes do not attest mapped memory,
    standard-library/bytecode, unrecognized library names or not-yet-loaded code.
    """
    repository = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    files = sorted((repository / "src").glob("*.py")) + [
        repository / "swapService.py", repository / "requirements.txt",
    ]
    digest = hashlib.sha256()
    for path in files:
        if not path.is_file():
            raise AdmissionError("runtime build fingerprint is incomplete")
        relative = path.relative_to(repository).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    # Domain separate interpreter evidence from the length-framed source manifest.
    # A binary upgrade intentionally invalidates previously approved build digests.
    digest.update(b"\x00swapservice-linux-interpreter-v1\x00")
    digest.update(bytes.fromhex(_interpreter_fingerprint()))
    digest.update(b"\x00swapservice-linux-native-mappings-v1\x00")
    digest.update(bytes.fromhex(_native_fingerprint()))
    return digest.hexdigest()


def configuration_fingerprint(config_module: Any = None) -> str:
    """Bind every effective static setting and independently pinned chain identity.

    Configuration is discovered by its public uppercase name so a new safety limit cannot
    silently fall outside the certificate.  Credential and transient session values are
    deliberately excluded (with their names recorded); locations are represented only by
    hashes so the published fingerprint cannot disclose private URLs or filesystem paths.
    """
    if config_module is None:
        from . import config as config_module

    import math
    from collections.abc import Mapping as MappingABC
    from enum import Enum
    from solders.hash import Hash

    location_terms = frozenset({"URL", "URI", "HOST", "PATH", "FILE", "COMMAND", "CLI"})

    def is_sensitive(name: str) -> bool:
        parts = set(name.split("_"))
        return bool(
            parts & {"PASSWORD", "SECRET"}
            or name.endswith((
                "_PIN", "_SESSION", "_API_KEY", "_ACCESS_KEY", "_PRIVATE_KEY",
                "_CREDENTIAL", "_CREDENTIALS", "_AUTH_TOKEN", "_SESSION_TOKEN",
                "_WITNESS_TOKEN",
            ))
            or name == "NEXUS_API_USER"
        )

    def normalized(value: object) -> object:
        if value is None or type(value) in {bool, int, str}:
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError
            return value
        if isinstance(value, Enum):
            return normalized(value.value)
        if hasattr(value, "__dataclass_fields__"):
            return normalized(asdict(value))
        if isinstance(value, MappingABC):
            result: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str) or key in result:
                    raise ValueError
                result[key] = normalized(item)
            return result
        if isinstance(value, (list, tuple)):
            return [normalized(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [normalized(item) for item in value]
            return sorted(items, key=lambda item: _canonical(item))
        if isinstance(value, Path):
            return str(value)
        # Public SDK identities such as solders.Pubkey have a stable canonical string but
        # are not JSON values.  Reject generic object reprs containing process addresses.
        text = str(value)
        if not text or (text.startswith("<") and " at 0x" in text):
            raise ValueError
        return text

    try:
        solana_genesis = os.getenv("CUSTODY_SOLANA_GENESIS_HASH")
        nexus_genesis = os.getenv("CUSTODY_NEXUS_GENESIS_HASH")
        if not solana_genesis or not nexus_genesis:
            raise AdmissionError("custody chain identity configuration missing")
        try:
            valid_solana_genesis = (
                solana_genesis == solana_genesis.strip()
                and str(Hash.from_string(solana_genesis)) == solana_genesis
            )
        except Exception:
            valid_solana_genesis = False
        if (not valid_solana_genesis
                or nexus_genesis != nexus_genesis.strip()
                or len(nexus_genesis) != 256
                or any(character not in _HEX for character in nexus_genesis)):
            raise AdmissionError("custody chain identity configuration invalid")

        settings: dict[str, object] = {}
        excluded: list[str] = []
        hashed: list[str] = []
        for name, value in sorted(vars(config_module).items()):
            if not name.isupper() or name.startswith("_"):
                continue
            if is_sensitive(name):
                excluded.append(name)
                continue
            safe_value = normalized(value)
            if set(name.split("_")) & location_terms:
                settings[name] = {
                    "sha256": hashlib.sha256(_canonical(safe_value)).hexdigest(),
                }
                hashed.append(name)
            else:
                settings[name] = safe_value

        identity = {
            "format": "custody-effective-config-v2",
            "settings": settings,
            "hashed_location_settings": hashed,
            "excluded_sensitive_settings": excluded,
            "expected_chain_identity": {
                "solana_genesis_hash": solana_genesis,
                "nexus_height_0_block_hash": nexus_genesis,
            },
        }
        return hashlib.sha256(_canonical(identity)).hexdigest()
    except AdmissionError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise AdmissionError("custody deployment identity is incomplete") from exc


def _parse_head(value: object) -> tuple[str, Certificate, str | None, str | None]:
    try:
        if not isinstance(value, dict) or set(value) != _HEAD_FIELDS:
            raise ValueError
        status = value["status"]
        if status not in {"ready", "claimed", "running", "held"}:
            raise ValueError
        certificate = Certificate.from_dict(value["certificate"])
        claim_nonce = value["claim_nonce"]
        if claim_nonce is not None and not _is_hex(claim_nonce):
            raise ValueError
        if not _is_hex(value["event_hash"]):
            raise ValueError
        hold_reason = value["hold_reason"]
        if hold_reason is not None and not isinstance(hold_reason, str):
            raise ValueError
        return status, certificate, claim_nonce, hold_reason
    except (TypeError, ValueError, WitnessError) as exc:
        raise AdmissionError("witness returned an invalid head") from exc


def _matches_claimed(head: object, cert: Certificate, claim_nonce: str) -> bool:
    try:
        status, observed, observed_claim, _reason = _parse_head(head)
    except AdmissionError:
        return False
    return status == "claimed" and observed == cert and observed_claim == claim_nonce


def _matches_running(head: object, cert: Certificate, claim_nonce: str) -> bool:
    try:
        status, observed, observed_claim, _reason = _parse_head(head)
    except AdmissionError:
        return False
    return status == "running" and observed == cert and observed_claim == claim_nonce


def _receipt_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".admission.json")


def _write_receipt(path: Path, value: Mapping[str, Any]) -> None:
    data = _canonical(dict(value)) + b"\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise AdmissionError("local custody receipt could not be persisted") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_receipt(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="ascii")
        value = loads_object(raw)
        if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
            raise ValueError
        if value["phase"] not in {"claimed", "running", "sealed", "held"}:
            raise ValueError
        if (not isinstance(value["generation"], int)
                or isinstance(value["generation"], bool) or value["generation"] < 0):
            raise ValueError
        for field in ("permit_nonce", "claim_nonce", "config_sha256", "build_sha256"):
            if not _is_hex(value[field]):
                raise ValueError
        if not isinstance(value["deployment_id"], str):
            raise ValueError
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError,
            WitnessError) as exc:
        raise AdmissionError("local custody receipt is unavailable") from exc


def _resolve(db_path: str | os.PathLike[str] | None, client: Any,
             deployment_id: str | None, config_sha256: str | None,
             build_sha256: str | None) -> tuple[Path, Any, str, str, str]:
    if db_path is None:
        from . import state_db
        db_path = state_db.DB_PATH
    if client is None:
        url = os.getenv("CUSTODY_WITNESS_URL", "").strip()
        token = os.getenv("CUSTODY_WITNESS_TOKEN", "")
        deployment_id = deployment_id or os.getenv("CUSTODY_DEPLOYMENT_ID", "").strip()
        if not url or not token or not deployment_id:
            raise AdmissionError("custody witness configuration missing")
        try:
            client = Client(url, token)
        except WitnessError as exc:
            raise AdmissionError("custody witness configuration invalid") from exc
    if deployment_id is None:
        deployment_id = os.getenv("CUSTODY_DEPLOYMENT_ID", "").strip()
    if not isinstance(deployment_id, str) or not deployment_id:
        raise AdmissionError("custody witness configuration missing")
    config_sha256 = config_sha256 or configuration_fingerprint()
    build_sha256 = build_sha256 or build_fingerprint()
    if not _is_hex(config_sha256) or not _is_hex(build_sha256):
        raise AdmissionError("custody deployment identity is invalid")
    return Path(db_path), client, deployment_id, config_sha256, build_sha256


@dataclass
class Lease:
    db_path: Path
    client: Any
    certificate: Certificate
    claim_nonce: str
    config_sha256: str
    build_sha256: str
    runtime_identity: dict[str, Any]
    image_fd: int
    phase: str = "claimed"

    def check_file_identity(self) -> None:
        try:
            stat = os.fstat(self.image_fd)
            exact = (stat.st_dev == self.runtime_identity['device']
                     and stat.st_ino == self.runtime_identity['inode'])
        except OSError:
            exact = False
        if not exact or not _identity_matches(self.db_path, self.runtime_identity):
            raise AdmissionError("custody file identity changed")

    def verify_image(self) -> None:
        """Revalidate the retained artifact immediately before mutable startup."""
        self.check_file_identity()
        evidence = _file_evidence(self.db_path)
        if evidence != (self.certificate.image_sha256, self.certificate.image_size):
            raise AdmissionError("sealed custody image changed before startup")
        self.check_file_identity()

    def _close_image(self) -> None:
        if self.image_fd >= 0:
            os.close(self.image_fd)
            self.image_fd = -1

    @property
    def generation(self) -> int:
        return self.certificate.generation

    def _receipt(self, phase: str) -> dict[str, Any]:
        return {
            "phase": phase,
            "deployment_id": self.certificate.deployment_id,
            "generation": self.certificate.generation,
            "permit_nonce": self.certificate.permit_nonce,
            "claim_nonce": self.claim_nonce,
            "config_sha256": self.config_sha256,
            "build_sha256": self.build_sha256,
            "runtime_identity": self.runtime_identity,
        }

    def _remote_head(self) -> object:
        try:
            return self.client.get_head(self.certificate.deployment_id)
        except Exception as exc:
            raise AdmissionError("witness readback failed") from exc

    def _permanent_hold(self, reason: str) -> None:
        try:
            self.client.hold(self.certificate.deployment_id, self.generation,
                             self.claim_nonce, reason)
        except Exception:
            pass
        try:
            _write_receipt(_receipt_path(self.db_path), self._receipt("held"))
        except AdmissionError:
            pass
        self.phase = "held"
        self._close_image()

    def complete(self) -> None:
        if self.phase != "claimed":
            raise AdmissionError("custody lease is not claimable")
        _write_receipt(_receipt_path(self.db_path), self._receipt("claimed"))
        try:
            result = self.client.complete(self.certificate.deployment_id, self.generation,
                                          self.claim_nonce)
            if not _matches_running(result, self.certificate, self.claim_nonce):
                raise AdmissionError("witness completion response is not exact")
        except Exception as exc:
            try:
                exact = _matches_running(self._remote_head(), self.certificate, self.claim_nonce)
            except AdmissionError:
                exact = False
            if not exact:
                self._permanent_hold("completion outcome unavailable")
                raise AdmissionError("custody lease completion failed") from exc
        _write_receipt(_receipt_path(self.db_path), self._receipt("running"))
        self.phase = "running"

    def assert_running(self) -> None:
        if self.phase != "running" or not _identity_matches(self.db_path, self.runtime_identity):
            raise AdmissionError("custody lease is not running")
        if not _matches_running(self._remote_head(), self.certificate, self.claim_nonce):
            raise AdmissionError("custody lease is not running")
        receipt = _read_receipt(_receipt_path(self.db_path))
        if receipt != self._receipt("running"):
            raise AdmissionError("local custody receipt does not match running lease")

    def hold(self, reason: str = "runtime custody hold") -> None:
        if self.phase not in {"claimed", "running"}:
            raise AdmissionError("custody lease cannot be held")
        self._permanent_hold(reason)
        try:
            status, _cert, observed_claim, _reason = _parse_head(self._remote_head())
        except AdmissionError as exc:
            raise AdmissionError("custody hold outcome unavailable") from exc
        if status != "held" or observed_claim != self.claim_nonce:
            raise AdmissionError("custody hold outcome unavailable")

    def seal(self) -> Certificate:
        self.assert_running()
        try:
            self.check_file_identity()
            connection = sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5,
            )
            try:
                self.check_file_identity()
                connection.execute("PRAGMA busy_timeout=5000")
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or checkpoint[0] != 0:
                    raise AdmissionError("custody checkpoint is busy")
                connection.execute("BEGIN EXCLUSIVE")
                rows = connection.execute("PRAGMA integrity_check").fetchall()
                if rows != [("ok",)]:
                    raise AdmissionError("custody integrity check failed")
                connection.commit()
            finally:
                connection.close()
            self.check_file_identity()
            _require_sealed_file(self.db_path)
            descriptor = os.dup(self.image_fd)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            directory_fd = os.open(self.db_path.parent,
                                   os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            evidence = inspect_image(self.db_path)
            self.check_file_identity()
            next_certificate = Certificate.from_dict({
                "deployment_id": self.certificate.deployment_id,
                "generation": self.generation + 1,
                "permit_nonce": secrets.token_hex(32),
                "image_sha256": evidence["image_sha256"],
                "image_size": evidence["image_size"],
                "config_sha256": self.config_sha256,
                "build_sha256": self.build_sha256,
                "schema_sha256": evidence["schema_sha256"],
                "issued_at": int(time.time()),
                "approval_rationale": "graceful quiescent seal from approved running lease",
            })
            try:
                result = self.client.seal(
                    self.certificate.deployment_id, self.generation, self.claim_nonce,
                    next_certificate.to_dict(),
                )
                status, observed, observed_claim, _reason = _parse_head(result)
                exact = status == "ready" and observed == next_certificate and observed_claim is None
                if not exact:
                    raise AdmissionError("witness seal response is not exact")
            except Exception as exc:
                try:
                    status, observed, observed_claim, _reason = _parse_head(self._remote_head())
                    exact = (status == "ready" and observed == next_certificate
                             and observed_claim is None)
                except AdmissionError:
                    exact = False
                if not exact:
                    self._permanent_hold("seal outcome unavailable")
                    raise AdmissionError("custody seal failed") from exc
        except AdmissionError:
            if self.phase != "held":
                self._permanent_hold("local seal failed")
            raise
        except (OSError, sqlite3.Error, WitnessError) as exc:
            self._permanent_hold("local seal failed")
            raise AdmissionError("custody seal failed") from exc
        self.certificate = next_certificate
        self.phase = "sealed"
        self._close_image()
        _write_receipt(_receipt_path(self.db_path), self._receipt("sealed"))
        return next_certificate


def claim(*, db_path: str | os.PathLike[str] | None = None, client: Any = None,
          deployment_id: str | None = None, config_sha256: str | None = None,
          build_sha256: str | None = None) -> Lease:
    """Atomically consume the exact externally certified sealed image permit."""
    path, client, deployment_id, config_sha256, build_sha256 = _resolve(
        db_path, client, deployment_id, config_sha256, build_sha256,
    )
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError as exc:
        raise AdmissionError("sealed custody image is unavailable") from exc
    try:
        return _claim_file(path, client, deployment_id, config_sha256, build_sha256, descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _claim_file(path, client, deployment_id, config_sha256, build_sha256, descriptor):
    identity = _runtime_identity(path)
    stat = os.fstat(descriptor)
    if (stat.st_dev, stat.st_ino) != (identity['device'], identity['inode']):
        raise AdmissionError("custody file identity changed")
    image_sha256, image_size = _file_evidence(path)
    try:
        head = client.get_head(deployment_id)
    except Exception as exc:
        raise AdmissionError("custody witness admission failed") from exc
    status, certificate, observed_claim, _reason = _parse_head(head)
    if status != "ready" or observed_claim is not None:
        raise AdmissionError("custody witness has no ready permit")
    if (certificate.deployment_id != deployment_id
            or certificate.config_sha256 != config_sha256
            or certificate.build_sha256 != build_sha256):
        raise AdmissionError("sealed image deployment identity does not match")
    if (certificate.image_sha256 != image_sha256
            or certificate.image_size != image_size):
        raise AdmissionError("sealed image does not match witness certificate")

    claim_nonce = secrets.token_hex(32)
    claimed = False
    try:
        try:
            result = client.claim(deployment_id, certificate.generation,
                                  certificate.permit_nonce, claim_nonce)
            if not _matches_claimed(result, certificate, claim_nonce):
                raise AdmissionError("witness claim response is not exact")
            claimed = True
        except Exception as exc:
            try:
                exact = _matches_claimed(client.get_head(deployment_id), certificate, claim_nonce)
            except Exception:
                exact = False
            if not exact:
                try:
                    client.hold(deployment_id, certificate.generation, claim_nonce,
                                "claim outcome unavailable")
                except Exception:
                    pass
                raise AdmissionError("custody claim outcome is not exact") from exc
            claimed = True

        second_hash, second_size = _file_evidence(path)
        if (second_hash, second_size) != (certificate.image_sha256, certificate.image_size):
            raise AdmissionError("sealed image changed during admission")
        _integrity_check(path)
        if _schema_digest(path) != certificate.schema_sha256:
            raise AdmissionError("sealed custody schema does not match")
        if (not _identity_matches(path, identity)
                or _file_evidence(path) != (certificate.image_sha256, certificate.image_size)
                or not _identity_matches(path, identity)):
            raise AdmissionError("custody file identity changed")
    except Exception as exc:
        if claimed:
            try:
                client.hold(deployment_id, certificate.generation, claim_nonce,
                            "post-claim image validation failed")
            except Exception:
                pass
        if isinstance(exc, AdmissionError):
            raise
        raise AdmissionError("post-claim custody validation failed") from exc

    return Lease(path, client, certificate, claim_nonce, config_sha256, build_sha256,
                 identity, descriptor)


def dashboard_status(*, db_path: str | os.PathLike[str] | None = None,
                     client: Any = None, deployment_id: str | None = None,
                     config_sha256: str | None = None,
                     build_sha256: str | None = None) -> dict[str, Any]:
    """Report healthy only from matching external running head plus local receipt."""
    def result(status: str, reason: str | None, detail: str,
               action: str) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "detail": detail,
            "operator_action": action,
            "liabilities_complete": False,
        }

    try:
        path, client, deployment_id, config_sha256, build_sha256 = _resolve(
            db_path, client, deployment_id, config_sha256, build_sha256,
        )
        status, certificate, claim_nonce, _hold_reason = _parse_head(
            client.get_head(deployment_id)
        )
        receipt = _read_receipt(_receipt_path(path))
        expected = {
            "phase": status, "deployment_id": deployment_id,
            "generation": certificate.generation, "permit_nonce": certificate.permit_nonce,
            "claim_nonce": claim_nonce, "config_sha256": config_sha256,
            "build_sha256": build_sha256, "runtime_identity": receipt.get("runtime_identity"),
        }
        if (status in {"running", "held"} and claim_nonce is not None
                and _identity_matches(path, receipt.get("runtime_identity"))
                and certificate.deployment_id == deployment_id
                and certificate.config_sha256 == config_sha256
                and certificate.build_sha256 == build_sha256
                and receipt == expected):
            if status == "held":
                return result("held", "external_witness_held",
                              "custody witness permanently holds this generation",
                              "keep service stopped and perform audited custody resolution")
            healthy = result("not_held", None, "sealed custody lease is running", "none")
            healthy['lease_identity'] = {
                'generation': certificate.generation, 'permit_nonce': certificate.permit_nonce,
                'claim_nonce': claim_nonce,
            }
            return healthy
        return result("unknown", "custody_admission_mismatch",
                      "custody admission evidence does not match",
                      "keep service stopped and inspect witness and local receipt")
    except Exception:
        return result("unknown", "custody_admission_unavailable",
                      "custody admission evidence is unavailable",
                      "keep service stopped and restore verified admission evidence")
