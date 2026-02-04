from asyncio import timeout
import json
import base64
from time import time
from typing import Optional
import os
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


# --- Optional Helius JSON-RPC helpers -----------------------------------------------------------
def _helius_rpc_url() -> Optional[str]:
    """Build the Helius RPC URL from config or environment.
    Priority: config.HELIUS_RPC_URL -> env HELIUS_RPC_URL -> https://rpc.helius.xyz/?api-key=KEY
    """
    try:
        url = getattr(config, "HELIUS_RPC_URL", None) or os.getenv("HELIUS_RPC_URL")
        if url:
            return url
    except Exception:
        pass
    try:
        key = getattr(config, "HELIUS_API_KEY", None) or os.getenv("HELIUS_API_KEY")
        if key:
            return f"https://rpc.helius.xyz/?api-key={key}"
    except Exception:
        pass
    return None


def _helius_rpc_call(method: str, params=None, timeout_sec: Optional[float] = None):
    """Call a Helius JSON-RPC method and return .result.
    Raises on HTTP/RPC errors. Returns the `result` field when available, else the whole JSON.
    """
    url = _helius_rpc_url()
    if not url:
        raise RuntimeError("Helius RPC not configured: set HELIUS_RPC_URL or HELIUS_API_KEY")
    payload = {
        "jsonrpc": "2.0",
        "id": "swapService",
        "method": method,
        "params": params if params is not None else [],
    }
    to = timeout_sec if timeout_sec is not None else getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8)
    resp = requests.post(url, json=payload, timeout=to)
    resp.raise_for_status()
    js = resp.json()
    if isinstance(js, dict) and js.get("error"):
        raise RuntimeError(f"Helius RPC error: {js['error']}")
    if isinstance(js, dict) and "result" in js:
        return js["result"]
    return js


def helius_get_transactions_for_address(
    address: str,
    *,
    limit: int = 100,
    before: Optional[str] = None,
    until: Optional[str] = None,
    commitment: str = "confirmed",
    encoding: Optional[str] = None,
) -> list:
    """Fetch transactions for an address via Helius `getTransactionsForAddress`.

    Returns a list of enriched transaction objects (shape defined by Helius). If the method
    is unavailable or fails, callers can catch and fallback to core RPC.
    """
    lim = max(1, min(1000, int(limit)))
    opts: dict = {"limit": lim, "commitment": commitment}
    if before:
        opts["before"] = before
    if until:
        opts["until"] = until
    if encoding:
        opts["encoding"] = encoding
    # Helius expects params: [address, options]
    return _helius_rpc_call("getTransactionsForAddress", [address, opts]) or []


def core_get_transactions_for_address(
    address: str,
    *,
    limit: int = 100,
    before: Optional[str] = None,
    until: Optional[str] = None,
    commitment: str = "confirmed",
) -> list:
    """Fallback using core RPC: getSignaturesForAddress + getTransaction (jsonParsed).
    Returns a list of transaction JSONs similar to getTransaction results.
    """
    client = Client(config.RPC_URL)
    lim = max(1, min(1000, int(limit)))
    sig_args = {"limit": lim}
    if before:
        sig_args["before"] = before
    if until:
        sig_args["until"] = until
    # Fetch signatures
    sig_resp = _rpc_call(
        client.get_signatures_for_address,
        PublicKey.from_string(address),
        **sig_args,
        timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
    )
    sig_entries = _rpc_get_result(sig_resp) or []
    if not isinstance(sig_entries, list):
        return []
    sigs = [e.get("signature") for e in sig_entries if isinstance(e, dict) and e.get("signature")]
    out: list = []
    for sig in sigs:
        try:
            tx_resp = _rpc_call(
                client.get_transaction,
                sig,
                encoding="jsonParsed",
                timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
            )
            tx = _rpc_get_result(tx_resp)
            if tx:
                out.append(tx)
        except Exception:
            continue
    return out


def get_transactions_for_address(
    address: str,
    *,
    limit: int = 100,
    before: Optional[str] = None,
    until: Optional[str] = None,
    commitment: str = "confirmed",
    prefer: str = "helius",
) -> list:
    """Unified helper: try Helius RPC first (if configured), else fallback to core RPC.
    prefer: "helius" | "core"
    """
    if prefer == "helius":
        try:
            return helius_get_transactions_for_address(
                address,
                limit=limit,
                before=before,
                until=until,
                commitment=commitment,
            )
        except Exception:
            # Fallback to core
            pass
    return core_get_transactions_for_address(
        address,
        limit=limit,
        before=before,
        until=until,
        commitment=commitment,
    )


