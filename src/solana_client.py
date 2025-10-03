from asyncio import timeout
import json
import base64
from time import time
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
import threading, queue
from . import state_db, nexus_client
import time

from . import config

# Expose last sent signature for higher-level idempotency logging (refund / quarantine / debit flows)
last_sent_sig: str | None = None

# SPL Token and ATA Program IDs (constants)
TOKEN_PROGRAM_ID = PublicKey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = PublicKey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def fetch_filtered_token_account_transaction_history(token_account_addr: str, wline: int, amount: int, timeout: float = 10.0) -> list:
    """Fetch the transaction history of a Solana token account.
    
    Args:
        token_account_addr: The token account address as a string.
        wline: The line number to filter transactions.
        amount: The minimum amount to filter transactions.

    Returns:
        A list of filtered transactions (dict), or an empty list on error.
    """
    try:
        client = Client(config.RPC_URL)
        time_start = time.monotonic()
        time_current = time_start

        # Step 1: Get recent signatures for the token account
        sig_resp = _rpc_call(client.get_signatures_for_address, PublicKey.from_string(token_account_addr), limit=1000)
        sig_entries = _rpc_get_value(sig_resp) or []

        # Filter transactions based on wline and amount
        filtered_txs = []
        for entry in sig_entries:

            if not isinstance(entry, dict):
                continue

            block_time = entry.get("blockTime")
            if block_time is None or block_time < wline:
                continue
            
            sig = entry.get("signature")
            if not sig:
                continue

            # Step 2: Fetch the full transaction for this signature
            tx_resp = _rpc_call(client.get_transaction, sig, encoding="jsonParsed")
            tx_data = _rpc_get_result(tx_resp)
            if not tx_data or not isinstance(tx_data, dict):
                continue

            # Extract block time (confirm it matches)
            tx_block_time = tx_data.get("blockTime")
            if tx_block_time != block_time:
                continue

            # Step 3: Calculate deposit amount from pre/post balances
            meta = tx_data.get("meta", {})
            pre_balances = meta.get("preTokenBalances", [])
            post_balances = meta.get("postTokenBalances", [])

            vault_delta = 0
            for post in post_balances:
                if not isinstance(post, dict):
                    continue

                # Find the vault ATA balance change (assuming token_account_addr is the vault ATA)
                if post.get("mint") == str(config.USDC_MINT) and post.get("owner") == str(config.SOL_MAIN_ACCOUNT):
                    post_amount = int(post.get("uiTokenAmount", {}).get("amount", "0"))
                    # Find matching pre balance
                    for pre in pre_balances:
                        if (isinstance(pre, dict) and
                            pre.get("accountIndex") == post.get("accountIndex") and
                            pre.get("mint") == post.get("mint")):
                            pre_amount = int(pre.get("uiTokenAmount", {}).get("amount", "0"))
                            vault_delta = post_amount - pre_amount
                            break
                    break

            # Filter by minimum amount (positive delta = deposit to vault)
            if vault_delta >= amount:
                filtered_txs.append({
                    "signature": sig,
                    "blocktime": block_time,
                    "amount": vault_delta,
                })

            time_current = time.monotonic()
            if time_current - time_start > timeout:
                break

        # 4. Sort by blocktime ascending (oldest first)
        filtered_txs.sort(key=lambda x: x["blocktime"])

        return filtered_txs
    
    except Exception as e:
        print(f"Error fetching token account transaction history: {e}")
        return []
    

