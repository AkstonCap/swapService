from asyncio import timeout
import json
import base64
import logging
from time import time
from typing import Any, Optional
import os
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey as PublicKey
from solders.keypair import Keypair
from solders.signature import Signature
from solders.instruction import Instruction as TransactionInstruction, AccountMeta
from solders.hash import Hash
from solders.transaction import Transaction, VersionedTransaction
from solders.message import Message
from struct import pack
import threading, queue
from . import state_db, nexus_client, nexus_memo
import time

from . import config, structured_logging


_LOG = structured_logging.get_logger("swapService.solana_client")


def _log(event: str, *, level: int = logging.INFO, **fields) -> None:
    """Best-effort secret-safe diagnostics that cannot alter payout state transitions."""
    try:
        structured_logging.emit(_LOG, level, event, **fields)
    except Exception:
        pass


# Expose last sent signature for higher-level idempotency logging (refund / quarantine / debit flows)
last_sent_sig: str | None = None


class PayoutCapExceeded(Exception):
    """Raised when a send is refused by the rolling 24h payout cap.

    Distinct from a send FAILURE: the payment is fine, we are just throttled. Callers
    must leave the item's status untouched and retry on a later cycle - treating this
    like a failure would divert a legitimate refund into quarantine over a temporary cap.
    """

# SPL Token and ATA Program IDs (constants)
TOKEN_PROGRAM_ID = PublicKey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = PublicKey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


# Process-wide RPC client so HTTP keep-alive / connection pooling is reused across
# calls instead of opening a new TCP/TLS session on every RPC.
_shared_client: Optional[Client] = None
_shared_client_url: Optional[str] = None


def _get_client() -> Client:
    """Return a shared RPC client (recreated only if the configured URL changes)."""
    global _shared_client, _shared_client_url
    url = config.RPC_URL
    if _shared_client is None or _shared_client_url != url:
        _shared_client = Client(url)
        _shared_client_url = url
    return _shared_client


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


