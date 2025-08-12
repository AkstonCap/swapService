#!/usr/bin/env python3
r"""
Helper to create a Nexus Asset for on-chain heartbeat and print its address.

What it creates (by default):
- A new `asset` with a mutable field `last_poll_timestamp` initialized to 0.
- Uses format=basic so later updates via format=basic work without fees (>=10s apart).

Optional waterlines:
- You can include per-chain waterline fields at creation time using --with-waterlines.
- Field names default to the service env defaults:
    - HEARTBEAT_WATERLINE_SOLANA_FIELD (default: last_safe_timestamp_solana)
    - HEARTBEAT_WATERLINE_NEXUS_FIELD  (default: last_safe_timestamp_usdd)
- Initial values default to 0, can be overridden via command line.

Usage (PowerShell):
    python .\create_heartbeat_asset.py --name local:swapServiceHeartbeat --with-waterlines
    # or without a name (cheaper; just address)
    python .\create_heartbeat_asset.py --with-waterlines

Requires in .env (or environment):
    NEXUS_CLI_PATH (default: ./nexus)
    NEXUS_PIN

After creation, set in .env of swapService:
    NEXUS_HEARTBEAT_ASSET_ADDRESS=<printed address>
    HEARTBEAT_ENABLED=true

Note: Creating an asset costs ~1 NXS once. Updates are free if not more often than every 10s.
"""
import os
import sys
import json
import subprocess
from argparse import ArgumentParser

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


def run_create(
    name: str | None,
    initial_last_poll: str,
    *,
    with_waterlines: bool,
    sol_field: str,
    nex_field: str,
    sol_initial: str,
    nex_initial: str,
) -> int:
    nexus_cli = os.getenv("NEXUS_CLI_PATH", "./nexus")
    pin = os.getenv("NEXUS_PIN")
    if not pin:
        print("ERROR: NEXUS_PIN is required in environment or .env")
        return 2

    cmd = [
        nexus_cli,
        "assets/create/asset",
        "format=basic",
        f"last_poll_timestamp={initial_last_poll}",
        f"pin={pin}",
    ]
    if name:
        cmd.insert(2, f"name={name}")

    if with_waterlines:
        # Insert waterline fields after format=basic (preserve order for readability)
        insert_at = 3 if name else 3  # after 'format=basic'
        cmd.insert(insert_at, f"{sol_field}={sol_initial}")
        cmd.insert(insert_at + 1, f"{nex_field}={nex_initial}")

    print("Creating heartbeat asset:", (cmd[:-1] + ["pin=***"]) if pin else cmd)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("ERROR: Nexus CLI timeout")
        return 3
    except FileNotFoundError:
        print(f"ERROR: Nexus CLI not found at {nexus_cli}")
        return 4

    if res.returncode != 0:
        print("ERROR from Nexus CLI:\n", res.stderr or res.stdout)
        return res.returncode

    out = res.stdout.strip()
    print("Raw output:\n", out)

    # Try to extract address from JSON
    address = None
    try:
        data = json.loads(out)
        # results may be dict or top-level
        if isinstance(data, dict):
            results = data.get("results") or data
            address = results.get("address") if isinstance(results, dict) else None
    except Exception:
        pass

    if address:
        print("\nSuccess! Heartbeat asset address:", address)
        print("Set in .env: NEXUS_HEARTBEAT_ASSET_ADDRESS=", address, sep="")
        return 0
    else:
        print("\nNOTE: Could not parse address from output automatically. Please copy it from the raw output above.")
        return 0


def main() -> int:
    ap = ArgumentParser(description="Create Nexus heartbeat asset for swapService")
    ap.add_argument("--name", help="Optional asset name (e.g., local:swapServiceHeartbeat)")
    ap.add_argument("--initial", default="0", help="Initial last_poll_timestamp value (default: 0)")
    ap.add_argument(
        "--with-waterlines",
        action="store_true",
        help="Also create waterline fields at 0 (or provided initial values)",
    )
    # Field names default to service conventions, but can be overridden
    env_sol_field = os.getenv("HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
    env_nex_field = os.getenv("HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_usdd")
    ap.add_argument(
        "--solana-waterline-field",
        default=env_sol_field,
        help=f"Solana waterline field name (default from env or '{env_sol_field}')",
    )
    ap.add_argument(
        "--nexus-waterline-field",
        default=env_nex_field,
        help=f"Nexus waterline field name (default from env or '{env_nex_field}')",
    )
    ap.add_argument(
        "--solana-initial",
        default="0",
        help="Initial Solana waterline value (default: 0)",
    )
    ap.add_argument(
        "--nexus-initial",
        default="0",
        help="Initial Nexus waterline value (default: 0)",
    )
    args = ap.parse_args()

    return run_create(
        args.name,
        args.initial,
        with_waterlines=args.with_waterlines,
        sol_field=args.solana_waterline_field,
        nex_field=args.nexus_waterline_field,
        sol_initial=args.solana_initial,
        nex_initial=args.nexus_initial,
    )


if __name__ == "__main__":
    sys.exit(main())
