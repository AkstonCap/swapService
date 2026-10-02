"""Independent anti-rollback witness for sealed custody database images.

The witness database belongs on a separately protected host.  Runtime credentials can
consume and advance permits but cannot create the first certificate or revive a hold.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit

import requests


class WitnessError(Exception):
    """Sanitized witness failure safe to surface to the custody runtime."""


_CERTIFICATE_FIELDS = frozenset({
    "deployment_id", "generation", "permit_nonce", "image_sha256", "image_size",
    "config_sha256", "build_sha256", "schema_sha256", "issued_at",
    "approval_rationale",
})
_HEX = frozenset("0123456789abcdef")


def _is_exact_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hex(value: object, length: int = 64) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in _HEX for c in value)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def loads_object(raw: str | bytes) -> dict[str, Any]:
    """Decode one strict JSON object, rejecting duplicate member names."""
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise WitnessError("invalid JSON payload")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except WitnessError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WitnessError("invalid JSON payload") from exc
    if not isinstance(value, dict):
        raise WitnessError("invalid JSON payload")
    return value


@dataclass(frozen=True)
class Certificate:
    deployment_id: str
    generation: int
    permit_nonce: str
    image_sha256: str
    image_size: int
    config_sha256: str
    build_sha256: str
    schema_sha256: str
    issued_at: int
    approval_rationale: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Certificate":
        try:
            if not isinstance(value, Mapping) or set(value) != _CERTIFICATE_FIELDS:
                raise ValueError
            deployment_id = value["deployment_id"]
            rationale = value["approval_rationale"]
            if (not isinstance(deployment_id, str) or not deployment_id.strip()
                    or len(deployment_id) > 128 or deployment_id != deployment_id.strip()):
                raise ValueError
            if (not isinstance(rationale, str) or not rationale.strip()
                    or len(rationale) > 1000 or rationale != rationale.strip()):
                raise ValueError
            if not _is_exact_int(value["generation"]) or value["generation"] < 0:
                raise ValueError
            if not _is_exact_int(value["image_size"]) or value["image_size"] <= 0:
                raise ValueError
            if not _is_exact_int(value["issued_at"]) or value["issued_at"] < 0:
                raise ValueError
            for name in ("permit_nonce", "image_sha256", "config_sha256",
                         "build_sha256", "schema_sha256"):
                if not _is_hex(value[name]):
                    raise ValueError
            return cls(**{name: value[name] for name in _CERTIFICATE_FIELDS})
        except (KeyError, TypeError, ValueError) as exc:
            raise WitnessError("invalid certificate") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "generation": self.generation,
            "permit_nonce": self.permit_nonce,
            "image_sha256": self.image_sha256,
            "image_size": self.image_size,
            "config_sha256": self.config_sha256,
            "build_sha256": self.build_sha256,
            "schema_sha256": self.schema_sha256,
            "issued_at": self.issued_at,
            "approval_rationale": self.approval_rationale,
        }


class Store:
    """Transactional reference witness backed by a separate SQLite database."""

    def __init__(self, path: str | Path, *, runtime_token: str,
                 admin_token: str | None = None):
        if not isinstance(runtime_token, str) or len(runtime_token) < 32:
            raise WitnessError("witness requires strong distinct capabilities")
        if (admin_token is not None
                and (not isinstance(admin_token, str) or len(admin_token) < 32
                     or hmac.compare_digest(runtime_token, admin_token))):
            raise WitnessError("witness requires strong distinct capabilities")
        self.path = str(path)
        self._runtime_token = runtime_token
        self._admin_token = admin_token
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS heads (
                    deployment_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('ready','claimed','running','held')),
                    generation INTEGER NOT NULL,
                    permit_nonce TEXT NOT NULL,
                    claim_nonce TEXT,
                    certificate_json TEXT NOT NULL,
                    hold_reason TEXT,
                    event_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    deployment_id TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update
                    BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                    BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
            """)

    @staticmethod
    def _authorized(supplied: str, expected: str | None) -> bool:
        return (isinstance(supplied, str) and isinstance(expected, str)
                and hmac.compare_digest(supplied, expected))

    def _require_runtime(self, token: str) -> None:
        if not self._authorized(token, self._runtime_token):
            raise WitnessError("unauthorized witness request")

    def _require_admin(self, token: str) -> None:
        if not self._authorized(token, self._admin_token):
            raise WitnessError("unauthorized witness request")

    @staticmethod
    def _row_head(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "status": row["status"],
            "certificate": loads_object(row["certificate_json"]),
            "claim_nonce": row["claim_nonce"],
            "event_hash": row["event_hash"],
            "hold_reason": row["hold_reason"],
        }

    @staticmethod
    def _validate_claim_nonce(value: object) -> str:
        if not _is_hex(value):
            raise WitnessError("invalid witness request")
        return str(value)

    def _append_event(self, conn: sqlite3.Connection, deployment_id: str,
                      action: str, details: Mapping[str, Any]) -> str:
        previous = conn.execute(
            "SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        prev_hash = previous[0] if previous else "0" * 64
        body = _canonical({
            "action": action,
            "deployment_id": deployment_id,
            "details": dict(details),
            "recorded_at": int(time.time()),
        })
        event_hash = hashlib.sha256((prev_hash + body).encode("ascii")).hexdigest()
        conn.execute(
            "INSERT INTO events(deployment_id,prev_hash,event_hash,event_json) VALUES(?,?,?,?)",
            (deployment_id, prev_hash, event_hash, body),
        )
        return event_hash

    def issue_initial(self, certificate: Certificate | Mapping[str, Any], *, actor: str,
                      rationale: str, admin_token: str) -> dict[str, Any]:
        self._require_admin(admin_token)
        cert = certificate if isinstance(certificate, Certificate) else Certificate.from_dict(certificate)
        if (not isinstance(actor, str) or not actor.strip() or len(actor) > 128
                or not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 1000):
            raise WitnessError("invalid administrative issuance")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM heads WHERE deployment_id=?",
                            (cert.deployment_id,)).fetchone():
                raise WitnessError("deployment already exists")
            event_hash = self._append_event(conn, cert.deployment_id, "issue", {
                "actor": actor.strip(), "rationale": rationale.strip(),
                "generation": cert.generation, "permit_nonce": cert.permit_nonce,
            })
            conn.execute(
                "INSERT INTO heads VALUES(?,?,?,?,?,?,?,?)",
                (cert.deployment_id, "ready", cert.generation, cert.permit_nonce,
                 None, _canonical(cert.to_dict()), None, event_hash),
            )
            row = conn.execute("SELECT * FROM heads WHERE deployment_id=?",
                               (cert.deployment_id,)).fetchone()
            return self._row_head(row)

    def get_head(self, deployment_id: str, *, token: str) -> dict[str, Any]:
        self._require_runtime(token)
        if not isinstance(deployment_id, str) or not deployment_id:
            raise WitnessError("invalid witness request")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM heads WHERE deployment_id=?",
                               (deployment_id,)).fetchone()
        if row is None:
            raise WitnessError("witness deployment not found")
        return self._row_head(row)

    def claim(self, deployment_id: str, generation: int, permit_nonce: str,
              claim_nonce: str, *, token: str) -> dict[str, Any]:
        self._require_runtime(token)
        claim_nonce = self._validate_claim_nonce(claim_nonce)
        if not _is_exact_int(generation) or generation < 0:
            raise WitnessError("invalid witness request")
        if not _is_exact_int(generation) or not _is_hex(permit_nonce):
            raise WitnessError("invalid witness request")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM heads WHERE deployment_id=?",
                               (deployment_id,)).fetchone()
            if row is None:
                raise WitnessError("witness deployment not found")
            if (row["status"] == "claimed" and row["generation"] == generation
                    and hmac.compare_digest(row["permit_nonce"], permit_nonce)
                    and hmac.compare_digest(row["claim_nonce"], claim_nonce)):
                return self._row_head(row)
            if (row["status"] != "ready" or row["generation"] != generation
                    or not hmac.compare_digest(row["permit_nonce"], permit_nonce)):
                raise WitnessError("stale or consumed witness permit")
            event_hash = self._append_event(conn, deployment_id, "claim", {
                "generation": generation, "claim_nonce": claim_nonce,
                "permit_nonce": permit_nonce,
            })
            conn.execute(
                "UPDATE heads SET status='claimed',claim_nonce=?,event_hash=? WHERE deployment_id=?",
                (claim_nonce, event_hash, deployment_id),
            )
            row = conn.execute("SELECT * FROM heads WHERE deployment_id=?",
                               (deployment_id,)).fetchone()
            return self._row_head(row)

    def complete(self, deployment_id: str, generation: int, claim_nonce: str,
                 *, token: str) -> dict[str, Any]:
        self._require_runtime(token)
        claim_nonce = self._validate_claim_nonce(claim_nonce)
        if not _is_exact_int(generation) or generation < 0:
            raise WitnessError("invalid witness request")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM heads WHERE deployment_id=?",
                               (deployment_id,)).fetchone()
            if row is None:
                raise WitnessError("witness deployment not found")
            if row["status"] == "held":
                raise WitnessError("witness generation permanently held")
            if (row["generation"] != generation or row["claim_nonce"] is None
                    or not hmac.compare_digest(row["claim_nonce"], claim_nonce)):
                raise WitnessError("witness claim ownership mismatch")
            if row["status"] == "running":
                return self._row_head(row)
            if row["status"] != "claimed":
                raise WitnessError("invalid witness transition")
            event_hash = self._append_event(conn, deployment_id, "complete", {
                "generation": generation, "claim_nonce": claim_nonce,
            })
            conn.execute("UPDATE heads SET status='running',event_hash=? WHERE deployment_id=?",
                         (event_hash, deployment_id))
            return self._row_head(conn.execute(
                "SELECT * FROM heads WHERE deployment_id=?", (deployment_id,)
            ).fetchone())

    def seal(self, deployment_id: str, generation: int, claim_nonce: str,
             next_certificate: Mapping[str, Any], *, token: str) -> dict[str, Any]:
        self._require_runtime(token)
        claim_nonce = self._validate_claim_nonce(claim_nonce)
        if not _is_exact_int(generation) or generation < 0:
            raise WitnessError("invalid witness request")
        cert = Certificate.from_dict(next_certificate)
        if cert.deployment_id != deployment_id or cert.generation != generation + 1:
            raise WitnessError("invalid next generation certificate")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM heads WHERE deployment_id=?",
                               (deployment_id,)).fetchone()
            if row is None:
                raise WitnessError("witness deployment not found")
            if row["status"] == "held":
                raise WitnessError("witness generation permanently held")
            if (row["generation"] != generation or row["claim_nonce"] is None
                    or not hmac.compare_digest(row["claim_nonce"], claim_nonce)):
                raise WitnessError("witness claim ownership mismatch")
            if row["status"] != "running":
                raise WitnessError("invalid witness transition")
            event_hash = self._append_event(conn, deployment_id, "seal", {
                "generation": generation, "next_generation": cert.generation,
                "claim_nonce": claim_nonce, "next_permit_nonce": cert.permit_nonce,
                "image_sha256": cert.image_sha256,
            })
            conn.execute("""
                UPDATE heads SET status='ready',generation=?,permit_nonce=?,claim_nonce=NULL,
                    certificate_json=?,hold_reason=NULL,event_hash=? WHERE deployment_id=?
            """, (cert.generation, cert.permit_nonce, _canonical(cert.to_dict()),
                  event_hash, deployment_id))
            return self._row_head(conn.execute(
                "SELECT * FROM heads WHERE deployment_id=?", (deployment_id,)
            ).fetchone())

    def hold(self, deployment_id: str, generation: int, claim_nonce: str, reason: str,
             *, token: str) -> dict[str, Any]:
        self._require_runtime(token)
        claim_nonce = self._validate_claim_nonce(claim_nonce)
        if not _is_exact_int(generation) or generation < 0:
            raise WitnessError("invalid witness request")
        if (not isinstance(reason, str) or not reason.strip() or len(reason) > 500
                or reason != reason.strip()):
            raise WitnessError("invalid witness request")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM heads WHERE deployment_id=?",
                               (deployment_id,)).fetchone()
            if row is None:
                raise WitnessError("witness deployment not found")
            if (row["generation"] != generation or row["claim_nonce"] is None
                    or not hmac.compare_digest(row["claim_nonce"], claim_nonce)):
                raise WitnessError("witness claim ownership mismatch")
            if row["status"] == "held":
                if row["hold_reason"] == reason:
                    return self._row_head(row)
                raise WitnessError("witness generation permanently held")
            if row["status"] not in {"claimed", "running"}:
                raise WitnessError("invalid witness transition")
            event_hash = self._append_event(conn, deployment_id, "hold", {
                "generation": generation, "claim_nonce": claim_nonce, "reason": reason,
            })
            conn.execute("UPDATE heads SET status='held',hold_reason=?,event_hash=? WHERE deployment_id=?",
                         (reason, event_hash, deployment_id))
            return self._row_head(conn.execute(
                "SELECT * FROM heads WHERE deployment_id=?", (deployment_id,)
            ).fetchone())