def fetch_incoming_usdc_deposits_via_helius(
    token_account_addr: str,
    since_ts: int,
    min_units: int = 0,
    limit: int = 200,
) -> list[tuple[str, int, str | None, str | None, int]]:
    """
    Fetch recent incoming USDC transfers to token_account_addr with memos.
    
    Performance comparison:
    - Helius: 1-2 API calls (enriched data with parsed tokenTransfers + memos)
    - Core RPC: N+1 calls (1 getSignaturesForAddress + N getTransaction calls)
    
    For 100 deposits, Helius is ~50-100x faster (1 call vs 101 calls).
    
    Returns a list of tuples: (signature, timestamp, memo, from_address, amount_usdc_units).
    Falls back to core RPC if Helius is not configured or fails.
    """
    # Try Helius first (fast path: 1-2 API calls for enriched data)
    helius_result = _fetch_deposits_helius(token_account_addr, since_ts, min_units, limit)
    if helius_result is not None:
        return helius_result
    
    # Fallback to core RPC (slow path: N+1 API calls)
    print("[HELIUS_FALLBACK] Helius unavailable, using core RPC (slower)")
    return _fetch_deposits_core_rpc(token_account_addr, since_ts, min_units, limit)


def _fetch_deposits_helius(
    token_account_addr: str,
    since_ts: int,
    min_units: int,
    limit: int,
) -> list[tuple[str, int, str | None, str | None, int]] | None:
    """
    Internal: Fetch deposits using Helius enriched RPC.
    Returns None if Helius is not configured or fails (signals fallback needed).
    """
    # Check if Helius is configured
    if not _helius_rpc_url():
        return None
    
    try:
        collected: list[tuple[str, int, str | None, str | None, int]] = []
        page_size = max(1, min(1000, limit))
        before: str | None = None
        usdc_mint = str(getattr(config, "USDC_MINT"))

        while len(collected) < limit:
            txs = helius_get_transactions_for_address(
                str(token_account_addr),
                limit=page_size,
                before=before,
                commitment="confirmed",
                encoding=None,
            ) or []
            if not txs:
                break

            for tx in txs:
                # Timestamp (Helius uses 'timestamp'); fall back to 'blockTime'
                ts = int(tx.get("timestamp") or tx.get("blockTime") or 0)
                if ts and ts <= int(since_ts):
                    # Older than our waterline; stop scanning further pages.
                    txs = []
                    break

                # Find incoming USDC transfer to our ATA
                for t in (tx.get("tokenTransfers") or []):
                    if str(t.get("toTokenAccount")) != str(token_account_addr):
                        continue
                    if str(t.get("mint")) != usdc_mint:
                        continue

                    # Amount in base units (tokenAmount is base units in enriched)
                    amt_str = str(t.get("tokenAmount") or "0")
                    try:
                        amount_units = int(amt_str)
                    except Exception:
                        # Fallback if tokenAmount was UI; convert with decimals if present
                        from decimal import Decimal, ROUND_DOWN
                        decimals = int(t.get("decimals") or 6)
                        amount_units = int((Decimal(amt_str) * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))

                    if amount_units < int(min_units):
                        continue

                    # Memo from enriched 'memos', else scan instructions (rare fallback)
                    memo = None
                    memos = tx.get("memos") or []
                    if memos:
                        memo = memos[0]
                    else:
                        for ix in (tx.get("instructions") or []):
                            if str(ix.get("programId")) == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr":
                                data = ix.get("data")
                                if isinstance(data, str) and data:
                                    memo = data
                                    break

                    sig = tx.get("signature") or None
                    from_addr = t.get("fromUserAccount") or t.get("fromTokenAccount") or None
                    if sig and ts:
                        collected.append((sig, ts, memo, from_addr, amount_units))
                    break  # one incoming transfer per tx to our ATA is typical

            # Prepare pagination
            last_sig = txs[-1].get("signature") if txs else None
            if not last_sig or len(txs) < page_size:
                break
            before = last_sig

        # Oldest-first ordering to match DB processing semantics
        collected.sort(key=lambda r: r[1])
        return collected
    except Exception as e:
        print(f"[HELIUS_ERROR] {e}")
        return None  # Signal fallback needed


