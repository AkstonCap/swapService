import json
import subprocess
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any
from . import config
from . import state_db, nexus_client
import time


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


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    cmd = apply_session(cmd)
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
            print(f"Error batch-fetching confirmations: {e}")
            return BatchLookup(out, False, "exception")

    return BatchLookup(out, False, "pagination_truncated")


def get_transaction_confirmations(txid: str) -> int | None:
    """Confirmations for a single txid (bounded). Prefer get_transactions_confirmations()."""
    cmd = [config.NEXUS_CLI, "finance/transactions/token/txid,confirmations",
           f"name={config.NEXUS_TOKEN_NAME}", "sort=timestamp", "order=desc", "limit=200"]
    try:
        code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20))
        if code != 0:
            return None
        res = _parse_json_lenient(out)
        if not isinstance(res, list):
            res = [res] if isinstance(res, dict) else []
        res = [tx for tx in res if isinstance(tx, dict) and tx.get("txid") == txid]
        return int(res[0].get("confirmations")) if res else None
    except Exception as e:
        print(f"Error fetching transaction {txid}: {e}")
    return None


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
            print(f"[DEBIT_CONFIRMATION_HOLD] sig={sig} has no txid; manual resolution required")
            continue

        confirmations = confirmation_lookup.values.get(str(txid))
        
        # A missing value is never proof that the debit did not execute.
        if confirmations is None:
            print(f"[DEBIT_CONFIRMATION_HOLD] sig={sig} txid={txid} "
                  f"lookup={confirmation_lookup.reason or 'not_observed'}")
            continue
        
        # Case 2: Transaction exists but not enough confirmations yet
        if confirmations < min_confirmations:
            # IMPORTANT: Do NOT refund! The debit happened, just not fully confirmed yet.
            # Wait for more confirmations - do not timeout a partially confirmed transaction.
            continue
        
        # Case 3: Transaction fully confirmed.  Archive the full immutable mint
        # evidence before deleting the queue row; balance reconciliation must never
        # depend on an unprocessed row surviving this transition.
        nexus_out_base = get_nexus_send_amount_units(int(amount_usdc_units or 0))
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
            print(f"[FEE_TRACKING] Error recording fee for sig={sig}: {e}")
        
        # Get reference from latest if needed (or pass None since it's optional)
        reference = state_db.get_latest_reference()
        
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
                print(f"[DEBIT_RESOLVE] sig={sig} has no reference; quarantining for manual review")
                resolved += 1
                continue

            found_txid = reference_lookup.values.get(str(reference).strip())
            if found_txid:
                state_db.update_unprocessed_sig_txid(sig, found_txid)
                state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")
                state_db.release_reservation(state_db.DEBIT_RESERVATION_KIND, sig)
                print(f"[DEBIT_RESOLVE] sig={sig} ref={reference} CONFIRMED on-chain txid={found_txid}")
                resolved += 1
                continue

            print(f"[DEBIT_RESOLVE_HOLD] sig={sig} ref={reference} "
                  f"lookup={reference_lookup.reason or 'not_observed'}")
            continue
        except Exception as e:
            print(f"[DEBIT_RESOLVE] error for sig={sig}: {e}")
            continue

    return resolved


def quarantine_nexus_token(txid: str, amount_usdd_units: int, reason: str = "") -> bool:
    """Actually MOVE quarantined funds out of the treasury.

    README/CONFIG/SETUP/SECURITY all state that funds from exhausted refunds is moved to
    NEXUS_USDD_QUARANTINE_ACCOUNT so it stops counting toward the backing ratio. Nothing
    ever moved it - only a DB status was written - so the funds stayed in the live
    treasury and the ratio was overstated by exactly the quarantined amount.

    Returns True if the transfer succeeded (or there was nothing to move).
    """
    dest = getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", None)
    if not dest:
        print("[quarantine_nexus_token] NEXUS_USDD_QUARANTINE_ACCOUNT not set; funds stay in the treasury "
              "and will keep counting toward the backing ratio")
        return False
    if int(amount_usdd_units or 0) <= 0:
        return True
    treas = config.NEXUS_USDD_TREASURY_ACCOUNT
    if not treas:
        print("[quarantine_nexus_token] NEXUS_USDD_TREASURY_ACCOUNT not set")
        return False
    ref = f"quarantine:{txid}" if txid else (reason or "quarantine")
    ok = transfer_nexus_between_accounts(treas, dest, int(amount_usdd_units), ref[:120])
    if ok:
        print(f"[quarantine_nexus_token] moved {amount_usdd_units} base units to {dest} ({ref})")
    return ok


