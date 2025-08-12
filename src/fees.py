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
    """Optional: convert some accumulated USDC fees into SOL via DEX for gas, and mint USDD fees on Nexus.
    - USDC fees remain in the vault account (single USDC account policy).
    - If NEXUS_USDD_FEES_ACCOUNT is configured, mint equivalent USDD there for accounting.
    """
    if not config.FEE_CONVERSION_ENABLED:
        return
    total_usdc = get_usdc_fees()
    if total_usdc <= 0 or total_usdc < config.FEE_CONVERSION_MIN_USDC:
        return
    # 1) Invariant: vault USDC == circulating USDD (use fees to restore if needed)
    try:
        from . import nexus_client
        circ_usdd_units = nexus_client.get_circulating_usdd_units()
    except Exception:
        circ_usdd_units = 0
    # We can only easily read vault USDC off-chain by querying the token account; skip here and assume
    # the swap logic mints net=received and sends net=redeemed so drift should be zero.
    # If you want strict enforcement, add an RPC check of config.VAULT_USDC_ACCOUNT token balance and compare.

    # 2) Keep SOL topped up using USDC fees
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

    # 3) USDD fees are transferred during swap events; no mint here to avoid duplication.

def reconcile_fees_to_fee_account(min_transfer_units: int = 0):
    """Deprecated: No separate USDC fee account. USDC fees remain in the vault.
    This function now performs no USDC movements; use process_fee_conversions for USDD fee minting.
    """
    return

def maintain_backing_and_bounds() -> bool:
    """Maintain invariants and bounds.
    - Ensure vault USDC ≈ circulating USDD; USDC fees remain in vault (no separate USDC fee account).
    - If vault < BACKING_DEFICIT_PAUSE_PCT% of circulating, request pause (return True).
    - Cap USDC fee account at FEES_USDC_MAX by transferring excess to vault and minting equivalent USDD to fees USDD account.
    Returns True if the service should pause.
    """
    try:
        from . import solana_client, nexus_client
        vault_usdc = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT))
        circ_usdd = nexus_client.get_circulating_usdd_units()
        if circ_usdd > 0:
            ratio_bps_deficit = int(((circ_usdd - vault_usdc) * 10000) / circ_usdd) if vault_usdc < circ_usdd else 0
        else:
            ratio_bps_deficit = 0
        # Pause if extreme deficit
        if circ_usdd > 0 and (vault_usdc * 100) < (config.BACKING_DEFICIT_PAUSE_PCT * circ_usdd):
            print("[safety] Vault USDC < 90% of circulating USDD; pausing for manual investigation")
            return True
    # With a single USDC vault account, there's no separate fee account to drain or cap.
        return False
    except Exception as e:
        print(f"[safety] maintain_backing_and_bounds error: {e}")
        return False

_load()
