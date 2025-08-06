#!/usr/bin/env python3
"""
swapService.py

Listens for USDC deposits into a Solana vault account (via Memo),
then mints nUSD on a Nexus node. Also provides a helper to send
USDC out of the vault to a given address.
"""

import os
import time
import json
import subprocess
from dotenv import load_dotenv

from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.transaction import Transaction, AccountMeta, TransactionInstruction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.memo.instructions import create_memo  # :contentReference[oaicite:2]{index=2}
from spl.token.instructions import transfer_checked

# Load config from .env
load_dotenv()

# Solana settings
RPC_URL           = os.getenv("SOLANA_RPC_URL")           # e.g. https://api.mainnet-beta.solana.com or http://127.0.0.1:8899
VAULT_KEYPAIR     = os.getenv("VAULT_KEYPAIR")             # path to vault keypair JSON
VAULT_USDC_ACCOUNT= PublicKey(os.getenv("VAULT_USDC_ACCOUNT"))  # your vault's USDC token-account
USDC_MINT         = PublicKey(os.getenv("USDC_MINT"))           # mainnet USDC mint or local test-mint
MEMO_PROGRAM_ID   = PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")  # latest Memo Program :contentReference[oaicite:3]{index=3}

# Nexus settings
NEXUS_CLI         = os.getenv("NEXUS_CLI_PATH", "nexus-cli")
NEXUS_TOKEN_NAME  = os.getenv("NEXUS_TOKEN_NAME", "nUSD")
NEXUS_RPC_HOST    = os.getenv("NEXUS_RPC_HOST", "http://127.0.0.1:8399")

# Polling / state
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "10"))
PROCESSED_SIG_FILE= os.getenv("PROCESSED_SIG_FILE", "processed_sigs.json")

# Load processed signatures to avoid double-processing
if os.path.exists(PROCESSED_SIG_FILE):
    processed_sigs = set(json.load(open(PROCESSED_SIG_FILE)))
else:
    processed_sigs = set()


def save_state():
    with open(PROCESSED_SIG_FILE, "w") as f:
        json.dump(list(processed_sigs), f)


def mint_nusd(to_nexus_addr: str, amount: int):
    """
    Calls nexus-cli to mint `amount` nUSD to the given Nexus address.
    """
    cmd = [
        NEXUS_CLI,
        "--rpc", NEXUS_RPC_HOST,
        "token", "mint",
        "--token", NEXUS_TOKEN_NAME,
        "--to", to_nexus_addr,
        "--amount", str(amount)
    ]
    print(">>> Minting on Nexus:", cmd)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("ERROR minting:", res.stderr)
    else:
        print("Mint successful:", res.stdout)


def send_usdc(to_solana_addr: str, amount: int):
    """
    Sends `amount` USDC (in base units, e.g. 6 decimals) from vault to given Solana address.
    """
    vault_kp = Keypair.from_secret_key(json.load(open(VAULT_KEYPAIR)))
    client   = Client(RPC_URL)

    # Build transfer_checked instruction
    tx = Transaction()
    tx.add(
        transfer_checked(
            program_id=TOKEN_PROGRAM_ID,
            source=VAULT_USDC_ACCOUNT,
            mint=USDC_MINT,
            dest=PublicKey(to_solana_addr),
            owner=vault_kp.public_key,
            amount=amount,
            decimals=6,
            signers=[]
        )
    )

    # Sign & send
    resp = client.send_transaction(tx, vault_kp)
    print("Sent USDC:", resp)


def poll_solana_deposits():
    """
    Poll Solana for new token-transfer signatures into VAULT_USDC_ACCOUNT.
    If we see a memo with a Nexus address, call mint_nusd().
    """
    client = Client(RPC_URL)
    # get up to 100 most recent signatures
    sigs = client.get_signatures_for_address(
        VAULT_USDC_ACCOUNT,
        limit=100,
        commitment="confirmed"
    )["result"]

    for entry in sigs:
        sig = entry["signature"]
        if sig in processed_sigs:
            continue

        # Fetch full tx details
        tx = client.get_transaction(sig, encoding="jsonParsed")["result"]
        if not tx:
            continue

        # 1) Find SPL token transfer to vault?
        for instr in tx["transaction"]["message"]["instructions"]:
            if instr.get("program") == "spl-token" and \
               instr["parsed"]["type"] == "transfer" and \
               instr["parsed"]["info"]["destination"] == str(VAULT_USDC_ACCOUNT):

                amount = int(instr["parsed"]["info"]["amount"])
                # 2) Find Memo instruction for Nexus address
                for ix in tx["transaction"]["message"]["instructions"]:
                    if ix.get("programId") == str(MEMO_PROGRAM_ID):
                        memo_data = bytes(ix["data"], "utf-8").decode()
                        # expected format e.g. "nexus:<NEXUS_PUBKEY>"
                        if memo_data.startswith("nexus:"):
                            nexus_addr = memo_data.split("nexus:")[1].strip()
                            print(f"→ Deposit of {amount} USDC; minting to {nexus_addr}")
                            mint_nusd(nexus_addr, amount)
                        else:
                            print("Skipping, bad memo:", memo_data)
                break

        processed_sigs.add(sig)
    save_state()


def main():
    print("🌐 Starting Solana vault monitor, RPC:", RPC_URL)
    try:
        while True:
            poll_solana_deposits()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("Shutting down…")
        save_state()


if __name__ == "__main__":
    main()