def _fetch_deposits_core_rpc(
    token_account_addr: str,
    since_ts: int,
    min_units: int,
    limit: int,
) -> list[tuple[str, int, str | None, str | None, int]]:
    """
    Internal: Fetch deposits using core Solana RPC (N+1 queries fallback).
    Slower but works without Helius API key.
    """
    try:
        client = Client(config.RPC_URL)
        collected: list[tuple[str, int, str | None, str | None, int]] = []
        usdc_mint = str(getattr(config, "USDC_MINT"))
        
        # Step 1: Get signatures (1 API call)
        sig_resp = _rpc_call(
            client.get_signatures_for_address,
            PublicKey.from_string(token_account_addr),
            limit=min(1000, limit * 2),  # Fetch extra since some may be filtered
            timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
        )
        sig_entries = _rpc_get_value(sig_resp) or []
        if not isinstance(sig_entries, list):
            return []
        
        # Step 2: For each signature, fetch full transaction (N API calls)
        for entry in sig_entries:
            if len(collected) >= limit:
                break
                
            if not isinstance(entry, dict):
                continue
            
            block_time = entry.get("blockTime")
            if block_time is None or block_time <= since_ts:
                continue  # Skip old transactions
            
            sig = entry.get("signature")
            if not sig:
                continue
            
            try:
                tx_resp = _rpc_call(
                    client.get_transaction,
                    sig,
                    encoding="jsonParsed",
                    timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 12),
                )
                tx_data = _rpc_get_result(tx_resp)
                if not tx_data or not isinstance(tx_data, dict):
                    continue
                
                # Parse transaction for USDC transfer and memo
                meta = tx_data.get("meta", {})
                pre_balances = meta.get("preTokenBalances", [])
                post_balances = meta.get("postTokenBalances", [])
                
                # Calculate vault delta
                vault_delta = 0
                from_addr = None
                for post in post_balances:
                    if not isinstance(post, dict):
                        continue
                    if post.get("mint") == usdc_mint and post.get("owner") == str(config.SOL_MAIN_ACCOUNT):
                        post_amount = int(post.get("uiTokenAmount", {}).get("amount", "0"))
                        for pre in pre_balances:
                            if (isinstance(pre, dict) and
                                pre.get("accountIndex") == post.get("accountIndex") and
                                pre.get("mint") == post.get("mint")):
                                pre_amount = int(pre.get("uiTokenAmount", {}).get("amount", "0"))
                                vault_delta = post_amount - pre_amount
                                break
                        break
                
                if vault_delta < min_units:
                    continue
                
                # Extract sender from preTokenBalances (account that decreased)
                for pre in pre_balances:
                    if isinstance(pre, dict) and pre.get("mint") == usdc_mint:
                        pre_amt = int(pre.get("uiTokenAmount", {}).get("amount", "0"))
                        for post in post_balances:
                            if (isinstance(post, dict) and 
                                post.get("accountIndex") == pre.get("accountIndex")):
                                post_amt = int(post.get("uiTokenAmount", {}).get("amount", "0"))
                                if post_amt < pre_amt:  # This account sent tokens
                                    from_addr = pre.get("owner")
                                    break
                        if from_addr:
                            break
                
                # Extract memo from instructions
                memo = None
                tx_obj = tx_data.get("transaction", {})
                msg = tx_obj.get("message", {})
                insts = msg.get("instructions", [])
                for ix in insts:
                    prog = ix.get("program")
                    if prog and str(prog) == "spl-memo":
                        memo = ix.get("parsed", {})
                        if isinstance(memo, str):
                            break
                        memo = None
                
                collected.append((sig, block_time, memo, from_addr, vault_delta))
                
            except Exception:
                continue
        
        # Oldest-first ordering
        collected.sort(key=lambda r: r[1])
        return collected
        
    except Exception as e:
        print(f"[CORE_RPC_ERROR] {e}")
        return []
    

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
    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc, status, txid in unprocessed[:limit]:
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
                # Bug #12 fix: Track the fee (entire deposit amount is kept as fee)
                state_db.add_fee_entry(
                    sig=sig,
                    txid=None,
                    kind="micro_deposit_fee",
                    amount_usdc_units=int(amount_usdc),
                    amount_usdd_units=None
                )
                state_db.mark_processed_sig(sig, timestamp, int(amount_usdc), None, 0, "processed, amount after fees <= 0", None)
                state_db.remove_unprocessed_sig(sig)
                proc_count_mic += 1
                continue

            # 7. Debit USDD if valid
            # Bug #9 fix: Use next_reference() which atomically increments to prevent duplicate references
            reference = state_db.next_reference()
            result = nexus_client.debit_usdd_with_txid(nexus_address, net_amount, reference)
            if result[0]:
                proc_count_swap += 1
                txid = str(result[1]) if result[1] else None
                if txid:
                    state_db.update_unprocessed_sig_txid(sig, txid)
                    state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")
                else:
                    # Debit succeeded but no txid returned - should not happen, mark for refund
                    state_db.update_unprocessed_sig_status(sig, "to be refunded")
                    proc_count_refund += 1
                    print(f"[DEBIT_NO_TXID] sig={sig} - debit succeeded but no txid, marking for refund")
            else:
                # Debit failed - mark for refund to prevent infinite retry loop
                state_db.update_unprocessed_sig_status(sig, "to be refunded")
                proc_count_refund += 1
                print(f"[DEBIT_FAILED] sig={sig} - Nexus debit failed, marking for refund")
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
    
    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in unprocessed[:limit]:
        
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
                # Bug #12 fix: Track the fee (entire deposit amount is kept as fee for failed refunds)
                state_db.add_fee_entry(
                    sig=sig,
                    txid=None,
                    kind="refund_micro_fee",
                    amount_usdc_units=int(amount_usdc_units),
                    amount_usdd_units=None
                )
                state_db.mark_processed_sig(sig, timestamp, amount_usdc_units, None, 0, "processed, amount after fees <= 0", None)
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
                # Bug #12 fix: Track the flat refund fee
                refund_fee = int(amount_usdc_units) - int(net_amount)
                if refund_fee > 0:
                    state_db.add_fee_entry(
                        sig=sig,
                        txid=None,
                        kind="refund_flat_fee",
                        amount_usdc_units=refund_fee,
                        amount_usdd_units=None
                    )
                state_db.update_unprocessed_sig_status(sig, "refund sent, awaiting confirmation")
                state_db.mark_refunded_sig(sig, timestamp, from_address, amount_usdc_units, memo, sig_r[1], net_amount, "awaiting confirmation")
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
    
    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in unprocessed[:limit]:
        
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
                # Bug #8 fix: Track the quarantine fee
                quarantine_fee = int(amount_usdc_units) - int(net_amount)
                if quarantine_fee > 0:
                    state_db.add_fee_entry(
                        sig=sig,
                        txid=None,
                        kind="quarantine_flat_fee",
                        amount_usdc_units=quarantine_fee,
                        amount_usdd_units=None
                    )
                # Bug #8 fix: Update status but DON'T remove from unprocessed yet
                # Confirmation will be checked by check_quarantine_confirmations()
                state_db.update_unprocessed_sig_status(sig, "quarantine sent, awaiting confirmation")
                state_db.mark_quarantined_sig(sig, timestamp, from_address, amount_usdc_units, memo, sig_q[1], net_amount, "awaiting confirmation")
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
        
        # Determine the actual destination token account
        # If destination is a wallet address (not a token account), derive the ATA
        dest_token_account = destination
        if not _is_token_account_for_mint(destination, config.USDC_MINT):
            # Destination is a wallet address - derive its USDC ATA
            try:
                owner_pubkey = PublicKey.from_string(destination)
                dest_token_account = str(get_associated_token_address(owner=owner_pubkey, mint=config.USDC_MINT))
            except Exception as e:
                print(f"Error deriving ATA for {destination}: {e}")
                return False, None
        
        # Build transfer instruction
        ix = transfer_checked(
            program_id=TOKEN_PROGRAM_ID,
            source=config.VAULT_USDC_ACCOUNT,
            mint=config.USDC_MINT,
            dest=PublicKey.from_string(dest_token_account),
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


def check_sig_confirmations(min_confirmations: int, timeout: float) -> int:
    """Check confirmation status for USDC refund transactions.
    
    This function queries refunded_sigs for entries awaiting confirmation,
    then checks the REFUND signature (not the original deposit signature)
    for on-chain confirmation status.
    """
    # Query refunded_sigs table for entries awaiting confirmation
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, timestamp, from_address, amount_usdc_units, memo, refund_sig, refunded_units, status
        FROM refunded_sigs
        WHERE status = 'awaiting confirmation'
        LIMIT 1000
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return 0
    
    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    client = Client(config.RPC_URL)

    for deposit_sig, timestamp, from_address, amount_usdc_units, memo, refund_sig, refunded_units, status in rows:
        # Timeout check
        current_time = time.monotonic()
        if current_time - time_start > timeout:
            break
        
        # Skip if no refund signature was recorded
        if not refund_sig:
            print(f"[REFUND_CHECK] No refund_sig for deposit {deposit_sig}, skipping")
            continue
        
        # Get confirmation status for the REFUND signature (not the deposit signature)
        try:
            resp = _rpc_call(client.get_signature_statuses, [refund_sig])
            val = _rpc_get_value(resp)
            confirmations = None
            if val and isinstance(val, list) and len(val) > 0:
                status_info = val[0]
                if isinstance(status_info, dict):
                    confirmations = status_info.get('confirmations')
        except Exception as e:
            print(f"Error checking confirmations for refund {refund_sig}: {e}")
            continue
        
        if confirmations is not None and confirmations < min_confirmations:
            continue
        elif confirmations is not None and confirmations >= min_confirmations:
            # Confirmed: update refunded_sigs status
            try:
                # Update refunded_sigs status to confirmed
                conn = state_db.sqlite3.connect(state_db.DB_PATH)
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE refunded_sigs SET status = 'refund_confirmed' WHERE sig = ?
                """, (deposit_sig,))
                conn.commit()
                conn.close()
                
                # Also remove from unprocessed_sigs if still present
                state_db.remove_unprocessed_sig(deposit_sig)
                
                processed_count += 1
                print(f"Refund confirmed for deposit {deposit_sig}, refund tx {refund_sig} with {confirmations} confirmations")
            except Exception as e:
                print(f"Error marking refund confirmed for {deposit_sig}: {e}")
        # If confirmations is None, skip (not confirmed yet)

    return processed_count


def check_quarantine_confirmations(min_confirmations: int, timeout: float) -> int:
    """Check confirmation status for USDC quarantine transactions.
    
    Bug #8 fix: This function queries quarantined_sigs for entries awaiting confirmation,
    then checks the QUARANTINE signature for on-chain confirmation status.
    Only after confirmation is the deposit removed from unprocessed_sigs.
    """
    # Query quarantined_sigs table for entries awaiting confirmation
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, timestamp, from_address, amount_usdc_units, memo, quarantine_sig, quarantined_units, status
        FROM quarantined_sigs
        WHERE status = 'awaiting confirmation'
        LIMIT 1000
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return 0
    
    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    client = Client(config.RPC_URL)

    for deposit_sig, timestamp, from_address, amount_usdc_units, memo, quarantine_sig, quarantined_units, status in rows:
        # Timeout check
        current_time = time.monotonic()
        if current_time - time_start > timeout:
            break
        
        # Skip if no quarantine signature was recorded
        if not quarantine_sig:
            print(f"[QUARANTINE_CHECK] No quarantine_sig for deposit {deposit_sig}, skipping")
            continue
        
        # Get confirmation status for the QUARANTINE signature (not the deposit signature)
        try:
            resp = _rpc_call(client.get_signature_statuses, [quarantine_sig])
            val = _rpc_get_value(resp)
            confirmations = None
            if val and isinstance(val, list) and len(val) > 0:
                status_info = val[0]
                if isinstance(status_info, dict):
                    confirmations = status_info.get('confirmations')
        except Exception as e:
            print(f"Error checking confirmations for quarantine {quarantine_sig}: {e}")
            continue
        
        if confirmations is not None and confirmations < min_confirmations:
            continue
        elif confirmations is not None and confirmations >= min_confirmations:
            # Confirmed: update quarantined_sigs status
            try:
                # Update quarantined_sigs status to confirmed
                conn = state_db.sqlite3.connect(state_db.DB_PATH)
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE quarantined_sigs SET status = 'quarantine_confirmed' WHERE sig = ?
                """, (deposit_sig,))
                conn.commit()
                conn.close()
                
                # Now safe to remove from unprocessed_sigs
                state_db.remove_unprocessed_sig(deposit_sig)
                
                processed_count += 1
                print(f"Quarantine confirmed for deposit {deposit_sig}, quarantine tx {quarantine_sig} with {confirmations} confirmations")
            except Exception as e:
                print(f"Error marking quarantine confirmed for {deposit_sig}: {e}")
        # If confirmations is None, skip (not confirmed yet)


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