def refund_nexus_token(to_addr: str, amount_usdd_units: int, reason: str) -> bool:
    """Refund by transferring from treasury to the recipient (amount in base units)."""
    # Check if this refund was already processed by checking for txid in reason
    from . import state_db
    if "txid:" in reason:
        potential_txid = reason.split("txid:")[-1].strip().split()[0]
        if state_db.is_processed_txid(potential_txid):
            return True  # Already refunded this transaction
    
    ref = reason if len(reason) <= 120 else reason[:117] + "..."
    treas = config.NEXUS_USDD_TREASURY_ACCOUNT
    if not treas:
        print("Refund failed: NEXUS_USDD_TREASURY_ACCOUNT not set")
        return False
    return transfer_nexus_between_accounts(treas, to_addr, amount_usdd_units, ref)

def transfer_nexus_between_accounts(from_addr: str, to_addr: str, amount_usdd_units: int, reference: str) -> bool:
    """Transfer the Nexus-side token between two Nexus token accounts. Amount is base units internally, formatted for CLI."""
    if not config.NEXUS_PIN:
        print("ERROR: NEXUS_PIN not set")
        return False
    amount_str = _format_nexus_amount(int(amount_usdd_units))
    cmd = [config.NEXUS_CLI, "finance/debit/account", f"from={from_addr}", f"to={to_addr}", f"amount={amount_str}", f"reference={reference}", f"pin={config.NEXUS_PIN}"]
    try:
        code, out, err = _run(cmd, timeout=30)
        if code != 0:
            print("Nexus transfer error:", redact(err or out))
            return False
        return True
    except Exception as e:
        print("Nexus transfer exception:", e)
        return False

def debit_account_with_txid(from_addr: str, to_addr: str, amount_units: int, reference: int | str) -> tuple[bool, str | None]:
    """Debit from a specific account (e.g., treasury) to recipient and parse txid.
    Input amount is in internal base units; formatted as decimal token amount for Nexus CLI.
    """
    if not config.NEXUS_PIN:
        return (False, None)
    amount_str = _format_nexus_amount(int(amount_units))
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/account",
        f"from={from_addr}",
        f"to={to_addr}",
        f"amount={amount_str}",
        f"reference={reference}",
        f"pin={config.NEXUS_PIN}",
    ]
    # Generous, consistent timeout (see debit_nexus_token_with_txid): avoid killing an
    # in-flight debit that may still commit on the node.
    code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 30))
    if code != 0:
        return (False, None)
    txid = None
    data = _parse_json_lenient(out)
    if isinstance(data, dict):
        txid = data.get("txid")
    if not txid:
        for line in (out or "").splitlines():
            if "txid=" in line:
                txid = line.split("txid=", 1)[1].strip().split()[0]
                break
    return (True, str(txid) if txid else None)


def mint_nexus_to_local(amount_units: int, reference: str | int = "REBALANCE") -> bool:
    """Mint the Nexus-side token from supply to the configured local account.

    ``amount_units`` is in base units (USDD_DECIMALS); it is converted to the
    token units the Nexus CLI expects. Used by the optional fee-conversion rebalancer.
    """
    acct = getattr(config, "NEXUS_USDD_LOCAL_ACCOUNT", None)
    if not acct or amount_units <= 0:
        return False
    ok, _txid = debit_nexus_token_with_txid(acct, int(amount_units), reference)
    return ok


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
                print("Nexus: batch debit-by-reference lookup error:", err or cli_out)
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
            print("Nexus: batch debit-by-reference lookup exception:", e)
            return BatchLookup(out, False, "exception")

    return BatchLookup(out, False, "pagination_truncated")


def find_nexus_debit_by_reference(reference, limit: int = 100) -> Optional[str]:
    """Return the txid of a Nexus-side DEBIT carrying exactly this reference, else None.

    This is the authoritative "did my debit actually execute?" check for the
    Solana->Nexus path. It keys on the per-attempt unique reference, so it is exact.

    NOTE: `was_nexus_debited_to_account_for_amount()` below cannot be used for this.
    It inspects the TREASURY account, but this path mints via
    `finance/debit/token from=<token>` (the token supply register), and it compares
    `int(contract.amount)` - a decimal token amount - against base units, so it never
    matches. Prefer this function.
    """
    if reference is None:
        return None
    cmd = [
        config.NEXUS_CLI,
        "finance/transactions/token/txid,timestamp,contracts.OP,contracts.reference,contracts.to,contracts.amount",
        f"name={config.NEXUS_TOKEN_NAME}",
        "sort=timestamp",
        "order=desc",
        f"limit={int(limit)}",
    ]
    try:
        code, out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20))
        if code != 0:
            print("Nexus: debit-by-reference lookup error:", redact(err or out))
            return None
        data = _parse_json_lenient(out)
        txs = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        target = str(reference).strip()
        for tx in txs or []:
            if not isinstance(tx, dict):
                continue
            for c in (tx.get("contracts") or []):
                if not isinstance(c, dict):
                    continue
                if str(c.get("OP") or "").upper() != "DEBIT":
                    continue
                ref = c.get("reference")
                if ref is not None and str(ref).strip() == target:
                    txid = tx.get("txid")
                    return str(txid) if txid else None
        return None
    except Exception as e:
        print("Nexus: debit-by-reference lookup exception:", e)
        return None