def process_filtered_deposits(filtered_txs: list, db_check: bool = True, timeout: float = 8.0) -> int:
    """
    Process a list of filtered transactions from fetch_filtered_token_account_transaction_history.
    For each sig, fetch full tx details (memo, from_address), check database status, 
    and add to unprocessed if not existing.
    
    Args:
        filtered_txs: List of dicts with 'signature', 'blocktime', 'amount'
        db_check: If True, check and skip if already processed/unprocessed
        
    Returns:
        Number of new unprocessed entries added
    """
    if not filtered_txs:
        return 0
    
    added_count = 0
    client = Client(config.RPC_URL)
    time_start = time.monotonic()
    time_current = time_start

    for tx_info in filtered_txs:
        sig = tx_info.get("signature")
        blocktime = tx_info.get("blocktime")
        usdc_amount_units = tx_info.get("amount")

        if not sig:
            continue

        # Check database if enabled
        if db_check:
            from . import state_db
            if state_db.is_processed_sig(sig) or state_db.is_unprocessed_sig(sig) or state_db.is_quarantined_sig(sig) or state_db.is_refunded_sig(sig):
                continue # Quarantined = processed??

        # Fetch full transaction for memo and from_address
        try:
            tx_resp = _rpc_call(client.get_transaction, sig, encoding="jsonParsed")
            tx_data = _rpc_get_result(tx_resp)
            if not tx_data or not isinstance(tx_data, dict):
                continue

            # Extract memo (from instructions or logs), amount check and source
            memo = None
            amtcheck = None
            source = None
            try:
                tx_obj = tx_data.get("transaction", {})
                msg = tx_obj.get("message", {})
                insts = msg.get("instructions", [])
                for ix in insts:
                    prog = ix.get("program")
                    if prog and str(prog) == "spl-memo":
                        memo = ix.get("parsed", {})
                    elif prog and str(prog) == "spl-token":
                        parsed = ix.get("parsed", {})
                        info = parsed.get("info", {})
                        source = info.get("source", {})
                        tokenAmount = info.get("tokenAmount", {})
                        amtcheck = tokenAmount.get("amount", 0)

            except Exception:
                pass

            # Add to unprocessed
            from . import state_db
            state_db.add_unprocessed_sig(sig, blocktime, memo or "", source, usdc_amount_units, "ready for processing", None)
            added_count += 1

            time_current = time.monotonic()
            if time_current - time_start > timeout:
                break

        except Exception as e:
            print(f"Error processing transaction {sig}: {e}")
            continue

    return added_count


def process_unprocessed_usdc_deposits(limit: int = 1000, timeout: float = 8.0) -> list:
    """
    Process unprocessed deposit signatures from DB.
    Fetches oldest unprocessed sigs up to limit, validates memo format "nexus:<address>",
    checks Nexus USDD account validity, runs idempotency checks, debits USDD if valid,
    and updates status accordingly.
    
    Returns: Number of sigs processed.
    """
    from . import state_db, nexus_client

    # 1. Fetch unprocessed sigs (oldest first)
    unprocessed = state_db.filter_unprocessed_sigs({
        'status': 'ready for processing',
        'limit': limit
    })
    if not unprocessed:
        return 0
    
    proc_count_swap = 0
    proc_count_refund = 0
    proc_count_quar = 0
    proc_count_mic = 0

    processing_secs = 0
    timestamp_start = time.monotonic()
    current_timestamp = time.monotonic()
    for sig, timestamp, memo, from_address, amount_usdc in unprocessed[:limit]:
        processing_secs = current_timestamp - timestamp_start
        if processing_secs >= timeout:
            break
        try:
            # 2. Check existing status "ready for processing"
            if state_db.get_unprocessed_sig_status(sig) != "ready for processing":
                continue

            # 3. Run idempotency checks: already processed?
            if state_db.is_processed_sig(sig) or state_db.is_quarantined_sig(sig) or state_db.is_refunded_sig(sig):
                state_db.remove_unprocessed_sig(sig)
                continue

            # 4. Validate memo format
            if not memo or not memo.lower().startswith("nexus:"):
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid memo
                proc_count_refund += 1
                continue

            nexus_address = memo.split(":", 1)[1].strip()
            if not nexus_address:
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid memo
                proc_count_refund += 1
                continue

            # 5. Check Nexus USDD account validity
            if not nexus_client.is_valid_usdd_account(nexus_address):
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid account
                proc_count_refund += 1
                continue

            # 6. Calculate amount minus fees
            net_amount = nexus_client.get_usdd_send_amount(amount_usdc)
            if net_amount <= 0:
                state_db.mark_processed_sig(sig, timestamp, f"amount after fees <= 0")
                state_db.remove_unprocessed_sig(sig)
                proc_count_mic += 1
                continue

            # 7. Debit USDD if valid
            reference = state_db.get_latest_reference() + 1
            result = nexus_client.debit_usdd_with_txid(nexus_address, net_amount, reference)
            if result[0]:
                proc_count_swap += 1
                txid = str(result[1]) if result[1] else None
                if txid:
                    state_db.update_unprocessed_sig_txid(sig, txid)
                    state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")
                elif not txid:
                    state_db.update_unprocessed_sig_status(sig, "to be refunded")
                    proc_count_refund += 1
        except Exception as e:
            print(f"Error processing deposit {sig}: {e}")
            continue

        current_timestamp = time.monotonic()

    return [proc_count_swap, proc_count_refund, proc_count_quar, proc_count_mic]


