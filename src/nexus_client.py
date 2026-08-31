import base64
import json
import logging
import subprocess
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from . import config
from . import state_db, nexus_client, structured_logging
import time


_LOG = structured_logging.get_logger("swapService.nexus_client")


def _log(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Best-effort secret-safe diagnostics that cannot interrupt money-path state changes."""
    try:
        structured_logging.emit(_LOG, level, event, **fields)
    except Exception:
        # Logging must never turn a known/unknown transfer result into a retryable state.
        pass


# API families whose endpoints operate under a logged-in signature chain. Per the Nexus
# docs these require `session=<id>` when the node runs with `multiuser=1`, and require the
# session to be ABSENT in single-user mode ("For single-user API mode the session should
# not be supplied"). `register/*` is a public register read and never takes a session.
_SESSION_SCOPED_APIS = ("finance/", "assets/", "market/", "supply/", "invoices/", "names/", "profiles/")


def needs_session(cmd: list[str]) -> bool:
    """True if this CLI invocation targets a session-scoped API."""
    for arg in cmd[1:]:
        a = str(arg)
        if a.startswith("-") or "=" in a:
            continue  # flags / key=value params, not the endpoint
        return a.startswith(_SESSION_SCOPED_APIS)
    return False


def apply_session(cmd: list[str]) -> list[str]:
    """Append `session=<id>` when the node is in multiuser mode and the API needs it.

    Applied centrally rather than at each call site: there are ~15 of them and missing
    one would fail only that operation, at runtime, in production.
    """
    if not getattr(config, "NEXUS_MULTIUSER", False):
        return cmd  # single-user: the session must NOT be supplied
    session = getattr(config, "NEXUS_SESSION", "") or ""
    if not session or not needs_session(cmd):
        return cmd
    if any(str(a).startswith("session=") for a in cmd):
        return cmd  # already explicit
    return list(cmd) + [f"session={session}"]


def redact(text: str) -> str:
    """Strip the PIN and session id from anything we log or forward."""
    out = str(text or "")
    for secret in (getattr(config, "NEXUS_PIN", ""), getattr(config, "NEXUS_SESSION", "")):
        if secret:
            out = out.replace(str(secret), "***")
    return out


class _NoRedirect(HTTPRedirectHandler):
    """Raise on 3xx instead of forwarding Basic credentials to another URL."""

    @staticmethod
    def _blocked_redirect(req: Request, fp: Any, code: int, msg: str,
                          headers: Any) -> Any:
        raise HTTPError(req.full_url, code, msg, headers, fp)

    def http_error_301(self, req: Request, fp: Any, code: int,
                       msg: str, headers: Any) -> Any:
        return self._blocked_redirect(req, fp, code, msg, headers)

    def http_error_302(self, req: Request, fp: Any, code: int,
                       msg: str, headers: Any) -> Any:
        return self._blocked_redirect(req, fp, code, msg, headers)

    def http_error_303(self, req: Request, fp: Any, code: int,
                       msg: str, headers: Any) -> Any:
        return self._blocked_redirect(req, fp, code, msg, headers)

    def http_error_307(self, req: Request, fp: Any, code: int,
                       msg: str, headers: Any) -> Any:
        return self._blocked_redirect(req, fp, code, msg, headers)

    def http_error_308(self, req: Request, fp: Any, code: int,
                       msg: str, headers: Any) -> Any:
        return self._blocked_redirect(req, fp, code, msg, headers)


def _is_valid_nexus_api_url(api_url: str) -> bool:
    """Require an unambiguous credential-free HTTPS base URL."""
    raw = str(api_url or "").strip()
    try:
        parsed = urlsplit(raw)
        # Accessing ``port`` validates malformed/non-numeric/out-of-range port values.
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and "?" not in raw
        and "#" not in raw
    )


def nexus_api_transport_errors() -> list[str]:
    """Return production-blocking errors for the credential-safe Nexus transport."""
    errors: list[str] = []
    api_url = str(getattr(config, "NEXUS_API_URL", "") or "").strip()
    # HTTP is adequate only for deliberately isolated development nodes. Production carries
    # a PIN and (often) a spending-authorising multiuser session, so it needs TLS and must
    # not permit userinfo embedded in a URL that could be logged by a proxy or exception.
    if not _is_valid_nexus_api_url(api_url):
        errors.append("NEXUS_API_URL (HTTPS)")
    if not str(getattr(config, "NEXUS_API_USER", "") or "").strip():
        errors.append("NEXUS_API_USER")
    if not str(getattr(config, "NEXUS_API_PASSWORD", "") or "").strip():
        errors.append("NEXUS_API_PASSWORD")
    return errors


def _run_via_nexus_api(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """POST a Nexus CLI-shaped command without putting credentials in ``argv``.

    Nexus accepts the same logical endpoint and key/value fields through its HTTP API.
    The CLI-shaped command is retained at call sites so the fallback remains compatible,
    while the PIN/session move into an in-memory form body protected by HTTPS.
    """
    if len(cmd) < 2 or not str(cmd[1]).strip():
        return 1, "", "Nexus API request has no endpoint"
    base_url = str(getattr(config, "NEXUS_API_URL", "") or "").strip().rstrip("/")
    if not _is_valid_nexus_api_url(base_url):
        return 1, "", "Nexus API URL is invalid"
    endpoint = str(cmd[1]).lstrip("/")
    if "?" in endpoint or "#" in endpoint:
        return 1, "", "Nexus API endpoint is invalid"

    fields: list[tuple[str, str]] = []
    for raw_arg in cmd[2:]:
        arg = str(raw_arg)
        if "=" not in arg:
            return 1, "", "Nexus API cannot encode a non key=value CLI argument"
        key, value = arg.split("=", 1)
        if not key:
            return 1, "", "Nexus API cannot encode an empty parameter name"
        fields.append((key, value))

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    api_user = str(getattr(config, "NEXUS_API_USER", "") or "")
    api_password = str(getattr(config, "NEXUS_API_PASSWORD", "") or "")
    if api_user or api_password:
        if not (api_user and api_password):
            return 1, "", "Nexus API Basic authentication is incomplete"
        credentials = base64.b64encode(f"{api_user}:{api_password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {credentials}"

    request = Request(
        f"{base_url}/{endpoint}", data=urlencode(fields).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        # Never follow redirects: an HTTP 3xx must not forward Basic credentials (or the
        # PIN/session body) from the configured Nexus API origin to another endpoint.
        opener = build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            return 0, response.read().decode("utf-8"), ""
    except HTTPError as exc:
        # Deliberately do not surface a server response body: a misconfigured node could
        # echo submitted form data, including the PIN/session, in its error text.
        return int(exc.code or 1), "", f"Nexus API HTTP {exc.code}"
    except (URLError, TimeoutError, OSError, UnicodeDecodeError):
        return 1, "", "Nexus API request failed"


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    cmd = apply_session(cmd)
    if str(getattr(config, "NEXUS_API_URL", "") or "").strip():
        return _run_via_nexus_api(cmd, timeout)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def _parse_json_lenient(text: str):
    """Try to parse JSON from CLI output that may contain extra lines.
    Attempts full parse, then line-by-line, then substring between first '{'/'[' and last '}'/']'.
    Returns parsed object or None.
    """
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try per-line
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            return json.loads(line)
        except Exception:
            continue
    # Try to extract first JSON-like span
    start = None
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start is not None:
        # find matching tail candidate
        for j in range(len(text) - 1, start, -1):
            if text[j] in "]}":
                snippet = text[start : j + 1]
                try:
                    return json.loads(snippet)
                except Exception:
                    continue
    return None


def get_account_info(nexus_addr: str) -> Optional[Dict[str, Any]]:
    cmd = [config.NEXUS_CLI, "register/get/finance:account", f"address={nexus_addr}"]
    try:
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            return None
        data = _parse_json_lenient(out)
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None


def is_valid_nexus_token_account(account: str) -> bool:
    """Check the Nexus account exists and holds the configured Nexus-side token."""
    info = get_account_info(account)
    if not info:
        return False
    if not info.get("address"):
        return False
    expected = str(getattr(config, "NEXUS_TOKEN_NAME", "USDD") or "USDD")
    if str(info.get("ticker") or "").upper() != expected.upper():
        return False
    return True


def account_exists_and_owner(account: Dict[str, Any], owner: str | None = None) -> bool:
    if not isinstance(account, dict):
        return False
    # Confirm finance account exists: look for an address field
    addr = account.get("address") or None
    
    if not addr:
        return False
    if not owner:
        return False
    # Compare owner fields when provided; require equality when owner is supplied
    own = account.get("owner")
    return str(own) == str(owner)


def _dict_get_ci(d: Dict[str, Any], key: str):
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def is_expected_token(account_info: Dict[str, Any], expected: str) -> bool:
    if not isinstance(account_info, dict):
        return False
    v = _dict_get_ci(account_info, "ticker")
    if isinstance(v, str) and v.upper() == expected.upper():
        return True
    for container in ("result", "account", "data"):
        inner = _dict_get_ci(account_info, container)
        if isinstance(inner, dict) and is_expected_token(inner, expected):
            return True
    return False


def _format_amount_units(amount_units: int, decimals: int) -> str:
    """Format integer base units as a plain fixed-point token amount."""
    try:
        decs = int(decimals)
        if decs <= 0:
            return str(int(amount_units))
        value = Decimal(int(amount_units)) / (Decimal(10) ** decs)
        result = format(value.normalize(), "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    except Exception:
        return str(int(amount_units))


def format_solana_units(amount_units: int) -> str:
    """Format Solana-side base units using the configured Solana token scale."""
    return _format_amount_units(amount_units, config.USDC_DECIMALS)


def format_nexus_units(amount_units: int) -> str:
    """Format Nexus-side base units using the configured Nexus token scale."""
    return _format_amount_units(amount_units, config.USDD_DECIMALS)


def _format_nexus_amount(amount_units: int) -> str:
    """Backward-compatible Nexus CLI formatter; prefer ``format_nexus_units``."""
    return format_nexus_units(amount_units)



def _dynamic_fee_units(amount_units: int, bps: int) -> int:
    """Floor the percentage fee in the input token's own base units."""
    return max(0, int(amount_units)) * max(0, int(bps)) // 10_000


def get_nexus_send_amount_units(amount_solana_units: int) -> int:
    """Net Nexus output for a Solana deposit, in Nexus base units.

    The input is rescaled once, rounded down so the bridge cannot over-credit an
    unrepresentable fractional Nexus unit, then all fees are computed in that same
    Nexus-unit domain.
    """
    gross_nexus_units = config.solana_units_to_nexus(int(amount_solana_units), round_up=False)
    dynamic_fee = _dynamic_fee_units(gross_nexus_units, config.DYNAMIC_FEE_BPS)
    return max(0, gross_nexus_units - int(config.FLAT_FEE_TO_NEXUS_UNITS) - dynamic_fee)


def get_solana_send_amount_units(amount_nexus_units: int) -> int:
    """Net Solana output for a Nexus credit, in Solana base units.

    This mirrors ``get_nexus_send_amount_units`` with the opposite input and
    output scales.  A conversion remainder is rounded down before fees, so a
    decimal mismatch can never cause an overpayment.
    """
    gross_solana_units = config.nexus_units_to_solana(int(amount_nexus_units), round_up=False)
    dynamic_fee = _dynamic_fee_units(gross_solana_units, config.DYNAMIC_FEE_BPS)
    return max(0, gross_solana_units - int(config.FLAT_FEE_TO_SOLANA_UNITS) - dynamic_fee)


def get_nexus_send_amount(amount_solana: int) -> Decimal:
    """Deprecated: prefer get_nexus_send_amount_units(). Returns Decimal token units."""
    return Decimal(get_nexus_send_amount_units(amount_solana)) / (Decimal(10) ** config.USDD_DECIMALS)


def debit_nexus_token_with_txid(to_addr: str, amount_usdd_units: int, reference: int) -> tuple[bool, str | None]:
    """Perform Nexus-side debit and attempt to parse a txid from output.

    `amount_usdd_units` is in BASE UNITS and is formatted for the CLI by
    _format_nexus_amount(), which emits a plain fixed-point decimal string. Passing a
    float here previously produced scientific notation in the command line.
    
    Args:
        to_addr: Destination Nexus Nexus token account address
        amount_usdd_units: Amount in BASE units (e.g. 10500000 for 10.5 of a 6-decimal token)
        reference: Unique reference number for this debit

    Returns:
        Tuple of (success, txid_or_None)
    """
    if not config.NEXUS_PIN:
        return (False, None)

    amount_str = _format_nexus_amount(int(amount_usdd_units))
    cmd = [config.NEXUS_CLI, "finance/debit/token", f"from={config.NEXUS_TOKEN_NAME}",
           f"to={to_addr}", f"amount={amount_str}", f"reference={reference}", f"pin={config.NEXUS_PIN}"]
    # Use a generous, consistent timeout: a debit killed mid-flight may still execute
    # on the node, which would desynchronize state and risk a double payout.
    code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 30))
    if code != 0:
        return (False, None)
    # Try to pick txid from output JSON or text
    txid = None
    data = _parse_json_lenient(out)
    if isinstance(data, dict):
        txid = data.get("txid")
    if not txid:
        return (False, None)
    return (True, str(txid) if txid else None)


