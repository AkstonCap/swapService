import time
from . import config, state
from .swap_solana import poll_solana_deposits
from .swap_nexus import poll_nexus_usdd_deposits

_last_heartbeat = 0
_last_reconcile = 0
_cached_waterlines = {"solana": 0, "nexus": 0}

def update_heartbeat_asset(force: bool = False, *, set_solana_waterline: int | None = None, set_nexus_waterline: int | None = None):
    from . import config as cfg
    import subprocess
    global _last_heartbeat
    if not cfg.HEARTBEAT_ENABLED or not cfg.NEXUS_HEARTBEAT_ASSET_ADDRESS:
        return
    now = int(time.time())
    if not force and (now - _last_heartbeat) < cfg.HEARTBEAT_MIN_INTERVAL_SEC:
        return
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
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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
        if not config.HEARTBEAT_ENABLED or not config.NEXUS_HEARTBEAT_ASSET_ADDRESS:
            return (0, 0)
        import subprocess, json
        cmd = [
            config.NEXUS_CLI,
            "register/get/assets:asset",
            f"address={config.NEXUS_HEARTBEAT_ASSET_ADDRESS}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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
    print("🌐 Starting bidirectional swap service")
    print(f"   Solana RPC: {config.RPC_URL}")
    print(f"   USDC Vault: {config.VAULT_USDC_ACCOUNT}")
    print(f"   USDD Treasury: {config.NEXUS_USDD_TREASURY_ACCOUNT}")
    print("   Monitoring:")
    print("   - USDC → USDD: Solana deposits with Nexus address in memo")
    print("   - USDD → USDC: USDD deposits with Solana address in reference")

    try:
        while True:
            # Safety and maintenance first
            try:
                from . import fees, nexus_client
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
                                _last_reconcile = now
                    except Exception as e:
                        print(f"[reconcile] error: {e}")
                # Optional: DEX conversions (SOL top-ups)
                if config.FEE_CONVERSION_ENABLED:
                    fees.process_fee_conversions()
                if should_pause:
                    time.sleep(config.POLL_INTERVAL)
                    continue
            except Exception as e:
                print(f"Maintenance error: {e}")

            poll_solana_deposits()
            poll_nexus_usdd_deposits()
            state.save_state()
            update_heartbeat_asset()
            time.sleep(config.POLL_INTERVAL)
    except KeyboardInterrupt:
        print("Shutting down…")
        state.save_state()
