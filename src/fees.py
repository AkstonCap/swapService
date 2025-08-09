import json
from typing import Dict
from . import config

_fees_state: Dict[str, int] = {"usdc_accumulated": 0}

def _load():
    global _fees_state
    try:
        with open(config.FEES_STATE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict) and "usdc_accumulated" in data:
                _fees_state = {"usdc_accumulated": int(data.get("usdc_accumulated", 0))}
    except Exception:
        pass

def _save():
    try:
        with open(config.FEES_STATE_FILE, "w") as f:
            json.dump(_fees_state, f)
    except Exception:
        pass

def add_usdc_fee(amount_base_units: int):
    if amount_base_units <= 0:
        return
    if not isinstance(amount_base_units, int):
        amount_base_units = int(amount_base_units)
    _fees_state["usdc_accumulated"] = int(_fees_state.get("usdc_accumulated", 0)) + amount_base_units
    _save()

def get_usdc_fees() -> int:
    return int(_fees_state.get("usdc_accumulated", 0))

def reset_usdc_fees():
    _fees_state["usdc_accumulated"] = 0
    _save()

def process_fee_conversions():
    """Placeholder: convert accumulated USDC fees into SOL (for fees) and USDD→NXS on Nexus (for fees).
    Guarded by FEE_CONVERSION_ENABLED. Keeps logic side-effect free until implemented.
    """
    if not config.FEE_CONVERSION_ENABLED:
        return
    total_usdc = get_usdc_fees()
    if total_usdc <= 0 or total_usdc < config.FEE_CONVERSION_MIN_USDC:
        return
    # Check SOL balance and top up using a portion of USDC fees
    try:
        from . import solana_client
        lamports = solana_client.get_vault_sol_balance()
    except Exception:
        lamports = None
    if lamports is not None and config.SOL_TOPUP_MIN_LAMPORTS and lamports < config.SOL_TOPUP_MIN_LAMPORTS:
        need = max(0, config.SOL_TOPUP_TARGET_LAMPORTS - lamports)
        # naive conversion target: assume ~1 USDC per 0.01 SOL; actual rate needs DEX quote. Keep small and safe.
        usdc_for_sol = min(get_usdc_fees(), max(config.FEE_CONVERSION_MIN_USDC, need))
        if usdc_for_sol > 0:
            ok = solana_client.swap_usdc_for_sol_via_jupiter(usdc_for_sol)
            if ok:
                _fees_state["usdc_accumulated"] = max(0, _fees_state["usdc_accumulated"] - usdc_for_sol)
                _save()
            else:
                print("[fees] USDC->SOL swap failed (stub or DEX error)")

    # Buy NXS on Nexus using USDD backed by remaining USDC fees
    # Convert a portion of USDC fees to an equivalent USDD budget (1:1 units if same decimals)
    from . import nexus_client
    usdc_remaining = get_usdc_fees()
    if usdc_remaining >= config.FEE_CONVERSION_MIN_USDC and config.NEXUS_NXS_TOPUP_MIN:
        usdd_budget_units = int(usdc_remaining)  # assume USDD_DECIMALS==USDC_DECIMALS
        spent = nexus_client.buy_nxs_with_usdd_budget(usdd_budget_units)
        if spent > 0:
            # reduce fee ledger by the USDD spent (backed by USDC fees)
            _fees_state["usdc_accumulated"] = max(0, _fees_state["usdc_accumulated"] - spent)
            _save()

_load()
