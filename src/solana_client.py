import json
from typing import Optional
import base64
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey as PublicKey
from solders.keypair import Keypair
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
    TOKEN_PROGRAM_ID = PublicKey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ASSOCIATED_TOKEN_PROGRAM_ID = PublicKey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

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
        # In solders, find_program_address is on Pubkey
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
            AccountMeta(pubkey=PublicKey.from_string("11111111111111111111111111111111"), is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PublicKey.from_string("SysvarRent111111111111111111111111111111111"), is_signer=False, is_writable=False),
        ]
        return TransactionInstruction(program_id=ASSOCIATED_TOKEN_PROGRAM_ID, keys=keys, data=b"")


def _get_vault_secret_bytes() -> bytes:
    with open(config.VAULT_KEYPAIR_PATH, "r") as f:
        data = json.load(f)
    if isinstance(data, list):
        return bytes(data)
    raise ValueError("Unsupported keypair format; expected JSON array of ints")

def load_vault_keypair() -> Keypair:
    return Keypair.from_bytes(_get_vault_secret_bytes())

def load_vault_solders_keypair():
    return load_vault_keypair()

def get_vault_sol_balance() -> int:
    """Return vault wallet SOL balance in lamports."""
    try:
        client = Client(config.RPC_URL)
        kp = load_vault_keypair()
        bal = client.get_balance(kp.public_key)
        return int((bal or {}).get("result", {}).get("value", 0))
    except Exception:
        return 0

def get_token_account_balance(token_account_addr: str) -> int:
    try:
        client = Client(config.RPC_URL)
        resp = client.get_token_account_balance(PublicKey.from_string(token_account_addr))
        amt = (((resp or {}).get("result") or {}).get("value") or {}).get("amount")
        return int(amt or 0)
    except Exception:
        return 0

def transfer_usdc_between_accounts(source_token_account: str, dest_token_account: str, amount_base_units: int) -> bool:
    """Transfer USDC between two token accounts owned by the vault wallet."""
    try:
        if amount_base_units <= 0:
            return True
        kp = load_vault_keypair()
        client = Client(config.RPC_URL)
        tx = Transaction()
        tx.fee_payer = kp.public_key
        tx.add(
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=PublicKey.from_string(source_token_account),
                mint=config.USDC_MINT,
                dest=PublicKey.from_string(dest_token_account),
                owner=kp.public_key,
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        )
        resp = client.send_transaction(tx, kp)
        sig = resp.get("result") if isinstance(resp, dict) else resp
        try:
            client.confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"USDC transfer token->token sig: {sig}")
        return True
    except Exception as e:
        print(f"Error transferring USDC accounts: {e}")
        return False

def swap_usdc_for_sol_via_jupiter(amount_usdc_base_units: int, slippage_bps: int = 50) -> bool:
    """Swap USDC->SOL using Jupiter. Returns True on success.
    Requires: config.USDC_MINT and vault keypair with USDC token account.
    """
    try:
        if amount_usdc_base_units <= 0:
            return False
        client = Client(config.RPC_URL)
        kp = load_vault_keypair()
        owner = kp.public_key

        # Jupiter Quote API v6
        base = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": str(config.USDC_MINT),
            "outputMint": "So11111111111111111111111111111111111111112",
            "amount": str(int(amount_usdc_base_units)),
            "slippageBps": str(int(slippage_bps)),
            "onlyDirectRoutes": "false",
        }
        q = requests.get(base, params=params, timeout=15)
        q.raise_for_status()
        qd = q.json()
        routes = qd.get("data") or []
        if not routes:
            print("[fees] Jupiter: no route found")
            return False
        route = routes[0]

        # Jupiter Swap API v6: get swap transaction
        swap_url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "quoteResponse": route,
            "userPublicKey": str(owner),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 0,
            "dynamicComputeUnitLimit": True,
        }
        s = requests.post(swap_url, json=payload, timeout=20)
        s.raise_for_status()
        sd = s.json()
        swap_tx_b64 = sd.get("swapTransaction")
        if not swap_tx_b64:
            print("[fees] Jupiter: missing swapTransaction")
            return False

        from solders.transaction import VersionedTransaction
        s_kp = load_vault_solders_keypair()
        tx_bytes = base64.b64decode(swap_tx_b64)
        vtx = VersionedTransaction.from_bytes(tx_bytes)
        vtx.sign([s_kp])
        raw = bytes(vtx)
        sig = client.send_raw_transaction(raw).get("result")
        try:
            client.confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"[fees] Jupiter swap sent: {sig}")
        return True
    except Exception as e:
        print(f"[fees] Jupiter swap error: {e}")
        return False

def ensure_send_usdc(to_owner_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send USDC base units to a Solana owner address. Requires recipient ATA to already exist.
    If memo is provided, attach it to the transaction for idempotency tracing.
    """
    try:
        kp = load_vault_keypair()
        client = Client(config.RPC_URL)
        owner = PublicKey.from_string(to_owner_addr)
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
        if memo:
            try:
                tx.add(_create_memo_ix(memo))
            except Exception:
                pass
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


def was_usdc_sent_for_nexus_tx(nexus_txid: str, to_owner_addr: str, lookback: int = 40) -> bool:
    """Check recent vault USDC outgoing txs for a memo 'NEXUS_TX:<txid>'.
    Returns True if found a confirmed tx to the recipient ATA with that memo.
    """
    try:
        client = Client(config.RPC_URL)
        sigs_resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, limit=max(10, lookback))
        sig_list = [r.get("signature") for r in (sigs_resp.get("result") or []) if r.get("signature")]
        if not sig_list:
            return False
        # Resolve recipient ATA once
        owner = PublicKey.from_string(to_owner_addr)
        dest_ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        target_memo = f"NEXUS_TX:{nexus_txid}"
        for sig in sig_list:
            tx = client.get_transaction(sig, encoding="jsonParsed").get("result")
            if not tx:
                continue
            instrs = tx["transaction"]["message"]["instructions"]
            memo_text = None
            try:
                memo_text = extract_memo_from_instructions(instrs)
            except Exception:
                memo_text = None
            if memo_text != target_memo:
                continue
            # check transfer destination matches dest_ata
            for ix in instrs:
                if ix.get("program") == "spl-token" and ix.get("parsed"):
                    p = ix["parsed"]
                    if p.get("type") in ("transfer", "transferChecked"):
                        info = p.get("info", {})
                        if info.get("destination") == str(dest_ata):
                            return True
        return False
    except Exception:
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
        owner = PublicKey.from_string(owner_addr)
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
        dest_token_acc = PublicKey.from_string(source_token_account)
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
