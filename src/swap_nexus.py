from decimal import Decimal, ROUND_DOWN, InvalidOperation
from . import config, state_db, solana_client, nexus_client, fees, alerts
import time

# Allowed lifecycle comments for unprocessed txids
NEXUS_STATUS_PENDING = "pending_receival"
NEXUS_STATUS_READY = "ready for processing"
NEXUS_STATUS_SENDING = "sending"
NEXUS_STATUS_AWAITING = "sig created, awaiting confirmations"
NEXUS_STATUS_REFUNDED = "refunded"  # (processed file)
NEXUS_STATUS_PROCESSED = "processed"  # (processed file)
NEXUS_STATUS_FEES = "processed as fees"
NEXUS_STATUS_REFUND_PENDING = "refund pending"
NEXUS_STATUS_QUARANTINED = "quarantined"
NEXUS_STATUS_TRADE_BAL_CHECK = "trade balance to be checked"
NEXUS_STATUS_COLLECTING_REFUND = "collecting refund"
_NEXUS_ALLOWED_STATUSES = {
    NEXUS_STATUS_PENDING,
    NEXUS_STATUS_READY,
    NEXUS_STATUS_SENDING,
    NEXUS_STATUS_AWAITING,
    NEXUS_STATUS_REFUNDED,
    NEXUS_STATUS_PROCESSED,
    NEXUS_STATUS_FEES,
    NEXUS_STATUS_REFUND_PENDING,
    NEXUS_STATUS_QUARANTINED,
    NEXUS_STATUS_TRADE_BAL_CHECK,
    NEXUS_STATUS_COLLECTING_REFUND,
}


def _log(kind: str, **fields):
    try:
        parts = [f"{k}={v}" for k, v in fields.items() if v is not None]
        print(f"[{kind}] " + " ".join(parts))
    except Exception:
        pass


def _parse_decimal_amount(val) -> Decimal:
    """Parse a Nexus token amount (string/number) into Decimal token units."""
    if val is None:
        return Decimal(0)
    try:
        return Decimal(str(val).strip())
    except (InvalidOperation, ValueError):
        try:
            return Decimal(float(val))
        except Exception:
            return Decimal(0)


def _format_token_amount(amount: Decimal, decimals: int) -> str:
    """Format a Decimal amount with given token decimals, rounded down, as plain string."""
    if amount < 0:
        amount = Decimal(0)
    q = amount.quantize(Decimal(10) ** -int(decimals), rounding=ROUND_DOWN)
    return format(q, 'f')

