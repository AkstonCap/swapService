#!/usr/bin/env python3
"""
swapService.py

Listens for USDC deposits into a Solana vault account (via Memo),
then mints USDD on a Nexus node. Also provides a helper to send
USDC out of the vault to a given address.
"""

import os
import time
import json
import subprocess
import base64
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv

from solana.rpc.api import Client
from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    transfer_checked,
    get_associated_token_address,
    create_associated_token_account,
)

# Load config from .env
load_dotenv()

# Validate required environment variables
required_env_vars = [
    "SOLANA_RPC_URL",
    "VAULT_KEYPAIR",
    "VAULT_USDC_ACCOUNT",
    "USDC_MINT",
    "NEXUS_PIN",
    "NEXUS_USDD_ACCOUNT",
]
for var in required_env_vars:
    if not os.getenv(var):
        raise ValueError(f"Required environment variable {var} is not set")

# Solana settings
RPC_URL = os.getenv("SOLANA_RPC_URL")  # e.g. https://api.mainnet-beta.solana.com or http://127.0.0.1:8899
VAULT_KEYPAIR = os.getenv("VAULT_KEYPAIR")  # path to vault keypair JSON
VAULT_USDC_ACCOUNT = PublicKey(os.getenv("VAULT_USDC_ACCOUNT"))  # your vault's USDC token-account
USDC_MINT = PublicKey(os.getenv("USDC_MINT"))  # mainnet USDC mint or local test-mint
MEMO_PROGRAM_ID = PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")  # latest Memo Program

# Token decimals (configurable)
USDC_DECIMALS = int(os.getenv("USDC_DECIMALS", "6"))
USDD_DECIMALS = int(os.getenv("USDD_DECIMALS", "6"))

# Nexus settings
NEXUS_CLI = os.getenv("NEXUS_CLI_PATH", "./nexus")
NEXUS_TOKEN_NAME = os.getenv("NEXUS_TOKEN_NAME", "USDD")
NEXUS_RPC_HOST = os.getenv("NEXUS_RPC_HOST", "http://127.0.0.1:8399")
NEXUS_USDD_ACCOUNT = os.getenv("NEXUS_USDD_ACCOUNT")  # Your USDD account address for monitoring deposits

# Polling / state
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
PROCESSED_SIG_FILE = os.getenv("PROCESSED_SIG_FILE", "processed_sigs.json")

# Load processed signatures to avoid double-processing
if os.path.exists(PROCESSED_SIG_FILE):
    with open(PROCESSED_SIG_FILE, "r") as f:
        processed_sigs = set(json.load(f))
else:
    processed_sigs = set()

# Load processed Nexus transactions to avoid double-processing
PROCESSED_NEXUS_FILE = os.getenv("PROCESSED_NEXUS_FILE", "processed_nexus_txs.json")
if os.path.exists(PROCESSED_NEXUS_FILE):
    with open(PROCESSED_NEXUS_FILE, "r") as f:
        processed_nexus_txs = set(json.load(f))
else:
    processed_nexus_txs = set()


def save_state():
    with open(PROCESSED_SIG_FILE, "w") as f:
        json.dump(list(processed_sigs), f)
    with open(PROCESSED_NEXUS_FILE, "w") as f:
        json.dump(list(processed_nexus_txs), f)


# --- Helpers -----------------------------------------------------------------

def scale_amount(amount: int, src_decimals: int, dst_decimals: int) -> int:
    """Scale integer base-unit amount between different decimals."""
    if src_decimals == dst_decimals:
        return int(amount)
    if src_decimals < dst_decimals:
        return int(amount) * (10 ** (dst_decimals - src_decimals))
    return int(amount) // (10 ** (src_decimals - dst_decimals))


def parse_amount_to_base_units(val, decimals: int) -> int:
    """Parse an amount that may be int (already base units) or decimal string into base units."""
    try:
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            return int(Decimal(str(val)).scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))
        s = str(val)
        if "." in s:
            return int(Decimal(s).scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))
        return int(s)
    except Exception:
        return 0


def extract_memo_from_instructions(instructions):
    """Extract memo text from jsonParsed transaction instructions."""
    for ix in instructions:
        prog = ix.get("program")
        if prog == "spl-memo":
            parsed = ix.get("parsed")
            if isinstance(parsed, dict):
                info = parsed.get("info", {})
                memo = info.get("memo")
                if isinstance(memo, str):
                    return memo
            elif isinstance(parsed, str):
                return parsed
        # Fallback: match by programId and base64-decode data
        if ix.get("programId") == str(MEMO_PROGRAM_ID) and "data" in ix:
            try:
                return base64.b64decode(ix["data"]).decode("utf-8")
            except Exception:
                continue
    return None


