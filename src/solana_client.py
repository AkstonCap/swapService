import json
from typing import Optional
from solana.rpc.api import Client
from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.transaction import Transaction, TransactionInstruction, AccountMeta
from . import config

# Optional dependency: spl.token (Python SPL Token helpers). Provide a fallback if missing.
try:
    from spl.token.constants import TOKEN_PROGRAM_ID
    from spl.token.instructions import (
        transfer_checked,
        get_associated_token_address,
        create_associated_token_account,
    )
    _HAS_SPL_TOKEN = True
except Exception:
    _HAS_SPL_TOKEN = False
    from struct import pack

    # SPL Token Program ID
    TOKEN_PROGRAM_ID = PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ASSOCIATED_TOKEN_PROGRAM_ID = PublicKey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

    def transfer_checked(*, program_id: PublicKey, source: PublicKey, mint: PublicKey, dest: PublicKey,
                         owner: PublicKey, amount: int, decimals: int, signers: list):
        """Minimal TransferChecked instruction builder (no multisig signers support)."""
        if signers:
            raise NotImplementedError("Multisig owners not supported without spl.token installed")
        data = pack("<BQB", 12, int(amount), int(decimals))  # 12 = TransferChecked
        keys = [
            AccountMeta(pubkey=source, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=dest, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ]
        return TransactionInstruction(program_id=program_id, keys=keys, data=data)

    def get_associated_token_address(*, owner: PublicKey, mint: PublicKey) -> PublicKey:
        seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
        ata, _ = PublicKey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
        return ata

    def create_associated_token_account(*, payer: PublicKey, owner: PublicKey, mint: PublicKey) -> TransactionInstruction:
        ata = get_associated_token_address(owner=owner, mint=mint)
        keys = [
            AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PublicKey("11111111111111111111111111111111"), is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PublicKey("SysvarRent111111111111111111111111111111111"), is_signer=False, is_writable=False),
        ]
        return TransactionInstruction(program_id=ASSOCIATED_TOKEN_PROGRAM_ID, keys=keys, data=b"")


def load_vault_keypair() -> Keypair:
    with open(config.VAULT_KEYPAIR_PATH, "r") as f:
        data = json.load(f)
    if isinstance(data, list):
        return Keypair.from_secret_key(bytes(data))
    raise ValueError("Unsupported keypair format; expected JSON array of ints")


def ensure_send_usdc(to_owner_addr: str, amount_base_units: int) -> bool:
    """Send USDC base units to a Solana owner address. Requires recipient ATA to already exist."""
    try:
        kp = load_vault_keypair()
        client = Client(config.RPC_URL)
        owner = PublicKey(to_owner_addr)
        tx = Transaction()
        tx.fee_payer = kp.public_key

        dest_ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        ata_info = client.get_account_info(dest_ata).get("result", {}).get("value")
        if ata_info is None:
            print("Recipient USDC ATA is missing; not creating it. Ask recipient to initialize their USDC ATA.")
            return False

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


def has_usdc_ata(owner_addr: str) -> bool:
    """Return True if the owner's USDC ATA exists."""
    try:
        client = Client(config.RPC_URL)
        owner = PublicKey(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        info = client.get_account_info(ata).get("result", {}).get("value")
        return info is not None
    except Exception:
        return False


def _create_memo_ix(text: str) -> TransactionInstruction:
    data = text.encode("utf-8")
    return TransactionInstruction(program_id=config.MEMO_PROGRAM_ID, keys=[], data=data)


def refund_usdc_to_source(source_token_account: str, amount_base_units: int, reason: str) -> bool:
    """Refund USDC back to the sender's token account with a memo reason."""
    try:
        kp = load_vault_keypair()
        client = Client(config.RPC_URL)
        dest_token_acc = PublicKey(source_token_account)

        memo = reason if len(reason) <= 120 else reason[:117] + "..."

        tx = Transaction()
        tx.fee_payer = kp.public_key
        tx.add(
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=dest_token_acc,
                owner=kp.public_key,
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        )
        tx.add(_create_memo_ix(memo))

        resp = client.send_transaction(tx, kp)
        sig = resp.get("result") if isinstance(resp, dict) else resp
        try:
            Client(config.RPC_URL).confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"Refunded USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error refunding USDC: {e}")
        return False
