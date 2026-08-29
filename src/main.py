import os
import time
import threading
from . import config, state_db, alerts  # switched from JSON state to DB only
from .swap_solana import poll_solana_deposits
from .swap_nexus import poll_nexus_deposits, process_unprocessed_txids
from .nexus_client import get_heartbeat_asset, update_heartbeat_asset

_last_heartbeat = 0
_last_reconcile = 0
_stop_event = None  # set in run()
RECONCILIATION_INTERVAL_SEC = 600


def report_startup_balance_reconciliation(bal_result: object) -> bool:
    """Report startup reconciliation without treating incomplete evidence as green."""
    if not isinstance(bal_result, dict):
        alerts.critical(
            "balance_reconciliation_incomplete",
            "double-mint reconciliation returned no result; no green result is valid",
            checked_addresses=0,
            incomplete_reasons=["balance reconciliation returned no result"],
            account_errors=[],
        )
        return False

    healthy = bal_result.get("healthy") is True
    if not healthy:
        alerts.critical(
            "balance_reconciliation_incomplete",
            "double-mint reconciliation is not healthy; no green result is valid",
            checked_addresses=bal_result.get("checked_addresses", 0),
            incomplete_reasons=bal_result.get("incomplete_reasons", []),
            account_errors=bal_result.get("account_errors", []),
        )
    if bal_result.get("discrepancies"):
        alerts.critical(
            "unbacked_nexus_surplus",
            "addresses hold more of the Nexus-side token than their deposits justify "
            "(possible double-mint)",
            addresses=len(bal_result["discrepancies"]),
            total_surplus_units=bal_result.get("total_surplus_nexus_units", 0),
        )
    elif healthy:
        print(
            f"   ✓ Balance check: All {bal_result.get('checked_addresses', 0)} "
            "Nexus token addresses match expected balances"
        )
    return healthy


def update_reconciliation_exposure_pause(
    paused: bool, bal_result: object = None, *, error: Exception | None = None
) -> bool:
    """Latch new cross-chain exposure on reconciliation uncertainty.

    The Nexus mint reconciler is a safety detector, not merely an alert source.  A failed,
    malformed, incomplete, discrepant, or exception-producing read-back permits no new
    Solana↔Nexus exposure.  The only transition out of this pause is a later result whose
    ``healthy`` field is explicitly ``True``.
    """
    if error is not None:
        alerts.critical(
            "balance_reconciliation_incomplete",
            "double-mint reconciliation failed; new exposure remains paused until an explicitly healthy result",
            checked_addresses=0,
            incomplete_reasons=[f"balance reconciliation failed: {error}"],
            account_errors=[],
        )
        return True

    # Calling the reporter retains the existing alert/discrepancy evidence.  It returns
    # False for invalid results as well as every incomplete or surplus-bearing result.
    # ``paused`` intentionally remains latched between reconciliation attempts; only a
    # newly observed explicit healthy result clears it.
    del paused
    return not report_startup_balance_reconciliation(bal_result)


def is_balance_reconciliation_due(now: int, last_attempt: int) -> bool:
    """Schedule reconciliation by elapsed time, not an exact wall-clock second."""
    return int(now) - int(last_attempt) >= RECONCILIATION_INTERVAL_SEC


def validate_production_controls() -> bool:
    """Refuse an explicitly production-mode process without basic loss controls."""
    if not getattr(config, "PRODUCTION_MODE", False):
        return True

    missing = []
    if int(getattr(config, "MAX_SWAP_SOLANA_UNITS", 0) or 0) <= 0:
        missing.append("MAX_SWAP_USDC")
    if int(getattr(config, "MAX_SWAP_NEXUS_UNITS", 0) or 0) <= 0:
        missing.append("MAX_SWAP_USDD")
    if int(getattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 0) or 0) <= 0:
        missing.append("DAILY_PAYOUT_CAP_USDC")
    if not (str(getattr(config, "ALERT_WEBHOOK_URL", "") or "").strip()
            or str(getattr(config, "ALERT_COMMAND", "") or "").strip()):
        missing.append("ALERT_WEBHOOK_URL or ALERT_COMMAND")

    if not missing:
        return True

    message = "refusing production startup because mandatory exposure controls are disabled"
    print(f"[startup] {message}: {', '.join(missing)}")
    alerts.critical("production_controls_missing", message, missing_controls=missing)
    return False


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