def _row_amount_units(r: dict) -> int:
    """Exact credited amount in base units for an unprocessed_txids row.

    Prefers the stored integer `amount_usdd_units`; falls back to the legacy REAL
    column for rows written before that column existed. The REAL path round-trips
    through binary float, which can lose the last base unit.
    """
    units = r.get("amount_usdd_units")
    if units is not None:
        try:
            return int(units)
        except Exception:
            pass
    amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
    return int((amt_dec * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value(rounding=ROUND_DOWN))


def _quarantine_txid(r: dict, reason: str = "") -> None:
    """Quarantine a stuck Nexus credit: MOVE the funds, then record the full row.

    Two bugs are fixed here. The funds were never actually moved (only a status string
    was written), so quarantined funds kept counting toward the backing ratio; and the
    row was written with only (txid, sig), so every other column stayed NULL and the
    operator's quarantine viewer always reported zero.
    """
    txid = r.get("txid")
    try:
        amt_units = _row_amount_units(r)
    except Exception:
        amt_units = 0
    moved = False
    try:
        moved = nexus_client.quarantine_nexus_token(txid, amt_units, reason)
    except Exception as e:
        _log("NEXUS_QUARANTINE_MOVE_ERROR", txid=txid, error=str(e))
    state_db.mark_quarantined_txid(
        txid=txid,
        sig="",
        timestamp=r.get("ts"),
        amount_usdd=float(r.get("amount_usdd") or 0),
        from_address=r.get("from"),
        to_address=r.get("to"),
        owner=r.get("owner"),
        status=NEXUS_STATUS_QUARANTINED if moved else "quarantined (funds NOT moved)",
    )
    alerts.warning(
        "nexus_quarantined",
        reason or "Nexus credit quarantined for manual review",
        txid=txid, amount_units=amt_units, funds_moved=moved,
    )


def _apply_congestion_fee(amount_dec: Decimal) -> Decimal:
    """Subtract a fixed Nexus congestion fee (configured in Nexus token units).

    NOT WIRED IN. Nothing calls this, so NEXUS_CONGESTION_FEE_USDD has no effect and every
    Nexus refund returns the full credited amount, with the operator absorbing the on-chain
    cost. Deducting it is a change to what users receive, so it is left as an explicit
    operator decision rather than switched on silently. Wire it into the four
    refund_nexus_token() call sites below to activate, or delete both if the policy is to
    refund in full.
    """
    try:
        fee_dec = _parse_decimal_amount(getattr(config, "NEXUS_CONGESTION_FEE_USDD", "0"))
    except Exception:
        fee_dec = Decimal(0)
    out = amount_dec - fee_dec
    return out if out > 0 else Decimal(0)

def process_unprocessed_txids(paused: bool = False):
    """Process queued Nexus→Solana entries as soon as possible.

    When `paused` (backing deficit) no NEW is sent out on Solana, but refunds, quarantine
    and confirmation passes still run so user funds are never frozen by the pause.
    
    Steps 3-5 from spec:
    - Resolve receival_account via assets
    - Send the Solana-side token with unique memo 
    - Check confirmations and finalize
    - Handle refunds/quarantine
    """
    from solana.rpc.api import Client as SolClient
    
    # Time budget for processing
    PROCESS_BUDGET_SEC = getattr(config, "UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC", 30)
    process_start = time.time()
    
    try:
        unprocessed = state_db.get_unprocessed_txids_as_dicts()
        refunded_txids = set()
        # Get refunded txids from database
        conn = state_db.sqlite3.connect(state_db.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT txid FROM refunded_txids")
        refunded_txids = {row[0] for row in cursor.fetchall()}
        conn.close()
        
        _log("NEXUS_PROCESS_START", count=len(unprocessed), budget=PROCESS_BUDGET_SEC)
        
        # Priority 1: Resolve receival_account for confirmed entries
        for r in list(unprocessed):
            if time.time() - process_start > PROCESS_BUDGET_SEC:
                _log("NEXUS_PROCESS_BUDGET_EXCEEDED", stage="receival_resolution")
                break
                
            cmt = r.get("comment") or ""
            if cmt and cmt not in _NEXUS_ALLOWED_STATUSES:
                _log("NEXUS_STATUS_UNKNOWN", txid=r.get("txid"), comment=cmt)
            if r.get("receival_account") or r.get("comment") == NEXUS_STATUS_READY:
                continue
            if r.get("comment") == NEXUS_STATUS_PENDING and int(r.get("confirmations") or 0) <= 1:
                continue
            txid = r.get("txid")
            if txid in refunded_txids:
                continue
            owner = r.get("owner")
            asset = nexus_client.find_asset_receival_account_by_txid_and_owner(txid, owner)
            recv = (asset or {}).get("receival_account")
            asset_owner = (asset or {}).get("owner")
            
            if recv and asset_owner and str(asset_owner) == str(owner) and solana_client.is_valid_solana_token_account(recv):
                state_db.update_unprocessed_txid(
                    txid=txid,
                    receival_account=recv,
                    status=NEXUS_STATUS_READY
                )
                r["receival_account"] = recv
                r["comment"] = NEXUS_STATUS_READY
                _log("NEXUS_READY", txid=txid, receival=recv)
            elif recv and asset_owner and str(asset_owner) != str(owner):
                _log("NEXUS_OWNER_MISMATCH", txid=txid, recv_owner=asset_owner, expected_owner=owner)
            elif recv:
                # Invalid token account -> refund path
                amt_units = _row_amount_units(r)
                sender = r.get("from")
                if sender:
                    refund_key = state_db.nexus_refund_attempt_key(txid)
                    if state_db.should_attempt(refund_key):
                        state_db.record_attempt(refund_key)
                        if nexus_client.refund_nexus_token(sender, amt_units, "invalid receival_account"):
                            # Mark as refunded
                            state_db.mark_refunded_txid(
                                txid=txid,
                                timestamp=r.get("ts"),
                                amount_usdd=float(r.get("amount_usdd") or 0),
                                from_address=sender,
                                to_address=r.get("to"),
                                owner_from_address=r.get("owner"),
                                confirmations_credit=r.get("confirmations"),
                                status=NEXUS_STATUS_REFUNDED
                            )
                            # Remove from unprocessed
                            state_db.remove_unprocessed_txid(txid)
                        else:
                            attempts = state_db.get_attempt_count(refund_key)
                            if state_db.attempts_exhausted(refund_key):
                                # Quarantine
                                _quarantine_txid(r)
                                state_db.remove_unprocessed_txid(txid)
                                _log("NEXUS_REFUND_QUARANTINED", txid=txid, reason="invalid_receival_account")
                            else:
                                # Mark as refund pending
                                state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_REFUND_PENDING)
            else:
                # No receival asset yet; if age exceeds timeout attempt refund
                ts_row = int(r.get("ts") or 0)
                if ts_row and (time.time() - ts_row) > getattr(config, "REFUND_TIMEOUT_SEC", 3600):
                    sender = r.get("from")
                    if sender:
                        amt_units = _row_amount_units(r)
                        refund_key = state_db.nexus_refund_unresolved_attempt_key(txid)
                        if state_db.should_attempt(refund_key):
                            state_db.record_attempt(refund_key)
                            if nexus_client.refund_nexus_token(sender, amt_units, "unresolved receival_account"):
                                # Mark as refunded
                                state_db.mark_refunded_txid(
                                    txid=txid,
                                    timestamp=r.get("ts"),
                                    amount_usdd=float(r.get("amount_usdd") or 0),
                                    from_address=sender,
                                    to_address=r.get("to"),
                                    owner_from_address=r.get("owner"),
                                    confirmations_credit=r.get("confirmations"),
                                    status=NEXUS_STATUS_REFUNDED
                                )
                                state_db.remove_unprocessed_txid(txid)
                            else:
                                attempts = state_db.get_attempt_count(refund_key)
                                if state_db.attempts_exhausted(refund_key):
                                    # Quarantine
                                    _quarantine_txid(r)
                                    state_db.remove_unprocessed_txid(txid)
                                    _log("NEXUS_REFUND_QUARANTINED", txid=txid, reason="unresolved_timeout")
                                else:
                                    # Mark for trade balance check
                                    state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_TRADE_BAL_CHECK)

        # Refresh unprocessed list for next priorities
        if time.time() - process_start <= PROCESS_BUDGET_SEC:
            unprocessed = state_db.get_unprocessed_txids_as_dicts()
            
            # Priority 2: Send the Solana-side token for ready entries (skipped while paused)
            for r in list(unprocessed) if not paused else []:
                if time.time() - process_start > PROCESS_BUDGET_SEC:
                    _log("NEXUS_PROCESS_BUDGET_EXCEEDED", stage="solana_sending")
                    break
                    
                # Recovery for entries stuck in SENDING with no recorded signature
                # (e.g. a crash between send and DB write): recover via the txid memo.
                if r.get("comment") == NEXUS_STATUS_SENDING and not r.get("sig"):
                    try:
                        found_sig = solana_client.find_signature_with_memo(f"nexus_txid:{r.get('txid')}")
                        if found_sig:
                            state_db.update_unprocessed_txid(
                                txid=r.get("txid"),
                                status=NEXUS_STATUS_AWAITING,
                                sig=found_sig
                            )
                            r["sig"] = found_sig
                            r["comment"] = NEXUS_STATUS_AWAITING
                            _log("NEXUS_RECOVERED_SIG", txid=r.get("txid"), sig=found_sig)
                    except Exception:
                        pass
                
                # Skip if not ready for sending
                if r.get("comment") != NEXUS_STATUS_READY:
                    continue
                
                txid = r.get("txid")
                recv_account = r.get("receival_account")
                
                if not recv_account:
                    continue  # Will be resolved in Priority 1 next cycle
                
                # Calculate net Solana amount after fees.
                # Derive from the exact integer column, not the legacy REAL one: this is the
                # figure that decides how much leaves the vault, and every refund and
                # quarantine path already reads it through _row_amount_units. Reading the
                # float here made the payout the one money decision in this module still
                # exposed to a binary round-trip.
                amt_nexus = Decimal(_row_amount_units(r)) / (Decimal(10) ** config.USDD_DECIMALS)
                flat_fee = _parse_decimal_amount(getattr(config, "FLAT_FEE_USDC", "0.1"))
                dyn_bps = int(getattr(config, "DYNAMIC_FEE_BPS", 10))
                dyn_fee = (amt_nexus * Decimal(dyn_bps)) / Decimal(10000)
                net_nexus = amt_nexus - flat_fee - dyn_fee
                
                if net_nexus <= 0:
                    # Bug #14 fix: Track the fee (entire Nexus amount is kept as fee)
                    nexus_decimals = int(getattr(config, "NEXUS_TOKEN_DECIMALS", 6))
                    total_fee_nexus_units = int((amt_nexus * (Decimal(10) ** nexus_decimals)).to_integral_value(rounding=ROUND_DOWN))
                    if total_fee_nexus_units > 0:
                        state_db.add_fee_entry(
                            sig=None,
                            txid=txid,
                            kind="fee_only_nexus_process",
                            amount_usdc_units=None,
                            amount_usdd_units=total_fee_nexus_units
                        )
                    # Mark as fee-only processed
                    state_db.mark_processed_txid(
                        txid=txid,
                        timestamp=r.get("ts"),
                        amount_usdd=float(amt_nexus),
                        from_address=r.get("from"),
                        to_address=r.get("to"),
                        owner=r.get("owner") or "",
                        sig="",
                        status=NEXUS_STATUS_FEES
                    )
                    state_db.remove_unprocessed_txid(txid)
                    _log("NEXUS_FEE_ONLY", txid=txid, amount_usdd=str(amt_nexus))
                    continue
                
                # Convert Nexus token units to Solana base units (accounting for decimal differences)
                solana_decimals = int(getattr(config, "SOLANA_TOKEN_DECIMALS", 6))
                nexus_decimals = int(getattr(config, "NEXUS_TOKEN_DECIMALS", 6))
                # net_nexus is already in token units (not base units), convert to Solana base units
                net_solana_units = int((net_nexus * (Decimal(10) ** solana_decimals)).to_integral_value(rounding=ROUND_DOWN))
                
                if net_solana_units <= 0:
                    continue

                # Liquidity pre-check: do not promise a payout the vault cannot cover.
                # Previously an underfunded vault only failed at send time, burning
                # attempts and pushing a good swap into the refund path.
                try:
                    vault_units = solana_client.get_token_account_balance(
                        str(config.VAULT_USDC_ACCOUNT), max_age_sec=5)
                    if vault_units < net_solana_units:
                        alerts.critical(
                            "insufficient_vault_liquidity",
                            "the vault cannot cover a ready swap; holding it (not failing)",
                            txid=txid, needed_units=net_solana_units, vault_units=vault_units,
                        )
                        continue  # stay READY; retry when the vault is topped up
                except Exception as e:
                    _log("NEXUS_LIQUIDITY_CHECK_ERROR", txid=txid, error=str(e))

                # Mark as sending before attempting
                state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_SENDING)

                # Attempt to send the Solana-side token
                send_key = state_db.payout_attempt_key(txid)
                if not state_db.should_attempt(send_key):
                    if state_db.attempts_exhausted(send_key):
                        state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_REFUND_PENDING)
                        _log("NEXUS_SEND_MAX_ATTEMPTS", txid=txid)
                    else:
                        # Only cooling down - keep it READY and retry on a later cycle.
                        state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_READY)
                    continue

                state_db.record_attempt(send_key)
                
                try:
                    # Send the Solana-side token with memo referencing the Nexus txid
                    memo = f"nexus_txid:{txid}"
                    ok, sig = solana_client.send_solana_token_to_account_with_sig(recv_account, net_solana_units, memo)
                    
                    if ok and sig:
                        # Bug #13 fix: Track the fee (Nexus amount - net Solana equivalent)
                        total_fee_nexus = flat_fee + dyn_fee
                        total_fee_nexus_units = int((total_fee_nexus * (Decimal(10) ** nexus_decimals)).to_integral_value(rounding=ROUND_DOWN))
                        if total_fee_nexus_units > 0:
                            state_db.add_fee_entry(
                                sig=None,
                                txid=txid,
                                kind="swap_nexus_to_solana",
                                amount_usdc_units=None,
                                amount_usdd_units=total_fee_nexus_units
                            )
                        state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_AWAITING, sig=sig)
                        _log("NEXUS_SOLANA_SENT", txid=txid, sig=sig, amount=net_solana_units)
                    elif ok and not sig:
                        # Idempotency - already sent
                        state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_AWAITING)
                        _log("NEXUS_SOLANA_ALREADY_SENT", txid=txid)
                    else:
                        # Send failed, leave in SENDING for retry
                        attempts = state_db.get_attempt_count(send_key)
                        max_attempts = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                        if attempts >= max_attempts:
                            state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_REFUND_PENDING)
                            _log("NEXUS_SEND_FAILED_MAX", txid=txid, attempts=attempts)
                        else:
                            _log("NEXUS_SEND_FAILED", txid=txid, attempts=attempts)
                except Exception as e:
                    _log("NEXUS_SEND_ERROR", txid=txid, error=str(e))
                
        # Priority 3: Check confirmations for Solana sends awaiting confirmation
        if time.time() - process_start <= PROCESS_BUDGET_SEC:
            unprocessed = state_db.get_unprocessed_txids_as_dicts()
            for r in list(unprocessed):
                if time.time() - process_start > PROCESS_BUDGET_SEC:
                    break
                    
                if r.get("comment") != NEXUS_STATUS_AWAITING:
                    continue
                
                txid = r.get("txid")
                
                # Confirm the Solana send. Fast path: check the recorded signature directly
                # (1 RPC, batchable) instead of scanning up to 50 txs by memo.
                try:
                    sent_sig = r.get("sig")
                    if sent_sig:
                        found_sig = sent_sig if solana_client.get_signatures_confirmation([sent_sig]).get(sent_sig) else None
                    else:
                        # Legacy/crash fallback: recover the signature by its memo.
                        found_sig = solana_client.find_signature_with_memo(f"nexus_txid:{txid}")
                    if found_sig:
                        # Solana send confirmed - mark as processed. Same exact-column
                        # derivation as the send path, so the archived amount matches the
                        # one the payout was computed from.
                        amt_nexus = Decimal(_row_amount_units(r)) / (Decimal(10) ** config.USDD_DECIMALS)
                        state_db.mark_processed_txid(
                            txid=txid,
                            timestamp=r.get("ts"),
                            amount_usdd=float(amt_nexus),
                            from_address=r.get("from"),
                            to_address=r.get("to"),
                            owner=r.get("owner") or "",
                            sig=found_sig,
                            status=NEXUS_STATUS_PROCESSED
                        )
                        state_db.remove_unprocessed_txid(txid)
                        _log("NEXUS_SOLANA_CONFIRMED", txid=txid, sig=found_sig)
                    else:
                        # Check for timeout - but DON'T auto-refund!
                        # Bug #15 fix: Auto-refunding on confirmation timeout is dangerous
                        # because the payout may have actually been sent (memo search failed).
                        # Mark for manual review instead of triggering automatic refund.
                        ts = int(r.get("ts") or 0)
                        confirm_timeout = int(getattr(config, "SOLANA_CONFIRM_TIMEOUT_SEC", 600))
                        if ts and (time.time() - ts) > confirm_timeout:
                            # Timeout waiting for confirmation - quarantine for manual review
                            # DO NOT auto-refund as the payout may have been sent successfully
                            state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_QUARANTINED)
                            _log("NEXUS_CONFIRM_TIMEOUT_QUARANTINE", txid=txid, age=int(time.time() - ts), reason="manual_review_required")
                except Exception as e:
                    _log("NEXUS_CONFIRM_CHECK_ERROR", txid=txid, error=str(e))
        
        # Priority 4: Process stuck 'trade balance to be checked' entries (FIX for stuck state)
        if time.time() - process_start <= PROCESS_BUDGET_SEC:
            unprocessed = state_db.get_unprocessed_txids_as_dicts()
            for r in list(unprocessed):
                if time.time() - process_start > PROCESS_BUDGET_SEC:
                    break
                    
                if r.get("comment") != NEXUS_STATUS_TRADE_BAL_CHECK:
                    continue
                
                txid = r.get("txid")
                sender = r.get("from")
                
                # Retry asset lookup one more time before refunding
                owner = r.get("owner")
                if owner:
                    asset = nexus_client.find_asset_receival_account_by_txid_and_owner(txid, owner)
                    # Re-verify the owner explicitly, exactly as Priority 1 does. Relying on
                    # the query filter alone made the two paths asymmetric.
                    asset_owner = (asset or {}).get("owner")
                    if asset and asset.get("receival_account") and asset_owner and str(asset_owner) == str(owner):
                        # Found mapping! Move back to ready for processing
                        recv = asset.get("receival_account")
                        if solana_client.is_valid_solana_token_account(recv):
                            state_db.update_unprocessed_txid(
                                txid=txid,
                                receival_account=recv,
                                status=NEXUS_STATUS_READY
                            )
                            _log("NEXUS_TRADE_BAL_RECOVERED", txid=txid, receival=recv)
                            continue
                
                # No recovery possible - move to collecting refund
                state_db.update_unprocessed_txid(txid=txid, status=NEXUS_STATUS_COLLECTING_REFUND)
                _log("NEXUS_TRADE_BAL_TO_REFUND", txid=txid)
        
        # Priority 5: Process 'collecting refund' entries (FIX for stuck state)
        if time.time() - process_start <= PROCESS_BUDGET_SEC:
            unprocessed = state_db.get_unprocessed_txids_as_dicts()
            for r in list(unprocessed):
                if time.time() - process_start > PROCESS_BUDGET_SEC:
                    break
                    
                if r.get("comment") != NEXUS_STATUS_COLLECTING_REFUND:
                    continue
                
                txid = r.get("txid")
                sender = r.get("from")
                
                if not sender:
                    # Can't refund without sender address - quarantine
                    _quarantine_txid(r)
                    state_db.remove_unprocessed_txid(txid)
                    _log("NEXUS_COLLECT_REFUND_NO_SENDER", txid=txid)
                    continue
                
                amt_units = _row_amount_units(r)
                
                refund_key = state_db.nexus_collect_refund_attempt_key(txid)
                if state_db.should_attempt(refund_key):
                    state_db.record_attempt(refund_key)
                    if nexus_client.refund_nexus_token(sender, amt_units, f"collecting_refund_txid:{txid}"):
                        # Mark as refunded
                        state_db.mark_refunded_txid(
                            txid=txid,
                            timestamp=r.get("ts"),
                            amount_usdd=float(r.get("amount_usdd") or 0),
                            from_address=sender,
                            to_address=r.get("to"),
                            owner_from_address=r.get("owner"),
                            confirmations_credit=r.get("confirmations"),
                            status=NEXUS_STATUS_REFUNDED
                        )
                        state_db.remove_unprocessed_txid(txid)
                        _log("NEXUS_COLLECT_REFUND_SUCCESS", txid=txid)
                    else:
                        attempts = state_db.get_attempt_count(refund_key)
                        if state_db.attempts_exhausted(refund_key):
                            _quarantine_txid(r)
                            state_db.remove_unprocessed_txid(txid)
                            _log("NEXUS_COLLECT_REFUND_QUARANTINED", txid=txid, attempts=attempts)
                        else:
                            _log("NEXUS_COLLECT_REFUND_RETRY", txid=txid, attempts=attempts)
                elif state_db.attempts_exhausted(refund_key):
                    _quarantine_txid(r, "collect refund attempts exhausted")
                    state_db.remove_unprocessed_txid(txid)
                    _log("NEXUS_COLLECT_REFUND_MAX_ATTEMPTS", txid=txid)
        
        # Priority 6: Process 'refund pending' entries
        if time.time() - process_start <= PROCESS_BUDGET_SEC:
            unprocessed = state_db.get_unprocessed_txids_as_dicts()
            for r in list(unprocessed):
                if time.time() - process_start > PROCESS_BUDGET_SEC:
                    break
                    
                if r.get("comment") != NEXUS_STATUS_REFUND_PENDING:
                    continue
                
                txid = r.get("txid")
                sender = r.get("from")
                
                if not sender:
                    _quarantine_txid(r)
                    state_db.remove_unprocessed_txid(txid)
                    _log("NEXUS_REFUND_PENDING_NO_SENDER", txid=txid)
                    continue
                
                amt_units = _row_amount_units(r)
                
                refund_key = state_db.nexus_refund_pending_attempt_key(txid)
                if state_db.should_attempt(refund_key):
                    state_db.record_attempt(refund_key)
                    if nexus_client.refund_nexus_token(sender, amt_units, f"refund_pending_txid:{txid}"):
                        state_db.mark_refunded_txid(
                            txid=txid,
                            timestamp=r.get("ts"),
                            amount_usdd=float(r.get("amount_usdd") or 0),
                            from_address=sender,
                            to_address=r.get("to"),
                            owner_from_address=r.get("owner"),
                            confirmations_credit=r.get("confirmations"),
                            status=NEXUS_STATUS_REFUNDED
                        )
                        state_db.remove_unprocessed_txid(txid)
                        _log("NEXUS_REFUND_PENDING_SUCCESS", txid=txid)
                    else:
                        attempts = state_db.get_attempt_count(refund_key)
                        if state_db.attempts_exhausted(refund_key):
                            _quarantine_txid(r)
                            state_db.remove_unprocessed_txid(txid)
                            _log("NEXUS_REFUND_PENDING_QUARANTINED", txid=txid)
                else:
                    _quarantine_txid(r)
                    state_db.remove_unprocessed_txid(txid)
                    _log("NEXUS_REFUND_PENDING_MAX_ATTEMPTS", txid=txid)
            
        # Update waterline after processing
        try:
            safety = int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0))
            active_rows = state_db.get_unprocessed_txids_as_dicts()
            if active_rows:
                ts_candidates = [int(x.get("ts") or 0) for x in active_rows if int(x.get("ts") or 0) > 0]
                if ts_candidates:
                    min_ts = min(ts_candidates)
                    wl = max(0, min_ts - safety)
                    state_db.propose_nexus_waterline(int(wl))
            else:
                # No unprocessed txids: advance waterline to current time minus buffer
                current_ts = int(time.time())
                waterline_ts = max(0, current_ts - safety)
                state_db.propose_nexus_waterline(waterline_ts)
                _log("NEXUS_WATERLINE_ADVANCED", new_ts=waterline_ts, reason="no_unprocessed_txids")
        except Exception:
            pass
            
        elapsed = time.time() - process_start
        _log("NEXUS_PROCESS_COMPLETE", elapsed=f"{elapsed:.2f}s", budget=PROCESS_BUDGET_SEC)
        
    except Exception as e:
        _log("NEXUS_PROCESS_ERROR", error=str(e))


