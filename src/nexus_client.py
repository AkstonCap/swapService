import json
import subprocess
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any
from . import config
from . import state_db, nexus_client
import time


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
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
    if info.get("ticker") != "USDD":
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



def get_usdd_send_amount(amount_usdc: int) -> float:
    """Calculate the USDD amount to send, accounting for fees.
    
    Args:
        amount_usdc: Input amount in USDC base units (e.g., 10500000 for 10.5 USDC)
    
    Returns:
        Net USDD in token units (human-readable, e.g., 10.3 for 10.3 USDD).
        This can be passed directly to Nexus CLI which expects token units.
    """
    base_amount = amount_usdc / 10**config.USDC_DECIMALS
    flat_fee = float(config.FLAT_FEE_USDD)  # Convert string to float
    fee = base_amount * config.DYNAMIC_FEE_BPS / 10000 + flat_fee
    return base_amount - fee


def debit_usdd_with_txid(to_addr: str, amount_usdd: float, reference: int) -> tuple[bool, str | None]:
    """Perform USDD debit and attempt to parse a txid from output.
    
    Args:
        to_addr: Destination Nexus USDD account address
        amount_usdd: Amount in token units (human-readable, e.g., 10.5 for 10.5 USDD).
                     Nexus CLI expects token units, not base units.
        reference: Unique reference number for this debit
    
    Returns:
        Tuple of (success, txid_or_None)
    """
    if not config.NEXUS_PIN:
        return (False, None)
    
    cmd = [config.NEXUS_CLI, "finance/debit/token", "from=USDD", f"to={to_addr}", f"amount={amount_usdd}", f"reference={reference}", f"pin={config.NEXUS_PIN}"]
    code, out, err = _run(cmd, timeout=5)
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


def get_transaction_confirmations(txid: str) -> int | None:
    """Fetch transaction details by txid."""
    cmd = [config.NEXUS_CLI, "finance/transactions/token", f"name=USDD"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            return None
        res = _parse_json_lenient(out)
        res = [tx for tx in res if tx.get("txid") == txid]
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

    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in sigs:
        
        confirmations = get_transaction_confirmations(txid)
        
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
        # Recalculate USDD amount from USDC (same fee logic as debit)
        amount_usdd_debited = get_usdd_send_amount(amount_usdc_units or 0)
        
        # Bug #10 fix: Track fees when debit is confirmed
        # Fee = amount_usdc_units (base units) - amount_usdd_debited (token units) * 10^decimals
        try:
            usdc_in_base = int(amount_usdc_units or 0)
            usdd_out_base = int(amount_usdd_debited * (10 ** config.USDD_DECIMALS))
            fee_usdc_units = max(0, usdc_in_base - usdd_out_base)
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
            print("Nexus transfer error:", err or out)
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
    code, out, err = _run(cmd, timeout=5)
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
            print("Nexus market list error:", err or out)
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
            print("Nexus market list error:", err or out)
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
            print("Nexus market execute error:", err or out)
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
            print("Nexus USDD current supply error:", err or out)
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

    if wline_nxs is not None:
        cmd.append(f"last_safe_timestamp_nexus={wline_nxs}")
    
    if wline_sol is not None:
        cmd.append(f"last_safe_timestamp_solana={wline_sol}")

    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            print("Nexus: update heartbeat asset error:", err or out)
            return False
        data = _parse_json_lenient(out)
        if data.get("success"):
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
    

def get_heartbeat_asset() -> Optional[Dict[str, Any]]:
    cmd = [config.NEXUS_CLI, "assets/get/asset", f"name={config.NEXUS_HEARTBEAT_ASSET_NAME}"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            print("Nexus: get heartbeat asset error:", err or out)
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
    base_cmd.append("name=USDD")
    base_cmd.append("sort=timestamp")
    base_cmd.append("order=desc")  # Newest first
    
    # Use WHERE filter if available (may reduce bandwidth)
    min_credit_threshold = getattr(config, "MIN_CREDIT_USDD_UNITS", 100101) / (10 ** config.USDD_DECIMALS)
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
    cmd = [config.NEXUS_CLI, "finance/transactions/token/timestamp,contracts.OP,contracts.id,contracts.reference", "name=USDD", "sort=timestamp", "order=desc", "limit=50"]
    try:
        code, out, err = _run(cmd, timeout=5)
        if code != 0:
            print("Nexus: get last reference error:", err or out)
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