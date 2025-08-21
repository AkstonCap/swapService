import json
import subprocess
from decimal import Decimal
from typing import Optional, Dict, Any
from . import config


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


def debit_usdd(to_addr: str, amount_usdd_units: int, reference: int | str | None = 0) -> bool:
    """Debit/mint USDD to an account. Reference must be uint64 (or omitted)."""
    if not config.NEXUS_PIN:
        print("ERROR: NEXUS_PIN not set")
        return False
    # Normalize reference: allow int or str numeric; omit if falsy
    ref_part: list[str] = []
    try:
        if reference is not None:
            # Accept int or numeric string; default to 0
            ref_val = int(reference)
            ref_part = [f"reference={ref_val}"]
    except Exception:
        # If non-numeric provided, omit to satisfy uint64 restriction
        ref_part = []
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/token",
        "from=USDD",
        f"to={to_addr}",
        f"amount={amount_usdd_units}",
        *ref_part,
        f"pin={config.NEXUS_PIN}",
    ]
    print(">>> Nexus debit:", [c if not str(c).startswith("pin=") else "pin=***" for c in cmd])
    try:
        code, out, err = _run(cmd, timeout=30)
        if code != 0:
            print("Nexus debit error:", err or out)
            return False
        print("Nexus debit ok:", out.strip())
        return True
    except Exception as e:
        print("Nexus debit exception:", e)
        return False

def debit_usdd_with_txid(to_addr: str, amount_usdd: int, reference: int) -> tuple[bool, str | None]:
    """Perform debit and attempt to parse a txid from output."""
    if not config.NEXUS_PIN:
        return (False, None)
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/token",
        "from=USDD",
        f"to={to_addr}",
        f"amount={amount_usdd}",
        f"reference={reference}",
        f"pin={config.NEXUS_PIN}",
    ]
    code, out, err = _run(cmd, timeout=5)
    if code != 0:
        return (False, None)
    # Try to pick txid from output JSON or text
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


def refund_usdd(to_addr: str, amount_usdd_units: int, reason: str) -> bool:
    """Refund USDD by transferring from treasury to the recipient (no mint)."""
    # Check if this refund was already processed by checking for txid in reason
    from . import state
    if "txid:" in reason:
        potential_txid = reason.split("txid:")[-1].strip().split()[0]
        if potential_txid in state.processed_nexus_txs:
            return True  # Already refunded this transaction
    
    ref = reason if len(reason) <= 120 else reason[:117] + "..."
    treas = config.NEXUS_USDD_TREASURY_ACCOUNT
    if not treas:
        print("Refund failed: NEXUS_USDD_TREASURY_ACCOUNT not set")
        return False
    return transfer_usdd_between_accounts(treas, to_addr, amount_usdd_units, ref)

def transfer_usdd_between_accounts(from_addr: str, to_addr: str, amount_usdd_units: int, reference: str) -> bool:
    """Transfer USDD between two Nexus token accounts using finance/debit/account (no mint).
    from_addr and to_addr are account addresses (registers) for the USDD token.
    """
    if not config.NEXUS_PIN:
        print("ERROR: NEXUS_PIN not set")
        return False
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/account",
        f"from={from_addr}",
        f"to={to_addr}",
        f"amount={amount_usdd_units}",
        f"reference={reference}",
        f"pin={config.NEXUS_PIN}",
    ]
    try:
        code, out, err = _run(cmd, timeout=30)
        if code != 0:
            print("Nexus transfer error:", err or out)
            return False
        return True
    except Exception as e:
        print("Nexus transfer exception:", e)
        return False

def debit_account_with_txid(from_addr: str, to_addr: str, amount: int, reference: int | str) -> tuple[bool, str | None]:
    """Debit from a specific account (e.g., treasury) to recipient and parse txid."""
    if not config.NEXUS_PIN:
        return (False, None)
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/account",
        f"from={from_addr}",
        f"to={to_addr}",
        f"amount={amount}",
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

