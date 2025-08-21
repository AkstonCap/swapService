import json
import base64
from typing import Optional
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey as PublicKey
from solders.keypair import Keypair
from solders.instruction import Instruction as TransactionInstruction, AccountMeta
from solders.hash import Hash
from solders.transaction import Transaction, VersionedTransaction
from solders.message import Message
from struct import pack

from . import config

# SPL Token and ATA Program IDs (constants)
TOKEN_PROGRAM_ID = PublicKey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = PublicKey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def _rpc_to_json(resp):
    try:
        if isinstance(resp, dict):
            return resp
        tj = getattr(resp, "to_json", None)
        if callable(tj):
            return json.loads(tj())
    except Exception:
        pass

def _rpc_get_result(resp):
    js = _rpc_to_json(resp)
    if isinstance(js, dict):
        return js.get("result") or js.get("value") or js
    # Fallback to .value on typed responses
    val = getattr(resp, "value", None)
    return val if val is not None else resp


def _rpc_get_value(resp):
    res = _rpc_get_result(resp)
    if isinstance(res, dict):
        v = res.get("value", None)
        return v if v is not None else res
    return res


def transfer_checked(*, program_id: PublicKey, source: PublicKey, mint: PublicKey, dest: PublicKey,
                     owner: PublicKey, amount: int, decimals: int, signers: list) -> TransactionInstruction:
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
    return TransactionInstruction(program_id=program_id, accounts=keys, data=data)


def _memo_ix(memo: str | None) -> TransactionInstruction | None:
    if not memo:
        return None
    try:
        memo_prog = PublicKey.from_string("Memo111111111111111111111111111111111111111")
        data = bytes(memo, "utf-8")
        return TransactionInstruction(program_id=memo_prog, accounts=[], data=data)
    except Exception:
        return None


def get_associated_token_address(*, owner: PublicKey, mint: PublicKey) -> PublicKey:
    # In solders, find_program_address is on Pubkey
    seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
    ata, _ = PublicKey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
    return ata


def _get_latest_blockhash_str(client: Client) -> Optional[str]:
    try:
        resp = client.get_latest_blockhash()
    except Exception:
        return None
    # Prefer to_json for stability
    js = _rpc_to_json(resp)
    if isinstance(js, dict):
        try:
            return (((js.get("result") or {}).get("value") or {}).get("blockhash"))
        except Exception:
            pass
    # Fallback to typed .value
    try:
        val = getattr(resp, "value", None)
        if val is not None:
            bh = getattr(val, "blockhash", None)
            if bh is not None:
                return str(bh)
    except Exception:
        pass
    return None


def _build_and_send_legacy_tx(instructions: list[TransactionInstruction], kp: Keypair) -> str:
    """Build, sign (legacy) and send a transaction using solders; return signature string."""
    client = Client(config.RPC_URL)
    bh = _get_latest_blockhash_str(client)
    if not bh:
        raise RuntimeError("Failed to fetch recent blockhash")
    recent = Hash.from_string(bh)
    tx = Transaction.new_signed_with_payer(instructions, kp.pubkey(), [kp], recent)
    send_resp = client.send_raw_transaction(bytes(tx))
    sig = _rpc_get_result(send_resp)
    if not isinstance(sig, str):
        raise RuntimeError(f"Failed to send tx, unexpected response: {send_resp}")
    try:
        client.confirm_transaction(sig, commitment="confirmed")
    except Exception:
        pass
    return sig


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
        resp = client.get_balance(kp.pubkey())
        val = _rpc_get_value(resp)
        if isinstance(val, dict):
            return int(val.get("value") or 0)
        if isinstance(val, int):
            return int(val)
        return 0
    except Exception:
        return 0


def get_token_account_balance(token_account_addr: str) -> int:
    try:
        client = Client(config.RPC_URL)
        resp = client.get_token_account_balance(PublicKey.from_string(token_account_addr))
        val = _rpc_get_value(resp)
        amt = None
        if isinstance(val, dict):
            amt = val.get("amount")
        return int(amt or 0)
    except Exception:
        return 0


def transfer_usdc_between_accounts(source_token_account: str, dest_token_account: str, amount_base_units: int) -> bool:
    """Transfer USDC between two token accounts owned by the vault wallet."""
    try:
        if amount_base_units <= 0:
            return True
        kp = load_vault_keypair()
        ix = transfer_checked(
            program_id=TOKEN_PROGRAM_ID,
            source=PublicKey.from_string(source_token_account),
            mint=config.USDC_MINT,
            dest=PublicKey.from_string(dest_token_account),
            owner=kp.pubkey(),
            amount=amount_base_units,
            decimals=config.USDC_DECIMALS,
            signers=[],
        )
        sig = _build_and_send_legacy_tx([ix], kp)
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
        owner = kp.pubkey()

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

        tx_bytes = base64.b64decode(swap_tx_b64)
        vtx = VersionedTransaction.from_bytes(tx_bytes)
        vtx.sign([kp])
        raw = bytes(vtx)
        send_resp = client.send_raw_transaction(raw)
        sig = _rpc_get_result(send_resp)
        if not isinstance(sig, str):
            print("[fees] Jupiter: unexpected send response")
            return False
        try:
            client.confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"[fees] Jupiter swap sent: {sig}")
        return True
    except Exception as e:
        print(f"[fees] Jupiter swap error: {e}")
        return False


