import json
import subprocess
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


def is_valid_usdd_account(account: str) -> bool:
    """Check if a Nexus account exists and is a USDD token account."""
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


def _format_usdd_amount(amount_units: int) -> str:
    """Convert internal base units (USDD_DECIMALS) into decimal string required by Nexus CLI.

    Nexus finance API expects human-readable whole/decimal token amounts, not raw base units.
    Example: with USDD_DECIMALS=6, 110000 base units -> "0.11".
    """
    try:
        decs = int(getattr(config, 'USDD_DECIMALS', 6))
        if decs <= 0:
            return str(int(amount_units))
        q = Decimal(amount_units) / (Decimal(10) ** decs)
        # Normalize: remove trailing zeros while keeping at least one digit
        s = format(q.normalize(), 'f')
        if '.' in s:
            s = s.rstrip('0').rstrip('.') or '0'
        return s
    except Exception:
        return str(int(amount_units))



def get_usdd_send_amount_units(amount_usdc_units: int) -> int:
    """Net USDD to send, in BASE UNITS, for a USDC deposit in base units.

    Exact integer/Decimal arithmetic throughout. The previous float version returned
    values like 8.989999999847731e-07, which was interpolated straight into the CLI
    command as `amount=8.989999999847731e-07` - scientific notation a decimal parser
    will not accept, with 17 significant digits against a 6-decimal token.
    """
    try:
        gross = Decimal(int(amount_usdc_units)) / (Decimal(10) ** config.USDC_DECIMALS)
    except Exception:
        return 0
    flat_fee = Decimal(str(config.FLAT_FEE_USDD))
    dyn_bps = Decimal(max(0, int(config.DYNAMIC_FEE_BPS)))
    net = gross - (gross * dyn_bps / Decimal(10000)) - flat_fee
    if net <= 0:
        return 0
    # Round DOWN to whole base units: never pay out a fraction we cannot represent.
    return int((net * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value(rounding=ROUND_DOWN))


def get_usdd_send_amount(amount_usdc: int) -> Decimal:
    """Deprecated: prefer get_usdd_send_amount_units(). Returns Decimal token units."""
    return Decimal(get_usdd_send_amount_units(amount_usdc)) / (Decimal(10) ** config.USDD_DECIMALS)


def debit_usdd_with_txid(to_addr: str, amount_usdd_units: int, reference: int) -> tuple[bool, str | None]:
    """Perform USDD debit and attempt to parse a txid from output.

    `amount_usdd_units` is in BASE UNITS and is formatted for the CLI by
    _format_usdd_amount(), which emits a plain fixed-point decimal string. Passing a
    float here previously produced scientific notation in the command line.
    
    Args:
        to_addr: Destination Nexus USDD account address
        amount_usdd_units: Amount in BASE units (e.g. 10500000 for 10.5 USDD)
        reference: Unique reference number for this debit

    Returns:
        Tuple of (success, txid_or_None)
    """
    if not config.NEXUS_PIN:
        return (False, None)

    amount_str = _format_usdd_amount(int(amount_usdd_units))
    cmd = [config.NEXUS_CLI, "finance/debit/token", "from=USDD", f"to={to_addr}", f"amount={amount_str}", f"reference={reference}", f"pin={config.NEXUS_PIN}"]
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


def get_transactions_confirmations(txids, limit: int = 200) -> dict:
    """Batch: {txid: confirmations} in ONE CLI call.

    The per-txid version below fetched the *entire* USDD transaction history (no limit)
    and did so once per unconfirmed row, so N pending debits meant N unbounded fetches.
    """
    wanted = {str(t) for t in txids if t}
    out: dict = {}
    if not wanted:
        return out
    cmd = [config.NEXUS_CLI, "finance/transactions/token/txid,confirmations",
           f"name={config.NEXUS_TOKEN_NAME}", "sort=timestamp", "order=desc", f"limit={int(limit)}"]
    try:
        code, cli_out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20))
        if code != 0:
            return out
        data = _parse_json_lenient(cli_out)
        txs = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for tx in txs or []:
            if not isinstance(tx, dict):
                continue
            t = str(tx.get("txid") or "")
            if t in wanted and tx.get("confirmations") is not None:
                try:
                    out[t] = int(tx.get("confirmations"))
                except Exception:
                    continue
    except Exception as e:
        print(f"Error batch-fetching confirmations: {e}")
    return out


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
    """Check unconfirmed Nexus debits and handle confirmations or timeouts.
    
    Critical fix: Added timeout handling for stuck 'debited, awaiting confirmation' entries.
    If a debit doesn't confirm within USDC_CONFIRM_TIMEOUT_SEC, it's marked for refund.
    
    IMPORTANT: Only refund if transaction was NEVER found (confirmations is None).
    If transaction exists with confirmations > 0, the debit happened - do NOT refund!
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
    confirm_timeout_sec = int(getattr(config, "USDC_CONFIRM_TIMEOUT_SEC", 600))
    # One bounded lookup for the whole batch instead of an unbounded fetch per row.
    conf_map = get_transactions_confirmations([row[6] for row in sigs if row[6]])

    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in sigs:
        
        confirmations = conf_map.get(str(txid)) if txid else None
        
        # Case 1: Transaction NOT found at all - may have been dropped or failed silently
        if confirmations is None:
            # Check if we've waited too long for this debit to appear
            age_sec = int(time.time()) - int(timestamp or 0)
            if age_sec > confirm_timeout_sec:
                # Timeout - mark for refund (transaction never appeared)
                state_db.update_unprocessed_sig_status(sig, "to be refunded")
                print(f"[DEBIT_TIMEOUT] sig={sig} txid={txid} age={age_sec}s > {confirm_timeout_sec}s - transaction never found, marking for refund")
                processed_count += 1
            continue
        
        # Case 2: Transaction exists but not enough confirmations yet
        if confirmations < min_confirmations:
            # IMPORTANT: Do NOT refund! The debit happened, just not fully confirmed yet.
            # Wait for more confirmations - do not timeout a partially confirmed transaction.
            continue
        
        # Case 3: Transaction fully confirmed
        # Recalculate USDD amount from USDC (same fee logic as the debit), in BASE UNITS.
        usdd_out_base = get_usdd_send_amount_units(amount_usdc_units or 0)
        amount_usdd_debited = float(Decimal(usdd_out_base) / (Decimal(10) ** config.USDD_DECIMALS))

        # Bug #10 fix: Track fees when debit is confirmed.
        # Both sides are base units (USDC and USDD share decimals at 1:1 parity), so this
        # is exact integer arithmetic - no float scaling.
        try:
            usdc_in_base = int(amount_usdc_units or 0)
            fee_usdc_units = max(0, usdc_in_base - int(usdd_out_base))
            if fee_usdc_units > 0:
                state_db.add_fee_entry(
                    sig=sig,
                    txid=txid,
                    kind="swap_usdc_to_usdd",
                    amount_usdc_units=fee_usdc_units,
                    amount_usdd_units=None
                )
        except Exception as e:
            print(f"[FEE_TRACKING] Error recording fee for sig={sig}: {e}")
        
        # Get reference from latest if needed (or pass None since it's optional)
        reference = state_db.get_latest_reference()
        
        state_db.mark_processed_sig(sig, timestamp, amount_usdc_units, txid, amount_usdd_debited, "debit_confirmed", reference)
        state_db.remove_unprocessed_sig(sig)
        processed_count += 1
        
        current_time = time.monotonic()
        if current_time - time_start > timeout:
            break

    return processed_count


DEBIT_UNVERIFIED_STATUSES = ("debit in flight", "debit unverified")


def resolve_unverified_debits(limit: int = 200) -> int:
    """Resolve USDD debits whose outcome is unknown, using the chain as the oracle.

    Covers both a crash between intent and state-write, and a CLI response we could not
    parse. For each row we look up the unique per-attempt reference on-chain:

      found            -> the debit DID execute; record the txid and proceed (never refund)
      not found + young -> still within the grace window; leave it alone
      not found + old   -> the debit definitively did not execute with that reference,
                           so it is safe to retry (a retry allocates a NEW reference).
                           After MAX_ACTION_ATTEMPTS, fall back to refunding.

    Returns the number of rows whose state was resolved.
    """
    rows = state_db.get_sigs_pending_debit_verification(DEBIT_UNVERIFIED_STATUSES, limit=limit)
    if not rows:
        return 0

    # One lookup for the whole batch instead of one subprocess per row.
    found_map = find_usdd_debits_by_references([r[7] for r in rows if r[7] is not None])

    grace = int(getattr(config, "DEBIT_VERIFY_GRACE_SEC", 300))
    max_attempts = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
    now = int(time.time())
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

            found_txid = found_map.get(str(reference).strip())
            if found_txid:
                state_db.update_unprocessed_sig_txid(sig, found_txid)
                state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")
                state_db.release_reservation("usdc_to_usdd_debit", sig)
                print(f"[DEBIT_RESOLVE] sig={sig} ref={reference} CONFIRMED on-chain txid={found_txid}")
                resolved += 1
                continue

            attempted_at = state_db.get_attempt_last_timestamp(f"usdd_debit:{sig}") or int(timestamp or 0)
            if now - attempted_at <= grace:
                continue  # still settling; check again next cycle

            attempts = state_db.get_attempt_count(f"usdd_debit:{sig}")
            state_db.release_reservation("usdc_to_usdd_debit", sig)
            if attempts >= max_attempts:
                state_db.update_unprocessed_sig_status(sig, "to be refunded")
                print(f"[DEBIT_RESOLVE] sig={sig} ref={reference} not on-chain after "
                      f"{attempts} attempts; refunding")
            else:
                # Safe to retry: this reference provably never landed.
                state_db.update_unprocessed_sig_status(sig, "ready for processing")
                print(f"[DEBIT_RESOLVE] sig={sig} ref={reference} not on-chain after {grace}s; "
                      f"retrying (attempt {attempts})")
            resolved += 1
        except Exception as e:
            print(f"[DEBIT_RESOLVE] error for sig={sig}: {e}")
            continue

    return resolved


def quarantine_usdd(txid: str, amount_usdd_units: int, reason: str = "") -> bool:
    """Actually MOVE quarantined USDD out of the treasury.

    README/CONFIG/SETUP/SECURITY all state that USDD from exhausted refunds is moved to
    NEXUS_USDD_QUARANTINE_ACCOUNT so it stops counting toward the backing ratio. Nothing
    ever moved it - only a DB status was written - so the funds stayed in the live
    treasury and the ratio was overstated by exactly the quarantined amount.

    Returns True if the transfer succeeded (or there was nothing to move).
    """
    dest = getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", None)
    if not dest:
        print("[quarantine_usdd] NEXUS_USDD_QUARANTINE_ACCOUNT not set; USDD stays in treasury "
              "and will keep counting toward the backing ratio")
        return False
    if int(amount_usdd_units or 0) <= 0:
        return True
    treas = config.NEXUS_USDD_TREASURY_ACCOUNT
    if not treas:
        print("[quarantine_usdd] NEXUS_USDD_TREASURY_ACCOUNT not set")
        return False
    ref = f"quarantine:{txid}" if txid else (reason or "quarantine")
    ok = transfer_usdd_between_accounts(treas, dest, int(amount_usdd_units), ref[:120])
    if ok:
        print(f"[quarantine_usdd] moved {amount_usdd_units} base units to {dest} ({ref})")
    return ok


def refund_usdd(to_addr: str, amount_usdd_units: int, reason: str) -> bool:
    """Refund USDD by transferring from treasury to the recipient (amount in base units)."""
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
    return transfer_usdd_between_accounts(treas, to_addr, amount_usdd_units, ref)

def transfer_usdd_between_accounts(from_addr: str, to_addr: str, amount_usdd_units: int, reference: str) -> bool:
    """Transfer USDD between two Nexus token accounts. Amount is base units internally, formatted for CLI."""
    if not config.NEXUS_PIN:
        print("ERROR: NEXUS_PIN not set")
        return False
    amount_str = _format_usdd_amount(int(amount_usdd_units))
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
    amount_str = _format_usdd_amount(int(amount_units))
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/account",
        f"from={from_addr}",
        f"to={to_addr}",
        f"amount={amount_str}",
        f"reference={reference}",
        f"pin={config.NEXUS_PIN}",
    ]
    # Generous, consistent timeout (see debit_usdd_with_txid): avoid killing an
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


def mint_usdd_to_local(amount_units: int, reference: str | int = "REBALANCE") -> bool:
    """Mint USDD from supply to the configured local account.

    ``amount_units`` is in base units (USDD_DECIMALS); it is converted to the
    token units the Nexus CLI expects. Used by the optional fee-conversion rebalancer.
    """
    acct = getattr(config, "NEXUS_USDD_LOCAL_ACCOUNT", None)
    if not acct or amount_units <= 0:
        return False
    ok, _txid = debit_usdd_with_txid(acct, int(amount_units), reference)
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

def find_asset_receival_account_by_txid_and_owner(txid: str, owner: str) -> Optional[Dict[str, Any]]:
    """Query assets by txid_toService and owner; return { receival_account } if present.
    Used for USDD->USDC: results.txid_toService=<txid> AND results.owner=<owner>.
    """
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
            return None
        data = _parse_json_lenient(out)
        raw = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        items = []
        for a in raw or []:
            if not isinstance(a, dict):
                continue
            res = a.get("results") or a
            if not isinstance(res, dict):
                continue
            core = res.get("asset") if isinstance(res.get("asset"), dict) else res
            items.append(core)
        if not items:
            return None
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
        return {"receival_account": best.get("receival_account"), "owner": best.get("owner")}
    except Exception:
        return None


def find_usdd_debits_by_references(references, limit: int = 100) -> dict:
    """Batch form of find_usdd_debit_by_reference: {reference_str: txid} for those found.

    One CLI invocation for the whole set. The per-row version spawned a Nexus CLI
    subprocess for each unverified debit, each pulling the same page of transactions.
    """
    wanted = {str(r).strip() for r in references if r is not None}
    out: dict = {}
    if not wanted:
        return out
    cmd = [
        config.NEXUS_CLI,
        "finance/transactions/token/txid,timestamp,contracts.OP,contracts.reference,contracts.to,contracts.amount",
        f"name={config.NEXUS_TOKEN_NAME}",
        "sort=timestamp",
        "order=desc",
        f"limit={int(limit)}",
    ]
    try:
        code, cli_out, err = _run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 20))
        if code != 0:
            print("Nexus: batch debit-by-reference lookup error:", err or cli_out)
            return out
        data = _parse_json_lenient(cli_out)
        txs = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for tx in txs or []:
            if not isinstance(tx, dict):
                continue
            for c in (tx.get("contracts") or []):
                if not isinstance(c, dict):
                    continue
                if str(c.get("OP") or "").upper() != "DEBIT":
                    continue
                ref = c.get("reference")
                if ref is None:
                    continue
                key = str(ref).strip()
                if key in wanted and key not in out:
                    txid = tx.get("txid")
                    if txid:
                        out[key] = str(txid)
    except Exception as e:
        print("Nexus: batch debit-by-reference lookup exception:", e)
    return out


def find_usdd_debit_by_reference(reference, limit: int = 100) -> Optional[str]:
    """Return the txid of a USDD DEBIT carrying exactly this reference, else None.

    This is the authoritative "did my debit actually execute?" check for the
    USDC->USDD path. It keys on the per-attempt unique reference, so it is exact.

    NOTE: `was_usdd_debited_to_account_for_amount()` below cannot be used for this.
    It inspects the TREASURY account, but this path mints via
    `finance/debit/token from=USDD` (the token supply register), and it compares
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


