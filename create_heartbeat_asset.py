#!/usr/bin/env python3
"""
Helper to create a Nexus Asset for on-chain heartbeat and print its address.
- Creates an `asset` with a mutable field `last_poll_timestamp` (string) initialized to 0.
- Uses format=basic so later updates via format=basic work without fees (>=10s apart).

Usage (PowerShell):
  python .\create_heartbeat_asset.py --name local:swapServiceHeartbeat
  # or without a name (cheaper; just address)
  python .\create_heartbeat_asset.py

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


def run_create(name: str | None, initial: str) -> int:
    nexus_cli = os.getenv("NEXUS_CLI_PATH", "./nexus")
    pin = os.getenv("NEXUS_PIN", "")
    if not pin:
        print("ERROR: NEXUS_PIN is required in environment or .env")
        return 2

    cmd = [
        nexus_cli,
        "assets/create/asset",
        "format=basic",
        f"last_poll_timestamp={initial}",
        f"pin={pin}",
    ]
    if name:
        cmd.insert(2, f"name={name}")

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
    args = ap.parse_args()

    return run_create(args.name, args.initial)


if __name__ == "__main__":
    sys.exit(main())