def _deposit_commitment() -> str:
    """Commitment for ingesting deposits / settling our own payouts.

    Defaults to 'finalized'. 'confirmed' is supermajority-voted but not rooted, so a
    deposit can be reorged away after we have minted supply against it - an irreversible
    loss, since Nexus cannot learn of a Solana reorg.
    """
    return str(getattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized") or "finalized")


def helius_get_transactions_for_address(
    address: str,
    *,
    limit: int = 100,
    before: Optional[str] = None,
    until: Optional[str] = None,
    commitment: str | None = None,
    encoding: Optional[str] = None,
) -> list:
    """Fetch transactions for an address via Helius `getTransactionsForAddress`.

    Returns a list of enriched transaction objects (shape defined by Helius). If the method
    is unavailable or fails, callers can catch and fallback to core RPC.
    """
    lim = max(1, min(1000, int(limit)))
    opts: dict = {"limit": lim, "commitment": commitment or _deposit_commitment()}
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
    commitment: str | None = None,
) -> list:
    """Fallback using core RPC: getSignaturesForAddress + getTransaction (jsonParsed).
    Returns a list of transaction JSONs similar to getTransaction results.
    """
    client = _get_client()
    lim = max(1, min(1000, int(limit)))
    sig_args = {"limit": lim, "commitment": commitment or _deposit_commitment()}
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
            signature_obj = Signature.from_string(sig)
            tx_resp = _rpc_call(
                client.get_transaction,
                signature_obj,
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
    commitment: str | None = None,
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


def fetch_incoming_deposits_via_helius(
    token_account_addr: str,
    since_ts: int,
    min_units: int = 0,
    limit: int = 200,
) -> list[tuple[str, int, str | None, str | None, int]]:
    """
    Fetch recent incoming token transfers to token_account_addr with memos.
    
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
    _log("solana_helius_fallback", level=logging.WARNING, fallback="core_rpc")
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
        solana_mint = str(getattr(config, "USDC_MINT"))

        while len(collected) < limit:
            txs = helius_get_transactions_for_address(
                str(token_account_addr),
                limit=page_size,
                before=before,
                # 'finalized' by default: a 'confirmed' deposit can still be reorged
                # away after we have already minted supply against it.
                commitment=getattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized"),
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

                # Find incoming token transfer to our ATA
                for t in (tx.get("tokenTransfers") or []):
                    if str(t.get("toTokenAccount")) != str(token_account_addr):
                        continue
                    if str(t.get("mint")) != solana_mint:
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
                            pid = str(ix.get("programId") or "")
                            if pid == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr" or pid.startswith("Memo111"):
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
        _log("solana_helius_fetch_failed", level=logging.WARNING, error=str(e))
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
        client = _get_client()
        collected: list[tuple[str, int, str | None, str | None, int]] = []
        solana_mint = str(getattr(config, "USDC_MINT"))
        
        # Step 1: Get signatures (1 API call)
        sig_resp = _rpc_call(
            client.get_signatures_for_address,
            PublicKey.from_string(token_account_addr),
            limit=min(1000, limit * 2),  # Fetch extra since some may be filtered
            commitment=_deposit_commitment(),  # reorg safety: see _deposit_commitment()
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
                signature_obj = Signature.from_string(sig)
                tx_resp = _rpc_call(
                    client.get_transaction,
                    signature_obj,
                    encoding="jsonParsed",
                    timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 12),
                )
                tx_data = _rpc_get_result(tx_resp)
                if not tx_data or not isinstance(tx_data, dict):
                    continue
                
                # Parse transaction for token transfer and memo
                meta = tx_data.get("meta", {})
                pre_balances = meta.get("preTokenBalances", [])
                post_balances = meta.get("postTokenBalances", [])
                
                # Calculate vault delta
                vault_delta = 0
                from_addr = None
                for post in post_balances:
                    if not isinstance(post, dict):
                        continue
                    if post.get("mint") == solana_mint and post.get("owner") == str(config.SOL_MAIN_ACCOUNT):
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
                    if isinstance(pre, dict) and pre.get("mint") == solana_mint:
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
        _log("solana_core_rpc_fetch_failed", level=logging.ERROR, error=str(e))
        return []
    

def process_helius_deposits(deposits: list, db_check: bool = True) -> tuple:
    """Persist enriched deposits from ``fetch_incoming_deposits_via_helius``.

    Each item is a tuple ``(sig, timestamp, memo, from_address, amount_units)`` that
    already contains everything we need, so we write straight to ``unprocessed_sigs``
    with **no per-deposit get_transaction re-fetch** (that would defeat the 1-2 call
    enriched fast path).

    Returns ``(added, oldest_deferred_ts)``. ``oldest_deferred_ts`` is the block time of
    the oldest deposit withheld pending finalization, or ``None``. The caller MUST keep
    the waterline behind it - a deferred deposit is not in the DB, so nothing else would
    stop the waterline advancing past it and hiding it forever.
    """
    if not deposits:
        return (0, None)
    from . import state_db

    # Carve-out: when the operator has relaxed ingestion below 'finalized', deposits at
    # or above SOLANA_FINALIZED_ABOVE_UNITS still require finalization before we mint
    # against them, so a reorg cannot cost us the large amounts.
    require_final: set = set()
    big_threshold = int(getattr(config, "SOLANA_FINALIZED_ABOVE_UNITS", 0) or 0)
    if big_threshold > 0 and _deposit_commitment() != "finalized":
        big_sigs = []
        for it in deposits:
            try:
                s, _ts, _memo, _from, amt = it
            except Exception:
                continue
            if s and int(amt or 0) >= big_threshold:
                big_sigs.append(s)
        if big_sigs:
            finalized = get_signatures_confirmation(big_sigs)
            require_final = {s for s in big_sigs if not finalized.get(s)}
            if require_final:
                _log("solana_deposits_finality_held", level=logging.WARNING,
                     deferred_count=len(require_final), reason="not_finalized")

    added = 0
    oldest_deferred_ts = None
    for item in deposits:
        try:
            sig, ts, memo, from_address, amount_units = item
        except Exception:
            # Tolerate dict-shaped rows too, for forward-compatibility.
            if isinstance(item, dict):
                sig = item.get("signature") or item.get("sig")
                ts = item.get("blocktime") or item.get("timestamp")
                memo = item.get("memo")
                from_address = item.get("from_address") or item.get("from")
                amount_units = item.get("amount")
            else:
                continue
        if not sig:
            continue
        if sig in require_final:
            # Large deposit awaiting finalization; picked up on a later poll. Track its
            # timestamp so the caller pins the waterline behind it.
            try:
                ts_i = int(ts or 0)
                if ts_i and (oldest_deferred_ts is None or ts_i < oldest_deferred_ts):
                    oldest_deferred_ts = ts_i
            except Exception:
                pass
            continue
        if db_check and (
            state_db.is_processed_sig(sig)
            or state_db.is_unprocessed_sig(sig)
            or state_db.is_quarantined_sig(sig)
            or state_db.is_refunded_sig(sig)
        ):
            continue
        state_db.add_unprocessed_sig(sig, ts, memo or "", from_address, amount_units, "ready for processing", None)
        added += 1
    return (added, oldest_deferred_ts)


def process_unprocessed_solana_deposits(limit: int = 1000, timeout: float = 8.0) -> list:
    """
    Process unprocessed deposit signatures from DB.
    Fetches oldest unprocessed sigs up to limit, validates memo format "nexus:<address>",
    checks the destination Nexus token account, runs idempotency checks, debits if valid,
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
    for sig, timestamp, memo, from_address, amount_solana, status, txid in unprocessed[:limit]:
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
            prefix = str(getattr(config, "DEPOSIT_MEMO_PREFIX", "nexus:"))
            if not memo or not memo.lower().startswith(prefix.lower()):
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid memo
                proc_count_refund += 1
                continue

            nexus_address = memo[len(prefix):].strip()
            if not nexus_address:
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid memo
                proc_count_refund += 1
                continue

            # 5. Check Nexus Nexus token account validity
            if not nexus_client.is_valid_nexus_token_account(nexus_address):
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid account
                proc_count_refund += 1
                continue

            # 5b. Per-swap size cap: refund oversized deposits rather than minting
            # against them. Bounds the blast radius of a bug or a hostile deposit.
            max_swap = int(getattr(config, "MAX_SWAP_SOLANA_UNITS", 0) or 0)
            if max_swap > 0 and int(amount_solana or 0) > max_swap:
                from . import alerts
                alerts.warning("swap_over_cap",
                               "deposit exceeds MAX_SWAP_USDC; refunding instead of swapping",
                               sig=sig, amount_units=int(amount_solana or 0), cap_units=max_swap)
                state_db.update_unprocessed_sig_status(sig, "to be refunded")
                proc_count_refund += 1
                continue

            # 6. Calculate amount minus fees
            # Base units, exact integer math (no float / scientific-notation hazard).
            net_amount = nexus_client.get_nexus_send_amount_units(amount_solana)
            if net_amount <= 0:
                # Bug #12 fix: Track the fee (entire deposit amount is kept as fee)
                state_db.add_fee_entry(
                    sig=sig,
                    txid=None,
                    kind="micro_deposit_fee",
                    amount_usdc_units=int(amount_solana),
                    amount_usdd_units=None
                )
                state_db.mark_processed_sig(sig, timestamp, int(amount_solana), None, 0, "processed, amount after fees <= 0", None)
                state_db.remove_unprocessed_sig(sig)
                proc_count_mic += 1
                continue

            # 7. Debit the Nexus-side token if valid.
            # Cross-cycle/cross-thread guard: only one worker may act on this deposit.
            # The literal state_db.DEBIT_RESERVATION_KIND is deliberately NOT renamed: it is a row
            # value in the `reservations` table, not a code identifier. Renaming it would
            # make a live reservation written by the previous build invisible to this one,
            # so a process that crashed mid-debit could be re-debited after the upgrade.
            # See DEBIT_RESERVATION_KIND in state_db for the frozen-name rationale.
            if not state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, sig, ttl_sec=600):
                continue

            # Bug #9 fix: next_reference() atomically increments to prevent duplicate references.
            reference = state_db.next_reference()

            # Persist INTENT before touching the chain. If we crash here, or the CLI
            # answer is unreadable, the reference is on disk and the outcome can be
            # resolved against the chain (resolve_unverified_debits) instead of guessed.
            # Guessing is what previously produced a double mint, or a mint AND a refund.
            state_db.set_unprocessed_sig_debit_intent(sig, reference, net_amount)
            state_db.record_attempt(state_db.debit_attempt_key(sig))

            try:
                result = nexus_client.debit_nexus_token_with_txid(nexus_address, net_amount, reference)
            except Exception as e:
                # Timeout or transport failure: the debit may still have executed.
                state_db.update_unprocessed_sig_status(sig, "debit unverified")
                _log("nexus_debit_outcome_unknown", level=logging.WARNING, sig=sig,
                     reference=reference, error=str(e))
                continue

            if result[0] and result[1]:
                proc_count_swap += 1
                state_db.update_unprocessed_sig_txid(sig, str(result[1]))
                state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")
            else:
                # The CLI reported failure OR returned an unparsable body. Both are
                # AMBIGUOUS - debit_nexus_token_with_txid returns (False, None) when the call
                # succeeded but no txid could be parsed. Never refund on this signal.
                state_db.update_unprocessed_sig_status(sig, "debit unverified")
                _log("nexus_debit_outcome_unknown", level=logging.WARNING, sig=sig,
                     reference=reference, reason="missing_remote_txid")
        except Exception as e:
            _log("solana_deposit_processing_failed", level=logging.ERROR, sig=sig, error=str(e))
            continue

        current_timestamp = time.monotonic()

    return [proc_count_swap, proc_count_refund, proc_count_quar, proc_count_mic]