class StoreClient:
    """In-process client used by tests and explicitly local deployments."""

    def __init__(self, store: Store, token: str):
        self._store = store
        self._token = token

    def get_head(self, deployment_id: str) -> dict[str, Any]:
        return self._store.get_head(deployment_id, token=self._token)

    def claim(self, deployment_id: str, generation: int, permit_nonce: str,
              claim_nonce: str) -> dict[str, Any]:
        return self._store.claim(deployment_id, generation, permit_nonce, claim_nonce,
                                 token=self._token)

    def complete(self, deployment_id: str, generation: int,
                 claim_nonce: str) -> dict[str, Any]:
        return self._store.complete(deployment_id, generation, claim_nonce, token=self._token)

    def seal(self, deployment_id: str, generation: int, claim_nonce: str,
             next_certificate: Mapping[str, Any]) -> dict[str, Any]:
        return self._store.seal(deployment_id, generation, claim_nonce, next_certificate,
                                token=self._token)

    def hold(self, deployment_id: str, generation: int, claim_nonce: str,
             reason: str) -> dict[str, Any]:
        return self._store.hold(deployment_id, generation, claim_nonce, reason,
                                token=self._token)


class HTTPApplication:
    """Strict runtime HTTP API adapter; expose it only behind authenticated TLS."""

    def __init__(self, store: Store, *, max_body: int = 64 * 1024):
        if not _is_exact_int(max_body) or not 1024 <= max_body <= 1024 * 1024:
            raise WitnessError("invalid HTTP body limit")
        self._store = store
        self._max_body = max_body

    @staticmethod
    def _response(status: int, value: Mapping[str, Any]) -> tuple[int, dict[str, str], bytes]:
        body = _canonical(dict(value)).encode("ascii")
        return status, {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        }, body

    @staticmethod
    def _exact(payload: Mapping[str, Any], fields: set[str]) -> None:
        if set(payload) != fields:
            raise WitnessError("invalid witness request")

    def dispatch(self, method: str, path: str, headers: Mapping[str, str],
                 body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
        """Dispatch one already-framed HTTP request without logging credentials."""
        try:
            if not isinstance(body, bytes) or len(body) > self._max_body:
                raise WitnessError("invalid witness request")
            authorization = headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                return self._response(401, {"error": "request rejected"})
            token = authorization[7:]
            parts = path.split("?")[0].split("/")
            if len(parts) not in {5, 6} or parts[1:3] != ["v1", "deployments"]:
                return self._response(404, {"error": "request rejected"})
            deployment_id = unquote(parts[3])
            action = parts[4] if len(parts) == 5 else ""
            if method == "GET" and action == "head" and not body:
                return self._response(200, self._store.get_head(deployment_id, token=token))
            if method != "POST" or action not in {"claim", "complete", "seal", "hold"}:
                return self._response(404, {"error": "request rejected"})
            if headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise WitnessError("invalid witness request")
            payload = loads_object(body)
            if action == "claim":
                self._exact(payload, {"generation", "permit_nonce", "claim_nonce"})
                result = self._store.claim(deployment_id, payload["generation"],
                                           payload["permit_nonce"], payload["claim_nonce"],
                                           token=token)
            elif action == "complete":
                self._exact(payload, {"generation", "claim_nonce"})
                result = self._store.complete(deployment_id, payload["generation"],
                                              payload["claim_nonce"], token=token)
            elif action == "seal":
                self._exact(payload, {"generation", "claim_nonce", "certificate"})
                if not isinstance(payload["certificate"], dict):
                    raise WitnessError("invalid witness request")
                result = self._store.seal(deployment_id, payload["generation"],
                                          payload["claim_nonce"], payload["certificate"],
                                          token=token)
            else:
                self._exact(payload, {"generation", "claim_nonce", "reason"})
                result = self._store.hold(deployment_id, payload["generation"],
                                          payload["claim_nonce"], payload["reason"],
                                          token=token)
            return self._response(200, result)
        except WitnessError as exc:
            status = 401 if "unauthorized" in str(exc) else 400
            if any(word in str(exc) for word in ("stale", "transition", "ownership", "held")):
                status = 409
            return self._response(status, {"error": "request rejected"})
        except Exception:
            return self._response(500, {"error": "request failed"})


class Client:
    """Authenticated HTTPS witness client with bounded I/O and no redirects."""

    def __init__(self, base_url: str, token: str, *, session: Any = None,
                 timeout: tuple[float, float] = (2.0, 5.0),
                 max_response: int = 64 * 1024):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise WitnessError("witness URL must be an HTTPS origin")
        if not isinstance(token, str) or len(token) < 32:
            raise WitnessError("invalid witness configuration")
        if (not isinstance(timeout, tuple) or len(timeout) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or v <= 0 or v > 30 for v in timeout)):
            raise WitnessError("invalid witness timeout")
        if not _is_exact_int(max_response) or not 1024 <= max_response <= 1024 * 1024:
            raise WitnessError("invalid witness response limit")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
        self._timeout = timeout
        self._max_response = max_response

    def _request(self, method: str, deployment_id: str, action: str,
                 payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if (not isinstance(deployment_id, str) or not deployment_id or "/" in deployment_id
                or len(deployment_id) > 128):
            raise WitnessError("invalid witness request")
        url = f"{self._base_url}/v1/deployments/{quote(deployment_id, safe='')}/{action}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = b""
        if payload is not None:
            data = _canonical(dict(payload)).encode("ascii")
            headers["Content-Type"] = "application/json"
        response = None
        try:
            response = self._session.request(
                method, url, headers=headers, data=data, timeout=self._timeout,
                allow_redirects=False, verify=True, stream=True,
            )
            declared = response.headers.get("Content-Length")
            if declared is not None:
                if not declared.isascii() or not declared.isdecimal():
                    raise WitnessError("invalid witness response")
                if int(declared) > self._max_response:
                    raise WitnessError("invalid witness response")
            chunks: list[bytes] = []
            length = 0
            for chunk in response.iter_content(chunk_size=min(8192, self._max_response + 1)):
                if not isinstance(chunk, bytes):
                    raise WitnessError("invalid witness response")
                length += len(chunk)
                if length > self._max_response:
                    raise WitnessError("invalid witness response")
                chunks.append(chunk)
            content = b"".join(chunks)
            if response.status_code != 200:
                raise WitnessError("witness request rejected")
            result = loads_object(content)
        except WitnessError:
            raise
        except (requests.RequestException, OSError, ValueError, TypeError) as exc:
            raise WitnessError("witness request failed") from exc
        finally:
            if response is not None:
                response.close()
        return result

    def get_head(self, deployment_id: str) -> dict[str, Any]:
        return self._request("GET", deployment_id, "head")

    def claim(self, deployment_id: str, generation: int, permit_nonce: str,
              claim_nonce: str) -> dict[str, Any]:
        return self._request("POST", deployment_id, "claim", {
            "generation": generation, "permit_nonce": permit_nonce,
            "claim_nonce": claim_nonce,
        })

    def complete(self, deployment_id: str, generation: int,
                 claim_nonce: str) -> dict[str, Any]:
        return self._request("POST", deployment_id, "complete", {
            "generation": generation, "claim_nonce": claim_nonce,
        })

    def seal(self, deployment_id: str, generation: int, claim_nonce: str,
             next_certificate: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", deployment_id, "seal", {
            "generation": generation, "claim_nonce": claim_nonce,
            "certificate": dict(next_certificate),
        })

    def hold(self, deployment_id: str, generation: int, claim_nonce: str,
             reason: str) -> dict[str, Any]:
        return self._request("POST", deployment_id, "hold", {
            "generation": generation, "claim_nonce": claim_nonce, "reason": reason,
        })


