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

def available_backing_surplus_solana_units(
    vault_solana_units: int, circulating_nexus_units: int
) -> int:
    """Spendable backing surplus after every unresolved user liability.

    Database read failures propagate intentionally. Callers wrap this function in
    fail-closed handling, so uncertainty disables minting and rebalancing.
    """
    circulating_solana_units = config.nexus_units_to_solana(circulating_nexus_units)
    unresolved_liability_units = state_db.get_unresolved_solana_liability_units()
    return max(
        0,
        int(vault_solana_units)
        - int(circulating_solana_units)
        - int(unresolved_liability_units),
    )



def maintain_backing_and_bounds() -> bool:
    """Maintain invariants and bounds.
    - Ensure the vault ≈ circulating supply; Solana-side fees remain in vault (no separate Solana-side fee account).
    - If vault < BACKING_DEFICIT_PAUSE_PCT% of circulating, request pause (return True).
    - Solana-side fees remain in the vault; this function does not move funds.
    Returns True if the service should pause.
    """
    try:
        from . import solana_client, nexus_client
        vault_solana = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT), max_age_sec=5)
        unresolved_liability = state_db.get_unresolved_solana_liability_units()
        available_vault_solana = max(0, int(vault_solana) - int(unresolved_liability))
        # Compare like with like: circulating supply is a Nexus-side liability, the vault a
        # Solana-side balance. Unresolved deposits are not backing assets: they remain owed
        # as a Nexus credit, refund, or quarantine transfer.
        circ_in_solana = config.nexus_units_to_solana(nexus_client.get_circulating_nexus_units())
        if circ_in_solana > 0:
            ratio_bps_deficit = int(((circ_in_solana - available_vault_solana) * 10000) / circ_in_solana) if available_vault_solana < circ_in_solana else 0
        else:
            ratio_bps_deficit = 0
        # Pause if extreme deficit
        if circ_in_solana > 0 and (available_vault_solana * 100) < (config.BACKING_DEFICIT_PAUSE_PCT * circ_in_solana):
            print("[safety] Available vault backing is below the configured floor; pausing for manual investigation")
            return True
    # With a single Solana vault account, there's no separate fee account to drain or cap.
        return False
    except Exception as e:
        print(f"[safety] maintain_backing_and_bounds error: {e}; pausing fail-closed")
        return True

_load()
