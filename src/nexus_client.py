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