def _is_token_account_for_mint(token_account_addr: str, mint: PublicKey) -> bool:
    """Return True if the address is an SPL token account for the given mint."""
    try:
        client = _get_client()
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
    """Return True if the address is a Solana wallet with an existing associated token account."""
    try:
        client = _get_client()

        # 1. Validate the wallet address exists (basic check)
        wallet_resp = _rpc_call(client.get_account_info, PublicKey.from_string(wallet_address))
        wallet_val = _rpc_get_value(wallet_resp)
        if not wallet_val or not isinstance(wallet_val, dict):
            return False # wallet doesn't exist
        
        # 2. Derive the expected token ATA address
        owner = PublicKey.from_string(wallet_address)
        ata_address = get_associated_token_address(owner, config.USDC_MINT)

        # 3. Check if the ATA account exists and is valid token account
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


def process_solana_deposits_refunding(limit: int = 1000, timeout: float = 8.0) -> int:

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
            net_amount = amount_usdc_units - int(config.SWAP_PAIR.fees.refund_solana_units)
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
                # (sig, timestamp, amount_usdc_units, txid, amount_nexus_debited, status, reference)
                state_db.remove_unprocessed_sig(sig)
                continue

            # 5. Validate from_address whether existing token ATA account or Solana wallet with existing ATA account
            if not from_address:
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                continue
            if not _is_token_account_for_mint(from_address, config.USDC_MINT) and not _is_solana_wallet_with_ata(from_address):
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                continue

            # 6. On-chain idempotency: only scan the chain when a prior attempt may have
            #    already sent a refund (e.g. crash between send and DB write). This avoids
            #    a ~50-tx signature scan on every first-time refund.
            refund_key = state_db.refund_attempt_key(sig)
            if state_db.get_attempt_count(refund_key) > 0:
                existing_refund = find_signature_with_memo(f"refundSig:{sig}")
                if existing_refund:
                    state_db.update_unprocessed_sig_status(sig, "refund sent, awaiting confirmation")
                    state_db.mark_refunded_sig(sig, timestamp, from_address, amount_usdc_units, memo, existing_refund, net_amount, "awaiting confirmation")
                    processed_count += 1
                    continue

            # 7. Process the refund. Memo prefix MUST match the startup-recovery scanner
            #    (refundSig:) so a crash mid-refund is reconstructed, not double-paid.
            state_db.record_attempt(refund_key)
            try:
                sig_r = send_solana_token(from_address, net_amount, memo=f"refundSig:{sig}")
            except PayoutCapExceeded as e:
                # Throttled, not failed: leave status as "to be refunded" and retry later.
                _log("solana_refund_deferred", level=logging.WARNING, sig=sig, reason=str(e))
                continue
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
            _log("solana_refund_processing_failed", level=logging.ERROR, sig=sig, error=str(e))
            continue

        current_timestamp = time.monotonic()

    return processed_count