@dataclass(frozen=True)
class TransferExecution:
    """Result of exactly one persisted Nexus account-to-account debit attempt."""

    executed: bool
    status: str
    remote_txid: str | None = None


@dataclass(frozen=True)
class TransferDebitEvidence:
    """A fully specified on-chain debit observed for a persisted transfer intent."""

    remote_txid: str
    from_address: str
    to_address: str
    amount_usdd_units: int


@dataclass(frozen=True)
class NexusMintDebitEvidence:
    """Authoritative token-history evidence for one Nexus-side bridge mint contract."""

    remote_txid: str
    timestamp: int
    confirmations: int
    from_address: str
    to_address: str
    amount_usdd_units: int
    reference: str
    contract_id: int


def _parse_exact_nexus_units(value: object) -> int | None:
    """Parse a chain amount only when it exactly fits the configured Nexus scale."""
    try:
        amount = Decimal(str(value).strip())
        if not amount.is_finite() or amount < 0:
            return None
        units = amount * (Decimal(10) ** int(config.USDD_DECIMALS))
        if units != units.to_integral_value():
            return None
        return int(units)
    except Exception:
        return None


def execute_nexus_transfer_intent(intent_id: str) -> TransferExecution:
    """Execute one prepared intent and persist its outcome before returning.

    This function deliberately never retries an ``executing``, ``submitted`` or
    ``outcome_unknown`` intent. A process loss, timeout, non-zero CLI result or
    unparseable successful output can all occur after the Nexus node accepted the
    debit, so each requires positive reference resolution rather than another debit.
    """
    intent = state_db.claim_nexus_transfer_intent(intent_id)
    if intent is None:
        existing = state_db.get_nexus_transfer_intent(intent_id)
        return TransferExecution(False, (existing or {}).get("status", "missing"),
                                 (existing or {}).get("remote_txid"))

    if not config.NEXUS_PIN:
        state_db.update_nexus_transfer_intent(intent_id, status="outcome_unknown")
        _log(
            "NEXUS_TRANSFER_OUTCOME_UNKNOWN",
            level=logging.WARNING,
            intent_id=intent_id,
            reason="missing_pin",
        )
        return TransferExecution(False, "outcome_unknown")

    amount_str = _format_nexus_amount(int(intent["amount_usdd_units"]))
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/account",
        f"from={intent['from_address']}",
        f"to={intent['to_address']}",
        f"amount={amount_str}",
        f"reference={intent['reference']}",
        f"pin={config.NEXUS_PIN}",
    ]
    try:
        code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 30))
    except Exception as exc:
        state_db.update_nexus_transfer_intent(intent_id, status="outcome_unknown")
        _log(
            "NEXUS_TRANSFER_OUTCOME_UNKNOWN",
            level=logging.WARNING,
            intent_id=intent_id,
            reference=intent["reference"],
            reason="exception",
            error=redact(str(exc)),
        )
        return TransferExecution(True, "outcome_unknown")

    if code != 0:
        state_db.update_nexus_transfer_intent(intent_id, status="outcome_unknown")
        _log(
            "NEXUS_TRANSFER_OUTCOME_UNKNOWN",
            level=logging.WARNING,
            intent_id=intent_id,
            reference=intent["reference"],
            reason="cli_error",
            error=redact(err or out),
        )
        return TransferExecution(True, "outcome_unknown")

    data = _parse_json_lenient(out)
    remote_txid = data.get("txid") if isinstance(data, dict) else None
    if not remote_txid:
        # Text-only success is deliberately not treated as a safe retry.  The
        # persisted reference is resolved against the chain in a later pass.
        state_db.update_nexus_transfer_intent(intent_id, status="outcome_unknown")
        _log(
            "NEXUS_TRANSFER_OUTCOME_UNKNOWN",
            level=logging.WARNING,
            intent_id=intent_id,
            reference=intent["reference"],
            reason="unparsed_success",
        )
        return TransferExecution(True, "outcome_unknown")

    remote_txid = str(remote_txid)
    state_db.update_nexus_transfer_intent(
        intent_id, status="submitted", remote_txid=remote_txid
    )
    _log(
        "NEXUS_TRANSFER_SUBMITTED",
        intent_id=intent_id,
        reference=intent["reference"],
        remote_txid=remote_txid,
    )
    return TransferExecution(True, "submitted", remote_txid)


