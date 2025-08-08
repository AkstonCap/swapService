import json
from typing import Optional
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
from . import config


def load_vault_keypair() -> Keypair:
    with open(config.VAULT_KEYPAIR_PATH, "r") as f:
        data = json.load(f)
    if isinstance(data, list):
        return Keypair.from_secret_key(bytes(data))
    raise ValueError("Unsupported keypair format; expected JSON array of ints")


def ensure_send_usdc(to_owner_addr: str, amount_base_units: int) -> bool:
    """Send USDC base units to a Solana owner address. Creates ATA if missing."""
    try:
        kp = load_vault_keypair()
        client = Client(config.RPC_URL)
        owner = PublicKey(to_owner_addr)
        dest_ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        ata_info = client.get_account_info(dest_ata).get("result", {}).get("value")

        tx = Transaction()
        tx.fee_payer = kp.public_key
        if ata_info is None:
            tx.add(create_associated_token_account(payer=kp.public_key, owner=owner, mint=config.USDC_MINT))

        tx.add(
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=dest_ata,
                owner=kp.public_key,
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        )
        resp = client.send_transaction(tx, kp)
        sig = resp.get("result") if isinstance(resp, dict) else resp
        try:
            Client(config.RPC_URL).confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"Sent USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error sending USDC: {e}")
        return False


def extract_memo_from_instructions(instructions) -> Optional[str]:
    import base64
    for ix in instructions:
        if ix.get("program") == "spl-memo":
            parsed = ix.get("parsed")
            if isinstance(parsed, dict):
                memo = parsed.get("info", {}).get("memo")
                if isinstance(memo, str):
                    return memo
            elif isinstance(parsed, str):
                return parsed
        if ix.get("programId") == str(config.MEMO_PROGRAM_ID) and "data" in ix:
            try:
                return base64.b64decode(ix["data"]).decode("utf-8")
            except Exception:
                continue
    return None