def was_usdd_debited_to_account_for_amount(to_addr: str, amount_units: int, lookback_sec: int = 60, min_confirmations: int = 0) -> bool:
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
def list_market_bids(market: str = "USDD/NXS", limit: int = 20) -> list[Dict[str, Any]]:
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

def list_market_asks(market: str = "NXS/USDD", limit: int = 20) -> list[Dict[str, Any]]:
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
        "from=USDD",
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


def buy_nxs_with_usdd_budget(usdd_budget_units: int) -> int:
    """Buy NXS using up to usdd_budget_units (USDD token units).
    Strategy: consider best prices from both sides:
    - bids on market=USDD/NXS
    - asks on market=NXS/USDD
    Normalize to USDD-per-NXS price and NXS quantity, pick lowest price orders first,
    and execute full orders that fit in remaining budget. Returns total USDD spent (<= budget).
    """
    if usdd_budget_units <= 0:
        return 0

    remaining = Decimal(usdd_budget_units)
    spent_total = Decimal(0)

    # Gather candidate sell offers (we're buying NXS):
    offers: list[dict] = []  # { txid: str, price: Decimal (USDD/NXS), qty_nxs: Decimal }

    # 1) From USDD/NXS bids (interpreted per API as executable opposite when we pay USDD)
    try:
        bids = list_market_bids("USDD/NXS", limit=20)
    except Exception:
        bids = []
    for bid in bids or []:
        txid = bid.get("txid")
        price = _to_decimal(bid.get("price"))  # USDD per NXS
        order = bid.get("order") or {}
        qty_nxs = _to_decimal(order.get("amount"))  # NXS amount
        if not txid or price <= 0 or qty_nxs <= 0:
            continue
        offers.append({"txid": str(txid), "price": price, "qty_nxs": qty_nxs})

    # 2) From NXS/USDD asks (sellers of NXS)
    try:
        asks = list_market_asks("NXS/USDD", limit=20)
    except Exception:
        asks = []
    for ask in asks or []:
        txid = ask.get("txid")
        price = _to_decimal(ask.get("price"))  # USDD per NXS (since quote is USDD)
        contract = ask.get("contract") or {}
        qty_nxs = _to_decimal(contract.get("amount"))  # NXS amount being sold
        if not txid or price <= 0 or qty_nxs <= 0:
            continue
        offers.append({"txid": str(txid), "price": price, "qty_nxs": qty_nxs})

    if not offers:
        return 0

    # Sort by best (lowest) price, then larger qty to reduce tx count
    offers.sort(key=lambda o: (o["price"], -o["qty_nxs"]))

    # Plan: include full orders that fit in remaining USDD budget
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

    # Return truncated integer token units of USDD spent
    try:
        return int(spent_total)
    except Exception:
        return 0