def validate_nexus_address(nexus_addr: str) -> bool:
    """
    Validates if a Nexus address exists on the Nexus chain using the correct API format.
    Uses: ./nexus register/get/finance:account address=<nexus_addr>
    """
    cmd = [NEXUS_CLI, "register/get/finance:account", f"address={nexus_addr}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return res.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"Timeout validating Nexus address: {nexus_addr}")
        return False
    except Exception as e:
        print(f"Error validating Nexus address {nexus_addr}: {e}")
        return False


def mint_usdd(to_nexus_addr: str, amount: int, usdc_txid: str):
    """
    Calls local nexus node to send `amount` USDD to the given Nexus address.
    Includes the USDC transaction ID as reference.
    Uses: ./nexus finance/debit/account from=USDD to=<nexus_addr> amount=<amount> reference=<reference> pin=<pin>
    """
    pin = os.getenv("NEXUS_PIN", "")
    if not pin:
        print("ERROR: NEXUS_PIN environment variable not set")
        return False

    cmd = [
        NEXUS_CLI,
        "finance/debit/account",
        "from=USDD",
        f"to={to_nexus_addr}",
        f"amount={amount}",
        f"reference=USDC_TX:{usdc_txid}",
        f"pin={pin}",
    ]
    print(">>> Sending USDD on Nexus:", cmd[:-1] + ["pin=***"])  # Hide PIN in logs
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            print("ERROR sending USDD:", res.stderr)
            return False
        else:
            print("USDD send successful:", res.stdout)
            return True
    except subprocess.TimeoutExpired:
        print("ERROR: Nexus CLI timeout")
        return False
    except Exception as e:
        print(f"ERROR executing Nexus CLI: {e}")
        return False


def send_usdc(to_solana_addr: str, amount: int):
    """
    Sends `amount` USDC (in base units) from vault to given Solana address.
    If a wallet address is provided, the recipient's USDC ATA will be created if missing.
    """
    try:
        with open(VAULT_KEYPAIR, "r") as f:
            kp_data = json.load(f)
        if isinstance(kp_data, list):
            vault_kp = Keypair.from_secret_key(bytes(kp_data))
        else:
            raise ValueError("Unsupported vault keypair format; expected JSON array of ints.")

        client = Client(RPC_URL)
        recipient_owner = PublicKey(to_solana_addr)
        dest_ata = get_associated_token_address(owner=recipient_owner, mint=USDC_MINT)

        # Check if recipient ATA exists
        ata_info = client.get_account_info(dest_ata).get("result", {}).get("value")

        tx = Transaction()
        tx.fee_payer = vault_kp.public_key
        if ata_info is None:
            tx.add(
                create_associated_token_account(
                    payer=vault_kp.public_key, owner=recipient_owner, mint=USDC_MINT
                )
            )

        tx.add(
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=VAULT_USDC_ACCOUNT,
                mint=USDC_MINT,
                dest=dest_ata,
                owner=vault_kp.public_key,
                amount=amount,
                decimals=USDC_DECIMALS,
                signers=[],
            )
        )

        # Sign & send, then try to confirm
        resp = client.send_transaction(tx, vault_kp)
        sig = resp.get("result") if isinstance(resp, dict) else resp
        try:
            Client(RPC_URL).confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"Sent USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error sending USDC: {e}")
        return False


def poll_nexus_usdd_deposits():
    """
    Poll Nexus for new USDD deposits into our USDD account.
    If we see a memo with a Solana address, send USDC to that address.
    """
    cmd = [NEXUS_CLI, "finance/transaction/account", f"address={NEXUS_USDD_ACCOUNT}"]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            print("Error fetching USDD transactions:", res.stderr)
            return

        # Parse JSON response
        transactions = json.loads(res.stdout)
        if not isinstance(transactions, list):
            transactions = [transactions]  # Handle single transaction response

        for tx in transactions:
            tx_id = tx.get("txid")
            if not tx_id or tx_id in processed_nexus_txs:
                continue

            mark_processed = True

            # Check if this is a credit (incoming) transaction
            if tx.get("type") == "CREDIT" and tx.get("confirmation", 0) > 0:
                raw_amount = tx.get("amount", 0)
                usdd_units = parse_amount_to_base_units(raw_amount, USDD_DECIMALS)
                amount_usdc_units = scale_amount(usdd_units, USDD_DECIMALS, USDC_DECIMALS)
                reference = tx.get("reference", "") or ""

                # Look for Solana address in reference (solana:<SOLANA_ADDRESS>)
                if reference.startswith("solana:"):
                    solana_addr = reference.split("solana:", 1)[1].strip()
                    print(
                        f"→ USDD deposit of {usdd_units} (base units); sending {amount_usdc_units} USDC base units to {solana_addr}"
                    )

                    # Validate Solana address strictly
                    try:
                        _ = PublicKey(solana_addr)
                    except Exception:
                        print(f"✗ Invalid Solana address format: {solana_addr}")
                    else:
                        if send_usdc(solana_addr, amount_usdc_units):
                            print(f"✓ Successfully sent {amount_usdc_units} USDC to {solana_addr}")
                        else:
                            print(f"✗ Failed to send USDC to {solana_addr}")
                            # Don't mark as processed if sending failed
                            mark_processed = False
                else:
                    print(f"No Solana address found in reference: {reference}")

            if mark_processed:
                processed_nexus_txs.add(tx_id)

    except json.JSONDecodeError as e:
        print(f"Error parsing Nexus transaction JSON: {e}")
    except subprocess.TimeoutExpired:
        print("Timeout fetching USDD transactions")
    except Exception as e:
        print(f"Error polling USDD deposits: {e}")