def was_nexus_debited_to_account_for_amount(to_addr: str, amount_units: int, lookback_sec: int = 60, min_confirmations: int = 0) -> bool:
    """Check treasury debits to a recipient for an exact amount within a recent window.
    This provides idempotency without relying on string references.
    """
    treas = config.NEXUS_USDD_TREASURY_ACCOUNT
    if not treas:
        return False
    cmd = [config.NEXUS_CLI, "finance/transaction/account", f"address={treas}"]
    try:
        code, out, err = _run(cmd, timeout=15)
        if code != 0:
            return False
        data = _parse_json_lenient(out)
        txs = data if isinstance(data, list) else [data]
        from time import time as _now
        cutoff = int(_now()) - int(lookback_sec or 0)
        scanned = 0
        for tx in (txs or []):
            if not isinstance(tx, dict):
                continue
            scanned += 1
            # Optional time filter if available
            try:
                ts = int(tx.get("timestamp") or 0)
                if ts and ts < cutoff:
                    break
            except Exception:
                pass
            conf = int(tx.get("confirmations") or 0)
            if conf < int(min_confirmations or 0):
                continue
            for c in (tx.get("contracts") or []):
                if not isinstance(c, dict):
                    continue
                if str(c.get("OP") or "").upper() != "DEBIT":
                    continue
                # Match by amount and recipient when possible
                amt = None
                try:
                    amt = int(c.get("amount") or c.get("value") or 0)
                except Exception:
                    amt = 0
                to_field = c.get("to") or c.get("address") or c.get("recipient") or None
                if amt == int(amount_units) and (not to_field or str(to_field) == str(to_addr)):
                    return True
            if scanned > 200:
                break
        return False
    except Exception:
        return False


# --- Nexus DEX (market) helpers ---
def token_nxs_market() -> str:
    """`<bridged token>/NXS` market pair for the configured Nexus-side token."""
    return f"{getattr(config, 'NEXUS_TOKEN_NAME', 'USDD')}/NXS"


def nxs_token_market() -> str:
    """`NXS/<bridged token>` market pair for the configured Nexus-side token."""
    return f"NXS/{getattr(config, 'NEXUS_TOKEN_NAME', 'USDD')}"


