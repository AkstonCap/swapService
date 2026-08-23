import json
import time
import threading
from typing import Dict, Any
from . import config, state_db

_fees_lock = threading.Lock()
FEE_EVENTS_FILE = getattr(config, "FEE_EVENTS_FILE", "fee_events.jsonl")

_fees_state: Dict[str, int] = {"solana_accumulated": 0}

def _load():
    global _fees_state
    try:
        with open(config.FEES_STATE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict) and "solana_accumulated" in data:
                _fees_state = {"solana_accumulated": int(data.get("solana_accumulated", 0))}
    except Exception:
        pass

def _save():
    try:
        with open(config.FEES_STATE_FILE, "w") as f:
            json.dump(_fees_state, f)
    except Exception:
        pass

def add_solana_fee(amount_base_units: int, *, sig: str | None = None, kind: str | None = None):
    """Record a Solana-side fee (base units).

    The DATABASE (`fee_entries`) is the single source of truth. This module previously
    kept a parallel JSON ledger (fees_state.json + fee_events.jsonl) that was never
    reconciled against the DB, so the two disagreed and neither could be trusted for
    accounting. The JSON journal is still appended as a legacy mirror only.
    """
    if amount_base_units <= 0:
        return
    if not isinstance(amount_base_units, int):
        amount_base_units = int(amount_base_units)
    try:
        state_db.add_fee_entry(sig=sig, txid=None, kind=kind or "generic",
                               amount_usdc_units=amount_base_units, amount_usdd_units=None)
    except Exception as e:
        print(f"[fees] failed to record fee in DB: {e}")
    with _fees_lock:
        _fees_state["solana_accumulated"] = int(_fees_state.get("solana_accumulated", 0)) + amount_base_units
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

def get_solana_fees() -> int:
    """Total Solana-side fees, read from the authoritative DB ledger."""
    try:
        return int(state_db.get_total_fees_collected()[0])
    except Exception:
        return int(_fees_state.get("solana_accumulated", 0))

def reset_solana_fees():
    _fees_state["solana_accumulated"] = 0
    _save()