def process_solana_deposits_quarantine(limit: int = 1000, timeout: float = 25.0) -> int:

    from . import state_db, nexus_client

    # 1. Fetch unprocessed sigs to be quarantined (oldest first)
    # Include 'quarantine failed': it was previously written and then never selected
    # again by any pass, stranding the row (and the funds) in unprocessed_sigs forever.
    unprocessed = state_db.filter_unprocessed_sigs({
        'status_in': ('to be quarantined', 'quarantine failed'),
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
            # 2. Re-check status (either pending or a previously failed attempt)
            if state_db.get_unprocessed_sig_status(sig) not in ("to be quarantined", "quarantine failed"):
                continue
            # Bound the retry so a permanently failing quarantine cannot spin every cycle.
            if not state_db.should_attempt(state_db.quarantine_send_attempt_key(sig)):
                continue
            state_db.record_attempt(state_db.quarantine_send_attempt_key(sig))

            # 3. Run idempotency checks: already processed?
            if state_db.is_processed_sig(sig) or state_db.is_refunded_sig(sig):
                state_db.remove_unprocessed_sig(sig)
                continue
            
            # 4. Check quarantine net amount
            net_amount = amount_usdc_units - int(config.SWAP_PAIR.fees.refund_solana_units)

            # 4b. Nothing left after the fee: there is nothing to move, so finalise here.
            # Without this, send_solana_token() returns (True, None) for a non-positive amount and
            # the row is marked "awaiting confirmation" with a NULL signature - which the
            # confirmation pass filters out, leaving it stuck in unprocessed_sigs forever.
            if net_amount <= 0:
                state_db.add_fee_entry(sig=sig, txid=None, kind="quarantine_micro_fee",
                                       amount_usdc_units=int(amount_usdc_units), amount_usdd_units=None)
                state_db.mark_processed_sig(sig, timestamp, int(amount_usdc_units), None, 0,
                                            "processed, amount after fees <= 0", None)
                state_db.remove_unprocessed_sig(sig)
                processed_count += 1
                continue

            # 5. On-chain idempotency: only scan after a prior attempt (avoids a per-item
            #    scan on the common first attempt). Double-quarantine is not an external
            #    loss, but this keeps state consistent after a crash.
            quar_key = state_db.quarantine_attempt_key(sig)
            if state_db.get_attempt_count(quar_key) > 0:
                existing_quar = find_signature_with_memo(f"quarantinedSig:{sig}")
                if existing_quar:
                    state_db.update_unprocessed_sig_status(sig, "quarantine sent, awaiting confirmation")
                    state_db.mark_quarantined_sig(sig, timestamp, from_address, amount_usdc_units, memo, existing_quar, net_amount, "awaiting confirmation")
                    processed_count += 1
                    continue

            # 6. Process the quarantine. Memo prefix MUST match the startup-recovery
            #    scanner (quarantinedSig:) for crash reconstruction.
            state_db.record_attempt(quar_key)
            try:
                sig_q = send_solana_token(config.USDC_QUARANTINE_ACCOUNT, net_amount, memo=f"quarantinedSig:{sig}")
            except PayoutCapExceeded as e:
                _log("solana_quarantine_deferred", level=logging.WARNING, sig=sig, reason=str(e))
                continue
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
            _log("solana_quarantine_processing_failed", level=logging.ERROR, sig=sig, error=str(e))
            continue

        current_timestamp = time.monotonic()

    return processed_count


def send_solana_token(destination: str, amount_base_units: int, memo: str | None = None) -> tuple[bool, str | None]:
    """
    Unified function to send the Solana-side token from vault to various destinations.
    
    Args:
        destination: Target address (owner address or token account address)
        amount_base_units: Amount in base units
        memo: Optional memo string for transaction
        
    Returns:
        Tuple of (success: bool, signature: str | None)
    """
    if amount_base_units <= 0:
        return True, None

    # Rolling 24h exposure cap, enforced at the single choke point every Solana payout
    # passes through, so a runaway loop or a stolen key cannot drain the vault at once.
    cap = int(getattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 0) or 0)
    if cap > 0:
        try:
            spent = state_db.payouts_since(86400)
            if spent + int(amount_base_units) > cap:
                from . import alerts
                alerts.critical(
                    "payout_cap_exceeded",
                    "24h outbound payout cap would be breached; payment deferred",
                    spent_units=spent, requested_units=int(amount_base_units), cap_units=cap,
                    destination=str(destination),
                )
                raise PayoutCapExceeded(
                    f"24h payout cap {cap} would be exceeded (spent {spent}, "
                    f"requested {int(amount_base_units)})")
        except PayoutCapExceeded:
            raise  # genuine cap breach - propagate as-is
        except Exception as e:
            raise PayoutCapExceeded(f"payout cap check failed, deferring payment to stay safe: {e}")

    try:
        kp = load_vault_keypair()
        client = _get_client()
        
        # Determine the actual destination token account
        # If destination is a wallet address (not a token account), derive the ATA
        dest_token_account = destination
        if not _is_token_account_for_mint(destination, config.USDC_MINT):
            # Destination is a wallet address - derive its token ATA
            try:
                owner_pubkey = PublicKey.from_string(destination)
                dest_token_account = str(get_associated_token_address(owner=owner_pubkey, mint=config.USDC_MINT))
            except Exception as e:
                _log("solana_payout_destination_invalid", level=logging.WARNING,
                     destination=destination, error=str(e))
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

        # Record against the rolling cap only after the send actually succeeded.
        try:
            state_db.record_payout("solana_send", int(amount_base_units), sig)
        except Exception as e:
            _log("solana_payout_ledger_record_failed", level=logging.ERROR, signature=sig, error=str(e))

        _log("solana_payout_submitted", signature=sig, amount_units=int(amount_base_units),
             token=config.SOLANA_TOKEN_SYMBOL)
        return True, sig

    except Exception as e:
        _log("solana_payout_submission_failed", level=logging.ERROR,
             token=config.SOLANA_TOKEN_SYMBOL, error=str(e))
        return False, None


def get_signatures_confirmation(sigs: list, min_confirmations: int = 1) -> dict:
    """Batch-check Solana signatures via getSignatureStatuses (up to 256 per call).

    Returns ``{sig: True}`` for signatures that are confirmed/finalized (or have at
    least ``min_confirmations`` confirmations). Unknown/None statuses are omitted.
    Uses the shared client and converts to Signature objects as the RPC expects.
    """
    from solders.signature import Signature
    out: dict = {}
    uniq = [s for s in dict.fromkeys(sigs) if s]
    if not uniq:
        return out
    client = _get_client()
    for i in range(0, len(uniq), 256):
        chunk = uniq[i:i + 256]
        objs = []
        keep = []
        for s in chunk:
            try:
                objs.append(Signature.from_string(s))
                keep.append(s)
            except Exception:
                continue
        if not objs:
            continue
        try:
            resp = _rpc_call(client.get_signature_statuses, objs)
            val = _rpc_get_value(resp)
        except Exception:
            continue
        if not isinstance(val, list):
            continue
        # If we ingest at 'finalized', settle our own payouts at 'finalized' too - a
        # merely 'confirmed' refund can still be reorged away after we mark it done.
        accepted = ("finalized",) if _deposit_commitment() == "finalized" else ("finalized", "confirmed")
        for s, st in zip(keep, val):
            if isinstance(st, dict):
                cs = st.get("confirmationStatus")
                confs = st.get("confirmations")
                if cs in accepted or (confs is not None and confs >= min_confirmations):
                    out[s] = True
    return out


def check_sig_confirmations(min_confirmations: int, timeout: float) -> int:
    """Check confirmation status for Solana refund transactions.
    
    This function queries refunded_sigs for entries awaiting confirmation,
    then checks the REFUND signature (not the original deposit signature)
    for on-chain confirmation status.
    """
    # Query refunded_sigs table for entries awaiting confirmation
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, refund_sig FROM refunded_sigs
        WHERE status = 'awaiting confirmation' AND refund_sig IS NOT NULL
        LIMIT 1000
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return 0
    
    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    # Batch-check all refund signatures in one (or few) getSignatureStatuses calls
    # instead of one RPC per row.
    finalized = get_signatures_confirmation([r[1] for r in rows], min_confirmations)

    for deposit_sig, refund_sig in rows:
        if time.monotonic() - time_start > timeout:
            break
        if refund_sig and finalized.get(refund_sig):
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
                _log("solana_refund_confirmed", deposit_sig=deposit_sig, refund_signature=refund_sig)
            except Exception as e:
                _log("solana_refund_confirmation_persist_failed", level=logging.ERROR,
                     deposit_sig=deposit_sig, error=str(e))
        # If confirmations is None, skip (not confirmed yet)

    return processed_count


def check_quarantine_confirmations(min_confirmations: int, timeout: float) -> int:
    """Check confirmation status for Solana-side quarantine transactions.
    
    Bug #8 fix: This function queries quarantined_sigs for entries awaiting confirmation,
    then checks the QUARANTINE signature for on-chain confirmation status.
    Only after confirmation is the deposit removed from unprocessed_sigs.
    """
    # Query quarantined_sigs table for entries awaiting confirmation
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, quarantine_sig FROM quarantined_sigs
        WHERE status = 'awaiting confirmation' AND quarantine_sig IS NOT NULL
        LIMIT 1000
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return 0
    
    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    # Batch-check all quarantine signatures in one (or few) getSignatureStatuses calls.
    finalized = get_signatures_confirmation([r[1] for r in rows], min_confirmations)

    for deposit_sig, quarantine_sig in rows:
        if time.monotonic() - time_start > timeout:
            break
        if quarantine_sig and finalized.get(quarantine_sig):
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
                _log("solana_quarantine_confirmed", deposit_sig=deposit_sig,
                     quarantine_signature=quarantine_sig)
            except Exception as e:
                _log("solana_quarantine_confirmation_persist_failed", level=logging.ERROR,
                     deposit_sig=deposit_sig, error=str(e))
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
    client = _get_client()
    bh = _get_latest_blockhash_str(client)
    if not bh:
        raise RuntimeError("Failed to fetch recent blockhash")
    recent = Hash.from_string(bh)
    tx = Transaction.new_signed_with_payer(instructions, kp.pubkey(), [kp], recent)
    send_resp = _rpc_call(client.send_raw_transaction, bytes(tx), timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))
    sig = _rpc_get_result(send_resp)
    if not isinstance(sig, str):
        raise RuntimeError(f"Failed to send tx, unexpected response: {send_resp}")
    # Do NOT block on confirmation here: every send path has a dedicated confirmation
    # pass (check_sig_confirmations / check_quarantine_confirmations / Nexus->Solana
    # Priority 3). Blocking here would throttle throughput to a few sends per cycle.
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
        client = _get_client()
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