def list_market_bids(market: str | None = None, limit: int = 20) -> list[Dict[str, Any]]:
    market = market or token_nxs_market()
    cmd = [config.NEXUS_CLI, "market/list/bid", f"market={market}", "sort=price", "order=desc", f"limit={limit}"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            print("Nexus market list error:", redact(err or out))
            return []
        data = _parse_json_lenient(out)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            v = data.get("bids")
            if isinstance(v, list):
                return v
        return []
    except Exception as e:
        print("Nexus market list exception:", e)
        return []

def list_market_asks(market: str | None = None, limit: int = 20) -> list[Dict[str, Any]]:
    market = market or nxs_token_market()
    cmd = [config.NEXUS_CLI, "market/list/ask", f"market={market}", "sort=price", "order=asc", f"limit={limit}"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            print("Nexus market list error:", redact(err or out))
            return []
        data = _parse_json_lenient(out)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            v = data.get("asks")
            if isinstance(v, list):
                return v
        return []
    except Exception as e:
        print("Nexus market list exception:", e)
        return []

def execute_market_order(txid: str) -> bool:
    if not config.NEXUS_PIN:
        print("ERROR: NEXUS_PIN not set for market execute")
        return False
    cmd = [
        config.NEXUS_CLI,
        "market/execute/order",
        f"txid={txid}",
        f"from={config.NEXUS_TOKEN_NAME}",
        "to=default",
        f"pin={config.NEXUS_PIN}",
    ]
    try:
        code, out, err = _run(cmd, timeout=30)
        if code != 0:
            print("Nexus market execute error:", redact(err or out))
            return False
        print("Nexus market execute ok:", (out or "").strip())
        return True
    except Exception as e:
        print("Nexus market execute exception:", e)
        return False


def _to_decimal(x) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(0)


def buy_nxs_with_token_budget(nexus_budget_units: int) -> int:
    """Buy NXS using up to nexus_budget_units (Nexus token units).
    Strategy: consider best prices from both sides:
    - bids on market=<token>/NXS
    - asks on market=NXS/<token>
    Normalize to token-per-NXS price and NXS quantity, pick lowest price orders first,
    and execute full orders that fit in remaining budget. Returns total Nexus token spent (<= budget).
    """
    if nexus_budget_units <= 0:
        return 0

    remaining = Decimal(nexus_budget_units)
    spent_total = Decimal(0)

    # Gather candidate sell offers (we're buying NXS):
    offers: list[dict] = []  # { txid: str, price: Decimal (token/NXS), qty_nxs: Decimal }

    # 1) From <token>/NXS bids (interpreted per API as executable opposite when we pay the bridged token)
    try:
        bids = list_market_bids(token_nxs_market(), limit=20)
    except Exception:
        bids = []
    for bid in bids or []:
        txid = bid.get("txid")
        price = _to_decimal(bid.get("price"))  # bridged token per NXS
        order = bid.get("order") or {}
        qty_nxs = _to_decimal(order.get("amount"))  # NXS amount
        if not txid or price <= 0 or qty_nxs <= 0:
            continue
        offers.append({"txid": str(txid), "price": price, "qty_nxs": qty_nxs})

    # 2) From NXS/<token> asks (sellers of NXS)
    try:
        asks = list_market_asks(nxs_token_market(), limit=20)
    except Exception:
        asks = []
    for ask in asks or []:
        txid = ask.get("txid")
        price = _to_decimal(ask.get("price"))  # bridged token per NXS (the quote side)
        contract = ask.get("contract") or {}
        qty_nxs = _to_decimal(contract.get("amount"))  # NXS amount being sold
        if not txid or price <= 0 or qty_nxs <= 0:
            continue
        offers.append({"txid": str(txid), "price": price, "qty_nxs": qty_nxs})

    if not offers:
        return 0

    # Sort by best (lowest) price, then larger qty to reduce tx count
    offers.sort(key=lambda o: (o["price"], -o["qty_nxs"]))

    # Plan: include full orders that fit in remaining Nexus token budget
    plan: list[dict] = []  # { txid, cost }
    plan_cost = Decimal(0)
    for o in offers:
        cost = o["price"] * o["qty_nxs"]
        if cost <= 0:
            continue
        if plan_cost + cost <= remaining:
            plan.append({"txid": o["txid"], "cost": cost})
            plan_cost += cost
        if plan_cost >= remaining:
            break

    if plan_cost <= 0:
        return 0

    # Execute planned orders
    for item in plan:
        txid = item["txid"]
        cost = item["cost"]
        if cost > remaining:
            continue
        if execute_market_order(txid):
            spent_total += cost
            remaining -= cost
        else:
            print(f"Nexus: execute failed for order {txid}")

    # Return truncated integer token units of Nexus token spent
    try:
        return int(spent_total)
    except Exception:
        return 0


# --- Treasury and metrics ---
def get_circulating_nexus_supply() -> int:
    cmd = [config.NEXUS_CLI, "finance/get/token/currentsupply", f"name={config.NEXUS_TOKEN_NAME}"]
    try:
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            print("Nexus Nexus token supply error:", redact(err or out))
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
        print("Nexus Nexus token supply exception:", e)
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
            print("Nexus: service record update error:", redact(err or out))
            return False
        data = _parse_json_lenient(out)
        return bool(isinstance(data, dict) and data.get("success"))
    except Exception as e:
        print("Nexus: service record update exception:", redact(str(e)))
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
            print("Nexus: update heartbeat asset error:", redact(err or out))
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
        print("Error updating heartbeat asset:", e)
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
            print("Nexus: get heartbeat asset error:", redact(err or out))
            return None
        data = _parse_json_lenient(out)
        if not isinstance(data, dict) or not data.get("address"):
            print("Nexus: get heartbeat asset failed:", out)
            return None
        return data
    except Exception as e:
        print("Error getting heartbeat asset:", e)
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
                print(f"Nexus: fetch deposits page {page} error:", err or out)
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
            print(f"Error fetching deposits page {page}:", e)
            break
    
    return results
    

## Reference integer fetching

def get_last_reference() -> int | None:
    cmd = [config.NEXUS_CLI, "finance/transactions/token/timestamp,contracts.OP,contracts.id,contracts.reference", f"name={config.NEXUS_TOKEN_NAME}", "sort=timestamp", "order=desc", "limit=50"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            print("Nexus: get last reference error:", redact(err or out))
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
        print("Error getting last reference:", e)
        return None