def _is_token_account_for_mint(token_account_addr: str, mint: PublicKey) -> bool:
    """Return True if the address is an SPL token account for the given mint."""
    try:
        client = Client(config.RPC_URL)
        resp = _rpc_call(client.get_account_info, PublicKey.from_string(token_account_addr), encoding="jsonParsed")
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
    

def _is_solana_wallet_with_ata(wallet_address: str) -> bool:
    """Return True if the address is a Solana wallet with an existing USDC ATA."""
    try:
        client = Client(config.RPC_URL)

        # 1. Validate the wallet address exists (basic check)
        wallet_resp = _rpc_call(client.get_account_info, PublicKey.from_string(wallet_address))
        wallet_val = _rpc_get_value(wallet_resp)
        if not wallet_val or not isinstance(wallet_val, dict):
            return False # wallet doesn't exist
        
        # 2. Derive the expected USDC ATA address
        owner = PublicKey.from_string(wallet_address)
        ata_address = get_associated_token_address(owner, config.USDC_MINT)

        # 3. Check if the ATA account exists and is valid USDC token account
        ata_resp = _rpc_call(client.get_account_info, ata_address, encoding="jsonParsed")
        ata_val = _rpc_get_value(ata_resp)
        if not ata_val or not isinstance(ata_val, dict):
            return False # ATA doesn't exist
        
        # Confirmed it's owned by Token Program and has correct mint
        if ata_val.get("owner") != str(TOKEN_PROGRAM_ID):
            return False

        data = ata_val.get("data", {})
        parsed = data.get("parsed") if isinstance(data, dict) else None
        if not isinstance(parsed, dict):
            return False
        
        info = parsed.get("info") or {}
        if not isinstance(info, dict):
            return False

        mint_str = info.get("mint")
        return str(mint_str) == str(config.USDC_MINT)

    except Exception:
        return False


def process_usdc_deposits_refunding(limit: int = 1000, timeout: float = 8.0) -> int:

    from . import state_db, nexus_client

    # 1. Fetch unprocessed sigs (oldest first)
    unprocessed = state_db.filter_unprocessed_sigs({
        'status_like': '%to be refunded%',
        'limit': limit
    })
    if not unprocessed:
        return 0
    
    processed_count = 0
    processing_secs = 0
    timestamp_start = time.monotonic()
    current_timestamp = time.monotonic()
    
    for sig, timestamp, from_address, memo, amount_usdc_units in unprocessed[:limit]:
        
        processing_secs = current_timestamp - timestamp_start
        if processing_secs >= timeout:
            break

        try:
            # 2. Check status "to be refunded"
            if state_db.get_unprocessed_sig_status(sig) != "to be refunded":
                continue

            # 3. Run idempotency checks: already processed?
            if state_db.is_processed_sig(sig) or state_db.is_quarantined_sig(sig):
                state_db.remove_unprocessed_sig(sig)
                continue
            
            # 4. Check refund net amount
            net_amount = amount_usdc_units - config.FLAT_FEE_USDC_UNITS_REFUND
            if net_amount <= 0:
                state_db.mark_processed_sig(sig, timestamp, amount_usdc_units, None, 0, f"processed, amount after fees <= 0", None)
                # (sig, timestamp, amount_usdc_units, txid, amount_usdd_debited, status, reference)
                state_db.remove_unprocessed_sig(sig)
                continue

            # 5. Validate from_address whether existing USDC ATA account or Solana wallet with existing ATA account
            if not from_address:
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                continue
            if not _is_token_account_for_mint(from_address, config.USDC_MINT) and not _is_solana_wallet_with_ata(from_address):
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                continue

            # 6. Process the refund
            sig_r = send_usdc(from_address, net_amount, memo=f"refund:{sig}")
            if sig_r[0]:
                processed_count += 1
                state_db.update_unprocessed_sig_status(sig, "refund sent, awaiting confirmation")
                state_db.mark_refunded_sig(sig, timestamp, from_address, amount_usdc_units, memo, sig_r[1], net_amount, f"awaiting confirmation")
            if not sig_r[0]:
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                continue

        except Exception as e:
            print(f"Error processing deposit {sig}: {e}")
            continue

        current_timestamp = time.monotonic()

    return processed_count