# Short-lived cache for hot, repeated balance reads (the maintenance block reads the
# vault balance for the backing check, reconcile, and metrics within one loop iteration).
_balance_cache: dict[str, tuple[float, int]] = {}


def get_token_account_balance(token_account_addr: str, *, max_age_sec: float = 0.0) -> int:
    """Return the SPL token account balance in base units.

    Pass ``max_age_sec`` > 0 to accept a recently-cached value (only successful reads
    are cached). Callers needing a fresh value (e.g. the deposit-delta poll) omit it.
    """
    key = str(token_account_addr)
    if max_age_sec > 0:
        ent = _balance_cache.get(key)
        if ent is not None and (time.monotonic() - ent[0]) <= max_age_sec:
            return ent[1]
    try:
        client = _get_client()
        resp = _rpc_call(client.get_token_account_balance, PublicKey.from_string(key))
        val = _rpc_get_value(resp)
        amt = None
        if isinstance(val, dict):
            amt = val.get("amount")
        result = int(amt or 0)
        _balance_cache[key] = (time.monotonic(), result)
        return result
    except Exception:
        return 0


def transfer_solana_token_between_accounts(source_token_account: str, dest_token_account: str, amount_base_units: int) -> bool:
    """Transfer the bridged token between two token accounts owned by the vault wallet."""
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
        _log("solana_internal_transfer_submitted", signature=sig,
             amount_units=int(amount_base_units))
        return True
    except Exception as e:
        _log("solana_internal_transfer_failed", level=logging.ERROR, error=str(e))
        return False


def check_timestamp_unpr_sigs() -> int | None:
    """
    Find the block time (timestamp) of the oldest unprocessed sig in DB and propose it as a new waterline.
    This can be used for recovery or waterline adjustment based on unprocessed entries.
    Returns the proposed waterline timestamp (int), or None if no unprocessed sigs found.
    """
    # NOTE: this used to be `from . import state_db, state` - there is no `state` module,
    # so every call raised ImportError, aborting the poll before the waterline/heartbeat
    # could be updated. Function-level imports like this are invisible to byte-compilation.
    from . import state_db

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
    _log("solana_waterline_proposed", proposed_waterline=new_waterline,
         oldest_unprocessed_timestamp=oldest_timestamp)
    
    return new_waterline


def swap_token_for_sol_via_jupiter(amount_solana_base_units: int, slippage_bps: int = 50) -> bool:
    """Swap token->SOL using Jupiter. Returns True on success.
    Requires: config.USDC_MINT (the configured Solana mint) and a vault keypair holding a
    token account for it.
    """
    try:
        if amount_solana_base_units <= 0:
            return False
        client = _get_client()
        kp = load_vault_keypair()
        owner = kp.pubkey()

        # Jupiter Quote API v6
        base = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": str(config.USDC_MINT),
            "outputMint": "So11111111111111111111111111111111111111112",
            "amount": str(int(amount_solana_base_units)),
            "slippageBps": str(int(slippage_bps)),
            "onlyDirectRoutes": "false",
        }
        q = requests.get(base, params=params, timeout=15)
        q.raise_for_status()
        qd = q.json()
        routes = qd.get("data") or []
        if not routes:
            _log("solana_jupiter_route_unavailable", level=logging.WARNING)
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
            _log("solana_jupiter_swap_missing_transaction", level=logging.ERROR)
            return False

        tx_bytes = base64.b64decode(swap_tx_b64)
        vtx = VersionedTransaction.from_bytes(tx_bytes)
        vtx.sign([kp])
        raw = bytes(vtx)
        send_resp = client.send_raw_transaction(raw)
        sig = _rpc_get_result(send_resp)
        if not isinstance(sig, str):
            _log("solana_jupiter_swap_invalid_response", level=logging.ERROR)
            return False
        try:
            client.confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        _log("solana_jupiter_swap_submitted", signature=sig,
             amount_units=int(amount_solana_base_units))
        return True
    except Exception as e:
        _log("solana_jupiter_swap_failed", level=logging.ERROR, error=str(e))
        return False