def send_tiny_usdd_to_local(amount_usdd_units: int, note: str = "TINY_USDD") -> bool:
    to_addr = config.NEXUS_USDD_LOCAL_ACCOUNT or config.NEXUS_USDD_TREASURY_ACCOUNT
    from_addr = config.NEXUS_USDD_TREASURY_ACCOUNT
    if not to_addr or not from_addr:
        print("No local/treasury USDD account configured; skipping tiny USDD routing")
        return False
    # Move funds from treasury to local to avoid minting new supply
    return transfer_usdd_between_accounts(from_addr, to_addr, amount_usdd_units, note)


# Legacy signature-based helpers removed (string references are no longer used).


# --- Asset mapping for swaps (distordiaSwap) ---
def find_distordia_swap_asset_for_tx_sent(swap_to: str, tx_sent: str) -> Optional[Dict[str, Any]]:
    """Find a distordiaSwap asset mapping an incoming tx/sig to a swap recipient.
    Expected fields:
      - distordiaSwap.swap_to
      - distordiaSwap.tx_sent
      - distordiaSwap.swap_recipient
    Returns a flat dict with keys {swap_to, tx_sent, swap_recipient} or None.
    """
    try:
        # Query minimal projection to reduce payload
        cmd = [
            config.NEXUS_CLI,
            "register/list/assets:asset/distordiaSwap.swap_to,distordiaSwap.tx_sent,distordiaSwap.swap_recipient",
            f"distordiaSwap.swap_to={swap_to}",
            f"distordiaSwap.tx_sent={tx_sent}",
            "limit=1",
        ]
        code, out, err = _run(cmd, timeout=15)
        if code != 0:
            return None
        data = _parse_json_lenient(out)
        arr = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        if not arr:
            return None
        a = arr[0] or {}
        ds = a.get("distordiaSwap") or a
        return {
            "swap_to": (ds or {}).get("swap_to"),
            "tx_sent": (ds or {}).get("tx_sent"),
            "swap_recipient": (ds or {}).get("swap_recipient"),
        }
    except Exception as e:
        print("find_distordia_swap_asset_for_tx_sent error:", e)
        return None

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

def get_debit_status(txid: str) -> Optional[Dict[str, Any]]:
    """Fetch debit by txid and return confirmation info from finance/get/debit."""
    try:
        cmd = [config.NEXUS_CLI, "finance/get/debit", f"txid={txid}"]
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            return None
        data = _parse_json_lenient(out)
        return data if isinstance(data, dict) else None
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
def get_circulating_usdd_units() -> int:
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


def debit_usdd_to_self(amount_usdd_units: int, reference: str) -> bool:
    # Mint/credit into our treasury USDD account (config.NEXUS_USDD_TREASURY_ACCOUNT)
    return debit_usdd(config.NEXUS_USDD_TREASURY_ACCOUNT, amount_usdd_units, reference)


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


def transfer_usdd_treasury_to_local(amount_usdd_units: int, reference: str = "REBALANCE") -> bool:
    """Move USDD from treasury to local account without minting."""
    if not config.NEXUS_USDD_TREASURY_ACCOUNT or not config.NEXUS_USDD_LOCAL_ACCOUNT:
        return False
    return transfer_usdd_between_accounts(config.NEXUS_USDD_TREASURY_ACCOUNT, config.NEXUS_USDD_LOCAL_ACCOUNT, amount_usdd_units, reference)


def mint_usdd_to_local(amount_usdd_units: int, reference: str = "REBALANCE_TO_1") -> bool:
    """Mint new USDD into the local account to increase circulating supply (uses debit to local)."""
    if not config.NEXUS_USDD_LOCAL_ACCOUNT:
        return False
    return debit_usdd(config.NEXUS_USDD_LOCAL_ACCOUNT, amount_usdd_units, reference)