# --- Treasury and metrics ---
def get_circulating_usdd() -> int:
    cmd = [config.NEXUS_CLI, "finance/get/token/currentsupply", f"name={config.NEXUS_TOKEN_NAME}"]
    try:
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            print("Nexus USDD current supply error:", redact(err or out))
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
        print("Nexus USDD current supply exception:", e)
        return 0


def get_circulating_usdd_units() -> int:
    """Circulating USDD supply in BASE units (token amount x 10**USDD_DECIMALS).

    Nexus 'currentsupply' is reported in human-readable token units (e.g. 4002.0),
    so it must be scaled to base units before comparing against on-chain base-unit
    balances (e.g. the vault USDC balance). Use THIS for backing/solvency math;
    use get_circulating_usdd() only for human-readable display.
    """
    cmd = [config.NEXUS_CLI, "finance/get/token/currentsupply", f"name={config.NEXUS_TOKEN_NAME}"]
    try:
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            print("Nexus USDD current supply error:", redact(err or out))
            return 0
        data = _parse_json_lenient(out)
        if isinstance(data, (int, float, str)):
            dec = Decimal(str(data))
        elif isinstance(data, dict):
            dec = Decimal(str(data["currentsupply"]))
        else:
            return 0
        decimals = int(getattr(config, "USDD_DECIMALS", 6))
        return int((dec * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))
    except Exception as e:
        print("Nexus USDD current supply exception:", e)
        return 0


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


def get_usdd_local_balance_units() -> int:
    """Return available USDD balance in the local account (if queryable via finance/get/account)."""
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
    """Fetch all USDD credits to treasury since given timestamp.
    
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
    
    # Use WHERE filter if available (may reduce bandwidth)
    # Filter at the dust floor, not the swap minimum: credits in between are real user
    # funds that must still be fetched so they can be recorded rather than lost.
    min_credit_threshold = config.DUST_CREDIT_USDD_UNITS / (10 ** config.USDD_DECIMALS)
    try:
        base_cmd.append(f"where='contracts.amount>={min_credit_threshold}'")
    except Exception:
        pass
    
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