def resolve_nexus_transfer_intents(limit: int = 200) -> int:
    """Complete only an exact observed debit; never re-debit an ambiguous intent."""
    intents = state_db.get_nexus_transfer_intents_by_status(
        ("executing", "submitted", "outcome_unknown"), limit=limit
    )
    if not intents:
        return 0
    lookup = find_nexus_transfer_debits_by_references(
        [intent["reference"] for intent in intents]
    )
    resolved = 0
    for intent in intents:
        reference = str(intent["reference"]).strip()
        candidates = [
            evidence for evidence in lookup.values.get(reference, [])
            if evidence.from_address == str(intent["from_address"])
            and evidence.to_address == str(intent["to_address"])
            and evidence.amount_usdd_units == int(intent["amount_usdd_units"])
            and (intent["status"] != "submitted"
                 or evidence.remote_txid == str(intent.get("remote_txid") or ""))
        ]
        matching_txids = {evidence.remote_txid for evidence in candidates}
        if len(matching_txids) != 1:
            _log(
                "NEXUS_TRANSFER_HELD",
                level=logging.WARNING,
                intent_id=intent["id"],
                reference=reference,
                reason=lookup.reason or "no_exact_debit_match",
                matching_txids=len(matching_txids),
            )
            continue
        remote_txid = matching_txids.pop()
        state_db.update_nexus_transfer_intent(
            intent["id"], status="completed", remote_txid=remote_txid, resolved=True
        )
        _log(
            "NEXUS_TRANSFER_RESOLVED",
            intent_id=intent["id"],
            reference=reference,
            remote_txid=remote_txid,
        )
        resolved += 1
    return resolved


@dataclass(frozen=True)
class BatchLookup:
    """Values returned by a bounded chain lookup plus proof of completeness.

    Missing values are authoritative only when ``complete`` is true. A transport error,
    malformed response, or exhausted page budget is an unknown outcome and must never
    authorize a retry or refund.
    """

    values: dict
    complete: bool
    reason: str | None = None


@dataclass(frozen=True)
class AssetLookup:
    """Receival-asset lookup with an explicit complete/incomplete outcome."""

    asset: dict | None
    complete: bool
    reason: str | None = None


def get_transactions_confirmations(txids, limit: int = 200) -> BatchLookup:
    """Batch confirmations without confusing an incomplete scan with absence."""
    wanted = {str(t) for t in txids if t}
    out: dict = {}
    if not wanted:
        return BatchLookup(out, True)

    page_size = max(1, int(limit))
    max_pages = max(1, int(getattr(config, "NEXUS_LOOKUP_MAX_PAGES", 5)))
    for page in range(max_pages):
        cmd = [config.NEXUS_CLI, "finance/transactions/token/txid,confirmations",
               f"name={config.NEXUS_TOKEN_NAME}", "sort=timestamp", "order=desc",
               f"limit={page_size}", f"offset={page * page_size}"]
        try:
            code, cli_out, err = _run(
                cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20)
            )
            if code != 0:
                return BatchLookup(out, False, "cli_error")
            data = _parse_json_lenient(cli_out)
            if isinstance(data, dict) and data.get("error"):
                return BatchLookup(out, False, "api_error")
            if data is None:
                return BatchLookup(out, False, "invalid_response")
            txs = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                t = str(tx.get("txid") or "")
                if t in wanted and tx.get("confirmations") is not None:
                    try:
                        out[t] = int(tx.get("confirmations"))
                    except Exception:
                        continue
            if wanted.issubset(out):
                return BatchLookup(out, True)
            if len(txs) < page_size:
                # A negative history scan is not a durable proof of non-execution: the
                # endpoint is live and offset pagination has no snapshot guarantee.
                # Only a positive txid/reference match is actionable automatically.
                return BatchLookup(out, False, "not_found_unverified")
        except Exception as e:
            _log("nexus_confirmation_lookup_failed", level=logging.ERROR, error=str(e))
            return BatchLookup(out, False, "exception")

    return BatchLookup(out, False, "pagination_truncated")


def check_unconfirmed_debits(min_confirmations: int, timeout: int) -> int:
    """Confirm positively observed Nexus debits; ambiguity stays pending.

    A negative history lookup is not proof of non-execution, so this pass never refunds.
    Missing txids and missing lookup values require manual resolution.
    """
    sigs = state_db.filter_unprocessed_sigs({
        'status': 'debited, awaiting confirmation',
        'limit': 1000
    })
    if not sigs:
        return 0

    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    # One bounded lookup for the whole batch instead of an unbounded fetch per row.
    confirmation_lookup = get_transactions_confirmations([row[6] for row in sigs if row[6]])

    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in sigs:
        if not txid:
            _log("nexus_debit_confirmation_held", level=logging.WARNING, sig=sig,
                 reason="missing_txid")
            continue

        confirmations = confirmation_lookup.values.get(str(txid))
        
        # A missing value is never proof that the debit did not execute.
        if confirmations is None:
            _log("nexus_debit_confirmation_held", level=logging.WARNING, sig=sig, txid=txid,
                 reason=confirmation_lookup.reason or "not_observed")
            continue
        
        # Case 2: Transaction exists but not enough confirmations yet
        if confirmations < min_confirmations:
            # IMPORTANT: Do NOT refund! The debit happened, just not fully confirmed yet.
            # Wait for more confirmations - do not timeout a partially confirmed transaction.
            continue
        
        # Case 3: Transaction is confirmed. Its persisted per-deposit reference is part
        # of the immutable on-chain identity. Never substitute the latest global
        # reference: another worker may have advanced it while this transaction waited.
        reference = state_db.get_unprocessed_sig_reference(sig)
        if reference is None:
            _log("nexus_debit_confirmation_held", level=logging.WARNING, sig=sig, txid=txid,
                 reason="missing_reference")
            continue
        if isinstance(amount_usdc_units, bool) or not isinstance(amount_usdc_units, int):
            _log("nexus_debit_confirmation_held", level=logging.WARNING, sig=sig, txid=txid,
                 reason="non_integer_solana_units")
            continue

        # Archive the immutable output fixed before the debit. Recomputing fees here
        # after an operator configuration change would make the local record disagree
        # with the already-submitted Nexus debit.
        nexus_out_base = state_db.get_unprocessed_sig_nexus_amount(sig)
        if nexus_out_base is None:
            _log("nexus_debit_confirmation_held", level=logging.WARNING, sig=sig, txid=txid,
                 reason="missing_nexus_output")
            continue
        amount_nexus_debited = float(Decimal(nexus_out_base) / (Decimal(10) ** config.USDD_DECIMALS))
        nexus_destination = None
        prefix = str(getattr(config, "DEPOSIT_MEMO_PREFIX", "nexus:"))
        if memo and str(memo).lower().startswith(prefix.lower()):
            nexus_destination = str(memo)[len(prefix):].strip() or None

        # Bug #10 fix: Track fees when debit is confirmed.
        # The fee is what the deposit gave up: deposit in, minus what was credited out.
        # Those are on different scales (Solana base units vs Nexus base units), so the
        # credited side is converted first - rounded up, so the recorded fee is never
        # overstated. Exact integer arithmetic throughout, no float scaling.
        try:
            solana_in_base = int(amount_usdc_units or 0)
            credited_in_solana_base = config.nexus_units_to_solana(int(nexus_out_base))
            fee_solana_units = max(0, solana_in_base - credited_in_solana_base)
            if fee_solana_units > 0:
                state_db.add_fee_entry(
                    sig=sig,
                    txid=txid,
                    kind="swap_solana_to_nexus",
                    amount_usdc_units=fee_solana_units,
                    amount_usdd_units=None
                )
        except Exception as e:
            _log("nexus_fee_record_failed", level=logging.ERROR, sig=sig, txid=txid, error=str(e))
        
        state_db.mark_processed_sig(
            sig, timestamp, int(amount_usdc_units or 0), txid, amount_nexus_debited,
            "debit_confirmed", reference,
            amount_usdd_units=nexus_out_base,
            nexus_destination=nexus_destination,
            memo=memo,
        )
        state_db.remove_unprocessed_sig(sig)
        processed_count += 1
        
        current_time = time.monotonic()
        if current_time - time_start > timeout:
            break

    return processed_count


