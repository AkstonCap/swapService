import json
import subprocess
from decimal import Decimal
from typing import Optional, Dict, Any
from . import config


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def get_account_info(nexus_addr: str) -> Optional[Dict[str, Any]]:
    cmd = [config.NEXUS_CLI, "register/get/finance:account", f"address={nexus_addr}"]
    try:
        code, out, err = _run(cmd, timeout=10)
        if code != 0:
            return None
        return json.loads(out)
    except Exception:
        return None


def _dict_get_ci(d: Dict[str, Any], key: str):
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def is_expected_token(account_info: Dict[str, Any], expected: str) -> bool:
    if not isinstance(account_info, dict):
        return False
    for key in ("ticker", "token", "symbol", "name"):
        v = _dict_get_ci(account_info, key)
        if isinstance(v, str) and v.upper() == expected.upper():
            return True
    for container in ("result", "account", "data"):
        inner = _dict_get_ci(account_info, container)
        if isinstance(inner, dict) and is_expected_token(inner, expected):
            return True
    return False


def _base_units_to_decimal_str(units: int, decimals: int) -> str:
    q = Decimal(10) ** -decimals
    return str((Decimal(int(units)) / (Decimal(10) ** decimals)).quantize(q))


def debit_usdd(to_addr: str, amount_usdd_units: int, reference: str) -> bool:
    if not config.NEXUS_PIN:
        print("ERROR: NEXUS_PIN not set")
        return False
    amt_str = _base_units_to_decimal_str(amount_usdd_units, config.USDD_DECIMALS)
    cmd = [
        config.NEXUS_CLI,
        "finance/debit/account",
        "from=USDD",
        f"to={to_addr}",
        f"amount={amt_str}",
        f"reference={reference}",
        f"pin={config.NEXUS_PIN}",
    ]
    print(">>> Nexus debit:", cmd[:-1] + ["pin=***"])  # hide PIN
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


def refund_usdd(to_addr: str, amount_usdd_units: int, reason: str) -> bool:
    ref = reason if len(reason) <= 120 else reason[:117] + "..."
    return debit_usdd(to_addr, amount_usdd_units, ref)


# --- Nexus DEX (market) helpers ---
def list_market_asks(market: str = "NXS/USDD", limit: int = 10) -> list[Dict[str, Any]]:
    cmd = [config.NEXUS_CLI, "market/list/ask", f"market={market}", "sort=price", "order=asc", f"limit={limit}"]
    try:
        code, out, err = _run(cmd, timeout=15)
        if code != 0:
            print("Nexus market list error:", err or out)
            return []
        data = json.loads(out)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Sometimes results might be under a key
            for k in ("result", "orders", "data", "asks"):
                v = data.get(k)
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
    """Buy NXS using up to usdd_budget_units. Returns spent USDD units (<= budget).
    Strategy: list top asks (cheapest first), execute orders whose total cost fits remaining budget, one by one.
    Assumptions: list_market_asks returns items with fields: txid, price (USDD per NXS), amount (NXS quantity).
    """
    if usdd_budget_units <= 0:
        return 0
    asks = list_market_asks("NXS/USDD", limit=20)
    if not asks:
        return 0

    remaining = Decimal(usdd_budget_units) / (Decimal(10) ** config.USDD_DECIMALS)
    spent_total = Decimal(0)

    # Build a plan of orders whose total cost <= budget
    plan: list[dict] = []
    plan_cost = Decimal(0)
    for ask in asks:
        txid = ask.get("txid") or ask.get("id") or ask.get("orderId")
        price = _to_decimal(ask.get("price"))
        amount = _to_decimal(ask.get("amount"))
        if not txid or price <= 0 or amount <= 0:
            continue
        cost = price * amount
        if cost <= 0:
            continue
        if plan_cost + cost <= remaining:
            plan.append({"txid": str(txid), "cost": cost})
            plan_cost += cost
        if plan_cost >= remaining:
            break

    if plan_cost <= 0:
        return 0

    # Mint USDD into our own account to cover plan_cost
    mint_units = int((plan_cost * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value())
    if not debit_usdd(config.NEXUS_USDD_ACCOUNT, mint_units, "FEE_CONV_NXS"):
        print("Nexus: failed to mint USDD for NXS purchase")
        return 0

    # Execute planned orders until budget is exhausted
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


    spent_units = int((spent_total * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value())
    return spent_units
