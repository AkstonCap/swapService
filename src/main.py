import time
import threading
from . import config, state_db  # switched from JSON state to DB only
from .swap_solana import poll_solana_deposits
from .swap_nexus import poll_nexus_usdd_deposits, process_unprocessed_txids
from .nexus_client import get_heartbeat_asset, update_heartbeat_asset

_last_heartbeat = 0
_last_reconcile = 0
_stop_event = None  # set in run()


def _safe_call(fn, *args, timeout_sec=5, **kwargs):
    """Execute function with timeout protection."""
    result = {}
    exc = {}

    def _worker():
        try:
            result["value"] = fn(*args, **kwargs)
        except Exception as e:
            exc["error"] = e

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout_sec)
    
    if thread.is_alive():
        raise TimeoutError(f"Operation {getattr(fn,'__name__','<fn>')} timed out after {timeout_sec}s")
    if "error" in exc:
        raise exc["error"]
    return result.get("value")


def _run_with_watchdog(func, label, budget_sec):
    """Run function in thread with timeout watchdog."""
    exc_result = {}
    
    def _wrapper():
        try:
            func()
        except Exception as e:
            exc_result["error"] = e
    
    thread = threading.Thread(target=_wrapper, daemon=True)
    thread.start()
    thread.join(budget_sec)
    
    if thread.is_alive():
        print(f"[watchdog] {label} exceeded {budget_sec}s budget; skipping remainder this cycle")
        print()
    if "error" in exc_result:
        print(f"[loop] {label} error: {exc_result['error']}")
        print()


#print("↻ Updating Nexus heartbeat asset:", cmd[:-1] + ["pin=***"] if cfg.NEXUS_PIN else cmd)