def poll_solana_deposits():
    """
    Poll Solana for new token-transfer signatures into VAULT_USDC_ACCOUNT.
    If we see a memo with a Nexus address, validate it and call mint_usdd().
    """
    try:
        client = Client(RPC_URL)
        # get up to 100 most recent signatures
        sigs = client.get_signatures_for_address(
            VAULT_USDC_ACCOUNT, limit=100, commitment="confirmed"
        )["result"]
    except Exception as e:
        print(f"Error fetching signatures: {e}")
        return

    for entry in sigs:
        sig = entry["signature"]
        if sig in processed_sigs:
            continue

        mark_processed = True

        # Fetch full tx details
        try:
            tx = Client(RPC_URL).get_transaction(sig, encoding="jsonParsed")["result"]
            if not tx:
                continue
        except Exception as e:
            print(f"Error fetching transaction {sig}: {e}")
            continue

        # 1) Find SPL token transfer to vault (transfer or transferChecked)
        for instr in tx["transaction"]["message"]["instructions"]:
            if instr.get("program") == "spl-token" and instr.get("parsed"):
                p = instr["parsed"]
                if p.get("type") in ("transfer", "transferChecked") and p.get("info", {}).get(
                    "destination"
                ) == str(VAULT_USDC_ACCOUNT):
                    info = p["info"]
                    if "amount" in info:
                        amount_usdc_units = int(info["amount"])  # already base units
                    elif "tokenAmount" in info and isinstance(info["tokenAmount"], dict):
                        amount_usdc_units = int(info["tokenAmount"].get("amount", 0))
                    else:
                        continue

                    # 2) Find Memo instruction for Nexus address
                    memo_data = extract_memo_from_instructions(
                        tx["transaction"]["message"]["instructions"]
                    )
                    if not memo_data:
                        print("No memo found; skipping")
                        break

                    if memo_data.startswith("nexus:"):
                        nexus_addr = memo_data.split("nexus:", 1)[1].strip()
                        print(
                            f"→ Deposit of {amount_usdc_units} USDC base units; checking Nexus address {nexus_addr}"
                        )

                        # Validate Nexus address exists
                        if validate_nexus_address(nexus_addr):
                            print(f"→ Nexus address validated, minting to {nexus_addr}")
                            usdd_units = scale_amount(
                                amount_usdc_units, USDC_DECIMALS, USDD_DECIMALS
                            )
                            if mint_usdd(nexus_addr, usdd_units, sig):
                                print(
                                    f"✓ Successfully minted {usdd_units} USDD base units to {nexus_addr}"
                                )
                            else:
                                print(f"✗ Failed to mint USDD to {nexus_addr}")
                                # Don't mark as processed if minting failed
                                mark_processed = False
                        else:
                            print(f"✗ Invalid Nexus address: {nexus_addr}")
                    else:
                        print("Skipping, bad memo format:", memo_data)
                    break

        if mark_processed:
            processed_sigs.add(sig)
    save_state()


def main():
    print("🌐 Starting bidirectional swap service")
    print(f"   Solana RPC: {RPC_URL}")
    print(f"   USDC Vault: {VAULT_USDC_ACCOUNT}")
    print(f"   USDD Account: {NEXUS_USDD_ACCOUNT}")
    print("   Monitoring:")
    print("   - USDC → USDD: Solana deposits with Nexus address in memo")
    print("   - USDD → USDC: USDD deposits with Solana address in reference")

    try:
        while True:
            # Poll for USDC deposits (Solana → USDD)
            poll_solana_deposits()

            # Poll for USDD deposits (USDD → USDC)
            poll_nexus_usdd_deposits()

            # Save state after each polling cycle
            save_state()

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("Shutting down…")
        save_state()


if __name__ == "__main__":
    main()
