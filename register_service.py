#!/usr/bin/env python3
"""Register this bridge on-chain, or inspect a bridge someone else registered.

The registration is a single Nexus asset that declares everything a user or auditor needs:
which token pair is bridged, the vault and treasury addresses that back it, the current
fees and minimums, the deposit memo format, and a liveness timestamp the service refreshes
each cycle. Discovery and proof-of-life come from the same record.

    python3 register_service.py --show                 # what WOULD be published, from .env
    python3 register_service.py --create --dry-run     # preview the CLI call, spend nothing
    python3 register_service.py --create --name myBridgeHeartbeat
    python3 register_service.py --inspect someOtherBridgeHeartbeat

`format=basic` fixes an asset's field set at creation, so the record is created complete
and the service afterwards rewrites only the mutable fields (status, terms, waterlines).
Adding a field later means creating a new asset.
"""
from __future__ import annotations

import argparse
import json
import sys

from src import config, nexus_client as nc

IMMUTABLE = set(nc.SERVICE_RECORD_IMMUTABLE)


def _print_record(rec: dict) -> None:
    width = max(len(k) for k in rec)
    for k, v in rec.items():
        tag = "immutable" if k in IMMUTABLE else "mutable  "
        print(f"  {tag}  {k:<{width}}  {v}")
    size = nc.service_record_size(rec)
    pct = 100.0 * size / nc.SERVICE_RECORD_MAX_BYTES
    print(f"\n  {len(rec)} fields, ~{size} bytes ({pct:.0f}% of the {nc.SERVICE_RECORD_MAX_BYTES}B budget)")


def cmd_show() -> int:
    rec = nc.build_service_record()
    print("Service record that would be published (derived from your .env):\n")
    _print_record(rec)
    missing = [k for k in ("provider", "nexus_treasury_address", "solana_vault_address") if not rec.get(k)]
    if rec.get("provider") == "unnamed-operator":
        print("\n  ⚠ SERVICE_PROVIDER is unset — users will see 'unnamed-operator'.")
    if missing:
        print(f"  ⚠ empty required fields: {', '.join(missing)}")
    return 0


def cmd_inspect(name: str) -> int:
    rec = nc.read_service_record(name)
    if not rec:
        print(f"No readable asset named {name!r}.")
        return 1
    known = {k: v for k, v in rec.items() if k in nc.SERVICE_RECORD_FIELDS
             or k in (config.HEARTBEAT_WATERLINE_SOLANA_FIELD, config.HEARTBEAT_WATERLINE_NEXUS_FIELD)}
    print(f"Bridge registration: {name}\n")
    for k, v in known.items():
        print(f"  {k:<28} {v}")
    extra = sorted(set(rec) - set(known) - {"address", "owner", "created", "modified", "name"})
    if extra:
        print(f"\n  (other fields on the asset: {', '.join(extra)})")

    import time
    lp = known.get("last_poll_timestamp")
    if lp:
        try:
            age = int(time.time()) - int(lp)
            state = "ONLINE" if age < 600 else "STALE — no beat for " + f"{age // 60}m"
            print(f"\n  Liveness: {state} (last beat {age}s ago), status field = {known.get('status', '?')}")
        except Exception:
            pass
    missing = [f for f in nc.SERVICE_RECORD_FIELDS if f not in rec]
    if missing:
        print(f"\n  ⚠ record is incomplete, missing: {', '.join(missing)}")
    return 0