def poll_nexus_deposits():
    """Detect new Nexus credits to treasury and queue them.
    
    Steps 1-2 from spec:
    - Fetch recent Nexus transactions 
    - Queue new credits >= threshold to unprocessed_txids database table
    """
    import subprocess

    treasury_addr = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
    # Build base command. Use register/transactions/finance:token to get both debits and credits.
    base_cmd = [config.NEXUS_CLI]
    projection = (
        "register/transactions/finance:token/"
        "txid,timestamp,confirmations,contracts.id,contracts.OP,contracts.from,contracts.to,contracts.amount"
    )
    base_cmd.append(projection)
    base_cmd.append(f"name={config.NEXUS_TOKEN_NAME}")
    base_cmd.append("sort=timestamp")
    base_cmd.append("order=desc")

    # Proper WHERE clause (Query DSL) for server-side filtering if enabled.
    # Filter at the DUST floor, not the swap minimum: credits between the two are real
    # user funds that must still be fetched so they can be recorded (see below).
    # DSL operates over 'results' root; transactions list returns objects where contracts[] nested.
    # We cannot address array members directly with > filter per doc, so we still download all then client-filter by OP/to.
    # However if CLI supports simple amount filter we'll keep backward compatibility.
    # Use WHERE only if flagged to avoid incompatibility on older CLI.
    where_threshold = getattr(config, "DUST_CREDIT_USDD", None)
    if getattr(config, "USE_NEXUS_WHERE_FILTER_USDD", True) and where_threshold:
        # Attempt to filter by amount and OP CREDIT *heuristically*; if unsupported the CLI should ignore or error (logged).
        # Syntax example from docs: command WHERE 'results.balance>10'
        # For nested contracts we fall back to 'where=contracts.amount>THRESHOLD' if supported; else rely on local filtering.
        try:
            # Prefer a conservative filter just on amount to reduce small tx volume (OP/to filtered client-side anyway)
            base_cmd.append(f"where='contracts.amount>={where_threshold}'")
        except Exception:
            pass
    limit = 100
    max_pages = int(getattr(config, "NEXUS_MAX_PAGES", 5))
    # Anti-DoS: Limit processing per loop iteration
    MAX_PROCESS_PER_LOOP = getattr(config, "MAX_CREDITS_PER_LOOP", 100)

    # Load current sets from database
    processed_txids = set()
    refunded_txids = set()
    unprocessed_txids = set()
    
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT txid FROM processed_txids")
    processed_txids = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT txid FROM refunded_txids")
    refunded_txids = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT txid FROM unprocessed_txids")
    unprocessed_txids = {row[0] for row in cursor.fetchall()}
    conn.close()

    wl_cutoff = 0
    if getattr(config, "HEARTBEAT_WATERLINE_ENABLED", False):
        try:
            # Bug #16 fix: Use nexus_client.get_heartbeat_asset() instead of non-existent read_heartbeat_waterlines
            heartbeat = nexus_client.get_heartbeat_asset()
            if heartbeat:
                wl_nexus = heartbeat.get("last_safe_timestamp_nexus") or heartbeat.get("last_safe_timestamp_usdd") or 0
                wl_cutoff = max(0, int(wl_nexus) - int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0)))
        except Exception:
            wl_cutoff = 0

    try:
        page_ts_candidates: list[int] = []
        backlog_truncated = False
        processed_count = 0
        # Step 1 & 2: fetch treasury credits with pagination
        for page in range(max_pages):
            cmd = list(base_cmd) + [f"limit={limit}", f"offset={page * limit}"]
            try:
                res = subprocess.run(
                    nexus_client.apply_session(cmd),
                    capture_output=True,
                    text=True,
                    timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 12),
                )
            except Exception as e:
                print(f"Error fetching Nexus transactions page {page}: {e}")
                break
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                print(f"Error fetching Nexus transactions page {page}: {err}")
                break
            txs = nexus_client._parse_json_lenient(res.stdout)
            if not isinstance(txs, list):
                txs = [txs]
            if not txs:
                break
            # Determine if we've reached below cutoff (descending order => last element oldest)
            min_ts_page = None
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                ts = int(tx.get("timestamp") or 0)
                if ts:
                    page_ts_candidates.append(ts)
                    min_ts_page = ts if (min_ts_page is None or ts < min_ts_page) else min_ts_page
            # Process credits
            micro_aggregated: list[dict] = []  # buffer of micro credits to aggregate (no owner lookup)
            for tx in txs:
                # Check processing limit for this loop iteration
                if processed_count >= MAX_PROCESS_PER_LOOP:
                    _log("NEXUS_LOOP_LIMIT_REACHED", processed=processed_count, remaining=len(txs) - txs.index(tx))
                    backlog_truncated = True
                    break
                    
                if not isinstance(tx, dict):
                    continue
                txid = tx.get("txid")
                ts = int(tx.get("timestamp") or 0)
                conf = int(tx.get("confirmations") or 0)
                if wl_cutoff and ts and ts < wl_cutoff:
                    continue  # below safety cutoff
                if not txid or txid in processed_txids:
                    continue
                # If already queued as pending, refresh confirmations
                if txid in unprocessed_txids:
                    if conf > 1:
                        state_db.update_unprocessed_txid(txid=txid, confirmations_credit=conf)
                        _log("NEXUS_CONF_THRESHOLD", txid=txid, confirmations=conf)
                    continue
                contracts = tx.get("contracts") or []
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    if str(c.get("OP") or "").upper() != "CREDIT":
                        continue
                    # Look for CREDIT operations TO the treasury account (user sending funds to the treasury for swapping)
                    to = c.get("to")
                    def _addr(obj) -> str:
                        if isinstance(obj, dict):
                            # Check both address field and name field
                            a = obj.get("address") or obj.get("name")
                            return str(a) if a else ""
                        if isinstance(obj, str):
                            return obj
                        return ""
                    
                    to_addr = _addr(to)
                    # Skip if this credit is not TO our treasury account
                    if to_addr != treasury_addr:
                        continue
                    sender = _addr(c.get("from"))
                    amount_dec = _parse_decimal_amount(c.get("amount"))
                    if amount_dec <= 0:
                        continue
                        
                    # Dust floor (anti-DoS): below this we ignore the credit entirely.
                    dust_threshold = Decimal(config.DUST_CREDIT_NEXUS_UNITS) / (Decimal(10) ** config.USDD_DECIMALS)
                    if amount_dec < dust_threshold:
                        # True spam dust: no state writes, no fee accounting.
                        continue

                    # Below the swap minimum but above dust: this is real user money.
                    # It must NEVER be dropped silently - record it so the funds are
                    # accounted for and the sender is traceable for manual resolution.
                    min_credit_threshold = Decimal(config.MIN_CREDIT_NEXUS_UNITS) / (Decimal(10) ** config.USDD_DECIMALS)
                    if amount_dec < min_credit_threshold:
                        owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                        below_min_units = int((amount_dec * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value(rounding=ROUND_DOWN))
                        if below_min_units > 0:
                            state_db.add_fee_entry(
                                sig=None,
                                txid=txid,
                                kind="below_min_credit_nexus",
                                amount_usdc_units=None,
                                amount_usdd_units=below_min_units,
                            )
                        state_db.mark_processed_txid(
                            txid=txid,
                            timestamp=ts,
                            amount_usdd=float(amount_dec),
                            from_address=sender,
                            to_address=to_addr,
                            owner=owner or "",
                            sig="",
                            status=NEXUS_STATUS_FEES,
                        )
                        processed_txids.add(txid)
                        processed_count += 1
                        _log("NEXUS_BELOW_MIN_CREDIT", txid=txid, amount=str(amount_dec),
                             minimum=str(min_credit_threshold), sender=sender)
                        continue


                    # Per-swap size cap: queue oversized credits for refund rather than
                    # committing the vault to a payout that large.
                    max_swap_nexus = int(getattr(config, "MAX_SWAP_NEXUS_UNITS", 0) or 0)
                    credit_units = int((amount_dec * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value(rounding=ROUND_DOWN))
                    if max_swap_nexus > 0 and credit_units > max_swap_nexus:
                        owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                        state_db.add_unprocessed_txid(
                            txid=txid, timestamp=ts, amount_usdd=float(amount_dec),
                            from_address=sender, to_address=to_addr, owner_from_address=owner,
                            confirmations_credit=conf, status=NEXUS_STATUS_REFUND_PENDING,
                            amount_usdd_units=credit_units,
                        )
                        unprocessed_txids.add(txid)
                        processed_count += 1
                        alerts.warning("swap_over_cap",
                                       "Nexus credit exceeds MAX_SWAP_USDD; queued for refund",
                                       txid=txid, amount_units=credit_units, cap_units=max_swap_nexus)
                        continue

                    flat_nexus_dec = _parse_decimal_amount(getattr(config, "FLAT_FEE_USDD", "0.1"))
                    dyn_bps = int(getattr(config, "DYNAMIC_FEE_BPS", 0))
                    dyn_fee_dec = (amount_dec * Decimal(max(0, dyn_bps))) / Decimal(10000)
                    if amount_dec <= (flat_nexus_dec + dyn_fee_dec):
                        # Add to processed as fees
                        owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                        # Bug #14 fix: Track the fee (entire amount is kept as fee)
                        total_fee_nexus_units = int((amount_dec * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value(rounding=ROUND_DOWN))
                        if total_fee_nexus_units > 0:
                            state_db.add_fee_entry(
                                sig=None,
                                txid=txid,
                                kind="fee_only_nexus_credit",
                                amount_usdc_units=None,
                                amount_usdd_units=total_fee_nexus_units
                            )
                        state_db.mark_processed_txid(
                            txid=txid,
                            timestamp=ts,
                            amount_usdd=float(amount_dec),
                            from_address=sender,
                            to_address=to_addr,
                            owner=owner or "",
                            sig="",
                            status=NEXUS_STATUS_FEES
                        )
                        processed_txids.add(txid)
                        processed_count += 1
                        continue
                    if txid in unprocessed_txids:
                        continue
                    # Owner lookup only for non-micro credits
                    owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                    state_db.add_unprocessed_txid(
                        txid=txid,
                        timestamp=ts,
                        amount_usdd=float(amount_dec),
                        from_address=sender,
                        to_address=to_addr,
                        owner_from_address=owner,
                        confirmations_credit=conf,
                        status=NEXUS_STATUS_PENDING,
                        # Exact base units: refunds are derived from this, not the REAL column.
                        amount_usdd_units=int((amount_dec * (Decimal(10) ** config.USDD_DECIMALS)).to_integral_value(rounding=ROUND_DOWN)),
                    )
                    unprocessed_txids.add(txid)
                    processed_count += 1
                    _log("NEXUS_QUEUED", txid=txid, amount=str(amount_dec))
            # Micro credits are fully ignored now (no aggregation flush)

            # Break conditions
            if len(txs) < limit:
                break  # no more pages
            if wl_cutoff and min_ts_page and min_ts_page < wl_cutoff:
                break  # older than cutoff reached
            if page + 1 >= max_pages:
                backlog_truncated = True
                break
        if backlog_truncated:
            print(f"[warn] USDD_PAGINATION_BACKLOG: reached max pages ({max_pages}) with full pages; potential older deposits pending.")

        # Step 6: waterline proposal (only advance when safe)
        try:
            safety = int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0))
            rows = state_db.get_unprocessed_txids_as_dicts()
            if rows:
                ts_candidates = [int(x.get("ts") or 0) for x in rows if int(x.get("ts") or 0) > 0]
                if ts_candidates:
                    min_ts = min(ts_candidates)
                    wl = max(0, min_ts - safety)
                    state_db.propose_nexus_waterline(int(wl))
            else:
                if page_ts_candidates and not backlog_truncated:
                    min_page_ts = min(int(t) for t in page_ts_candidates if int(t) > 0)
                    wl = max(0, min_page_ts - safety)
                    state_db.propose_nexus_waterline(int(wl))
                elif backlog_truncated:
                    _log("NEXUS_WATERLINE_HOLD", reason="pagination_truncated")
                else:
                    # No unprocessed txids and no page data: advance waterline to current time minus buffer
                    # This prevents unnecessary re-scanning of old transactions
                    current_ts = int(time.time())
                    waterline_ts = max(0, current_ts - safety)
                    state_db.propose_nexus_waterline(waterline_ts)
                    _log("NEXUS_WATERLINE_ADVANCED", new_ts=waterline_ts, reason="no_unprocessed_txids")
        except Exception:
            pass
    except Exception as e:
        print(f"Error polling Nexus deposits: {e}")