DEBIT_UNVERIFIED_STATUSES = ("debit in flight", "debit unverified")


def resolve_unverified_debits(limit: int = 200) -> int:
    """Resolve Nexus-side debits whose outcome is unknown, using the chain as the oracle.

    Covers both a crash between intent and state-write, and a CLI response we could not
    parse. For each row we look up the unique per-attempt reference on-chain:

      found            -> the debit DID execute; record the txid and proceed (never refund)
      not found / failed / incomplete -> leave pending for manual resolution

    Returns the number of rows whose state was resolved.
    """
    rows = state_db.get_sigs_pending_debit_verification(DEBIT_UNVERIFIED_STATUSES, limit=limit)
    if not rows:
        return 0

    # One lookup for the whole batch instead of one subprocess per row.
    reference_lookup = find_nexus_debits_by_references(
        [r[7] for r in rows if r[7] is not None]
    )

    resolved = 0

    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid, reference in rows:
        try:
            if reference is None:
                # Intent was never recorded (pre-upgrade row): fall back to the memo-scan-free
                # safe option - leave for manual review rather than risk a double action.
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                _log("nexus_debit_resolution_quarantined", level=logging.WARNING, sig=sig,
                     reason="missing_reference")
                resolved += 1
                continue

            found_txid = reference_lookup.values.get(str(reference).strip())
            if found_txid:
                state_db.update_unprocessed_sig_txid(sig, found_txid)
                state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")
                state_db.release_reservation(state_db.DEBIT_RESERVATION_KIND, sig)
                _log("nexus_debit_resolution_confirmed", sig=sig, reference=reference,
                     remote_txid=found_txid)
                resolved += 1
                continue

            _log("nexus_debit_resolution_held", level=logging.WARNING, sig=sig,
                 reference=reference, reason=reference_lookup.reason or "not_observed")
            continue
        except Exception as e:
            _log("nexus_debit_resolution_failed", level=logging.ERROR, sig=sig, error=str(e))
            continue

    return resolved


def quarantine_nexus_token(txid: str, amount_usdd_units: int, reason: str = "") -> bool:
    """Prepare (but never automatically execute) a treasury-to-quarantine transfer.

    Automatic quarantine movement has the same ambiguous debit semantics as a refund.
    It stays held until a separately authorized caller executes and resolves the durable
    intent through ``execute_nexus_transfer_intent``.
    """
    dest = getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", None)
    treas = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
    if not dest or not treas or int(amount_usdd_units or 0) <= 0 or not txid:
        _log("nexus_quarantine_intent_held", level=logging.WARNING, reason="invalid_intent_input")
        return False
    try:
        intent = state_db.create_nexus_transfer_intent(
            kind="quarantine",
            source_txid=str(txid),
            from_address=str(treas),
            to_address=str(dest),
            amount_usdd_units=int(amount_usdd_units),
        )
    except ValueError as exc:
        _log("nexus_quarantine_intent_conflict", level=logging.WARNING, error=redact(str(exc)))
        return False
    _log("nexus_quarantine_intent_prepared", level=logging.WARNING, intent_id=intent["id"],
         reason=redact(reason), automatic_execution=False)
    return False


def _refund_source_txid(reason: str) -> str | None:
    """Extract the source credit identifier retained by legacy refund call sites."""
    marker = "txid:"
    if marker not in str(reason):
        return None
    value = str(reason).split(marker, 1)[1].strip().split()
    return value[0] if value else None


def refund_nexus_token(to_addr: str, amount_usdd_units: int, reason: str) -> bool:
    """Prepare a refund intent and hold; automatic Nexus refunds remain disabled."""
    source_txid = _refund_source_txid(reason)
    treas = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
    if not source_txid or not treas or not to_addr or int(amount_usdd_units or 0) <= 0:
        _log("nexus_refund_intent_held", level=logging.WARNING, reason="invalid_intent_input")
        return False
    try:
        intent = state_db.create_nexus_transfer_intent(
            kind="refund",
            source_txid=source_txid,
            from_address=str(treas),
            to_address=str(to_addr),
            amount_usdd_units=int(amount_usdd_units),
        )
    except ValueError as exc:
        _log("nexus_refund_intent_conflict", level=logging.WARNING, error=redact(str(exc)))
        return False
    _log("nexus_refund_intent_prepared", level=logging.WARNING, intent_id=intent["id"],
         automatic_execution=False)
    return False


def transfer_nexus_between_accounts(from_addr: str, to_addr: str, amount_usdd_units: int, reference: str) -> bool:
    """Legacy unsafe entrypoint retained only to fail closed.

    Callers must first create an immutable ``nexus_transfer_intents`` row and then use
    ``execute_nexus_transfer_intent``. This function deliberately cannot issue a debit.
    """
    _log("nexus_transfer_blocked", level=logging.WARNING, reason="durable_intent_required")
    return False

def debit_account_with_txid(from_addr: str, to_addr: str, amount_units: int, reference: int | str) -> tuple[bool, str | None]:
    """Legacy direct-debit entrypoint retained only to fail closed.

    A parsed response does not prove an account debit is safe to retry after a process
    loss.  The durable intent ledger therefore owns the only permitted invocation path:
    create a ``nexus_transfer_intents`` row, record the audited preparation,
    authorization and execution request, then call ``execute_nexus_transfer_intent``.
    """
    _log("NEXUS_TRANSFER_BLOCKED", level=logging.WARNING,
         reason="durable_intent_required")
    return (False, None)


# --- Asset mapping for swaps (distordiaBridge) ---
# See ASSET_STANDARD.md for full specification.
# User assets use fields: txid_toService, receival_account
# Service queries by txid_toService + owner to prevent front-running.