def ensure_send_token(to_owner_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send Solana base units to a Solana owner address. Requires recipient ATA to already exist.
    If memo is provided, attach it to the transaction for idempotency tracing.
    """
    try:
        kp = load_vault_keypair()
        owner = PublicKey.from_string(to_owner_addr)
        dest_ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        client = _get_client()
        ata_info = _rpc_get_value(_rpc_call(client.get_account_info, dest_ata))
        if ata_info is None:
            _log("solana_recipient_ata_missing", level=logging.WARNING,
                 destination=to_owner_addr)
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
        _log("solana_owner_payout_submitted", signature=sig, destination=to_owner_addr,
             amount_units=int(amount_base_units), token=config.SOLANA_TOKEN_SYMBOL)
        return True
    except Exception as e:
        _log("solana_owner_payout_failed", level=logging.ERROR,
             destination=to_owner_addr, token=config.SOLANA_TOKEN_SYMBOL, error=str(e))
        return False





def send_solana_token_to_account_with_sig(dest_token_account_addr: str, amount_base_units: int, memo: str | None = None) -> tuple[bool, str | None]:
    """Submit Solana base units directly to an existing token account.

    This RPC helper deliberately owns no Nexus idempotency or terminal state.  The
    caller must persist an exact source intent before submission and finalize it only
    after independently validating the returned transaction evidence.
    """
    
    try:
        if amount_base_units <= 0:
            return True, None
        if not _is_token_account_for_mint(dest_token_account_addr, config.USDC_MINT):
            _log("solana_payout_destination_invalid", level=logging.WARNING,
                 destination=dest_token_account_addr, reason="wrong_token_account_or_mint")
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
        _log("solana_token_account_payout_submitted", signature=sig,
             destination=dest_token_account_addr, amount_units=int(amount_base_units),
             token=config.SOLANA_TOKEN_SYMBOL)
        return True, sig
    except Exception as e:
        _log("solana_token_account_payout_failed", level=logging.ERROR, error=str(e))
        return False, None


def ensure_send_token_to_account(dest_token_account_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    ok, _sig = send_solana_token_to_account_with_sig(dest_token_account_addr, amount_base_units, memo)
    return ok


def ensure_send_token_owner_or_ata(addr_maybe_owner_or_token: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send the Solana-side token to either a Solana owner address (deriving ATA) or a direct token account address."""
    try:
        if _is_token_account_for_mint(addr_maybe_owner_or_token, config.USDC_MINT):
            return ensure_send_token_to_account(addr_maybe_owner_or_token, amount_base_units, memo)
        return ensure_send_token(addr_maybe_owner_or_token, amount_base_units, memo)
    except Exception as e:
        _log("solana_recipient_resolution_failed", level=logging.ERROR,
             destination=addr_maybe_owner_or_token, error=str(e))
        return False


def is_valid_solana_token_account(addr: str) -> bool:
    """Public helper: True if addr is a valid SPL token account for the configured mint."""
    return _is_token_account_for_mint(addr, config.USDC_MINT)


def get_nexus_payout_evidence(
    signature: str,
    txid: str,
    contract_id: int,
) -> nexus_memo.NexusPayoutEvidence | None:
    """Read one finalized signature and return exact attributable payout evidence.

    A status response is intentionally insufficient: finality, success, the direct
    transaction signature, exact composite memo and vault transfer are all checked in
    the transaction returned by ``getTransaction``.
    """
    expected_memo = nexus_memo.parse_nexus_payout_memo(
        f"nexus_txid:{txid}:{contract_id}"
    )
    if (not isinstance(signature, str) or not signature
            or expected_memo is None or expected_memo.is_legacy):
        return None
    try:
        from solders.signature import Signature

        signature_obj = Signature.from_string(signature)
        client = _get_client()
        response = _rpc_call(
            client.get_transaction,
            signature_obj,
            encoding="jsonParsed",
            commitment="finalized",
            max_supported_transaction_version=0,
            timeout=getattr(
                config, "SOLANA_TX_FETCH_TIMEOUT_SEC",
                getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
            ),
        )
        tx_value = _rpc_get_result(response)
    except Exception:
        return None
    if not isinstance(tx_value, dict):
        return None
    transaction = tx_value.get("transaction")
    transaction_signatures = (
        transaction.get("signatures") if isinstance(transaction, dict) else None
    )
    if (not isinstance(transaction_signatures, list) or not transaction_signatures
            or str(transaction_signatures[0]) != signature):
        return None

    message = transaction.get("message")
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if not isinstance(instructions, list):
        return None
    payout_memos: list[nexus_memo.NexusPayoutMemo | None] = []
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        program = instruction.get("programId") or instruction.get("program")
        if not program or not (
            str(program).startswith("Memo111")
            or str(program) == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
            or str(program) == "spl-memo"
        ):
            continue
        data = instruction.get("data", instruction.get("parsed"))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        candidates = [data] if isinstance(data, str) else []
        if isinstance(data, str):
            try:
                candidates.append(base64.b64decode(data, validate=True).decode("utf-8"))
            except Exception:
                pass
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.startswith("nexus_txid:"):
                payout_memos.append(nexus_memo.parse_nexus_payout_memo(candidate))
    if payout_memos != [expected_memo]:
        return None
    return _extract_nexus_payout_evidence(
        tx_value, signature=signature, memo=expected_memo
    )


def find_nexus_payout_evidence(
    memo: str,
    search_limit: int = 50,
) -> nexus_memo.NexusPayoutEvidence | None:
    """Recover one unambiguous finalized payout with full transaction evidence."""
    expected = nexus_memo.parse_nexus_payout_memo(memo)
    if (expected is None or expected.is_legacy
            or type(search_limit) is not int or search_limit <= 0):
        return None
    try:
        client = _get_client()
    except Exception:
        return None

    addresses = [str(config.VAULT_USDC_ACCOUNT)]
    vault_owner = getattr(config, "VAULT_OWNER", None)
    if vault_owner:
        addresses.append(str(vault_owner))
    seen: set[str] = set()
    matches: list[nexus_memo.NexusPayoutEvidence] = []
    for address in dict.fromkeys(addresses):
        try:
            response = _rpc_call(
                client.get_signatures_for_address,
                PublicKey.from_string(address),
                limit=search_limit,
                commitment="finalized",
            )
            entries = _rpc_get_value(response)
        except Exception:
            return None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            signature = entry.get("signature")
            if (not isinstance(signature, str) or not signature or signature in seen
                    or entry.get("confirmationStatus") != "finalized"
                    or entry.get("err") is not None):
                continue
            seen.add(signature)
            evidence = get_nexus_payout_evidence(
                signature, expected.txid, expected.contract_id
            )
            if evidence is not None:
                matches.append(evidence)
                if len(matches) > 1:
                    return None
    return matches[0] if len(matches) == 1 else None


def find_signature_with_memo(memo: str, search_limit: int = 50) -> Optional[str]:
    """Return a finalized signature for generic refund/quarantine memo paths.

    Nexus payout callers should consume :func:`find_nexus_payout_evidence`; this
    compatibility wrapper retains the signature-only API for existing non-Nexus uses.
    """
    if not isinstance(memo, str) or not memo:
        return None
    payout_memo = None
    if memo.startswith("nexus_txid:"):
        payout_memo = nexus_memo.parse_nexus_payout_memo(memo)
        if payout_memo is None or payout_memo.is_legacy:
            return None
        evidence = find_nexus_payout_evidence(memo, search_limit)
        return evidence.solana_signature if evidence is not None else None
    try:
        client = _get_client()
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
            resp = _rpc_call(
                client.get_signatures_for_address,
                PublicKey.from_string(addr),
                limit=search_limit,
                commitment="finalized",
            )
            js = _rpc_get_value(resp)
            if isinstance(js, list):
                sig_list = js
            else:
                sig_list = []
        except Exception:
            continue
        for entry in sig_list:
            if not isinstance(entry, dict):
                continue
            sig = entry.get("signature")
            if (not isinstance(sig, str) or not sig or sig in seen
                    or entry.get("err") is not None
                    or entry.get("confirmationStatus") != "finalized"):
                continue
            seen.add(sig)
            try:
                signature_obj = Signature.from_string(sig)
                tx_resp = _rpc_call(
                    client.get_transaction,
                    signature_obj,
                    encoding="jsonParsed",
                    timeout=getattr(
                        config, "SOLANA_TX_FETCH_TIMEOUT_SEC",
                        getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
                    ),
                )
                tx_val = _rpc_get_result(tx_resp)
            except Exception:
                continue
            if not isinstance(tx_val, dict):
                continue
            meta = tx_val.get("meta")
            tx_obj = tx_val.get("transaction")
            msg = tx_obj.get("message") if isinstance(tx_obj, dict) else None
            insts = msg.get("instructions") if isinstance(msg, dict) else None
            if (not isinstance(meta, dict) or meta.get("err") is not None
                    or not isinstance(insts, list)
                    or not _is_attributable_vault_transaction(tx_val)):
                continue
            for ix in insts:
                if not isinstance(ix, dict):
                    continue
                prog = ix.get("programId") or ix.get("program")
                if not prog or not (
                    str(prog).startswith("Memo111")
                    or str(prog) == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
                    or str(prog) == "spl-memo"
                ):
                    continue
                data = ix.get("data", ix.get("parsed"))
                if isinstance(data, list) and len(data) == 1:
                    data = data[0]
                candidates = [data] if isinstance(data, str) else []
                if isinstance(data, str):
                    try:
                        candidates.append(base64.b64decode(data, validate=True).decode("utf-8"))
                    except Exception:
                        pass
                if memo not in candidates:
                    continue
                if payout_memo is not None and _extract_nexus_payout_evidence(
                    tx_val, signature=sig, memo=payout_memo
                ) is None:
                    continue
                return sig
    return None


def _is_attributable_vault_transaction(tx_value: object) -> bool:
    """Require the configured vault authority to be an actual transaction signer."""
    if not isinstance(tx_value, dict):
        return False
    transaction = tx_value.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    account_keys = message.get("accountKeys") if isinstance(message, dict) else None
    if not isinstance(account_keys, list):
        return False
    authority = str(config.SOL_MAIN_ACCOUNT)
    for account in account_keys:
        if isinstance(account, dict):
            if str(account.get("pubkey") or "") == authority and account.get("signer") is True:
                return True
    return False


def _extract_nexus_payout_evidence(
    tx_value: object,
    *,
    signature: str,
    memo: nexus_memo.NexusPayoutMemo,
) -> nexus_memo.NexusPayoutEvidence | None:
    """Bind an exact composite memo to one exact successful vault token transfer."""
    if memo.contract_id is None or not _is_attributable_vault_transaction(tx_value):
        return None
    if not isinstance(tx_value, dict) or not isinstance(tx_value.get("meta"), dict):
        return None
    meta = tx_value["meta"]
    if "err" not in meta or meta["err"] is not None:
        return None
    transaction = tx_value.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if not isinstance(instructions, list):
        return None

    transfers: list[tuple[str, int]] = []
    for instruction in instructions:
        if not isinstance(instruction, dict) or str(instruction.get("program") or "") != "spl-token":
            continue
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in {"transfer", "transferChecked"}:
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        if str(info.get("source") or "") != str(config.VAULT_USDC_ACCOUNT):
            continue
        mint = info.get("mint")
        if str(mint or "") != str(config.USDC_MINT):
            continue
        destination = info.get("destination")
        raw_amount = info.get("amount")
        token_amount = info.get("tokenAmount")
        if isinstance(token_amount, dict):
            raw_amount = token_amount.get("amount")
        if not isinstance(destination, str) or not destination.strip():
            continue
        if isinstance(raw_amount, bool):
            continue
        amount_text = str(raw_amount or "")
        if not amount_text.isdigit() or (len(amount_text) > 1 and amount_text.startswith("0")):
            continue
        amount_units = int(amount_text)
        if amount_units <= 0:
            continue
        transfers.append((destination, amount_units))

    if len(transfers) != 1:
        return None
    destination, amount_units = transfers[0]
    return nexus_memo.NexusPayoutEvidence(
        txid=memo.txid,
        contract_id=memo.contract_id,
        solana_signature=signature,
        to_token_account=destination,
        amount_solana_units=amount_units,
    )


def scan_recent_memos(search_limit: int = 400) -> dict:
    """Scan recent signatures for the vault account collecting structured memos.
    Returns dict: {
        'nexus_txids': { txid: signature },
        'refund_sigs': { deposit_sig: refund_tx_sig },
    }
    Best effort; ignores errors.
    """
    out = {
        "nexus_payouts": {},
        "legacy_nexus_txids": {},
        "malformed_nexus_memos": [],
        "refund_sigs": {},
        "complete": False,
        "reason": "bounded_recent_scan",
    }
    try:
        client = _get_client()
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
            if (not sig or ent.get("err") is not None
                    or ent.get("confirmationStatus") != "finalized"):
                continue
            # Fetch transaction (short timeout)
            try:
                signature_obj = Signature.from_string(sig)
                tx_resp = _rpc_call(client.get_transaction, signature_obj, encoding="jsonParsed", timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 6))
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
                if isinstance(m, str) and m.startswith("nexus_txid:"):
                    parsed = nexus_memo.parse_nexus_payout_memo(m)
                    if parsed is None:
                        out["malformed_nexus_memos"].append(m)
                    elif parsed.is_legacy:
                        out["legacy_nexus_txids"].setdefault(parsed.txid, sig)
                    else:
                        evidence = _extract_nexus_payout_evidence(
                            tx_val, signature=sig, memo=parsed
                        )
                        if evidence is None:
                            out["malformed_nexus_memos"].append(m)
                        else:
                            out["nexus_payouts"].setdefault(
                                (parsed.txid, parsed.contract_id), evidence
                            )
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
    """Enumerate finalized vault transactions without turning gaps into absence proof."""
    out = {
        "nexus_payouts": {},
        "legacy_nexus_txids": {},
        "malformed_nexus_memos": [],
        "refund_sigs": {},
        "quarantined_sigs": {},
        "deposits": [],
        "complete": False,
        "reason": None,
    }

    def incomplete(reason: str) -> dict:
        out["nexus_payouts"].clear()
        out["legacy_nexus_txids"].clear()
        out["refund_sigs"].clear()
        out["quarantined_sigs"].clear()
        out["complete"] = False
        out["reason"] = reason
        return out

    if (isinstance(since_timestamp, bool) or not isinstance(since_timestamp, int)
            or since_timestamp <= 0):
        return incomplete("invalid_waterline")
    if (isinstance(max_signatures, bool) or not isinstance(max_signatures, int)
            or max_signatures <= 0):
        return incomplete("invalid_signature_budget")

    try:
        client = _get_client()
    except Exception:
        return incomplete("client_unavailable")

    fetched_count = 0
    before_sig = None
    batch_size = 1000

    while fetched_count < max_signatures:
        request_limit = min(batch_size, max_signatures - fetched_count)
        params: dict[str, Any] = {
            "limit": request_limit,
            "commitment": "finalized",
        }
        if before_sig:
            params["before"] = before_sig
        try:
            resp = _rpc_call(
                client.get_signatures_for_address,
                PublicKey.from_string(str(config.VAULT_USDC_ACCOUNT)),
                **params,
            )
        except Exception as exc:
            _log("solana_memo_scan_failed", level=logging.ERROR, error=str(exc))
            return incomplete("signature_page_fetch_failed")

        entries = _rpc_get_value(resp)
        if not isinstance(entries, list):
            return incomplete("invalid_signature_page")
        if not entries:
            out["complete"] = True
            return out

        reached_waterline = False
        for ent in entries:
            if not isinstance(ent, dict):
                return incomplete("invalid_signature_entry")
            sig = ent.get("signature")
            block_time = ent.get("blockTime")
            if not isinstance(sig, str) or not sig.strip():
                return incomplete("invalid_signature")
            if (isinstance(block_time, bool) or not isinstance(block_time, int)
                    or block_time <= 0):
                return incomplete("invalid_block_time")
            if block_time < since_timestamp:
                reached_waterline = True
                break
            if ent.get("confirmationStatus") != "finalized":
                return incomplete("signature_not_finalized")
            if ent.get("err") is not None:
                continue

            try:
                signature_obj = Signature.from_string(sig)
            except Exception:
                return incomplete("invalid_signature")
            try:
                tx_resp = _rpc_call(
                    client.get_transaction,
                    signature_obj,
                    encoding="jsonParsed",
                    timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 6),
                )
            except Exception:
                return incomplete("transaction_fetch_failed")

            tx_val = _rpc_get_result(tx_resp)
            if not isinstance(tx_val, dict):
                return incomplete("invalid_transaction")
            meta = tx_val.get("meta")
            tx_obj = tx_val.get("transaction")
            msg = tx_obj.get("message") if isinstance(tx_obj, dict) else None
            insts = msg.get("instructions") if isinstance(msg, dict) else None
            if not isinstance(meta, dict) or not isinstance(insts, list):
                return incomplete("invalid_transaction")
            if meta.get("err") is not None:
                continue

            memos: list[str] = []
            for ix in insts:
                if not isinstance(ix, dict):
                    return incomplete("invalid_instruction")
                prog = ix.get("programId") or ix.get("program")
                if not prog or not (
                    str(prog).startswith("Memo111")
                    or str(prog) == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
                    or str(prog) == "spl-memo"
                ):
                    continue
                data = ix.get("data", ix.get("parsed"))
                if isinstance(data, list) and len(data) == 1:
                    data = data[0]
                if not isinstance(data, str):
                    return incomplete("invalid_memo_instruction")
                memos.append(data)

            for memo in memos:
                if memo.startswith("nexus_txid:"):
                    parsed = nexus_memo.parse_nexus_payout_memo(memo)
                    if parsed is None:
                        out["malformed_nexus_memos"].append(memo)
                        return incomplete("malformed_nexus_payout_memo")
                    if parsed.is_legacy:
                        existing = out["legacy_nexus_txids"].get(parsed.txid)
                        if existing is not None and existing != sig:
                            return incomplete("duplicate_nexus_payout_identity")
                        out["legacy_nexus_txids"][parsed.txid] = sig
                    else:
                        evidence = _extract_nexus_payout_evidence(
                            tx_val, signature=sig, memo=parsed
                        )
                        if evidence is None:
                            return incomplete("missing_nexus_payout_transfer_evidence")
                        identity = (parsed.txid, parsed.contract_id)
                        existing = out["nexus_payouts"].get(identity)
                        if (existing is not None
                                and existing.solana_signature != evidence.solana_signature):
                            return incomplete("duplicate_nexus_payout_identity")
                        out["nexus_payouts"][identity] = evidence
                elif memo.startswith("refundSig:"):
                    deposit_sig = memo[len("refundSig:"):]
                    if not deposit_sig or deposit_sig.strip() != deposit_sig or any(
                        ch.isspace() for ch in deposit_sig
                    ):
                        return incomplete("malformed_refund_memo")
                    out["refund_sigs"].setdefault(deposit_sig, sig)
                elif memo.startswith("quarantinedSig:"):
                    deposit_sig = memo[len("quarantinedSig:"):]
                    if not deposit_sig or deposit_sig.strip() != deposit_sig or any(
                        ch.isspace() for ch in deposit_sig
                    ):
                        return incomplete("malformed_quarantine_memo")
                    out["quarantined_sigs"][deposit_sig] = sig

        fetched_count += len(entries)
        if reached_waterline or len(entries) < request_limit:
            out["complete"] = True
            return out
        cursor = entries[-1].get("signature")
        if not isinstance(cursor, str) or not cursor:
            return incomplete("invalid_pagination_cursor")
        try:
            before_sig = Signature.from_string(cursor)
        except Exception:
            return incomplete("invalid_pagination_cursor")

    return incomplete("pagination_truncated")