def process_usdc_deposits_quarantine(limit: int = 1000, timeout: float = 25.0) -> int:

    from . import state_db, nexus_client

    # 1. Fetch unprocessed sigs to be quarantined (oldest first)
    unprocessed = state_db.filter_unprocessed_sigs({
        'status_like': '%to be quarantined%',
        'limit': limit
    })
    if not unprocessed:
        return 0
    
    processed_count = 0
    processing_secs = 0
    timestamp_start = time.monotonic()
    current_timestamp = time.monotonic()
    
    for sig, timestamp, from_address, memo, amount_usdc_units in unprocessed[:limit]:
        
        processing_secs = current_timestamp - timestamp_start
        if processing_secs >= timeout:
            break

        try:
            # 2. Check status "to be quarantined"
            if state_db.get_unprocessed_sig_status(sig) != "to be quarantined":
                continue

            # 3. Run idempotency checks: already processed?
            if state_db.is_processed_sig(sig) or state_db.is_refunded_sig(sig):
                state_db.remove_unprocessed_sig(sig)
                continue
            
            # 4. Check quarantine net amount
            net_amount = amount_usdc_units - config.FLAT_FEE_USDC_UNITS_REFUND  

            # 5. Process the quarantine
            sig_q = send_usdc(config.USDC_QUARANTINE_ACCOUNT, net_amount, memo=f"quarantine:{sig}")
            if sig_q[0]:
                processed_count += 1
                state_db.mark_quarantined_sig(sig, timestamp, from_address, amount_usdc_units, memo, sig_q[1], net_amount, f"quarantine sent, awaiting confirmation")
                state_db.remove_unprocessed_sig(sig)
            if not sig_q[0]:
                state_db.update_unprocessed_sig_status(sig, "quarantine failed")
                continue

        except Exception as e:
            print(f"Error processing deposit {sig}: {e}")
            continue

        current_timestamp = time.monotonic()

    return processed_count


def send_usdc(destination: str, amount_base_units: int, memo: str | None = None) -> tuple[bool, str | None]:
    """
    Unified function to send USDC from vault to various destinations.
    
    Args:
        destination: Target address (owner address or token account address)
        amount_base_units: Amount in base units
        memo: Optional memo string for transaction
        
    Returns:
        Tuple of (success: bool, signature: str | None)
    """
    if amount_base_units <= 0:
        return True, None
    
    try:
        kp = load_vault_keypair()
        client = Client(config.RPC_URL)
        
        # Build transfer instruction
        ix = transfer_checked(
            program_id=TOKEN_PROGRAM_ID,
            source=config.VAULT_USDC_ACCOUNT,
            mint=config.USDC_MINT,
            dest=destination,
            owner=kp.pubkey(),
            amount=amount_base_units,
            decimals=config.USDC_DECIMALS,
            signers=[],
        )
        
        # Build instructions list
        ixs = [ix]
        mix = _memo_ix(memo)
        if mix:
            ixs.append(mix)
        
        # Send transaction
        sig = _build_and_send_legacy_tx(ixs, kp)
        
        print(f"Sent USDC tx sig: {sig}")
        return True, sig
        
    except Exception as e:
        print(f"Error sending USDC: {e}")
        return False, None