def find_asset_receival_account_by_sig(sig: str) -> Optional[Dict[str, Any]]:
    """Query assets by sig_toService and return a vetted { receival_account, owner }.
    Security: when multiple assets match, filter by a configurable owner whitelist, and then
    prefer the oldest (smallest block/tx order) to avoid front-running or spoofing.
    """
    try:
        cmd = [
            config.NEXUS_CLI,
            "register/list/assets:asset/owner,distordiaType,fromToken,toToken,txid_toService,sig_toService,receival_account,created,modified",
            f"results.sig_toService={sig}",
            "order=asc",
            "sort=created",
        ]
        code, out, err = _run(cmd, timeout=15)
        if code != 0:
            return None
        data = _parse_json_lenient(out)
        # Normalize to a list of items with results
        raw = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        items = []
        for a in raw or []:
            if not isinstance(a, dict):
                continue
            res = a.get("results") or a
            if not isinstance(res, dict):
                continue
            # Some projections wrap fields under 'asset'
            core = res.get("asset") if isinstance(res.get("asset"), dict) else res
            items.append(core)
        if not items:
            return None
    # Whitelist removed: consider all matching items
        # Stable order by created then modified
        def _key(r):
            try:
                c = r.get("created")
                m = r.get("modified")
                # created/modified might be nested under meta too
                if isinstance(c, dict):
                    c = c.get("value") or c.get("ts")
                if isinstance(m, dict):
                    m = m.get("value") or m.get("ts")
                return (int(c or 0), int(m or 0))
            except Exception:
                return (0, 0)
        items.sort(key=_key)
        best = items[0]
        return {
            "receival_account": best.get("receival_account"),
            "owner": best.get("owner"),
        }
    except Exception:
        return None

def find_asset_receival_account_by_txid_and_owner(
    txid: str, owner: str
) -> AssetLookup:
    """Query the receival mapping without collapsing lookup failure into absence."""
    if not txid or not owner:
        return AssetLookup(None, False, "invalid_request")
    try:
        cmd = [
            config.NEXUS_CLI,
            "register/list/assets:asset/owner,distordiaType,fromToken,toToken,txid_toService,receival_account,created,modified",
            f"results.txid_toService={txid}",
            f"results.owner={owner}",
            "order=asc",
            "sort=created",
        ]
        code, out, err = _run(cmd, timeout=15)
        if code != 0:
            return AssetLookup(None, False, "cli_error")
        data = _parse_json_lenient(out)
        if isinstance(data, dict) and data.get("error"):
            return AssetLookup(None, False, "api_error")
        if data is None or not isinstance(data, (list, dict)):
            return AssetLookup(None, False, "invalid_response")
        raw = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        items = []
        for a in raw or []:
            if not isinstance(a, dict):
                return AssetLookup(None, False, "invalid_item")
            res = a.get("results") if "results" in a else a
            if not isinstance(res, dict):
                return AssetLookup(None, False, "invalid_results")
            if "asset" in res and not isinstance(res.get("asset"), dict):
                return AssetLookup(None, False, "invalid_asset")
            core = res.get("asset") if "asset" in res else res
            if not isinstance(core, dict):
                return AssetLookup(None, False, "invalid_asset")
            mapped_txid = core.get("txid_toService")
            mapped_owner = core.get("owner")
            receival = core.get("receival_account")
            if not mapped_txid or not mapped_owner or not receival:
                return AssetLookup(None, False, "missing_required_field")
            if str(mapped_txid) != str(txid) or str(mapped_owner) != str(owner):
                return AssetLookup(None, False, "query_mismatch")
            items.append(core)
        if not items:
            return AssetLookup(None, True, "not_found")
        def _key(r):
            try:
                c = r.get("created")
                m = r.get("modified")
                if isinstance(c, dict):
                    c = c.get("value") or c.get("ts")
                if isinstance(m, dict):
                    m = m.get("value") or m.get("ts")
                return (int(c or 0), int(m or 0))
            except Exception:
                return (0, 0)
        items.sort(key=_key)
        best = items[0]
        return AssetLookup(
            {"receival_account": best.get("receival_account"), "owner": best.get("owner")},
            True,
        )
    except Exception:
        return AssetLookup(None, False, "exception")


def find_nexus_mint_debits_since(
    recipients, since_timestamp: int, limit: int = 100
) -> BatchLookup:
    """Enumerate exact remote mint contracts for completed-mint reconciliation.

    The token history is the remote source of truth. A result is complete only after the
    ordered scan reaches the requested time boundary (or the endpoint returns a short
    final page). Any malformed DEBIT, API error, unstable ordering, or exhausted page
    budget remains explicitly incomplete and therefore cannot authorize a green result.
    """
    # The caller provides known recipients for interface clarity, but the scan must
    # retain every DEBIT. Restricting by known recipients would hide an unauthorized
    # token-supply emission to a new address and permit a false green result.
    _ = recipients
    page_size = max(1, int(limit))
    boundary = max(0, int(since_timestamp or 0))
    found: dict[str, list[NexusMintDebitEvidence]] = {}
    seen_contracts: dict[tuple[str, int], NexusMintDebitEvidence] = {}
    previous_timestamp: int | None = None

    # The endpoint is live-offset paginated, not snapshot/cursor based. A head
    # insertion between pages can shift an unseen transaction past the next offset.
    # Read one page and fail closed if it does not establish the requested boundary.
    for page in range(1):
        cmd = [
            config.NEXUS_CLI,
            "finance/transactions/token/txid,timestamp,confirmations,contracts.id,contracts.OP,contracts.reference,contracts.from,contracts.to,contracts.amount",
            f"name={config.NEXUS_TOKEN_NAME}",
            "sort=timestamp",
            "order=desc",
            f"limit={page_size}",
            f"offset={page * page_size}",
        ]
        try:
            code, cli_out, err = _run(
                cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20)
            )
        except Exception as exc:
            _log("nexus_mint_history_lookup_failed", level=logging.ERROR, error=redact(str(exc)))
            return BatchLookup(found, False, "exception")
        if code != 0:
            _log("nexus_mint_history_lookup_failed", level=logging.ERROR,
                 error=redact(err or cli_out), reason="cli_error")
            return BatchLookup(found, False, "cli_error")

        data = _parse_json_lenient(cli_out)
        if isinstance(data, dict) and data.get("error"):
            return BatchLookup(found, False, "api_error")
        if data is None or not isinstance(data, list):
            return BatchLookup(found, False, "invalid_response")

        oldest_on_page: int | None = None
        for tx in data:
            if not isinstance(tx, dict) or not tx.get("txid"):
                return BatchLookup(found, False, "invalid_transaction")
            try:
                timestamp = int(tx["timestamp"])
                confirmations = int(tx["confirmations"])
            except (KeyError, TypeError, ValueError):
                return BatchLookup(found, False, "invalid_transaction_metadata")
            if timestamp < 0 or confirmations < 0:
                return BatchLookup(found, False, "invalid_transaction_metadata")
            if previous_timestamp is not None and timestamp > previous_timestamp:
                return BatchLookup(found, False, "unstable_history_order")
            previous_timestamp = timestamp
            oldest_on_page = timestamp if oldest_on_page is None else min(oldest_on_page, timestamp)
            if timestamp < boundary:
                continue

            contracts = tx.get("contracts")
            if not isinstance(contracts, list):
                return BatchLookup(found, False, "invalid_contracts")
            for contract in contracts:
                if not isinstance(contract, dict):
                    return BatchLookup(found, False, "invalid_contract")
                if str(contract.get("OP") or "").upper() != "DEBIT":
                    continue
                source = contract.get("from")
                destination = contract.get("to")
                # A DEBIT without complete endpoints cannot be classified as a token
                # mint versus an account transfer, so fail closed rather than skip it.
                if (source is None or not str(source).strip() or
                        destination is None or not str(destination).strip()):
                    return BatchLookup(found, False, "invalid_debit_endpoints")
                source = str(source).strip()
                destination = str(destination).strip()
                reference = contract.get("reference")
                amount_units = _parse_exact_nexus_units(contract.get("amount"))
                if reference is None or not str(reference).strip() or amount_units is None:
                    return BatchLookup(found, False, "invalid_debit_evidence")
                contract_id = contract.get("id")
                if isinstance(contract_id, bool) or not isinstance(contract_id, int):
                    return BatchLookup(found, False, "invalid_contract_id")
                txid = str(tx["txid"])
                identity = (txid, contract_id)
                evidence = NexusMintDebitEvidence(
                    remote_txid=txid,
                    timestamp=timestamp,
                    confirmations=confirmations,
                    from_address=source,
                    to_address=destination,
                    amount_usdd_units=amount_units,
                    reference=str(reference).strip(),
                    contract_id=contract_id,
                )
                previous = seen_contracts.get(identity)
                if previous is not None:
                    if previous != evidence:
                        return BatchLookup(found, False, "conflicting_contract_identity")
                    continue
                seen_contracts[identity] = evidence
                found.setdefault(txid, []).append(evidence)

        if len(data) < page_size or (oldest_on_page is not None and oldest_on_page < boundary):
            return BatchLookup(found, True)

    return BatchLookup(found, False, "pagination_snapshot_unavailable")


