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
) -> int:
    nexus_cli = os.getenv("NEXUS_CLI_PATH", "./nexus")
    pin = os.getenv("NEXUS_PIN")
    if not pin:
        print("ERROR: NEXUS_PIN is required in environment or .env")
        return 2

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
    
    # PIN last
    cmd.append(f"pin={pin}")

    # Print command with masked PIN
    print("Creating heartbeat asset:")
    print("  " + " \\\n    ".join(cmd[:-1] + ["pin=***"]))
    print()
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
        print("\n" + "=" * 60)
        print("SUCCESS! Heartbeat asset created.")
        print("=" * 60)
        print(f"\nAsset address: {address}")
        if name:
            print(f"Asset name:    {name}")
        print("\nAdd to your .env file:")
        print(f"  NEXUS_HEARTBEAT_ASSET_ADDRESS={address}")
        if name:
            print(f"  NEXUS_HEARTBEAT_ASSET_NAME={name}")
        print("  HEARTBEAT_ENABLED=true")
        return 0
    else:
        print("\nNOTE: Could not parse address from output automatically.")
        print("Please copy it from the raw output above.")
        return 0


def main() -> int:
    # Load defaults from environment
    env_sol_field = os.getenv("HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
    env_nex_field = os.getenv("HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_nexus")
    env_treasury = os.getenv("NEXUS_USDD_TREASURY_ACCOUNT", "")
    env_vault = os.getenv("VAULT_USDC_ACCOUNT", "")
    env_mint = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    
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
    
    args = ap.parse_args()

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
    )


if __name__ == "__main__":
    sys.exit(main())