def check_sig_confirmations(min_confirmations: int, timeout: float) -> bool:
    
    sigs = state_db.filter_unprocessed_sigs({
        'status': 'refund sent, awaiting_confirmation',
        'limit': 1000
    })
    if not sigs:
        return 0
    
    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    client = Client(config.RPC_URL)

    for sig, timestamp, amount_usdc_units, from_address, memo in sigs:
        # Timeout check
        current_time = time.monotonic()
        if current_time - time_start > timeout:
            break
        
        # Get confirmation status for this signature
        try:
            resp = _rpc_call(client.get_signature_statuses, [sig])
            val = _rpc_get_value(resp)
            confirmations = None
            if val and isinstance(val, list) and len(val) > 0:
                status_info = val[0]
                if isinstance(status_info, dict):
                    confirmations = status_info.get('confirmations')
                    # If confirmations is None, tx is not yet confirmed; if 0 or more, it's the count
        except Exception as e:
            print(f"Error checking confirmations for {sig}: {e}")
            continue
        
        if confirmations is not None and confirmations < min_confirmations:
            continue
        elif confirmations is not None and confirmations >= min_confirmations:
            # Confirmed: update status and mark as refunded
            try:
                # Update unprocessed status to confirmed
                state_db.update_unprocessed_sig_status(sig, "refund_confirmed")
                
                # Mark as refunded (assuming mark_refunded_sig takes: sig, timestamp, from_address, amount_usdc_units, memo, txid, net_amount, status)
                # Note: net_amount is not directly available here; using amount_usdc_units as approximation or fetch from DB if stored separately
                net_amount = amount_usdc_units  # Adjust if you store net_amount separately
                state_db.mark_refunded_sig(sig, timestamp, from_address, amount_usdc_units, memo, None, net_amount, "refund_confirmed")
                
                # Remove from unprocessed
                state_db.remove_unprocessed_sig(sig)
                
                processed_count += 1
                print(f"Refund confirmed for sig {sig} with {confirmations} confirmations")
            except Exception as e:
                print(f"Error marking refund confirmed for {sig}: {e}")
        # If confirmations is None, skip (not confirmed yet)

    return processed_count


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


def _rpc_call(method, *args, timeout: Optional[float] = None, **kwargs):
    """Run an RPC client method in a thread with timeout to avoid hangs."""
    if timeout is None:
        timeout = getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8)
    q: "queue.Queue[tuple[bool, object]]" = queue.Queue(maxsize=1)
    def _runner():
        try:
            res = method(*args, **kwargs)
            q.put((True, res))
        except Exception as e:  # pragma: no cover
            q.put((False, e))
    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    try:
        ok, val = q.get(timeout=timeout)
    except Exception:  # timeout
        raise TimeoutError(f"RPC call timeout after {timeout}s: {getattr(method, '__name__', method)}")
    if ok:
        return val
    raise val  # re-raise exception from thread


def _get_latest_blockhash_str(client: Client) -> Optional[str]:
    try:
        resp = _rpc_call(client.get_latest_blockhash, timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))
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
    """Build, sign (legacy) and send a transaction using solders; return signature string.
    Wrapped with per-step RPC timeouts.
    """
    client = Client(config.RPC_URL)
    bh = _get_latest_blockhash_str(client)
    if not bh:
        raise RuntimeError("Failed to fetch recent blockhash")
    recent = Hash.from_string(bh)
    tx = Transaction.new_signed_with_payer(instructions, kp.pubkey(), [kp], recent)
    send_resp = _rpc_call(client.send_raw_transaction, bytes(tx), timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))
    sig = _rpc_get_result(send_resp)
    if not isinstance(sig, str):
        raise RuntimeError(f"Failed to send tx, unexpected response: {send_resp}")
    # Fire-and-forget confirmation with timeout; ignore errors
    try:
        _rpc_call(client.confirm_transaction, sig, commitment="confirmed", timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))
    except Exception:
        pass
    global last_sent_sig
    last_sent_sig = sig
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
        resp = _rpc_call(client.get_balance, kp.pubkey())
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
        resp = _rpc_call(client.get_token_account_balance, PublicKey.from_string(token_account_addr))
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