def find_nexus_transfer_debits_by_references(references, limit: int = 100) -> BatchLookup:
    """Return complete debit terms keyed by reference for durable transfer resolution.

    A transfer reference is only an identifier.  Finalization additionally requires a
    debit's exact source account, destination account, and integer Nexus amount to
    match the durable local intent.  Malformed candidates are deliberately ignored,
    leaving the intent held rather than authorizing a disposition.
    """
    wanted = {str(reference).strip() for reference in references if reference is not None}
    out: dict[str, list[TransferDebitEvidence]] = {}
    if not wanted:
        return BatchLookup(out, True)

    page_size = max(1, int(limit))
    max_pages = max(1, int(getattr(config, "NEXUS_LOOKUP_MAX_PAGES", 5)))
    for page in range(max_pages):
        cmd = [
            config.NEXUS_CLI,
            "finance/transactions/token/txid,timestamp,contracts.OP,contracts.reference,contracts.from,contracts.to,contracts.amount",
            f"name={config.NEXUS_TOKEN_NAME}",
            "sort=timestamp",
            "order=desc",
            f"limit={page_size}",
            f"offset={page * page_size}",
        ]
        try:
            code, cli_out, err = _run(
                cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20)
            )
            if code != 0:
                _log("nexus_transfer_debit_lookup_failed", level=logging.ERROR,
                     error=redact(err or cli_out), reason="cli_error")
                return BatchLookup(out, False, "cli_error")
            data = _parse_json_lenient(cli_out)
            if isinstance(data, dict) and data.get("error"):
                return BatchLookup(out, False, "api_error")
            if data is None:
                return BatchLookup(out, False, "invalid_response")
            txs = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            for tx in txs:
                if not isinstance(tx, dict) or not tx.get("txid"):
                    continue
                for contract in (tx.get("contracts") or []):
                    if not isinstance(contract, dict):
                        continue
                    if str(contract.get("OP") or "").upper() != "DEBIT":
                        continue
                    reference = contract.get("reference")
                    if reference is None:
                        continue
                    key = str(reference).strip()
                    if key not in wanted:
                        continue
                    amount_usdd_units = _parse_exact_nexus_units(contract.get("amount"))
                    from_address = contract.get("from")
                    to_address = contract.get("to")
                    if (amount_usdd_units is None or from_address is None or to_address is None or
                            not str(from_address).strip() or not str(to_address).strip()):
                        continue
                    out.setdefault(key, []).append(TransferDebitEvidence(
                        remote_txid=str(tx["txid"]),
                        from_address=str(from_address).strip(),
                        to_address=str(to_address).strip(),
                        amount_usdd_units=amount_usdd_units,
                    ))
            if len(txs) < page_size:
                return BatchLookup(out, False, "not_found_unverified")
        except Exception as exc:
            _log("nexus_transfer_debit_lookup_failed", level=logging.ERROR, error=redact(str(exc)))
            return BatchLookup(out, False, "exception")

    return BatchLookup(out, False, "pagination_truncated")


def find_nexus_debits_by_references(references, limit: int = 100) -> BatchLookup:
    """Find debit references while preserving whether missing values are authoritative."""
    wanted = {str(r).strip() for r in references if r is not None}
    out: dict = {}
    if not wanted:
        return BatchLookup(out, True)

    page_size = max(1, int(limit))
    max_pages = max(1, int(getattr(config, "NEXUS_LOOKUP_MAX_PAGES", 5)))
    for page in range(max_pages):
        cmd = [
            config.NEXUS_CLI,
            "finance/transactions/token/txid,timestamp,contracts.OP,contracts.reference,contracts.to,contracts.amount",
            f"name={config.NEXUS_TOKEN_NAME}",
            "sort=timestamp",
            "order=desc",
            f"limit={page_size}",
            f"offset={page * page_size}",
        ]
        try:
            code, cli_out, err = _run(
                cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20)
            )
            if code != 0:
                _log("nexus_debit_reference_lookup_failed", level=logging.ERROR,
                     error=redact(err or cli_out), reason="cli_error")
                return BatchLookup(out, False, "cli_error")
            data = _parse_json_lenient(cli_out)
            if isinstance(data, dict) and data.get("error"):
                return BatchLookup(out, False, "api_error")
            if data is None:
                return BatchLookup(out, False, "invalid_response")
            txs = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                for contract in (tx.get("contracts") or []):
                    if not isinstance(contract, dict):
                        continue
                    if str(contract.get("OP") or "").upper() != "DEBIT":
                        continue
                    reference = contract.get("reference")
                    if reference is None:
                        continue
                    key = str(reference).strip()
                    if key in wanted and key not in out:
                        txid = tx.get("txid")
                        if txid:
                            out[key] = str(txid)
            if wanted.issubset(out):
                return BatchLookup(out, True)
            if len(txs) < page_size:
                # A negative history scan is not a durable proof of non-execution: the
                # endpoint is live and offset pagination has no snapshot guarantee.
                # Only a positive txid/reference match is actionable automatically.
                return BatchLookup(out, False, "not_found_unverified")
        except Exception as e:
            _log("nexus_debit_reference_lookup_failed", level=logging.ERROR, error=redact(str(e)))
            return BatchLookup(out, False, "exception")

    return BatchLookup(out, False, "pagination_truncated")


def _to_decimal(x) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(0)


# --- Treasury and metrics ---
def get_circulating_nexus_supply() -> int:
    cmd = [config.NEXUS_CLI, "finance/get/token/currentsupply", f"name={config.NEXUS_TOKEN_NAME}"]
    try:
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            _log("nexus_token_supply_lookup_failed", level=logging.ERROR, error=redact(err or out))
            return 0
        data = _parse_json_lenient(out)
        # Accept either raw number or an object containing value/amount
        if isinstance(data, (int, float, str)):
            s = str(data)
            dec = Decimal(s)
        elif isinstance(data, dict):
            dec = Decimal(str(data["currentsupply"]))
        else:
            return 0
        units = int(dec)
        return units
    except Exception as e:
        _log("nexus_token_supply_lookup_failed", level=logging.ERROR, error=redact(str(e)))
        return 0


def get_circulating_nexus_units() -> int:
    """Circulating supply in base units, raising when the value is unavailable.

    Returning zero on a transport or parse failure makes an unavailable liability look
    like no liability at all and causes backing checks to fail open.
    """
    cmd = [config.NEXUS_CLI, "finance/get/token/currentsupply", f"name={config.NEXUS_TOKEN_NAME}"]
    try:
        code, out, err = _run(cmd, timeout=10)
    except Exception as e:
        raise RuntimeError(f"Nexus token supply lookup failed: {e}") from e
    if code != 0:
        raise RuntimeError(f"Nexus token supply lookup failed: {redact(err or out)}")

    data = _parse_json_lenient(out)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Nexus token supply API error: {redact(str(data['error']))}")
    try:
        if isinstance(data, (int, float, str)):
            dec = Decimal(str(data))
        elif isinstance(data, dict) and data.get("currentsupply") is not None:
            dec = Decimal(str(data["currentsupply"]))
        else:
            raise ValueError("missing currentsupply")
        decimals = int(getattr(config, "NEXUS_TOKEN_DECIMALS", 6))
        units = int(
            (dec * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN)
        )
        if units < 0:
            raise ValueError("negative currentsupply")
        return units
    except (ArithmeticError, KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"Invalid Nexus token supply response: {e}") from e