def reconcile_accounting(expected_total: int | None = None) -> dict:
    """Compare the legacy JSON journal against the authoritative DB ledger.

    Returns {journal_sum, stored, db_total, drift_vs_db, delta}. A non-zero
    `drift_vs_db` means the legacy files disagree with the database; the DB wins.
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
    stored = int(_fees_state.get("solana_accumulated", 0))
    if expected_total is not None and journal_sum != expected_total:
        # Could update or log discrepancy; for now just compute.
        pass
    delta = journal_sum - stored
    # If journal ahead of stored (delta>0), bring stored up (self-heal).
    if delta > 0:
        with _fees_lock:
            _fees_state["solana_accumulated"] = journal_sum
            _save()
    try:
        db_total = int(state_db.get_total_fees_collected()[0])
    except Exception:
        db_total = 0
    drift_vs_db = journal_sum - db_total
    if drift_vs_db:
        # Report, never auto-correct: a silent "fix" would hide whichever side is wrong.
        print(f"[fees] legacy JSON journal disagrees with the DB ledger by {drift_vs_db} "
              f"Solana base units (journal={journal_sum} db={db_total}); the DB is authoritative")
    return {"journal_sum": journal_sum, "stored": stored, "db_total": db_total,
            "drift_vs_db": drift_vs_db, "delta": delta}

def process_fee_conversions():
    """Policy-driven rebalance when backing ratio > 1.
    - Check balances: SOL (lamports), the bridged token (vault ATA), NXS (via finance/get/balances), Nexus circulating supply.
    - Only act if vault_solana > circ_nexus (backing ratio > 1).
    - Cases:
      1) Only SOL below min: spend the Solana-side token to buy SOL until ratio == 1 or SOL reaches target; if hit target first, move funds from the treasury to local to bring ratio to 1.
      2) Only NXS below min: move funds from the treasury to local to bring ratio to 1, then buy NXS using up to all the local Nexus account.
      3) Both SOL and NXS below min: spend 50% of Solana-side surplus to SOL, 50% via Nexus-side buy path to NXS.
      4) Neither below min and ratio >= 1.05 and the vault > threshold: move funds from the treasury to local to bring ratio to 1.
    - Solana-side fees remain in the vault. This function uses actual vault balance, not the "accumulated" tracker.
    """
    if not config.FEE_CONVERSION_ENABLED:
        return
    try:
        from . import solana_client, nexus_client
        # Read balances
        vault_solana = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT), max_age_sec=5)
        circ_nexus = nexus_client.get_circulating_nexus_units()
        lamports = solana_client.get_vault_sol_balance()
        nxs_units = nexus_client.get_nxs_default_balance_units()
        # thresholds
        sol_min = int(config.SOL_TOPUP_MIN_LAMPORTS or 0)
        sol_target = int(config.SOL_TOPUP_TARGET_LAMPORTS or 0)
        nxs_min = int(config.NEXUS_NXS_TOPUP_MIN or 0)

        # Compute backing surplus (vault - circulating)
        surplus = max(0, vault_solana - circ_nexus)
        if surplus <= 0:
            return

        sol_below = lamports is not None and sol_min and lamports < sol_min
        nxs_below = nxs_min and nxs_units < nxs_min

        # Helper to mint funds from the treasury to local up to delta
        def _mint_nexus_to_local(units: int) -> int:
            if units <= 0:
                return 0
            ok = nexus_client.mint_nexus_to_local(units, "REBALANCE_TO_1")
            return units if ok else 0

        # Helper to buy SOL using Jupiter spending Solana base units (not exceeding surplus)
        def _buy_sol_with_solana_token(solana_units: int) -> int:
            amt = max(0, min(solana_units, surplus))
            if amt <= 0:
                return 0
            ok = solana_client.swap_token_for_sol_via_jupiter(amt)
            if ok:
                return amt
            return 0

        # Helper to buy NXS using the local Nexus account: spends up to given usdd budget
        def _buy_nxs_with_local_nexus_token(nexus_budget: int) -> int:
            return int(nexus_client.buy_nxs_with_token_budget(nexus_budget))

        # Case evaluations
        if sol_below and not nxs_below:
            # Spend the Solana-side token to buy SOL until ratio 1 or SOL reaches target
            # Spend at most the 'surplus' amount
            spent_solana = _buy_sol_with_solana_token(surplus)
            # Recompute surplus after spend
            vault_solana2 = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT))
            surplus2 = max(0, vault_solana2 - circ_nexus)
            # If SOL reached target before ratio 1, move funds from the treasury to local to reduce ratio to 1
            if sol_target and lamports is not None:
                lamports = solana_client.get_vault_sol_balance()
                if lamports >= sol_target and surplus2 > 0:
                    _mint_nexus_to_local(min(surplus2, vault_solana2))
            return

        if nxs_below and not sol_below:
            # First bring ratio to 1 by moving funds from the treasury to local
            moved = _mint_nexus_to_local(surplus)
            # Then purchase NXS with all available the local Nexus account
            local_nexus = nexus_client.get_nexus_local_balance_units()
            if local_nexus > 0:
                _buy_nxs_with_local_nexus_token(local_nexus)
            return

        if sol_below and nxs_below:
            half = surplus // 2
            _buy_sol_with_solana_token(half)
            moved = _mint_nexus_to_local(surplus - half)
            local_nexus = nexus_client.get_nexus_local_balance_units()
            if local_nexus > 0:
                _buy_nxs_with_local_nexus_token(local_nexus)
            return

        # Neither below min: if ratio above 1.05 and the vault > threshold, move the Nexus-side token to local to bring back to 1
        # Use 5% margin on circulating (ceil): amount to move = min(surplus, vault_solana)
        if vault_solana * 100 >= circ_nexus * 105 and vault_solana > config.BACKING_SURPLUS_MINT_THRESHOLD_SOLANA_UNITS:
            _mint_nexus_to_local(min(surplus, vault_solana))
            return
    except Exception as e:
        print(f"[fees] process_fee_conversions error: {e}")

def reconcile_fees_to_fee_account(min_transfer_units: int = 0):
    """Deprecated: No separate Solana-side fee account. Solana-side fees remain in the vault.
    This function now performs no Solana-side movements; use process_fee_conversions for Nexus-side fee minting.
    """
    return

def maintain_backing_and_bounds() -> bool:
    """Maintain invariants and bounds.
    - Ensure the vault ≈ circulating supply; Solana-side fees remain in vault (no separate Solana-side fee account).
    - If vault < BACKING_DEFICIT_PAUSE_PCT% of circulating, request pause (return True).
    - Cap the Solana-side fee account at FEES_USDC_MAX by transferring the excess to the
      vault and minting the equivalent amount into the Nexus fees account.
    Returns True if the service should pause.
    """
    try:
        from . import solana_client, nexus_client
        vault_solana = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT), max_age_sec=5)
        circ_nexus = nexus_client.get_circulating_nexus_units()
        if circ_nexus > 0:
            ratio_bps_deficit = int(((circ_nexus - vault_solana) * 10000) / circ_nexus) if vault_solana < circ_nexus else 0
        else:
            ratio_bps_deficit = 0
        # Pause if extreme deficit
        if circ_nexus > 0 and (vault_solana * 100) < (config.BACKING_DEFICIT_PAUSE_PCT * circ_nexus):
            print("[safety] The vault < 90% of circulating supply; pausing for manual investigation")
            return True
    # With a single Solana vault account, there's no separate fee account to drain or cap.
        return False
    except Exception as e:
        print(f"[safety] maintain_backing_and_bounds error: {e}")
        return False

_load()