_lock_handle = None


def acquire_singleton_lock() -> bool:
    """Take an exclusive lock so two instances can never share the state DB.

    Without this, a double start (systemd restart that fails to reap, or a manual run
    alongside the service) puts two processes on one SQLite file with no cross-process
    mutual exclusion - the same debit can be executed twice. The handle is kept for the
    process lifetime; the OS releases it on exit.
    """
    global _lock_handle
    lock_path = os.getenv("SWAP_LOCK_PATH") or (str(getattr(state_db, "DB_PATH", "swap_service.db")) + ".lock")
    try:
        import fcntl
    except ImportError:
        print(f"[lock] fcntl unavailable on this platform; SINGLE-INSTANCE IS NOT ENFORCED. "
              f"Ensure by other means that only one instance runs.")
        return True
    try:
        handle = open(lock_path, "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
        _lock_handle = handle  # keep open: closing would release the lock
        print(f"[lock] acquired {lock_path} (pid {os.getpid()})")
        return True
    except BlockingIOError:
        print(f"[lock] another swapService instance already holds {lock_path}; refusing to start. "
              f"Two instances sharing the state DB can double-spend.")
        return False
    except Exception as e:
        print(f"[lock] could not acquire {lock_path}: {e}; refusing to start.")
        return False


# Threads started by _run_with_watchdog, keyed by label, so a cycle never starts a
# second copy of a poller that is still running.
_running_pollers: dict = {}


def _run_with_watchdog(func, label, budget_sec):
    """Run `func` in a thread, bounded by `budget_sec`.

    A thread cannot be forcibly cancelled, so exceeding the budget does NOT stop the
    work - it keeps running. Previously the loop then started another copy each cycle,
    letting two pollers race the same rows and debit twice. Now an over-budget poller
    simply blocks its own next run until it finishes.
    """
    prev = _running_pollers.get(label)
    if prev is not None and prev.is_alive():
        print(f"[watchdog] {label} from a previous cycle is still running; not starting another")
        return

    exc_result = {}

    def _wrapper():
        try:
            func()
        except Exception as e:
            exc_result["error"] = e

    thread = threading.Thread(target=_wrapper, daemon=True)
    _running_pollers[label] = thread
    thread.start()
    thread.join(budget_sec)

    if thread.is_alive():
        print(f"[watchdog] {label} exceeded {budget_sec}s budget; still running in background "
              f"(its next cycle will be skipped until it finishes)")


def _process_stale_deposits():
    """Process stale deposits that exceed STALE_DEPOSIT_QUARANTINE_SEC.
    
    FIX: This config variable was defined but never used. Now deposits
    stuck in unprocessable states for too long are moved to quarantine.
    """
    try:
        stale_threshold_sec = int(getattr(config, 'STALE_DEPOSIT_QUARANTINE_SEC', 86400))  # 24h default
        now = int(time.time())
        
        # Get all unprocessed signatures
        unproc_rows = state_db.get_unprocessed_sigs()
        
        stale_statuses = {'memo unresolved', 'ready for processing', None}
        quarantined_count = 0
        
        for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in unproc_rows:
            if not timestamp:
                continue
                
            age_sec = now - int(timestamp)
            
            # Check if this deposit is stale and in a non-terminal state
            if age_sec > stale_threshold_sec and status in stale_statuses:
                # Mark for quarantine - the quarantine handler will process it
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                quarantined_count += 1
                print(f"[stale_deposit] sig={sig} age={age_sec}s > {stale_threshold_sec}s status={status} - marking for quarantine")
        
        if quarantined_count > 0:
            print(f"[stale_deposit] Marked {quarantined_count} stale deposits for quarantine")
            
    except Exception as e:
        print(f"[stale_deposit] error: {e}")


#print("↻ Updating Nexus heartbeat asset:", cmd[:-1] + ["pin=***"] if cfg.NEXUS_PIN else cmd)

def run():
    # An explicit production deployment must have finite blast-radius controls and an
    # alert route before it opens mutable state or starts polling either chain.
    if not validate_production_controls():
        return False

    # Ensure the SQLite schema exists before any state access (idempotent).
    state_db.init_db()

    # Refuse to run a second instance against the same state DB.
    if not acquire_singleton_lock():
        return

    print("\n")
    print("🌐 Starting bidirectional swap service")
    print(f"   Solana RPC: {config.RPC_URL}")
    print(f"   {config.SOLANA_TOKEN_SYMBOL} vault (Solana): {config.VAULT_USDC_ACCOUNT}")
    print(f"   {config.NEXUS_TOKEN_NAME} treasury (Nexus): {config.NEXUS_USDD_TREASURY_ACCOUNT}")
    print(f"   Bridging: {config.SOLANA_TOKEN_SYMBOL} (Solana, {config.SOLANA_TOKEN_DECIMALS}dp) "
          f"<-> {config.NEXUS_TOKEN_NAME} (Nexus, {config.NEXUS_TOKEN_DECIMALS}dp)")
    print(f"   Deposit memo format: {config.DEPOSIT_MEMO_PREFIX}<your {config.NEXUS_TOKEN_NAME} account>")
    print("   Monitoring:")
    print(f"   - {config.SOLANA_TOKEN_SYMBOL} → {config.NEXUS_TOKEN_NAME}: Solana deposits with memo "
          f"{config.DEPOSIT_MEMO_PREFIX}<{config.NEXUS_TOKEN_NAME}_ACCOUNT>")
    print(f"   - {config.NEXUS_TOKEN_NAME} → {config.SOLANA_TOKEN_SYMBOL}: credits mapped via asset "
          f"(txid_toService + receival_account)\n")

    # A minimum at or below its flat fee means the user nets ~nothing while the swap is
    # still recorded as successful. config raises such minimums to 2x the fee; say so.
    if getattr(config, "MIN_DEPOSIT_SOLANA_RAISED", False):
        print(f"   ⚠ MIN_DEPOSIT_USDC was below 2x the flat fee and has been raised to "
              f"{config.MIN_DEPOSIT_SOLANA_UNITS} base units. Update your docs/.env to match.")
    if getattr(config, "MIN_CREDIT_NEXUS_RAISED", False):
        print(f"   ⚠ MIN_CREDIT_USDD was below 2x the flat fee and has been raised to "
              f"{config.MIN_CREDIT_NEXUS_UNITS} base units. Update your docs/.env to match.")
    if not (getattr(config, "ALERT_WEBHOOK_URL", None) or getattr(config, "ALERT_COMMAND", None)):
        print("   ⚠ No ALERT_WEBHOOK_URL/ALERT_COMMAND configured — backing pauses, unbacked-mint "
              "discrepancies and halted pollers will only appear on stdout.")

    # Nexus session/multiuser configuration. Wrong here means every money operation fails.
    try:
        from . import nexus_client as _nc0
        sess_ok, sess_msg = _nc0.validate_session_config()
        print(f"   {'✓' if sess_ok else '⚠'} Nexus session: {sess_msg}")
        if not sess_ok:
            alerts.critical("nexus_session_misconfigured", sess_msg)
    except Exception as e:
        print(f"   ⚠ Nexus session validation error: {e}")

    # Fail loudly on a heartbeat asset that cannot accept the fields we write: every
    # update would fail atomically, silently freezing the heartbeat and both waterlines.
    try:
        from . import nexus_client as _nc
        hb_ok, hb_msg = _safe_call(_nc.validate_heartbeat_asset, timeout_sec=10)
        print(f"   {'✓' if hb_ok else '⚠'} Heartbeat: {hb_msg}")
        if not hb_ok:
            alerts.critical("heartbeat_asset_invalid", hb_msg)
    except Exception as e:
        print(f"   ⚠ Heartbeat validation error: {e}")

    # Startup balances summary (Solana vault + Nexus circulating supply) with timeout protection
    try:
        from decimal import Decimal
        from . import solana_client, nexus_client

        def _fmt_units(units: int, decimals: int) -> str:
            try:
                q = Decimal(10) ** -decimals
                return str((Decimal(int(units)) / (Decimal(10) ** decimals)).quantize(q))
            except Exception:
                return str(units)

        solana_units = _safe_call(solana_client.get_token_account_balance, str(config.VAULT_USDC_ACCOUNT), timeout_sec=5)
        solana_disp = _fmt_units(solana_units, config.USDC_DECIMALS)
        print(f"   Vault Balance: {solana_disp} {config.SOLANA_TOKEN_SYMBOL} "
              f"({solana_units} base) — {config.VAULT_USDC_ACCOUNT}")

        nexus_amount = _safe_call(nexus_client.get_circulating_nexus_supply, timeout_sec=10)
        
        treas = getattr(config, 'NEXUS_USDD_TREASURY_ACCOUNT', '')
        suffix = f" — Treasury: {treas}" if treas else ""
        print(f"   Circulating Supply: {nexus_amount} {config.NEXUS_TOKEN_NAME}{suffix}")
    except Exception as e:
        print(f"   Startup metrics error: {e}")

    # Startup recovery (idempotent) – rebuild processed markers & seed reference counter if needed
    try:
        from . import startup_recovery
        rec = startup_recovery.perform_startup_recovery()
        print(f"   Startup recovery: ref_seeded={rec.get('reference_seeded')} interrupted_nexus_transfers_held={rec.get('interrupted_nexus_transfers_held', 0)} added_nexus_processed={rec.get('added_nexus_processed')} added_refunded={rec.get('added_refunded_sigs')} (memos scanned nexus={rec.get('found_nexus_memos')} refunds={rec.get('found_refund_memos')})")
    except Exception as e:
        print(f"   Startup recovery error: {e}")

    # Balance reconciliation check (Solana→Nexus direction) – detect potential double-mints.
    # Start latched: an unavailable startup read-back is never permission to create exposure.
    reconciliation_pause = True
    try:
        from . import balance_reconciler
        bal_result = balance_reconciler.run_balance_reconciliation(dry_run=True)
        reconciliation_pause = update_reconciliation_exposure_pause(
            reconciliation_pause, bal_result
        )
    except Exception as e:
        print(f"   Balance reconciliation error: {e}")
        reconciliation_pause = update_reconciliation_exposure_pause(
            reconciliation_pause, error=e
        )
    last_balance_reconciliation_attempt = int(time.time())

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
            # Fail safe: if the backing check itself errors we treat the cycle as paused
            # (no new exposure) rather than assuming everything is fine.
            should_pause = True
            # Safety and maintenance first with timeout protection
            try:
                from . import fees, nexus_client, solana_client
                should_pause = _safe_call(fees.maintain_backing_and_bounds, timeout_sec=5)
                
                # Periodic backing reconcile: mint the Nexus-side token to fees account to bring the vault back to 1:1 with circulating
                now = int(time.time())
                global _last_reconcile
                if (now - _last_reconcile) >= max(60, config.BACKING_RECONCILE_INTERVAL_SEC):
                    try:
                        vault_solana = _safe_call(solana_client.get_token_account_balance, str(config.VAULT_USDC_ACCOUNT), max_age_sec=5, timeout_sec=8)
                        circ_nexus = _safe_call(nexus_client.get_circulating_nexus_units, timeout_sec=8)
                        # Surplus in SOLANA base units. The circulating figure is a Nexus-side
                        # liability on a possibly different scale, so it is converted (rounded
                        # up) before subtracting - otherwise a pair with mismatched decimals
                        # reports surplus that does not exist and mints against it.
                        # User deposits in every non-terminal state are liabilities,
                        # including refund and quarantine states. They are not fee surplus
                        # merely because no Nexus supply has been minted against them yet.
                        if vault_solana is None or circ_nexus is None:
                            raise RuntimeError("backing inputs unavailable")
                        surplus = fees.available_backing_surplus_solana_units(
                            vault_solana, circ_nexus
                        )
                        threshold_units = getattr(config, 'BACKING_SURPLUS_MINT_THRESHOLD_SOLANA_UNITS', 0)
                        if (surplus >= threshold_units > 0) and getattr(config, 'NEXUS_USDD_FEES_ACCOUNT', None):
                            # Do not automate this debit until it has the same durable
                            # intent + chain-resolution protocol as user mints. A timeout
                            # here is ambiguous and a blind retry can mint fees twice.
                            alerts.warning(
                                "backing_surplus_manual_action_required",
                                "automatic surplus mint is safety-disabled",
                                surplus_solana_units=surplus,
                                threshold_solana_units=threshold_units,
                            )
                            _last_reconcile = now
                    except Exception as e:
                        print(f"[reconcile] error: {e}")

                # Periodic reconciliation must be scheduled by elapsed time: a loop that
                # misses an exact wall-clock second must still be able to clear a pause.
                if is_balance_reconciliation_due(now, last_balance_reconciliation_attempt):
                    last_balance_reconciliation_attempt = now
                    try:
                        from . import balance_reconciler
                        bal_result = _safe_call(balance_reconciler.run_balance_reconciliation, dry_run=True, timeout_sec=15)
                        reconciliation_pause = update_reconciliation_exposure_pause(
                            reconciliation_pause, bal_result
                        )
                    except Exception as e:
                        print(f"[balance_check] error: {e}")
                        reconciliation_pause = update_reconciliation_exposure_pause(
                            reconciliation_pause, error=e
                        )

                # Reconciliation is an exposure gate, not only a monitoring signal.  Keep
                # processing existing refunds/quarantines in paused poller mode, but do not
                # accept or pay out new Solana↔Nexus swaps until a later explicit green run.
                should_pause = bool(should_pause or reconciliation_pause)
                
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
                        vault_solana = _safe_call(solana_client.get_token_account_balance, str(config.VAULT_USDC_ACCOUNT), max_age_sec=5, timeout_sec=5)
                        circ_nexus = _safe_call(nexus_client.get_circulating_nexus_units, timeout_sec=5)
                        # Backing ratio compares the two sides, so both must be on one scale.
                        circ_in_solana = config.nexus_units_to_solana(circ_nexus)
                        ratio = (vault_solana / circ_in_solana) if circ_in_solana else 0
                        fees_state = _safe_call(fees.reconcile_accounting, timeout_sec=3)
                        
                        # Unprocessed stats from DB
                        unproc_rows = state_db.get_unprocessed_sigs()
                        ready = sum(1 for r in unproc_rows if r[5] == 'ready for processing')
                        debiting = sum(1 for r in unproc_rows if r[5] == 'debited, awaiting confirmations')
                        unresolved = sum(1 for r in unproc_rows if r[5] == 'memo unresolved')
                        refund_pending = sum(1 for r in unproc_rows if r[5] == 'refund pending')
                        quarantined = sum(1 for r in unproc_rows if r[5] == 'quarantined')
                        print(f"[metrics] vault_solana={vault_solana} circ_nexus={circ_nexus} ratio={ratio:.4f} unprocessed={len(unproc_rows)} ready={ready} debiting={debiting} unresolved={unresolved} refund_pending={refund_pending} quarantined={quarantined}")

                        # Persist for the operator dashboard (which never touches the chain).
                        try:
                            f_solana, f_nexus = state_db.get_total_fees_collected()
                            state_db.save_metrics_snapshot(
                                vault_usdc_units=vault_solana,
                                circulating_usdd_units=circ_nexus,
                                paused=bool(should_pause),
                                payouts_24h_units=state_db.payouts_since(86400),
                                fees_usdc_units=f_solana,
                                fees_usdd_units=f_nexus,
                                # state_db deliberately does not import config, so it cannot
                                # convert between the two sides' decimals itself. Pass the
                                # ratio computed on a single scale rather than letting it
                                # divide two differently-scaled columns.
                                ratio_bps=int((vault_solana * 10000) // circ_in_solana) if circ_in_solana > 0 else None,
                            )
                        except Exception as e:
                            print(f"[metrics] snapshot save error: {e}")
                    except Exception as e:
                        print(f"[metrics] error: {e}")
                
                if should_pause:
                    # Do NOT skip the cycle. Pausing used to `continue`, freezing refunds
                    # and quarantine of already-stuck user funds along with new swaps.
                    # Instead run the pollers in paused mode: no new exposure, but money
                    # already owed to users keeps moving.
                    if reconciliation_pause:
                        alerts.critical(
                            "balance_reconciliation_exposure_pause",
                            "Nexus mint reconciliation is incomplete, discrepant, or unavailable; "
                            "new swaps paused until an explicitly healthy read-back",
                        )
                    else:
                        alerts.critical("backing_deficit_pause",
                                        "the vault below the configured floor vs circulating supply; "
                                        "new swaps paused, refunds and quarantine still running")
            except Exception as e:
                print(f"Maintenance error: {e}")

            # Guard long-running pollers with watchdog timeouts so Ctrl+C remains responsive
            loop_slice_start = time.time()
            
            SOLANA_BUDGET = getattr(config, "SOLANA_POLL_TIME_BUDGET_SEC", 20)
            NEXUS_POLL_BUDGET = getattr(config, "NEXUS_POLL_TIME_BUDGET_SEC", 20)
            NEXUS_PROCESS_BUDGET = getattr(config, "UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC", 30)
            
            if _stop_event.is_set():
                break
            _run_with_watchdog(lambda: poll_solana_deposits(paused=bool(should_pause)), "solana", SOLANA_BUDGET)
            
            if _stop_event.is_set():
                break
            # Process stale deposits that need quarantine (FIX: STALE_DEPOSIT_QUARANTINE_SEC now used)
            _run_with_watchdog(_process_stale_deposits, "stale_deposits", 5)
            
            if _stop_event.is_set():
                break
            _run_with_watchdog(poll_nexus_deposits, "nexus_poll", NEXUS_POLL_BUDGET)
            
            if _stop_event.is_set():
                break
            _run_with_watchdog(lambda: process_unprocessed_txids(paused=bool(should_pause)), "nexus_process", NEXUS_PROCESS_BUDGET)
            
            if _stop_event.is_set():
                break
                
            # Save state with timeout protection
            try:
                # removed legacy state.save_state call (DB persistence is automatic)
                pass
            except Exception as e:
                print(f"[state_save] error: {e}")

            # Apply waterline proposals to heartbeat asset (FIX: was commented out)
            try:
                sol_wl, nxs_wl = state_db.get_and_clear_proposed_waterlines()
                if sol_wl or nxs_wl:
                    current_ts = int(time.time())
                    nexus_client.update_heartbeat_asset(current_ts, nxs_wl, sol_wl)
                    print(f"[waterline] Applied proposals: solana={sol_wl} nexus={nxs_wl}")
            except Exception as e:
                print(f"[waterline] apply error: {e}")
            
            # Cleanup expired reservations periodically
            try:
                deleted = state_db.cleanup_expired_reservations(ttl_sec=300)
                if deleted > 0:
                    print(f"[cleanup] Expired {deleted} reservations")
            except Exception:
                pass
    except KeyboardInterrupt:
        print()
        print("Shutting down…")
    finally:
        try:
            # no final JSON state save needed
            pass
        except Exception as e:
            print(f"Final state save error: {e}")