def get_nxs_default_balance_units() -> int:
    """Return available balance of the NXS account named 'default'."""
    cmd = [config.NEXUS_CLI, "finance/get/account", "name=default"]
    try:
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            return 0
        data = _parse_json_lenient(out)
        if not isinstance(data, dict):
            return 0
        bal = data.get("balance")
        if bal is None and isinstance(data.get("result"), dict):
            bal = data["result"].get("balance")
        return int(_to_decimal(bal)) if bal is not None else 0
    except Exception:
        return 0


def get_nexus_local_balance_units() -> int:
    """Return available Nexus balance in the local account (if queryable via finance/get/account)."""
    try:
        info = get_account_info(config.NEXUS_USDD_LOCAL_ACCOUNT)
        if not info:
            return 0
        # balance may be in "balance" or nested
        v = info.get("balance")
        if v is None and isinstance(info.get("result"), dict):
            v = info["result"].get("balance")
        return int(_to_decimal(v)) if v is not None else 0
    except Exception:
        return 0


## Heartbeat asset handling
# last_poll_timestamp, 
# last_safe_timestamp_nexus, 
# last_safe_timestamp_solana,
# vaulted_token {chain, ticker, vault_address, balance}
# minted_nexus_token {name, address, supply}

# --- On-chain service registration ---------------------------------------------------
# The heartbeat asset doubles as the bridge's public registration record: it declares the
# token pair, the vault/treasury addresses that back it, the current terms, and a liveness
# timestamp. A client can discover everything needed to use (or audit) the bridge from
# this one asset, and can tell whether the operator is currently online.
#
# `format=basic` FIXES THE FIELD SET AT CREATION, so the record must be created complete;
# the service then only rewrites the mutable subset. Field names are defined once here and
# used by both the registration tool and the runtime updater, so they cannot drift apart.

SERVICE_RECORD_IMMUTABLE = (
    "distordiaType", "provider", "memo_prefix",
    "nexus_token", "nexus_treasury_address",
    "solana_token", "solana_vault_address", "solana_vault_mint",
)
SERVICE_RECORD_MUTABLE = (
    "last_poll_timestamp", "last_safe_timestamp_solana", "last_safe_timestamp_nexus",
    "status", "version", "contact",
    "fee_flat_to_nexus", "fee_flat_to_solana", "fee_bps",
    "min_to_nexus", "min_to_solana",
)
SERVICE_RECORD_FIELDS = SERVICE_RECORD_IMMUTABLE + SERVICE_RECORD_MUTABLE
# Nexus `format=basic` assets are small; keep the whole record well inside one register.
SERVICE_RECORD_MAX_BYTES = 1024


def build_service_record(status: str = "online", last_poll: int | None = None,
                         wline_sol: int | None = None, wline_nxs: int | None = None) -> dict:
    """The complete public description of this bridge, derived from config."""
    import time as _t
    sol_field = getattr(config, "HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
    nxs_field = getattr(config, "HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_nexus")
    rec = {
        # identity + pair (immutable)
        "distordiaType": "nexusBridgeHeartbeat",
        "provider": str(getattr(config, "SERVICE_PROVIDER", "") or "unnamed-operator"),
        "memo_prefix": str(getattr(config, "DEPOSIT_MEMO_PREFIX", "nexus:")),
        "nexus_token": str(config.NEXUS_TOKEN_NAME),
        "nexus_treasury_address": str(config.NEXUS_USDD_TREASURY_ACCOUNT or ""),
        "solana_token": str(getattr(config, "SOLANA_TOKEN_SYMBOL", "USDC")),
        "solana_vault_address": str(config.VAULT_USDC_ACCOUNT),
        "solana_vault_mint": str(config.USDC_MINT),
        # liveness + terms (mutable)
        "last_poll_timestamp": int(last_poll if last_poll is not None else _t.time()),
        sol_field: int(wline_sol or 0),
        nxs_field: int(wline_nxs or 0),
        "status": status,
        "version": str(getattr(config, "SERVICE_VERSION", "1.0.0")),
        "contact": str(getattr(config, "SERVICE_CONTACT", "") or "-"),
        # Terms, so a client can compute what they will receive before sending anything.
        "fee_flat_to_nexus": str(config.FLAT_FEE_USDD),
        "fee_flat_to_solana": str(config.FLAT_FEE_USDC),
        "fee_bps": str(int(config.DYNAMIC_FEE_BPS)),
        "min_to_nexus": format_solana_units(int(config.MIN_DEPOSIT_SOLANA_UNITS)),
        "min_to_solana": format_nexus_units(int(config.MIN_CREDIT_NEXUS_UNITS)),
    }
    return rec


def service_record_size(rec: dict) -> int:
    """Approximate on-register size of the record as `key=value` pairs."""
    return sum(len(str(k).encode()) + len(str(v).encode()) + 2 for k, v in rec.items())


def publish_service_record(status: str = "online", last_poll: int | None = None,
                           wline_sol: int | None = None, wline_nxs: int | None = None) -> bool:
    """Rewrite the MUTABLE part of the registration record (terms, status, liveness).

    Only fields that already exist on the asset can be written: `format=basic` fixes the
    schema at creation, and one unknown field fails the whole atomic update.
    """
    if not getattr(config, "HEARTBEAT_ENABLED", True):
        return False
    name = getattr(config, "NEXUS_HEARTBEAT_ASSET_NAME", None)
    if not name:
        return False
    asset = get_heartbeat_asset()
    if not asset:
        return False
    rec = build_service_record(status=status, last_poll=last_poll,
                               wline_sol=wline_sol, wline_nxs=wline_nxs)
    sol_field = getattr(config, "HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
    nxs_field = getattr(config, "HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_nexus")
    mutable = set(SERVICE_RECORD_MUTABLE) | {sol_field, nxs_field}
    cmd = [config.NEXUS_CLI, "assets/update/asset", f"name={name}", "format=basic",
           f"pin={config.NEXUS_PIN}"]
    wrote = 0
    for k, v in rec.items():
        if k in mutable and k in asset:   # never send a field the asset lacks
            cmd.append(f"{k}={v}")
            wrote += 1
    if not wrote:
        return False
    try:
        code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20))
        if code != 0:
            _log("nexus_service_record_update_failed", level=logging.ERROR, error=redact(err or out))
            return False
        data = _parse_json_lenient(out)
        return bool(isinstance(data, dict) and data.get("success"))
    except Exception as e:
        _log("nexus_service_record_update_failed", level=logging.ERROR, error=redact(str(e)))
        return False


def read_service_record(name: str | None = None) -> Optional[Dict[str, Any]]:
    """Read another operator's (or our own) published bridge registration."""
    target = name or getattr(config, "NEXUS_HEARTBEAT_ASSET_NAME", None)
    if not target:
        return None
    cmd = [config.NEXUS_CLI, "assets/get/asset", f"name={target}"]
    try:
        code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20))
        if code != 0:
            return None
        data = _parse_json_lenient(out)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def update_heartbeat_asset(last_poll: int, wline_nxs: int | None, wline_sol: int | None) -> bool:
    """Update the heartbeat asset information."""
    cmd = [
        config.NEXUS_CLI, 
        "assets/update/asset", 
        f"name={config.NEXUS_HEARTBEAT_ASSET_NAME}", 
        f"format=basic",  
        f"pin={config.NEXUS_PIN}"
    ]

    # Conditionally add fields only if they are not None
    if last_poll is not None:
        cmd.append(f"last_poll_timestamp={last_poll}")

    # Use the CONFIGURED field names. Hardcoding them here meant a config/asset mismatch
    # silently failed every update, freezing the heartbeat and both waterlines.
    if wline_nxs is not None:
        cmd.append(f"{config.HEARTBEAT_WATERLINE_NEXUS_FIELD}={wline_nxs}")

    if wline_sol is not None:
        cmd.append(f"{config.HEARTBEAT_WATERLINE_SOLANA_FIELD}={wline_sol}")

    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            _log("nexus_heartbeat_update_failed", level=logging.ERROR, error=redact(err or out))
            return False
        data = _parse_json_lenient(out)
        if isinstance(data, dict) and data.get("success"):
            state_db.update_heartbeat(
                name=config.NEXUS_HEARTBEAT_ASSET_NAME,
                last_beat=last_poll,
                wline_sol=wline_sol,
                wline_nxs=wline_nxs
            )
            return True
        else:
            return False
    except Exception as e:
        _log("nexus_heartbeat_update_failed", level=logging.ERROR, error=redact(str(e)))
        return False
    

