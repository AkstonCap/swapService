#!/usr/bin/env python3
r"""
Helper to create a Nexus Heartbeat Asset according to the Provider Heartbeat Asset Standard.

See ASSET_STANDARD.md for full specification.

What it creates:
- A new asset with all required, recommended, and transparency fields.
- Uses format=basic so later updates via format=basic work without fees (>=10s apart).

Required Fields:
- last_poll_timestamp: Unix timestamp of last service poll cycle
- last_safe_timestamp_solana: Solana chain waterline
- last_safe_timestamp_nexus: Nexus chain waterline

Recommended Fields:
- distordiaType: Asset type identifier (nexusBridgeHeartbeat)
- provider: Provider/operator name
- version: Service version string

Transparency Fields (for public backing validation):
- supported_chains: Comma-separated list of destination chains
- supported_tokens: Comma-separated list of bridgeable token pairs
- nexus_treasury_address: Nexus treasury account holding incoming USDD
- nexus_treasury_token: Token ticker held in Nexus treasury
- solana_vault_address: Solana vault token account (ATA) holding USDC
- solana_vault_token: Token ticker held in Solana vault
- solana_vault_mint: Mint address of Solana vault token

Usage:
    python create_heartbeat_asset.py --name distordiaBridgeHeartbeat \
        --provider distordia \
        --nexus-treasury-address 8CuyRASoeBCR... \
        --solana-vault-address Bg1MUQDMjAuX...

    # Minimal (uses defaults from .env where possible):
    python create_heartbeat_asset.py --name distordiaBridgeHeartbeat

Requires in .env (or environment):
    NEXUS_CLI_PATH (default: ./nexus)
    NEXUS_PIN
    NEXUS_USDD_TREASURY_ACCOUNT (optional, for default treasury address)
    VAULT_USDC_ACCOUNT (optional, for default vault address)
    USDC_MINT (optional, for default mint address)

After creation, set in .env of swapService:
    NEXUS_HEARTBEAT_ASSET_ADDRESS=<printed address>
    NEXUS_HEARTBEAT_ASSET_NAME=<name if provided>
    HEARTBEAT_ENABLED=true

Note: Creating an asset costs ~1 NXS once. Updates are free if not more often than every 10s.
"""
import os
import sys
import json
import re
import subprocess
from argparse import ArgumentParser

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