# Memo instruction helper removed


def ensure_send_usdc(to_owner_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send USDC base units to a Solana owner address. Requires recipient ATA to already exist.
    If memo is provided, attach it to the transaction for idempotency tracing.
    """
    try:
        kp = load_vault_keypair()
        owner = PublicKey.from_string(to_owner_addr)
        dest_ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        client = Client(config.RPC_URL)
        ata_info = _rpc_get_value(client.get_account_info(dest_ata))
        if ata_info is None:
            print("Recipient USDC ATA is missing; not creating it. Ask recipient to initialize their USDC ATA.")
            return False

        ixs = [
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=dest_ata,
                owner=kp.pubkey(),
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        ]
        mix = _memo_ix(memo)
        if mix:
            ixs.append(mix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        print(f"Sent USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error sending USDC: {e}")
        return False


def _is_token_account_for_mint(token_account_addr: str, mint: PublicKey) -> bool:
    """Return True if the address is an SPL token account for the given mint."""
    try:
        client = Client(config.RPC_URL)
        resp = client.get_account_info(PublicKey.from_string(token_account_addr), encoding="jsonParsed")
        val = _rpc_get_value(resp)
        if not val or not isinstance(val, dict):
            return False
        if val.get("owner") != str(TOKEN_PROGRAM_ID):
            return False
        data = val.get("data", {})
        parsed = data.get("parsed") if isinstance(data, dict) else None
        if not isinstance(parsed, dict):
            return False
        info = parsed.get("info") or {}
        if not isinstance(info, dict):
            return False
        mint_str = info.get("mint")
        return str(mint_str) == str(mint)
    except Exception:
        return False


def send_usdc_to_token_account_with_sig(dest_token_account_addr: str, amount_base_units: int, memo: str | None = None) -> tuple[bool, str | None]:
    """Send USDC base units directly to an existing USDC token account address."""
    try:
        if amount_base_units <= 0:
            return True, None
        if not _is_token_account_for_mint(dest_token_account_addr, config.USDC_MINT):
            print("Destination is not a valid USDC token account")
            return False, None
        kp = load_vault_keypair()
        dest = PublicKey.from_string(dest_token_account_addr)
        ixs = [
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=dest,
                owner=kp.pubkey(),
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        ]
        mix = _memo_ix(memo)
        if mix:
            ixs.append(mix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        print(f"Sent USDC to token account tx sig: {sig}")
        return True, sig
    except Exception as e:
        print(f"Error sending USDC to token account: {e}")
        return False, None


def ensure_send_usdc_to_token_account(dest_token_account_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    ok, _sig = send_usdc_to_token_account_with_sig(dest_token_account_addr, amount_base_units, memo)
    return ok


def ensure_send_usdc_owner_or_ata(addr_maybe_owner_or_token: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send USDC to either a Solana owner address (deriving ATA) or a direct USDC token account address."""
    try:
        if _is_token_account_for_mint(addr_maybe_owner_or_token, config.USDC_MINT):
            return ensure_send_usdc_to_token_account(addr_maybe_owner_or_token, amount_base_units, memo)
        return ensure_send_usdc(addr_maybe_owner_or_token, amount_base_units, memo)
    except Exception as e:
        print(f"Error in ensure_send_usdc_owner_or_ata: {e}")
        return False


def was_usdc_sent_for_nexus_tx(nexus_txid: str, addr_maybe_owner_or_token: str, lookback: int = 40) -> bool:
    """Deprecated: memo-based check removed. Always return False to avoid memo reliance."""
    return False


def is_valid_usdc_token_account(addr: str) -> bool:
    """Public helper: True if addr is a valid SPL token account for the USDC mint."""
    return _is_token_account_for_mint(addr, config.USDC_MINT)


## Memo extraction removed.


def has_usdc_ata(owner_addr: str) -> bool:
    """Return True if the owner's USDC ATA exists."""
    try:
        client = Client(config.RPC_URL)
        owner = PublicKey.from_string(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        info = _rpc_get_value(client.get_account_info(ata))
        return info is not None
    except Exception:
        return False


def refund_usdc_to_source(source_token_account: str, amount_base_units: int, reason: str) -> bool:
    """Refund USDC back to the sender's token account (no memo required)."""
    try:
        kp = load_vault_keypair()
        dest_token_acc = PublicKey.from_string(source_token_account)
        ixs = [
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=dest_token_acc,
                owner=kp.pubkey(),
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            ),
        ]
        sig = _build_and_send_legacy_tx(ixs, kp)
        print(f"Refunded USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error refunding USDC: {e}")
        return False


def move_usdc_to_quarantine(amount_base_units: int, note: str | None = None) -> bool:
    """Move USDC from vault token account to a self-owned quarantine USDC token account (does not affect backing ratio)."""
    try:
        dest = getattr(config, "USDC_QUARANTINE_ACCOUNT", None)
        if not dest:
            print("Quarantine account not configured; skipping move")
            return False
        kp = load_vault_keypair()
        ixs = [
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=PublicKey.from_string(dest),
                owner=kp.pubkey(),
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        ]
        sig = _build_and_send_legacy_tx(ixs, kp)
        print(f"Moved USDC to quarantine, sig: {sig}")
        return True
    except Exception as e:
        print(f"Error moving USDC to quarantine: {e}")
    return False