def make_http_server(host: str, port: int,
                     application: HTTPApplication) -> ThreadingHTTPServer:
    """Create a loopback-only reference server intended for a TLS reverse proxy."""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError as exc:
        raise WitnessError("reference server must bind a loopback address") from exc
    if not loopback:
        raise WitnessError("reference server must bind a loopback address")
    if (not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535
            or not isinstance(application, HTTPApplication)):
        raise WitnessError("invalid reference server configuration")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            del format, args
            return

        def _run(self) -> None:
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise WitnessError("invalid HTTP framing")
                raw_length = self.headers.get("Content-Length", "0")
                if not raw_length.isascii() or not raw_length.isdecimal():
                    raise WitnessError("invalid HTTP framing")
                length = int(raw_length)
                if length > application._max_body:  # framing bound before allocation
                    raise WitnessError("invalid HTTP framing")
                body = self.rfile.read(length)
                status, headers, response = application.dispatch(
                    self.command, self.path, dict(self.headers.items()), body,
                )
            except WitnessError:
                status, headers, response = application._response(
                    400, {"error": "request rejected"},
                )
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response)

        do_GET = _run
        do_POST = _run

    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise WitnessError("reference server could not bind") from exc


def build_cli_parser() -> argparse.ArgumentParser:
    """Build the deliberately small witness operator CLI."""
    parser = argparse.ArgumentParser(description="Independent sealed-custody witness")
    commands = parser.add_subparsers(dest="command", required=True,
                                     help="issue reviewed certificate or serve runtime API")
    issue = commands.add_parser("issue", help="issue one externally reviewed initial certificate")
    issue.add_argument("--store", required=True)
    issue.add_argument("--certificate", required=True,
                       help="reviewed certificate JSON; this command never hashes a custody DB")
    issue.add_argument("--actor", required=True)
    issue.add_argument("--rationale", required=True)
    serve = commands.add_parser("serve", help="serve runtime API on loopback")
    serve.add_argument("--store", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8790)
    return parser


def run_cli(argv: list[str] | None = None, *,
            environ: Mapping[str, str] | None = None) -> int:
    """Run administrative issuance or the loopback reference server."""
    args = build_cli_parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    runtime_token = environment.get("CUSTODY_WITNESS_TOKEN", "")
    if args.command == "issue":
        admin_token = environment.get("CUSTODY_WITNESS_ADMIN_TOKEN", "")
        store = Store(args.store, runtime_token=runtime_token, admin_token=admin_token)
        try:
            certificate = Certificate.from_dict(loads_object(Path(args.certificate).read_bytes()))
        except OSError as exc:
            raise WitnessError("reviewed certificate could not be read") from exc
        store.issue_initial(certificate, actor=args.actor, rationale=args.rationale,
                            admin_token=admin_token)
        return 0
    store = Store(args.store, runtime_token=runtime_token, admin_token=None)
    application = HTTPApplication(store)
    server = make_http_server(args.host, args.port, application)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through run_cli
    try:
        raise SystemExit(run_cli())
    except WitnessError as error:
        raise SystemExit(str(error)) from None
