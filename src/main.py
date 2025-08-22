import time
from . import config, state
from .swap_solana import poll_solana_deposits
from .swap_nexus import poll_nexus_usdd_deposits

_last_heartbeat = 0
_last_reconcile = 0
_cached_waterlines = {"solana": 0, "nexus": 0}
_stop_event = None  # set in run()

def update_heartbeat_asset(force: bool = False, *, set_solana_waterline: int | None = None, set_nexus_waterline: int | None = None):
    from . import config as cfg
    import subprocess
    global _last_heartbeat
    # Require heartbeat enabled AND at least one of (asset name, asset address)
    if not cfg.HEARTBEAT_ENABLED or not (cfg.NEXUS_HEARTBEAT_ASSET_NAME or cfg.NEXUS_HEARTBEAT_ASSET_ADDRESS):
        return
    now = int(time.time())
    if not force and (now - _last_heartbeat) < cfg.HEARTBEAT_MIN_INTERVAL_SEC:
        return
    if cfg.NEXUS_HEARTBEAT_ASSET_NAME:
        fields = [
            cfg.NEXUS_CLI,
            "assets/update/asset",
            "format=basic",
            f"name={cfg.NEXUS_HEARTBEAT_ASSET_NAME}",
            f"last_poll_timestamp={now}",
        ]
    else:
        fields = [
            cfg.NEXUS_CLI,
            "assets/update/asset",
            f"address={cfg.NEXUS_HEARTBEAT_ASSET_ADDRESS}",
            "format=basic",
            f"last_poll_timestamp={now}",
        ]
    if cfg.HEARTBEAT_WATERLINE_ENABLED:
        if set_solana_waterline is not None:
            fields.append(f"{cfg.HEARTBEAT_WATERLINE_SOLANA_FIELD}={int(set_solana_waterline)}")
        if set_nexus_waterline is not None:
            fields.append(f"{cfg.HEARTBEAT_WATERLINE_NEXUS_FIELD}={int(set_nexus_waterline)}")
    cmd = fields
    if cfg.NEXUS_PIN:
        cmd.append(f"pin={cfg.NEXUS_PIN}")
    try:
        print("↻ Updating Nexus heartbeat asset:", cmd[:-1] + ["pin=***"] if cfg.NEXUS_PIN else cmd)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            print("Heartbeat update failed:", res.stderr.strip() or res.stdout.strip())
        else:
            _last_heartbeat = now
            out = (res.stdout or "").strip()
            if out:
                print("Heartbeat updated:", out)
    except Exception as e:
        print(f"Heartbeat update error: {e}")