def scan_memos_since_timestamp(since_timestamp: int, max_signatures: int = 10000) -> dict:
    """Scan ALL signatures for vault USDC account since timestamp, collecting structured memos.
    
    Args:
        since_timestamp: Unix timestamp to scan from
        max_signatures: Maximum signatures to fetch (safety limit)
    
    Returns:
        dict: {
            'nexus_txids': { txid: signature },
            'refund_sigs': { deposit_sig: refund_tx_sig },
            'quarantined_sigs': { sig: True },
            'deposits': [ { sig, from, amount, memo, timestamp } ],  # Unprocessed deposits
        }
    
    Note: This can be slow for large time ranges. Use waterline properly to minimize scan range.
    """
    out = {"nexus_txids": {}, "refund_sigs": {}, "quarantined_sigs": {}, "deposits": []}
    
    try:
        client = Client(config.RPC_URL)
    except Exception:
        return out
    
    # Fetch signatures in batches, working backwards from most recent
    fetched_count = 0
    before_sig = None
    batch_size = 1000  # Max allowed by Solana RPC
    
    while fetched_count < max_signatures:
        try:
            # Get batch of signatures
            params = {"limit": min(batch_size, max_signatures - fetched_count)}
            if before_sig:
                params["before"] = before_sig
            
            resp = _rpc_call(
                client.get_signatures_for_address,
                PublicKey.from_string(str(config.VAULT_USDC_ACCOUNT)),
                **params
            )
            entries = _rpc_get_value(resp)
            
            if not isinstance(entries, list) or not entries:
                break  # No more signatures
            
            reached_waterline = False
            for ent in entries:
                try:
                    sig = ent.get("signature") if isinstance(ent, dict) else None
                    block_time = ent.get("blockTime") if isinstance(ent, dict) else None
                    
                    if not sig:
                        continue
                    
                    # Check if we've reached the waterline
                    if block_time and block_time < since_timestamp:
                        reached_waterline = True
                        break
                    
                    # Fetch transaction details
                    try:
                        tx_resp = _rpc_call(
                            client.get_transaction,
                            sig,
                            encoding="jsonParsed",
                            timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 6)
                        )
                    except Exception:
                        continue
                    
                    tx_val = _rpc_get_result(tx_resp)
                    if not tx_val:
                        continue
                    
                    # Extract memos
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
                    
                    # Fallback: check logs
                    if not memos:
                        try:
                            logs = (tx_val.get("meta") or {}).get("logMessages") if isinstance(tx_val, dict) else None
                            if isinstance(logs, list):
                                for lg in logs:
                                    if isinstance(lg, str) and any(x in lg for x in ["nexus_txid:", "refundSig:", "quarantinedSig:"]):
                                        memos.append(lg)
                        except Exception:
                            pass
                    
                    # Process memos
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
                        
                        if "quarantinedSig:" in m:
                            try:
                                qsig = m.split("quarantinedSig:", 1)[1].strip().split()[0]
                                if qsig:
                                    out["quarantined_sigs"][qsig] = True
                            except Exception:
                                pass
                    
                    # Check if this is a deposit to vault (no processed marker)
                    # We'll collect ALL deposits and let caller filter out processed ones
                    # This is simpler than trying to determine processing status here
                    
                except Exception:
                    continue
            
            fetched_count += len(entries)
            before_sig = entries[-1].get("signature") if entries else None
            
            # Stop conditions
            if reached_waterline:
                break
            if len(entries) < batch_size:
                break  # No more signatures available
            
        except Exception as e:
            print(f"Error scanning signatures: {e}")
            break
    
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