def run_create(
    name: str | None,
    *,
    # Required fields
    initial_last_poll: str,
    sol_waterline_field: str,
    nex_waterline_field: str,
    sol_initial: str,
    nex_initial: str,
    # Recommended fields
    distordia_type: str,
    provider: str,
    version: str,
    # Transparency fields
    supported_chains: str,
    supported_tokens: str,
    nexus_treasury_address: str,
    nexus_treasury_token: str,
    solana_vault_address: str,
    solana_vault_token: str,
    solana_vault_mint: str,
    assume_yes: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    nexus_cli = os.getenv("NEXUS_CLI_PATH", "./nexus")
    pin = os.getenv("NEXUS_PIN")
    if not pin:
        print("ERROR: NEXUS_PIN is required in environment or .env")
        return 2

    # assets/create and assets/get are session-scoped: on a multiuser=1 node they need
    # session=<id>, and on a single-user node the session must NOT be supplied.
    multiuser = os.getenv("NEXUS_MULTIUSER", "false").lower() in ("1", "true", "yes", "on")
    session = (os.getenv("NEXUS_SESSION") or "").strip()
    if multiuser and not session:
        print("ERROR: NEXUS_MULTIUSER is set but NEXUS_SESSION is empty; assets/create "
              "requires session=<id> on a multiuser node.")
        return 2
    session_args = [f"session={session}"] if (multiuser and session) else []

    # Build command with all fields
    cmd = [
        nexus_cli,
        "assets/create/asset",
        "format=basic",
    ]
    
    if name:
        cmd.append(f"name={name}")
    
    # Required fields
    cmd.append(f"last_poll_timestamp={initial_last_poll}")
    cmd.append(f"{sol_waterline_field}={sol_initial}")
    cmd.append(f"{nex_waterline_field}={nex_initial}")
    
    # Recommended fields
    cmd.append(f"distordiaType={distordia_type}")
    cmd.append(f"provider={provider}")
    cmd.append(f"version={version}")
    
    # Transparency fields
    cmd.append(f"supported_chains={supported_chains}")
    cmd.append(f"supported_tokens={supported_tokens}")
    if nexus_treasury_address:
        cmd.append(f"nexus_treasury_address={nexus_treasury_address}")
    cmd.append(f"nexus_treasury_token={nexus_treasury_token}")
    if solana_vault_address:
        cmd.append(f"solana_vault_address={solana_vault_address}")
    cmd.append(f"solana_vault_token={solana_vault_token}")
    if solana_vault_mint:
        cmd.append(f"solana_vault_mint={solana_vault_mint}")
    
    cmd.extend(session_args)
    # PIN last
    cmd.append(f"pin={pin}")

    def _redact_argv(argv):
        # Redact by KEY. The old code masked cmd[:-1] + ["pin=***"], i.e. it assumed the
        # PIN was last; any later append would have printed the real PIN in cleartext.
        return [("pin=***" if a.startswith("pin=") else
                 "session=***" if a.startswith("session=") else a) for a in argv]

    def _redact_text(text):
        out = text or ""
        for secret in (pin, session):
            if secret:
                out = out.replace(secret, "***")
        return out

    print("Creating heartbeat asset:")
    print("  " + " \\\n    ".join(_redact_argv(cmd)))
    print()

    # Idempotency: creating a second asset costs another ~1 NXS and leaves two
    # conflicting heartbeats, so refuse unless the caller insists.
    if name and not force:
        try:
            probe = subprocess.run([nexus_cli, "assets/get/asset", f"name={name}"] + session_args,
                                   capture_output=True, text=True, timeout=20)
            if probe.returncode == 0 and '"address"' in (probe.stdout or ""):
                print(f"ERROR: an asset named '{name}' already exists.")
                print("       Creating another would spend ~1 NXS and leave two heartbeats.")
                print("       Re-run with --force only if you are certain.")
                return 5
        except Exception:
            pass  # probe is best-effort; never block creation on a failed check

    if dry_run:
        print("--dry-run: no asset created, no NXS spent.")
        return 0

    if not assume_yes:
        print("This creates a PERMANENT on-chain asset and spends ~1 NXS.")
        print("format=basic fixes the field set at creation - fields cannot be added later.")
        try:
            if input("Type 'yes' to proceed: ").strip().lower() != "yes":
                print("Aborted.")
                return 6
        except (EOFError, KeyboardInterrupt):
            print("\nAborted (no TTY; pass --yes to run non-interactively).")
            return 6
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("ERROR: Nexus CLI timeout")
        return 3
    except FileNotFoundError:
        print(f"ERROR: Nexus CLI not found at {nexus_cli}")
        return 4

    if res.returncode != 0:
        print("ERROR from Nexus CLI:\n", _redact_text(res.stderr or res.stdout))
        return res.returncode

    out = res.stdout.strip()
    print("Raw output:\n", _redact_text(out))

    # Try to extract address from JSON
    address = None
    data_parsed = None
    try:
        data = json.loads(out)
        data_parsed = data
        # results may be dict or top-level
        if isinstance(data, dict):
            results = data.get("results") or data
            address = results.get("address") if isinstance(results, dict) else None
    except Exception:
        pass

    if isinstance(data_parsed, dict) and data_parsed.get("error"):
        print("ERROR: Nexus CLI reported an error despite exit code 0:")
        print("  ", _redact_text(str(data_parsed.get("error"))))
        return 7

    if address:
        print("\n" + "=" * 60)
        print("SUCCESS! Heartbeat asset created.")
        print("=" * 60)
        print(f"\nAsset address: {address}")
        if name:
            print(f"Asset name:    {name}")
        print("\nAdd to your .env file:")
        if name:
            print(f"  NEXUS_HEARTBEAT_ASSET_NAME={name}   # <-- the service resolves BY NAME")
        else:
            print("  WARNING: no --name given. The service addresses the heartbeat asset by")
            print("           NAME, so an unnamed asset is unreachable. Re-create it with --name.")
        print(f"  # NEXUS_HEARTBEAT_ASSET_ADDRESS={address}   (recorded for reference; not read by the service)")
        print("  HEARTBEAT_ENABLED=true")
        return 0
    else:
        print("\nERROR: could not parse an asset address from the CLI output.")
        print("The asset may or may not have been created - check the raw output above")
        print("and query it with: assets/get/asset name=<NAME>")
        return 8


def main() -> int:
    # Load defaults from environment
    env_sol_field = os.getenv("HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
    env_nex_field = os.getenv("HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_nexus")
    env_treasury = os.getenv("NEXUS_USDD_TREASURY_ACCOUNT", "")
    env_vault = os.getenv("VAULT_USDC_ACCOUNT", "")
    env_mint = (os.getenv("SOLANA_TOKEN_MINT") or
            os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
    
    ap = ArgumentParser(
        description="Create Nexus heartbeat asset for swapService (per ASSET_STANDARD.md)"
    )
    
    # Asset identity
    ap.add_argument(
        "--name",
        help="Asset name (e.g., distordiaBridgeHeartbeat). Recommended for easy updates.",
    )
    
    # Required fields
    ap.add_argument(
        "--initial-timestamp",
        default="0",
        help="Initial last_poll_timestamp value (default: 0)",
    )
    ap.add_argument(
        "--solana-waterline-field",
        default=env_sol_field,
        help=f"Solana waterline field name (default: {env_sol_field})",
    )
    ap.add_argument(
        "--nexus-waterline-field",
        default=env_nex_field,
        help=f"Nexus waterline field name (default: {env_nex_field})",
    )
    ap.add_argument(
        "--solana-waterline-initial",
        default="0",
        help="Initial Solana waterline value (default: 0)",
    )
    ap.add_argument(
        "--nexus-waterline-initial",
        default="0",
        help="Initial Nexus waterline value (default: 0)",
    )
    
    # Recommended fields
    ap.add_argument(
        "--type",
        dest="distordia_type",
        default="nexusBridgeHeartbeat",
        help="Asset type identifier (default: nexusBridgeHeartbeat)",
    )
    ap.add_argument(
        "--provider",
        default="distordia",
        help="Provider/operator name (default: distordia)",
    )
    ap.add_argument(
        "--version",
        default="1.0.0",
        help="Service version string (default: 1.0.0)",
    )
    
    # Transparency fields
    ap.add_argument(
        "--supported-chains",
        default="solana",
        help="Comma-separated list of supported destination chains (default: solana)",
    )
    ap.add_argument(
        "--supported-tokens",
        default="USDD:USDC",
        help="Comma-separated list of bridgeable token pairs (default: USDD:USDC)",
    )
    ap.add_argument(
        "--nexus-treasury-address",
        default=env_treasury,
        help=f"Nexus treasury account address (default from env: {env_treasury[:20]}...)" if env_treasury else "Nexus treasury account address",
    )
    ap.add_argument(
        "--nexus-treasury-token",
        default="USDD",
        help="Token ticker held in Nexus treasury (default: USDD)",
    )
    ap.add_argument(
        "--solana-vault-address",
        default=env_vault,
        help=f"Solana vault token account (ATA) address (default from env: {env_vault[:20]}...)" if env_vault else "Solana vault token account (ATA) address",
    )
    ap.add_argument(
        "--solana-vault-token",
        default="USDC",
        help="Token ticker held in Solana vault (default: USDC)",
    )
    ap.add_argument(
        "--solana-vault-mint",
        default=env_mint,
        help=f"Mint address of Solana vault token (default: {env_mint[:20]}...)",
    )
    
    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the confirmation prompt (required for non-interactive use)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the command that would run; create nothing, spend no NXS")
    ap.add_argument("--force", action="store_true",
                    help="create even if an asset with this name already exists")

    args = ap.parse_args()

    # A non-numeric timestamp would be written on-chain and then fail every consumer.
    for label, value in (("--initial-timestamp", args.initial_timestamp),
                         ("--solana-waterline-initial", args.solana_waterline_initial),
                         ("--nexus-waterline-initial", args.nexus_waterline_initial)):
        if value is not None and not str(value).isdigit():
            print(f"ERROR: {label} must be a non-negative integer (got {value!r})")
            return 2

    # Field NAMES form the key side of `key=value` CLI tokens, so an unchecked value can
    # inject an API parameter (e.g. a second `pin=`). Keep them identifier-safe and
    # refuse names that collide with Nexus API parameters.
    RESERVED = {"pin", "session", "name", "format", "address", "username", "password"}
    for label, value in (("--solana-waterline-field", args.solana_waterline_field),
                         ("--nexus-waterline-field", args.nexus_waterline_field)):
        text = str(value or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
            print(f"ERROR: {label} must be a plain field name (got {value!r})")
            return 2
        if text.lower() in RESERVED:
            print(f"ERROR: {label}={text!r} collides with a Nexus API parameter")
            return 2

    # Warn if transparency addresses are missing
    if not args.nexus_treasury_address:
        print("WARNING: --nexus-treasury-address not provided. Set NEXUS_USDD_TREASURY_ACCOUNT in .env or provide explicitly.")
    if not args.solana_vault_address:
        print("WARNING: --solana-vault-address not provided. Set VAULT_USDC_ACCOUNT in .env or provide explicitly.")

    return run_create(
        args.name,
        initial_last_poll=args.initial_timestamp,
        sol_waterline_field=args.solana_waterline_field,
        nex_waterline_field=args.nexus_waterline_field,
        sol_initial=args.solana_waterline_initial,
        nex_initial=args.nexus_waterline_initial,
        distordia_type=args.distordia_type,
        provider=args.provider,
        version=args.version,
        supported_chains=args.supported_chains,
        supported_tokens=args.supported_tokens,
        nexus_treasury_address=args.nexus_treasury_address,
        nexus_treasury_token=args.nexus_treasury_token,
        solana_vault_address=args.solana_vault_address,
        solana_vault_token=args.solana_vault_token,
        solana_vault_mint=args.solana_vault_mint,
        assume_yes=args.yes,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