def cmd_create(name: str, assume_yes: bool, dry_run: bool, force: bool) -> int:
    if not config.NEXUS_PIN:
        print("ERROR: NEXUS_PIN is not set.")
        return 2
    multiuser = bool(getattr(config, "NEXUS_MULTIUSER", False))
    session = (getattr(config, "NEXUS_SESSION", "") or "").strip()
    if multiuser and not session:
        print("ERROR: NEXUS_MULTIUSER is true but NEXUS_SESSION is empty; assets/create needs it.")
        return 2

    rec = nc.build_service_record()
    size = nc.service_record_size(rec)
    if size > nc.SERVICE_RECORD_MAX_BYTES:
        print(f"ERROR: record is ~{size} bytes, over the {nc.SERVICE_RECORD_MAX_BYTES}B budget. "
              f"Shorten SERVICE_PROVIDER / SERVICE_CONTACT and retry.")
        return 3

    print(f"Registering bridge '{name}':\n")
    _print_record(rec)

    if not force:
        existing = nc.read_service_record(name)
        if existing and existing.get("address"):
            print(f"\nERROR: an asset named '{name}' already exists (address {existing.get('address')}).")
            print("       Creating another spends ~1 NXS and leaves two registrations.")
            print("       Use --force only if you are certain, or --inspect to view it.")
            return 5

    cmd = [config.NEXUS_CLI, "assets/create/asset", "format=basic", f"name={name}"]
    cmd += [f"{k}={v}" for k, v in rec.items()]
    cmd += [f"pin={config.NEXUS_PIN}"]
    printable = [("pin=***" if a.startswith("pin=") else
                  "session=***" if a.startswith("session=") else a)
                 for a in nc.apply_session(cmd)]
    print("\nCommand:\n  " + " \\\n    ".join(printable))

    if dry_run:
        print("\n--dry-run: nothing created, no NXS spent.")
        return 0
    if not assume_yes:
        print("\nThis creates a PERMANENT on-chain asset and spends ~1 NXS.")
        print("format=basic fixes the field set at creation — fields cannot be added later.")
        try:
            if input("Type 'yes' to proceed: ").strip().lower() != "yes":
                print("Aborted.")
                return 6
        except (EOFError, KeyboardInterrupt):
            print("\nAborted (no TTY; pass --yes for non-interactive use).")
            return 6

    code, out, err = nc._run(cmd, timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 30))
    if code != 0:
        print("ERROR from Nexus CLI:\n", nc.redact(err or out))
        return code
    data = nc._parse_json_lenient(out)
    if isinstance(data, dict) and data.get("error"):
        print("ERROR: Nexus reported an error despite exit code 0:\n ", nc.redact(str(data["error"])))
        return 7
    address = (data or {}).get("address") if isinstance(data, dict) else None
    print("\nRaw output:\n", nc.redact(out.strip()))
    if not address:
        print("\nERROR: could not parse an asset address; verify with --inspect before relying on it.")
        return 8

    print("\n" + "=" * 62)
    print("Registered.")
    print("=" * 62)
    print(f"\nAsset address: {address}")
    print("\nAdd to .env (the service resolves the asset BY NAME):")
    print(f"  NEXUS_HEARTBEAT_ASSET_NAME={name}")
    print("  HEARTBEAT_ENABLED=true")
    print(f"\nVerify with:  python3 register_service.py --inspect {name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print the record that would be published")
    g.add_argument("--create", action="store_true", help="create the on-chain registration asset")
    g.add_argument("--inspect", metavar="NAME", help="read a published bridge registration")
    ap.add_argument("--name", default=config.NEXUS_HEARTBEAT_ASSET_NAME or "",
                    help="asset name to create (defaults to NEXUS_HEARTBEAT_ASSET_NAME)")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--dry-run", action="store_true", help="show the command without creating")
    ap.add_argument("--force", action="store_true", help="create even if the name already exists")
    ap.add_argument("--json", action="store_true", help="machine-readable output for --show")
    a = ap.parse_args()

    if a.show:
        if a.json:
            print(json.dumps(nc.build_service_record(), indent=2))
            return 0
        return cmd_show()
    if a.inspect:
        return cmd_inspect(a.inspect)
    if not a.name:
        print("ERROR: --name is required (or set NEXUS_HEARTBEAT_ASSET_NAME). The service "
              "looks the asset up by name, so an unnamed asset is unreachable.")
        return 2
    return cmd_create(a.name, a.yes, a.dry_run, a.force)


if __name__ == "__main__":
    sys.exit(main())