def check_timestamp_unpr_sigs() -> int | None:
    """
    Find the block time (timestamp) of the oldest unprocessed sig in DB and propose it as a new waterline.
    This can be used for recovery or waterline adjustment based on unprocessed entries.
    Returns the proposed waterline timestamp (int), or None if no unprocessed sigs found.
    """
    from . import state_db, state
    
    # Fetch the oldest unprocessed sig (limit=1, sorted by timestamp ASC)
    unprocessed = state_db.filter_unprocessed_sigs({'limit': 1})
    if not unprocessed:
        return None
    
    # Extract timestamp from the oldest sig (index 1 in tuple)
    oldest_timestamp = unprocessed[0][1]
    
    # Propose the waterline as oldest_timestamp - 1 to ensure the oldest sig is included in the next poll
    new_waterline = oldest_timestamp - 1
    
    # Propose the new waterline
    state_db.propose_solana_waterline(new_waterline)
    print(f"Proposed new waterline: {new_waterline} (based on oldest unprocessed sig timestamp: {oldest_timestamp})")
    
    return new_waterline


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




def ensure_send_usdc(to_owner_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send USDC base units to a Solana owner address. Requires recipient ATA to already exist.
    If memo is provided, attach it to the transaction for idempotency tracing.
    """
    try:
        kp = load_vault_keypair()
        owner = PublicKey.from_string(to_owner_addr)
        dest_ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        client = Client(config.RPC_URL)
        ata_info = _rpc_get_value(_rpc_call(client.get_account_info, dest_ata))
        if ata_info is None:
            print("Recipient USDC ATA is missing; not creating it. Ask recipient to initialize their USDC ATA.")
            return False
        if amount_base_units <= 0:
            return True
        ixs = [transfer_checked(
            program_id=TOKEN_PROGRAM_ID,
            source=config.VAULT_USDC_ACCOUNT,
            mint=config.USDC_MINT,
            dest=dest_ata,
            owner=kp.pubkey(),
            amount=amount_base_units,
            decimals=config.USDC_DECIMALS,
            signers=[],
        )]
        mix = _memo_ix(memo)
        if mix:
            ixs.append(mix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        print(f"Sent USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error sending USDC: {e}")
        return False





def send_usdc_to_token_account_with_sig(dest_token_account_addr: str, amount_base_units: int, memo: str | None = None) -> tuple[bool, str | None]:
    """Send USDC base units directly to an existing USDC token account address."""
    # Idempotency short‑circuit for memo formats we recognize
    if memo:
        # Legacy numeric reference
        if memo.isdigit():
            ref_key = f"nexus_ref_{memo}"
            if state_db.is_processed_txid(ref_key):
                return True, None
        # New structured memo nexus_txid:<txid>
        elif memo.startswith("nexus_txid:"):
            txid_part = memo.split(":", 1)[1]
            proc_key = f"nexus_txid:{txid_part}"
            if state_db.is_processed_txid(proc_key):
                return True, None
    
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
        
        # Mark processed based on memo form for future idempotency
        try:
            if memo:
                if memo.isdigit():
                    state_db.mark_processed_txid(f"nexus_ref_{memo}", timestamp=int(__import__('time').time()), amount_usdd=0, from_address="", to_address="", owner="", sig="", status="usdc_sent")
                elif memo.startswith("nexus_txid:"):
                    txid_part = memo.split(":", 1)[1]
                    state_db.mark_processed_txid(f"nexus_txid:{txid_part}", timestamp=int(__import__('time').time()), amount_usdd=0, from_address="", to_address="", owner="", sig="", status="usdc_sent")
        except Exception:
            pass
        
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


def is_valid_usdc_token_account(addr: str) -> bool:
    """Public helper: True if addr is a valid SPL token account for the USDC mint."""
    return _is_token_account_for_mint(addr, config.USDC_MINT)


def find_signature_with_memo(memo: str, search_limit: int = 50) -> Optional[str]:
    """Best-effort lookup of a recently sent signature containing the given memo string.
    Searches recent signatures for the vault USDC token account (and optionally the vault owner)
    and inspects transaction instructions for the Memo program.
    Returns first matching signature or None.
    """
    if not memo:
        return None
    try:
        client = Client(config.RPC_URL)
    except Exception:
        return None
    addresses: list[str] = []
    try:
        if config.VAULT_USDC_ACCOUNT:
            addresses.append(str(config.VAULT_USDC_ACCOUNT))
    except Exception:
        pass
    # Optionally include vault wallet (owner) if present
    try:
        if getattr(config, "VAULT_OWNER", None):
            addresses.append(str(config.VAULT_OWNER))
    except Exception:
        pass
    seen = set()
    for addr in addresses:
        try:
            try:
                resp = _rpc_call(client.get_signatures_for_address, PublicKey.from_string(addr), limit=search_limit)
            except TimeoutError:
                continue
            js = _rpc_get_value(resp)
            if isinstance(js, list):
                sig_list = js
            else:
                sig_list = []
        except Exception:
            continue
        for entry in sig_list:
            try:
                sig = entry.get("signature") if isinstance(entry, dict) else None
            except Exception:
                sig = None
            if not sig or sig in seen:
                continue
            seen.add(sig)
            # Fetch transaction to inspect memo instruction
            try:
                try:
                    tx_resp = _rpc_call(
                        client.get_transaction,
                        sig,
                        encoding="jsonParsed",
                        timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8)),
                    )
                except TimeoutError:
                    continue
                tx_val = _rpc_get_result(tx_resp)
            except Exception:
                continue
            # Various shapes; try to drill into transaction.message.instructions
            try:
                tx_obj = tx_val.get("transaction") if isinstance(tx_val, dict) else None
                msg = (tx_obj or {}).get("message") if isinstance(tx_obj, dict) else None
                insts = (msg or {}).get("instructions") if isinstance(msg, dict) else None
                if isinstance(insts, list):
                    for ix in insts:
                        try:
                            # jsonParsed may embed program info differently
                            prog = ix.get("programId") or ix.get("program")
                            if prog and (str(prog).startswith("Memo111") or "memo" in str(prog).lower()):
                                # Data may be base64 or raw string
                                data = ix.get("data")
                                if isinstance(data, list):
                                    # Sometimes list like [b64, encoding]
                                    if data:
                                        data = data[0]
                                if isinstance(data, str):
                                    # Try direct compare
                                    if data == memo:
                                        return sig
                                    # Try base64 decode
                                    try:
                                        decoded = base64.b64decode(data + "==").decode("utf-8", errors="ignore")
                                        if decoded == memo:
                                            return sig
                                    except Exception:
                                        pass
                        except Exception:
                            continue
            except Exception:
                continue
            # Fallback: inspect log messages
            try:
                meta = tx_val.get("meta") if isinstance(tx_val, dict) else None
                logs = (meta or {}).get("logMessages") if isinstance(meta, dict) else None
                if isinstance(logs, list):
                    for lg in logs:
                        if isinstance(lg, str) and memo in lg:
                            return sig
            except Exception:
                continue
    return None


def scan_recent_memos(search_limit: int = 400) -> dict:
    """Scan recent signatures for vault USDC account collecting structured memos.
    Returns dict: {
        'nexus_txids': { txid: signature },
        'refund_sigs': { deposit_sig: refund_tx_sig },
    }
    Best effort; ignores errors.
    """
    out = {"nexus_txids": {}, "refund_sigs": {}}
    try:
        client = Client(config.RPC_URL)
    except Exception:
        return out
    try:
        resp = _rpc_call(client.get_signatures_for_address, PublicKey.from_string(str(config.VAULT_USDC_ACCOUNT)), limit=search_limit)
    except Exception:
        return out
    entries = _rpc_get_value(resp)
    if not isinstance(entries, list):
        return out
    for ent in entries:
        try:
            sig = ent.get("signature") if isinstance(ent, dict) else None
            if not sig:
                continue
            # Fetch transaction (short timeout)
            try:
                tx_resp = _rpc_call(client.get_transaction, sig, encoding="jsonParsed", timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 6))
            except Exception:
                continue
            tx_val = _rpc_get_result(tx_resp)
            # Inspect instructions for memo
            try:
                tx_obj = tx_val.get("transaction") if isinstance(tx_val, dict) else None
                msg = (tx_obj or {}).get("message") if isinstance(tx_obj, dict) else None
                insts = (msg or {}).get("instructions") if isinstance(msg, dict) else None
            except Exception:
                insts = None
            memos: list[str] = []
            if isinstance(insts, list):
                for ix in insts:
                    try:
                        prog = ix.get("programId") or ix.get("program")
                        if prog and str(prog).startswith("Memo111"):
                            data = ix.get("data")
                            if isinstance(data, list) and data:
                                data = data[0]
                            if isinstance(data, str):
                                memos.append(data)
                    except Exception:
                        continue
            # Fallback logs
            if not memos:
                try:
                    logs = (tx_val.get("meta") or {}).get("logMessages") if isinstance(tx_val, dict) else None
                    if isinstance(logs, list):
                        for lg in logs:
                            if isinstance(lg, str) and ("nexus_txid:" in lg or "refundSig:" in lg):
                                memos.append(lg)
                except Exception:
                    pass
            for m in memos:
                if "nexus_txid:" in m:
                    try:
                        txid = m.split("nexus_txid:", 1)[1].strip().split()[0]
                        if txid and txid not in out["nexus_txids"]:
                            out["nexus_txids"][txid] = sig
                    except Exception:
                        pass
                if "refundSig:" in m:
                    try:
                        dsig = m.split("refundSig:", 1)[1].strip().split()[0]
                        if dsig and dsig not in out["refund_sigs"]:
                            out["refund_sigs"][dsig] = sig
                    except Exception:
                        pass
        except Exception:
            continue
    return out


## Memo extraction removed.


def has_usdc_ata(owner_addr: str) -> bool:
    """Return True if the owner's USDC ATA exists."""
    try:
        client = Client(config.RPC_URL)
        owner = PublicKey.from_string(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        info = _rpc_get_value(_rpc_call(client.get_account_info, ata))
        return info is not None
    except Exception:
        return False


def derive_usdc_ata(owner_addr: str) -> str | None:
    """Derive the expected USDC ATA address for a given owner (string form). Returns None on failure."""
    try:
        owner = PublicKey.from_string(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        return str(ata)
    except Exception:
        return None


def refund_usdc_to_source(source_token_account: str, amount_base_units: int, reason: str, deposit_sig: str | None = None) -> bool:
    """Refund USDC back to the sender's token account.
    Adds memo refundSig:<deposit_sig> if deposit_sig provided for idempotent replay detection.
    """
    # Check if this refund was already processed by checking the reason for signature context
    if ":" in reason and len(reason.split(":")) >= 2:
        potential_sig = reason.split(":")[-1]
        if len(potential_sig) > 40 and state_db.is_refunded_sig(potential_sig):
            return True  # Already refunded this signature
    
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
        memo_ix = None
        if deposit_sig:
            memo_ix = _memo_ix(f"refundSig:{deposit_sig}")
        if memo_ix:
            ixs.append(memo_ix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        print(f"Refunded USDC tx sig: {sig}")  # retain basic trace
        return True
    except Exception as e:
        print(f"Error refunding USDC: {e}")
        return False


def move_usdc_to_quarantine(amount_base_units: int, note: str | None = None, deposit_sig: str | None = None) -> bool:
    """Move USDC to quarantine with structured memo for later idempotency.
    Memo precedence:
      quarantinedSig:<deposit_sig>
      quarantined:<note>
      quarantined
    """
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
        memo_txt = None
        if deposit_sig:
            memo_txt = f"quarantinedSig:{deposit_sig}"
        elif note:
            memo_txt = f"quarantined:{note}"
        else:
            memo_txt = "quarantined"
        mix = _memo_ix(memo_txt)
        if mix:
            ixs.append(mix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        print(f"Moved USDC to quarantine, sig: {sig} memo={memo_txt}")
        return True
    except Exception as e:
        print(f"Error moving USDC to quarantine: {e}")
    return False