def validate_session_config() -> tuple[bool, str]:
    """Check the multiuser/session configuration before any money operation runs.

    With multiuser=1 every finance/* and assets/* call needs `session=<id>`; without it
    they all fail, which would look like a total Nexus outage. With multiuser=0 a session
    must NOT be sent, so a stray value is worth flagging too.
    """
    multiuser = bool(getattr(config, "NEXUS_MULTIUSER", False))
    session = (getattr(config, "NEXUS_SESSION", "") or "").strip()
    if multiuser and not session:
        return (False,
                "NEXUS_MULTIUSER=true but NEXUS_SESSION is empty. Every finance/* and "
                "assets/* call requires session=<id> on a multiuser node, so debits, "
                "refunds and heartbeat updates will all fail. Create one with "
                "`sessions/create/local` and set NEXUS_SESSION.")
    if not multiuser and session:
        return (True,
                "NEXUS_SESSION is set but NEXUS_MULTIUSER is false; the session will NOT "
                "be sent (single-user nodes reject it). Set NEXUS_MULTIUSER=true if your "
                "node runs multiuser=1.")
    if multiuser:
        return (True, f"multiuser mode, session configured ({session[:6]}…)")
    return (True, "single-user mode (no session sent)")


def validate_heartbeat_asset() -> tuple[bool, str]:
    """Check at startup that the heartbeat asset carries every field we will write.

    `assets/update/asset format=basic` is atomic and the field set is fixed at creation,
    so writing one unknown field fails the WHOLE update - freezing last_poll_timestamp
    and both waterlines with no error surfaced anywhere. Returns (ok, message).
    """
    if not getattr(config, "HEARTBEAT_ENABLED", True):
        return (True, "heartbeat disabled")
    if not config.NEXUS_HEARTBEAT_ASSET_NAME:
        return (False, "NEXUS_HEARTBEAT_ASSET_NAME is not set; the service addresses the asset by name")
    asset = get_heartbeat_asset()
    if not asset:
        return (False, f"heartbeat asset '{config.NEXUS_HEARTBEAT_ASSET_NAME}' not readable")
    required = [
        "last_poll_timestamp",
        config.HEARTBEAT_WATERLINE_NEXUS_FIELD,
        config.HEARTBEAT_WATERLINE_SOLANA_FIELD,
    ]
    missing = [f for f in required if f not in asset]
    if missing:
        return (False,
                f"heartbeat asset is missing {missing}; every update will fail atomically. "
                f"Recreate the asset with these fields, or set HEARTBEAT_WATERLINE_*_FIELD "
                f"to the names it actually has: {sorted(k for k in asset.keys())}")
    return (True, f"heartbeat asset OK ({', '.join(required)})")


def get_heartbeat_asset() -> Optional[Dict[str, Any]]:
    cmd = [config.NEXUS_CLI, "assets/get/asset", f"name={config.NEXUS_HEARTBEAT_ASSET_NAME}"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            _log("nexus_heartbeat_lookup_failed", level=logging.ERROR, error=redact(err or out))
            return None
        data = _parse_json_lenient(out)
        if not isinstance(data, dict) or not data.get("address"):
            _log("nexus_heartbeat_lookup_failed", level=logging.ERROR,
                 error=redact(out), reason="invalid_response")
            return None
        return data
    except Exception as e:
        _log("nexus_heartbeat_lookup_failed", level=logging.ERROR, error=redact(str(e)))
        return None


def fetch_deposits_since(treasury_addr: str, since_timestamp: int, max_pages: int = 50) -> list[dict]:
    """Fetch all Nexus credits to treasury since given timestamp.
    
    Args:
        treasury_addr: Nexus treasury account address
        since_timestamp: Unix timestamp to start from
        max_pages: Maximum pages to fetch (default 50)
    
    Returns:
        List of transaction dicts with CREDIT contracts to treasury
    """
    results = []
    limit = 100
    
    # Build base command
    base_cmd = [config.NEXUS_CLI]
    projection = (
        "register/transactions/finance:token/"
        "txid,timestamp,confirmations,contracts.id,contracts.OP,contracts.from,contracts.to,contracts.amount"
    )
    base_cmd.append(projection)
    base_cmd.append(f"name={config.NEXUS_TOKEN_NAME}")
    base_cmd.append("sort=timestamp")
    base_cmd.append("order=desc")  # Newest first
    
    # Do not apply a server-side amount filter to nested contracts.  A target node may
    # accept the expression but omit matching credits, which makes restart recovery lossy.
    # Callers apply their policy after receiving the complete transaction enumeration.
    
    for page in range(max_pages):
        cmd = list(base_cmd) + [f"limit={limit}", f"offset={page * limit}"]
        try:
            code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 12))
            if code != 0:
                _log("nexus_deposit_page_fetch_failed", level=logging.ERROR, page=page,
                     error=redact(err or out))
                break
            
            txs = _parse_json_lenient(out)
            if not isinstance(txs, list):
                txs = [txs] if txs else []
            
            if not txs:
                break  # No more results
            
            page_has_old_txs = False
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                
                ts = int(tx.get("timestamp") or 0)
                
                # Stop if we've gone past the waterline
                if ts < since_timestamp:
                    page_has_old_txs = True
                    continue
                
                # Check if this tx has CREDIT to treasury
                contracts = tx.get("contracts") or []
                has_credit_to_treasury = False
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    if str(c.get("OP") or "").upper() != "CREDIT":
                        continue
                    
                    # Extract 'to' address
                    to = c.get("to")
                    to_addr = ""
                    if isinstance(to, dict):
                        to_addr = str(to.get("address") or to.get("name") or "")
                    elif isinstance(to, str):
                        to_addr = to
                    
                    if to_addr == treasury_addr:
                        has_credit_to_treasury = True
                        break
                
                if has_credit_to_treasury:
                    results.append(tx)
            
            # Stop conditions
            if page_has_old_txs:
                break  # Reached below waterline
            if len(txs) < limit:
                break  # No more pages
        
        except Exception as e:
            _log("nexus_deposit_page_fetch_failed", level=logging.ERROR, page=page,
                 error=redact(str(e)))
            break
    
    return results
    

## Reference integer fetching

def get_last_reference() -> int | None:
    cmd = [config.NEXUS_CLI, "finance/transactions/token/timestamp,contracts.OP,contracts.id,contracts.reference", f"name={config.NEXUS_TOKEN_NAME}", "sort=timestamp", "order=desc", "limit=50"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            _log("nexus_reference_lookup_failed", level=logging.ERROR, error=redact(err or out))
            return None
        data = _parse_json_lenient(out)
        txs = data if isinstance(data, list) else [data]
        for tx in (txs or []):
            if not isinstance(tx, dict):
                continue
            for c in (tx.get("contracts") or []):
                if not isinstance(c, dict):
                    continue
                if str(c.get("OP")).upper() != "DEBIT":
                    continue
                ref = c.get("reference")
                if ref is not None:
                    try:
                        return int(ref)
                    except Exception:
                        continue
                elif ref is None:
                    continue
        return None
    except Exception as e:
        _log("nexus_reference_lookup_failed", level=logging.ERROR, error=redact(str(e)))
        return None