def run():
    print("\n")
    print("🌐 Starting bidirectional swap service")
    print(f"   Solana RPC: {config.RPC_URL}")
    print(f"   USDC Vault: {config.VAULT_USDC_ACCOUNT}")
    print(f"   USDD Treasury: {config.NEXUS_USDD_TREASURY_ACCOUNT}")
    print("   Monitoring:")
    print("   - USDC → USDD: Solana deposits mapped via Nexus asset (distordiaSwap)")
    print("   - USDD → USDC: USDD deposits mapped to Solana recipients (internal state/idempotency)\n")

    # Startup balances summary (USDC vault + USDD circulating supply) with timeout protection
    try:
        from decimal import Decimal
        from . import solana_client, nexus_client

        def _fmt_units(units: int, decimals: int) -> str:
            try:
                q = Decimal(10) ** -decimals
                return str((Decimal(int(units)) / (Decimal(10) ** decimals)).quantize(q))
            except Exception:
                return str(units)

        usdc_units = _safe_call(solana_client.get_token_account_balance, str(config.VAULT_USDC_ACCOUNT), timeout_sec=5)
        usdc_disp = _fmt_units(usdc_units, config.USDC_DECIMALS)
        print(f"   USDC Vault Balance: {usdc_disp} USDC ({usdc_units} base) — {config.VAULT_USDC_ACCOUNT}")

        usdd_amount = _safe_call(nexus_client.get_circulating_usdd, timeout_sec=10)
        
        treas = getattr(config, 'NEXUS_USDD_TREASURY_ACCOUNT', '')
        suffix = f" — Treasury: {treas}" if treas else ""
        print(f"   USDD Circulating Supply: {usdd_amount} USDD ){suffix}")
    except Exception as e:
        print(f"   Startup metrics error: {e}")

    # Startup recovery (idempotent) – rebuild processed markers & seed reference counter if needed
    try:
        from . import startup_recovery
        rec = startup_recovery.perform_startup_recovery()
        print(f"   Startup recovery: ref_seeded={rec.get('reference_seeded')} added_nexus_processed={rec.get('added_nexus_processed')} added_refunded={rec.get('added_refunded_sigs')} (memos scanned nexus={rec.get('found_nexus_memos')} refunds={rec.get('found_refund_memos')})")
    except Exception as e:
        print(f"   Startup recovery error: {e}")

    # Balance reconciliation check (USDC→USDD direction) – detect potential double-mints
    try:
        from . import balance_reconciler
        bal_result = balance_reconciler.run_balance_reconciliation(dry_run=True)
        if bal_result.get('discrepancies'):
            print(f"   ⚠ Balance check: {len(bal_result['discrepancies'])} addresses have surplus USDD (total: {bal_result.get('total_surplus_usdd', 0)} units)")
        else:
            print(f"   ✓ Balance check: All {bal_result.get('checked_addresses', 0)} USDD addresses match expected balances")
    except Exception as e:
        print(f"   Balance reconciliation error: {e}")

    # Setup graceful shutdown via Ctrl+C (SIGINT) or SIGTERM
    import signal, threading
    global _stop_event
    _stop_event = threading.Event()

    def _request_stop(signum, frame):
        try:
            sig_name = {getattr(signal, n): n for n in dir(signal) if n.startswith('SIG')}.get(signum, str(signum))
        except Exception:
            sig_name = str(signum)
        print(f"Received {sig_name}, stopping…")
        _stop_event.set()

    for _sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, _sig):
            try:
                signal.signal(getattr(signal, _sig), _request_stop)
            except Exception:
                pass

    try:
        while not _stop_event.is_set():
            # Safety and maintenance first with timeout protection
            try:
                from . import fees, nexus_client, solana_client
                should_pause = _safe_call(fees.maintain_backing_and_bounds, timeout_sec=5)
                
                # Periodic backing reconcile: mint USDD to fees account to bring vault USDC back to 1:1 with circulating
                now = int(time.time())
                global _last_reconcile
                if (now - _last_reconcile) >= max(60, config.BACKING_RECONCILE_INTERVAL_SEC):
                    try:
                        vault_usdc = _safe_call(solana_client.get_token_account_balance, str(config.VAULT_USDC_ACCOUNT), timeout_sec=8)
                        circ_usdd = _safe_call(nexus_client.get_circulating_usdd, timeout_sec=8)
                        surplus = max(0, vault_usdc - circ_usdd)
                        # Skip reconcile if any pending Solana deposits not yet swapped
                        pending_deposits = False
                        try:
                            unproc_rows = state_db.get_unprocessed_sigs()  # DB rows
                            if any(True for _sig, _ts, _memo, _from, _amt, _status, _txid in unproc_rows if _status in (None, 'ready for processing','memo unresolved','refunded','debited, awaiting confirmations')):
                                pending_deposits = True
                        except Exception:
                            pending_deposits = True  # fail safe
                        threshold_units = getattr(config, 'BACKING_SURPLUS_MINT_THRESHOLD_USDC_UNITS', 0)
                        if (surplus >= threshold_units > 0) and not pending_deposits and getattr(config, 'NEXUS_USDD_FEES_ACCOUNT', None):
                            if _safe_call(nexus_client.debit_usdd, config.NEXUS_USDD_FEES_ACCOUNT, surplus, 'FEE_RECONCILE', timeout_sec=10):
                                print(f"[reconcile] Minted {surplus} USDD to fees account (no pending deposits; surplus >= threshold {threshold_units})")
                                print()
                                _last_reconcile = now
                    except Exception as e:
                        print(f"[reconcile] error: {e}")

                # Periodic balance reconciliation check (every 10 minutes) – detect double-mints
                if now % 600 == 0:  # Every 10 minutes
                    try:
                        from . import balance_reconciler
                        bal_result = _safe_call(balance_reconciler.run_balance_reconciliation, dry_run=True, timeout_sec=15)
                        if bal_result.get('discrepancies'):
                            print(f"[balance_check] ⚠ Found {len(bal_result['discrepancies'])} addresses with surplus USDD (total: {bal_result.get('total_surplus_usdd', 0)} units)")
                    except Exception as e:
                        print(f"[balance_check] error: {e}")
                
                # Optional: DEX conversions (SOL top-ups) with timeout protection
                if config.FEE_CONVERSION_ENABLED:
                    try:
                        _safe_call(fees.process_fee_conversions, timeout_sec=15)
                    except Exception as e:
                        print(f"[fee_conversions] error: {e}")

                # Periodic operational metrics (lightweight) every METRICS_INTERVAL_SEC with timeout budget
                METRICS_INTERVAL = getattr(config, 'METRICS_INTERVAL_SEC', 30)
                if now % max(5, METRICS_INTERVAL) == 0:  # coarse modulus trigger
                    metrics_start = time.time()
                    try:
                        vault_usdc = _safe_call(solana_client.get_token_account_balance, str(config.VAULT_USDC_ACCOUNT), timeout_sec=5)
                        circ_usdd = _safe_call(nexus_client.get_circulating_usdd, timeout_sec=5)
                        ratio = (vault_usdc / circ_usdd) if circ_usdd else 0
                        fees_state = _safe_call(fees.reconcile_accounting, timeout_sec=3)
                        
                        # Unprocessed stats from DB
                        unproc_rows = state_db.get_unprocessed_sigs()
                        ready = sum(1 for r in unproc_rows if r[5] == 'ready for processing')
                        debiting = sum(1 for r in unproc_rows if r[5] == 'debited, awaiting confirmations')
                        unresolved = sum(1 for r in unproc_rows if r[5] == 'memo unresolved')
                        refund_pending = sum(1 for r in unproc_rows if r[5] == 'refund pending')
                        quarantined = sum(1 for r in unproc_rows if r[5] == 'quarantined')
                        print(f"[metrics] vault_usdc={vault_usdc} circ_usdd={circ_usdd} ratio={ratio:.4f} unprocessed={len(unproc_rows)} ready={ready} debiting={debiting} unresolved={unresolved} refund_pending={refund_pending} quarantined={quarantined}")
                    except Exception as e:
                        print(f"[metrics] error: {e}")
                
                if should_pause:
                    if _stop_event.wait(config.POLL_INTERVAL):
                        break
                    continue
            except Exception as e:
                print(f"Maintenance error: {e}")

            # Guard long-running pollers with watchdog timeouts so Ctrl+C remains responsive
            loop_slice_start = time.time()
            
            SOLANA_BUDGET = getattr(config, "SOLANA_POLL_TIME_BUDGET_SEC", 20)
            NEXUS_POLL_BUDGET = getattr(config, "NEXUS_POLL_TIME_BUDGET_SEC", 20)
            UNPROCESSED_BUDGET = getattr(config, "UNPROCESSED_PROCESS_BUDGET_SEC", 30)
            NEXUS_PROCESS_BUDGET = getattr(config, "UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC", 30)
            
            if _stop_event.is_set():
                break
            _run_with_watchdog(poll_solana_deposits, "solana", SOLANA_BUDGET)
            
            if _stop_event.is_set():
                break
            # Process unprocessed entries independently of signature polling
            #_run_with_watchdog(process_unprocessed_entries, "unprocessed", UNPROCESSED_BUDGET)
            
            if _stop_event.is_set():
                break
            _run_with_watchdog(poll_nexus_usdd_deposits, "nexus_poll", NEXUS_POLL_BUDGET)
            
            if _stop_event.is_set():
                break
            _run_with_watchdog(process_unprocessed_txids, "nexus_process", NEXUS_PROCESS_BUDGET)
            
            if _stop_event.is_set():
                break
                
            # Save state with timeout protection
            try:
                # removed legacy state.save_state call (DB persistence is automatic)
                pass
            except Exception as e:
                print(f"[state_save] error: {e}")

            # Remove waterline proposal apply & prune (DB variant would go here if implemented)
            # ...existing code...
    except KeyboardInterrupt:
        print()
        print("Shutting down…")
    finally:
        try:
            # no final JSON state save needed
            pass
        except Exception as e:
            print(f"Final state save error: {e}")