## Memo extraction removed.


def has_token_ata(owner_addr: str) -> bool:
    """Return True if the owner's associated token account exists."""
    try:
        client = _get_client()
        owner = PublicKey.from_string(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        info = _rpc_get_value(_rpc_call(client.get_account_info, ata))
        return info is not None
    except Exception:
        return False


def derive_token_ata(owner_addr: str) -> str | None:
    """Derive the expected associated token account address for a given owner (string form). Returns None on failure."""
    try:
        owner = PublicKey.from_string(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        return str(ata)
    except Exception:
        return None


def refund_solana_token_to_source(source_token_account: str, amount_base_units: int, reason: str, deposit_sig: str | None = None) -> bool:
    """Refund funds back to the sender's token account.
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
        _log("solana_refund_submitted", signature=sig, amount_units=int(amount_base_units),
             deposit_sig=deposit_sig)
        return True
    except Exception as e:
        _log("solana_refund_submission_failed", level=logging.ERROR,
             token=config.SOLANA_TOKEN_SYMBOL, error=str(e))
        return False


def move_solana_token_to_quarantine(amount_base_units: int, note: str | None = None, deposit_sig: str | None = None) -> bool:
    """Move the deposit to quarantine with structured memo for later idempotency.
    Memo precedence:
      quarantinedSig:<deposit_sig>
      quarantined:<note>
      quarantined
    """
    try:
        dest = getattr(config, "USDC_QUARANTINE_ACCOUNT", None)
        if not dest:
            _log("solana_quarantine_not_configured", level=logging.ERROR)
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
        _log("solana_quarantine_submitted", signature=sig, amount_units=int(amount_base_units),
             memo=memo_txt)
        return True
    except Exception as e:
        _log("solana_quarantine_submission_failed", level=logging.ERROR, error=str(e))
    return False