def read_heartbeat_waterlines() -> tuple[int, int]:
    """Fetch waterline timestamps (solana, nexus) from heartbeat asset, cache locally.
    Returns tuple (solana_waterline, nexus_waterline).
    """
    try:
        if not config.HEARTBEAT_ENABLED or not config.NEXUS_HEARTBEAT_ASSET_NAME:
            return (0, 0)
        import subprocess, json
        cmd = [
            config.NEXUS_CLI,
            "register/get/assets:asset",
            f"name={config.NEXUS_HEARTBEAT_ASSET_NAME}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
        if res.returncode != 0:
            return (_cached_waterlines["solana"], _cached_waterlines["nexus"])  # fallback
        data = json.loads(res.stdout or "{}")
        results = data.get("results") or data
        sol = int(results.get(config.HEARTBEAT_WATERLINE_SOLANA_FIELD, 0) or 0)
        nex = int(results.get(config.HEARTBEAT_WATERLINE_NEXUS_FIELD, 0) or 0)
        _cached_waterlines["solana"], _cached_waterlines["nexus"] = sol, nex
        return (sol, nex)
    except Exception:
        return (_cached_waterlines["solana"], _cached_waterlines["nexus"])  # fallback


def run():
    print()
    print("🌐 Starting bidirectional swap service")
    print(f"   Solana RPC: {config.RPC_URL}")
    print(f"   USDC Vault: {config.VAULT_USDC_ACCOUNT}")
    print(f"   USDD Treasury: {config.NEXUS_USDD_TREASURY_ACCOUNT}")
    print("   Monitoring:")
    print("   - USDC → USDD: Solana deposits mapped via Nexus asset (distordiaSwap)")
    print("   - USDD → USDC: USDD deposits mapped to Solana recipients (internal state/idempotency)")
    print()

    # Startup balances summary (USDC vault + USDD circulating supply)
    try:
        from decimal import Decimal
        from . import solana_client, nexus_client

        def _fmt_units(units: int, decimals: int) -> str:
            try:
                q = Decimal(10) ** -decimals
                return str((Decimal(int(units)) / (Decimal(10) ** decimals)).quantize(q))
            except Exception:
                return str(units)

        usdc_units = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT))
        usdc_disp = _fmt_units(usdc_units, config.USDC_DECIMALS)
        print(f"   USDC Vault Balance: {usdc_disp} USDC ({usdc_units} base) — {config.VAULT_USDC_ACCOUNT}")

        usdd_units = nexus_client.get_circulating_usdd_units()
        usdd_disp = _fmt_units(usdd_units, config.USDD_DECIMALS)
        treas = getattr(config, 'NEXUS_USDD_TREASURY_ACCOUNT', '')
        suffix = f" — Treasury: {treas}" if treas else ""
        print(f"   USDD Circulating Supply: {usdd_disp} USDD ({usdd_units} base){suffix}")
    except Exception as e:
        print(f"   Startup metrics error: {e}")

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
            # Safety and maintenance first
            try:
                from . import fees, nexus_client, solana_client
                should_pause = fees.maintain_backing_and_bounds()
                # Periodic backing reconcile: mint USDD to fees account to bring vault USDC back to 1:1 with circulating
                now = int(time.time())
                global _last_reconcile
                if (now - _last_reconcile) >= max(60, config.BACKING_RECONCILE_INTERVAL_SEC):
                    try:
                        # Compute surplus: vault_usdc - circ_usdd
                        from . import solana_client
                        vault_usdc = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT))
                        circ_usdd = nexus_client.get_circulating_usdd_units()
                        surplus = max(0, vault_usdc - circ_usdd)
                        if surplus > 0 and getattr(config, 'NEXUS_USDD_FEES_ACCOUNT', None):
                            if nexus_client.debit_usdd(config.NEXUS_USDD_FEES_ACCOUNT, surplus, "FEE_RECONCILE"):
                                print(f"[reconcile] Minted {surplus} USDD to fees account to restore 1:1 backing")
                                print()
                                _last_reconcile = now
                    except Exception as e:
                        print(f"[reconcile] error: {e}")
                # Optional: DEX conversions (SOL top-ups)
                if config.FEE_CONVERSION_ENABLED:
                    fees.process_fee_conversions()

                # Periodic operational metrics (lightweight) every METRICS_INTERVAL_SEC
                METRICS_INTERVAL = getattr(config, 'METRICS_INTERVAL_SEC', 30)
                if now % max(5, METRICS_INTERVAL) == 0:  # coarse modulus trigger
                    try:
                        vault_usdc = solana_client.get_token_account_balance(str(config.VAULT_USDC_ACCOUNT))
                        circ_usdd = nexus_client.get_circulating_usdd_units()
                        ratio = (vault_usdc / circ_usdd) if circ_usdd else 0
                        fees_state = fees.reconcile_accounting()
                        # Unprocessed stats
                        unproc = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
                        ready = sum(1 for r in unproc if r.get('comment') == 'ready for processing')
                        debiting = sum(1 for r in unproc if r.get('comment') == 'debited, awaiting confirmations')
                        unresolved = sum(1 for r in unproc if r.get('comment') == 'memo unresolved')
                        refund_pending = sum(1 for r in unproc if r.get('comment') == 'refund pending')
                        quarantined = sum(1 for r in unproc if r.get('comment') == 'quarantined')
                        print(f"[metrics] vault_usdc={vault_usdc} circ_usdd={circ_usdd} ratio={ratio:.4f} fees_stored={fees_state['stored']} fees_journal={fees_state['journal_sum']} delta={fees_state['delta']} unprocessed={len(unproc)} ready={ready} debiting={debiting} unresolved={unresolved} refund_pending={refund_pending} quarantined={quarantined}")
                    except Exception as e:
                        print(f"[metrics] error: {e}")
                if should_pause:
                    if _stop_event.wait(config.POLL_INTERVAL):
                        break
                    continue
            except Exception as e:
                print(f"Maintenance error: {e}")

            # Guard long-running pollers with soft timeouts so Ctrl+C remains responsive
            loop_slice_start = time.time()
            try:
                poll_solana_deposits()
            except Exception as e:
                print(f"[loop] solana poll error: {e}")
            if _stop_event.is_set():
                break
            try:
                poll_nexus_usdd_deposits()
            except Exception as e:
                print(f"[loop] nexus poll error: {e}")
            if _stop_event.is_set():
                break
            # If this iteration exceeded 80% of poll interval, skip extra waiting granularity
            elapsed = time.time() - loop_slice_start
            state.save_state()
            # Apply any conservative waterline proposals, if present
            try:
                sol_ts, nex_ts = state.get_and_clear_proposed_waterlines()
            except Exception:
                sol_ts, nex_ts = (None, None)
            update_heartbeat_asset(set_solana_waterline=sol_ts, set_nexus_waterline=nex_ts)
            # After committing heartbeat, prune processed markers below waterlines for hygiene
            try:
                state.prune_processed(sol_ts, nex_ts)
                state.save_state()
            except Exception as e:
                print(f"Prune error: {e}")
            remaining = max(0, config.POLL_INTERVAL - elapsed)
            # Sleep in short chunks to react quickly to Ctrl+C
            sleep_chunk = min(1.0, remaining)
            slept = 0.0
            while slept < remaining and not _stop_event.is_set():
                _stop_event.wait(sleep_chunk)
                slept += sleep_chunk
                if remaining - slept < sleep_chunk:
                    sleep_chunk = remaining - slept
                # break early if stop requested
                if _stop_event.is_set():
                    break
            if _stop_event.is_set():
                break
    except KeyboardInterrupt:
        print()
        print("Shutting down…")
    finally:
        state.save_state()
