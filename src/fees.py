import json
import time
import threading
from typing import Dict, Any
from . import config, state_db

_fees_lock = threading.Lock()
FEE_EVENTS_FILE = getattr(config, "FEE_EVENTS_FILE", "fee_events.jsonl")

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

def add_usdc_fee(amount_base_units: int, *, sig: str | None = None, kind: str | None = None):
    """Accumulate USDC fee (base units) with simple locking and event journal.
    kind examples: flat, dynamic, fee_only, refund_flat
    """
    if amount_base_units <= 0:
        return
    if not isinstance(amount_base_units, int):
        amount_base_units = int(amount_base_units)
    with _fees_lock:
        _fees_state["usdc_accumulated"] = int(_fees_state.get("usdc_accumulated", 0)) + amount_base_units
        _save()
        try:
            evt: Dict[str, Any] = {
                "ts": int(time.time()),
                "sig": sig,
                "amount": amount_base_units,
                "kind": kind or "generic"
            }
            with open(FEE_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(evt, ensure_ascii=False)+"\n")
        except Exception:
            pass

def get_usdc_fees() -> int:
    return int(_fees_state.get("usdc_accumulated", 0))

def reset_usdc_fees():
    _fees_state["usdc_accumulated"] = 0
    _save()

def reconcile_accounting(expected_total: int | None = None) -> dict:
    """Recalculate fee sum from journal; optionally compare with expected_total.
    Returns dict with {journal_sum, stored, delta}.
    """
    journal_sum = 0
    try:
        if FEE_EVENTS_FILE and open:
            with open(FEE_EVENTS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line=line.strip()
                    if not line:
                        continue
                    try:
                        row=json.loads(line)
                        journal_sum += int(row.get("amount") or 0)
                    except Exception:
                        continue
    except Exception:
        pass
    stored = int(_fees_state.get("usdc_accumulated", 0))
    if expected_total is not None and journal_sum != expected_total:
        # Could update or log discrepancy; for now just compute.
        pass
    delta = journal_sum - stored
    # If journal ahead of stored (delta>0), bring stored up (self-heal).
    if delta > 0:
        with _fees_lock:
            _fees_state["usdc_accumulated"] = journal_sum
            _save()
    return {"journal_sum": journal_sum, "stored": stored, "delta": delta}

def process_fee_conversions():
    """Policy-driven rebalance when backing ratio > 1.
    - Check balances: SOL (lamports), USDC (vault ATA), NXS (via finance/get/balances), USDD circulating supply.
    - Only act if vault_usdc > circ_usdd (backing ratio > 1).
    - Cases:
      1) Only SOL below min: spend USDC to buy SOL until ratio == 1 or SOL reaches target; if hit target first, move USDD from treasury to local to bring ratio to 1.
      2) Only NXS below min: move USDD from treasury to local to bring ratio to 1, then buy NXS using up to all local USDD.
      3) Both SOL and NXS below min: spend 50% of USDC surplus to SOL, 50% via USDD buy path to NXS.
      4) Neither below min and ratio >= 1.05 and vault USDC > threshold: move USDD from treasury to local to bring ratio to 1.
    - USDC fees remain in the vault. This function uses actual vault USDC balance, not the "accumulated" tracker.
    """
    if not config.FEE_CONVERSION_ENABLED:
        return
    try:
        from . import solana_client, nexus_client
        # Read balances
        vault_usdc = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT))
        circ_usdd = nexus_client.get_circulating_usdd_units()
        lamports = solana_client.get_vault_sol_balance()
        nxs_units = nexus_client.get_nxs_default_balance_units()
        # thresholds
        sol_min = int(config.SOL_TOPUP_MIN_LAMPORTS or 0)
        sol_target = int(config.SOL_TOPUP_TARGET_LAMPORTS or 0)
        nxs_min = int(config.NEXUS_NXS_TOPUP_MIN or 0)

        # Compute backing surplus (USDC - USDD)
        surplus = max(0, vault_usdc - circ_usdd)
        if surplus <= 0:
            return

        sol_below = lamports is not None and sol_min and lamports < sol_min
        nxs_below = nxs_min and nxs_units < nxs_min

        # Helper to mint USDD from treasury to local up to delta
        def _mint_usdd_to_local(units: int) -> int:
            if units <= 0:
                return 0
            ok = nexus_client.mint_usdd_to_local(units, "REBALANCE_TO_1")
            return units if ok else 0

        # Helper to buy SOL using Jupiter spending USDC base units (not exceeding surplus)
        def _buy_sol_with_usdc(usdc_units: int) -> int:
            amt = max(0, min(usdc_units, surplus))
            if amt <= 0:
                return 0
            ok = solana_client.swap_usdc_for_sol_via_jupiter(amt)
            if ok:
                return amt
            return 0

        # Helper to buy NXS using local USDD: spends up to given usdd budget
        def _buy_nxs_with_local_usdd(usdd_budget: int) -> int:
            return int(nexus_client.buy_nxs_with_usdd_budget(usdd_budget))

        # Case evaluations
        if sol_below and not nxs_below:
            # Spend USDC to buy SOL until ratio 1 or SOL reaches target
            # Spend at most 'surplus' USDC
            spent_usdc = _buy_sol_with_usdc(surplus)
            # Recompute surplus after spend
            vault_usdc2 = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT))
            surplus2 = max(0, vault_usdc2 - circ_usdd)
            # If SOL reached target before ratio 1, move USDD from treasury to local to reduce ratio to 1
            if sol_target and lamports is not None:
                lamports = solana_client.get_vault_sol_balance()
                if lamports >= sol_target and surplus2 > 0:
                    _mint_usdd_to_local(min(surplus2, vault_usdc2))
            return

        if nxs_below and not sol_below:
            # First bring ratio to 1 by moving USDD from treasury to local
            moved = _mint_usdd_to_local(surplus)
            # Then purchase NXS with all available local USDD
            local_usdd = nexus_client.get_usdd_local_balance_units()
            if local_usdd > 0:
                _buy_nxs_with_local_usdd(local_usdd)
            return

        if sol_below and nxs_below:
            half = surplus // 2
            _buy_sol_with_usdc(half)
            moved = _mint_usdd_to_local(surplus - half)
            local_usdd = nexus_client.get_usdd_local_balance_units()
            if local_usdd > 0:
                _buy_nxs_with_local_usdd(local_usdd)
            return

        # Neither below min: if ratio above 1.05 and vault usdc > threshold, move USDD to local to bring back to 1
        # Use 5% margin on circulating (ceil): amount to move = min(surplus, vault_usdc)
        if vault_usdc * 100 >= circ_usdd * 105 and vault_usdc > config.BACKING_SURPLUS_MINT_THRESHOLD_USDC_UNITS:
            _mint_usdd_to_local(min(surplus, vault_usdc))
            return
    except Exception as e:
        print(f"[fees] process_fee_conversions error: {e}")

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
