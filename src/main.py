import time
from . import config, state
from .swap_solana import poll_solana_deposits
from .swap_nexus import poll_nexus_usdd_deposits

_last_heartbeat = 0

def update_heartbeat_asset(force: bool = False):
    from . import config as cfg
    import subprocess
    global _last_heartbeat
    if not cfg.HEARTBEAT_ENABLED or not cfg.NEXUS_HEARTBEAT_ASSET_ADDRESS:
        return
    now = int(time.time())
    if not force and (now - _last_heartbeat) < cfg.HEARTBEAT_MIN_INTERVAL_SEC:
        return
    cmd = [
        cfg.NEXUS_CLI,
        "assets/update/asset",
        f"address={cfg.NEXUS_HEARTBEAT_ASSET_ADDRESS}",
        "format=basic",
        f"last_poll_timestamp={now}",
    ]
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


def run():
    print("🌐 Starting bidirectional swap service")
    print(f"   Solana RPC: {config.RPC_URL}")
    print(f"   USDC Vault: {config.VAULT_USDC_ACCOUNT}")
    print(f"   USDD Account: {config.NEXUS_USDD_ACCOUNT}")
    print("   Monitoring:")
    print("   - USDC → USDD: Solana deposits with Nexus address in memo")
    print("   - USDD → USDC: USDD deposits with Solana address in reference")

    try:
        while True:
            poll_solana_deposits()
            poll_nexus_usdd_deposits()
            state.save_state()
            # Optional: process fee conversions (stubbed until implemented)
            try:
                if config.FEE_CONVERSION_ENABLED:
                    from . import fees
                    fees.process_fee_conversions()
            except Exception as e:
                print(f"Fee conversion processing error: {e}")
            update_heartbeat_asset()
            time.sleep(config.POLL_INTERVAL)
    except KeyboardInterrupt:
        print("Shutting down…")
        state.save_state()
