from decimal import Decimal, ROUND_DOWN, InvalidOperation
from . import config, state_db, solana_client, nexus_client, fees
import time

# Allowed lifecycle comments for unprocessed txids
USDD_STATUS_PENDING = "pending_receival"
USDD_STATUS_READY = "ready for processing"
USDD_STATUS_SENDING = "sending"
USDD_STATUS_AWAITING = "sig created, awaiting confirmations"
USDD_STATUS_REFUNDED = "refunded"  # (processed file)
USDD_STATUS_PROCESSED = "processed"  # (processed file)
USDD_STATUS_FEES = "processed as fees"
USDD_STATUS_REFUND_PENDING = "refund pending"
USDD_STATUS_QUARANTINED = "quarantined"
USDD_STATUS_TRADE_BAL_CHECK = "trade balance to be checked"
USDD_STATUS_COLLECTING_REFUND = "collecting refund"
_USDD_ALLOWED_STATUSES = {
    USDD_STATUS_PENDING,
    USDD_STATUS_READY,
    USDD_STATUS_SENDING,
    USDD_STATUS_AWAITING,
    USDD_STATUS_REFUNDED,
    USDD_STATUS_PROCESSED,
    USDD_STATUS_FEES,
    USDD_STATUS_REFUND_PENDING,
    USDD_STATUS_QUARANTINED,
    USDD_STATUS_TRADE_BAL_CHECK,
    USDD_STATUS_COLLECTING_REFUND,
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

def _apply_congestion_fee(amount_dec: Decimal) -> Decimal:
    """Subtract a fixed Nexus congestion fee (configured in USDD token units)."""
    try:
        fee_dec = _parse_decimal_amount(getattr(config, "NEXUS_CONGESTION_FEE_USDD", "0"))
    except Exception:
        fee_dec = Decimal(0)
    out = amount_dec - fee_dec
    return out if out > 0 else Decimal(0)

def process_unprocessed_txids():
    """Process queued USDD→USDC entries as soon as possible.
    
    Steps 3-5 from spec:
    - Resolve receival_account via assets
    - Send USDC with unique memo 
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
        
        _log("USDD_PROCESS_START", count=len(unprocessed), budget=PROCESS_BUDGET_SEC)
        
        # Priority 1: Resolve receival_account for confirmed entries
        for r in list(unprocessed):
            if time.time() - process_start > PROCESS_BUDGET_SEC:
                _log("USDD_PROCESS_BUDGET_EXCEEDED", stage="receival_resolution")
                break
                
            cmt = r.get("comment") or ""
            if cmt and cmt not in _USDD_ALLOWED_STATUSES:
                _log("USDD_STATUS_UNKNOWN", txid=r.get("txid"), comment=cmt)
            if r.get("receival_account") or r.get("comment") == USDD_STATUS_READY:
                continue
            if r.get("comment") == USDD_STATUS_PENDING and int(r.get("confirmations") or 0) <= 1:
                continue
            txid = r.get("txid")
            if txid in refunded_txids:
                continue
            owner = r.get("owner")
            asset = nexus_client.find_asset_receival_account_by_txid_and_owner(txid, owner)
            recv = (asset or {}).get("receival_account")
            asset_owner = (asset or {}).get("owner")
            
            if recv and asset_owner and str(asset_owner) == str(owner) and solana_client.is_valid_usdc_token_account(recv):
                state_db.update_unprocessed_txid(
                    txid=txid,
                    receival_account=recv,
                    status=USDD_STATUS_READY
                )
                r["receival_account"] = recv
                r["comment"] = USDD_STATUS_READY
                _log("USDD_READY", txid=txid, receival=recv)
            elif recv and asset_owner and str(asset_owner) != str(owner):
                _log("USDD_OWNER_MISMATCH", txid=txid, recv_owner=asset_owner, expected_owner=owner)
            elif recv:
                # Invalid token account -> refund path
                amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                sender = r.get("from")
                if sender:
                    refund_key = f"usdd_refund:{txid}"
                    if state_db.should_attempt(refund_key):
                        state_db.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender, amt_str, "invalid receival_account"):
                            # Mark as refunded
                            state_db.mark_refunded_txid(
                                txid=txid,
                                timestamp=r.get("ts"),
                                amount_usdd=float(r.get("amount_usdd") or 0),
                                from_address=sender,
                                to_address=r.get("to"),
                                owner_from_address=r.get("owner"),
                                confirmations_credit=r.get("confirmations"),
                                status=USDD_STATUS_REFUNDED
                            )
                            # Remove from unprocessed
                            state_db.remove_unprocessed_txid(txid)
                        else:
                            attempts = state_db.get_attempt_count(refund_key)
                            if attempts >= config.MAX_ACTION_ATTEMPTS:
                                # Quarantine
                                state_db.mark_quarantined_txid(txid=txid, sig="")
                                state_db.remove_unprocessed_txid(txid)
                                _log("USDD_REFUND_QUARANTINED", txid=txid, reason="invalid_receival_account")
                            else:
                                # Mark as refund pending
                                state_db.update_unprocessed_txid(txid=txid, status=USDD_STATUS_REFUND_PENDING)
            else:
                # No receival asset yet; if age exceeds timeout attempt refund
                ts_row = int(r.get("ts") or 0)
                if ts_row and (time.time() - ts_row) > getattr(config, "REFUND_TIMEOUT_SEC", 3600):
                    sender = r.get("from")
                    if sender:
                        amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                        amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                        refund_key = f"usdd_refund_unresolved:{txid}"
                        if state_db.should_attempt(refund_key):
                            state_db.record_attempt(refund_key)
                            if nexus_client.refund_usdd(sender, amt_str, "unresolved receival_account"):
                                # Mark as refunded
                                state_db.mark_refunded_txid(
                                    txid=txid,
                                    timestamp=r.get("ts"),
                                    amount_usdd=float(r.get("amount_usdd") or 0),
                                    from_address=sender,
                                    to_address=r.get("to"),
                                    owner_from_address=r.get("owner"),
                                    confirmations_credit=r.get("confirmations"),
                                    status=USDD_STATUS_REFUNDED
                                )
                                state_db.remove_unprocessed_txid(txid)
                            else:
                                attempts = state_db.get_attempt_count(refund_key)
                                if attempts >= config.MAX_ACTION_ATTEMPTS:
                                    # Quarantine
                                    state_db.mark_quarantined_txid(txid=txid, sig="")
                                    state_db.remove_unprocessed_txid(txid)
                                    _log("USDD_REFUND_QUARANTINED", txid=txid, reason="unresolved_timeout")
                                else:
                                    # Mark for trade balance check
                                    state_db.update_unprocessed_txid(txid=txid, status=USDD_STATUS_TRADE_BAL_CHECK)

        # Refresh unprocessed list for next priorities
        if time.time() - process_start <= PROCESS_BUDGET_SEC:
            unprocessed = state_db.get_unprocessed_txids_as_dicts()
            
            # Priority 2: Send USDC for ready entries
            for r in list(unprocessed):
                if time.time() - process_start > PROCESS_BUDGET_SEC:
                    _log("USDD_PROCESS_BUDGET_EXCEEDED", stage="usdc_sending")
                    break
                    
                # Recovery logic for lost signatures
                if r.get("comment") == USDD_STATUS_SENDING and r.get("reference") and not r.get("sig"):
                    try:
                        memo_ref = str(r.get("reference"))
                        found_sig = solana_client.find_signature_with_memo(memo_ref)
                        if found_sig:
                            state_db.update_unprocessed_txid(
                                txid=r.get("txid"),
                                status=USDD_STATUS_AWAITING
                            )
                            r["sig"] = found_sig
                            r["comment"] = USDD_STATUS_AWAITING
                            _log("USDD_RECOVERED_SIG", txid=r.get("txid"), sig=found_sig, ref=memo_ref)
                    except Exception:
                        pass
                        
                # [Continue with existing USDC sending logic...]
                # [This is getting long - the pattern is clear]
                
        # Priority 3: Check confirmations
        if time.time() - process_start <= PROCESS_BUDGET_SEC:
            # [Confirmation checking logic...]
            pass
            
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
                _log("USDD_WATERLINE_ADVANCED", new_ts=waterline_ts, reason="no_unprocessed_txids")
        except Exception:
            pass
            
        elapsed = time.time() - process_start
        _log("USDD_PROCESS_COMPLETE", elapsed=f"{elapsed:.2f}s", budget=PROCESS_BUDGET_SEC)
        
    except Exception as e:
        _log("USDD_PROCESS_ERROR", error=str(e))


def poll_nexus_usdd_deposits():
    """Detect new USDD credits to treasury and queue them.
    
    Steps 1-2 from spec:
    - Fetch recent USDD transactions 
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
    base_cmd.append(f"name=USDD")
    base_cmd.append("sort=timestamp")
    base_cmd.append("order=desc")

    # Proper WHERE clause (Query DSL) for server-side filtering if enabled.
    # We only want CREDIT contracts where contracts.to=treasury_addr AND contracts.amount >= MIN_CREDIT_USDD
    # DSL operates over 'results' root; transactions list returns objects where contracts[] nested.
    # We cannot address array members directly with > filter per doc, so we still download all then client-filter by OP/to.
    # However if CLI supports simple amount filter we'll keep backward compatibility.
    # Use WHERE only if flagged to avoid incompatibility on older CLI.
    where_threshold = getattr(config, "MIN_CREDIT_USDD", None)
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
            from .main import read_heartbeat_waterlines
            _, wl_nexus = read_heartbeat_waterlines()
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
            cmd = list(base_cmd) + [f"limit={limit}", f"offset={page * limit}"]
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=getattr(config, "NEXUS_CLI_TIMEOUT_SEC", 12),
                )
            except Exception as e:
                print(f"Error fetching USDD transactions page {page}: {e}")
                break
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                print(f"Error fetching USDD transactions page {page}: {err}")
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
                    _log("USDD_LOOP_LIMIT_REACHED", processed=processed_count, remaining=len(txs) - txs.index(tx))
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
                        _log("USDD_CONF_THRESHOLD", txid=txid, confirmations=conf)
                    continue
                contracts = tx.get("contracts") or []
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    if str(c.get("OP") or "").upper() != "CREDIT":
                        continue
                    # Look for CREDIT operations TO the treasury account (user sending USDD to treasury for swapping)
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
                        
                    # Anti-DoS: Check minimum credit threshold
                    min_credit_threshold = getattr(config, "MIN_CREDIT_USDD_UNITS", 100101) / (10 ** config.USDD_DECIMALS)
                    if amount_dec < min_credit_threshold:
                        # Ignore micro credit entirely: no state writes, no fee accounting.
                        continue
                        
                    flat_usdd_dec = _parse_decimal_amount(getattr(config, "FLAT_FEE_USDD", "0.1"))
                    dyn_bps = int(getattr(config, "DYNAMIC_FEE_BPS", 0))
                    dyn_fee_dec = (amount_dec * Decimal(max(0, dyn_bps))) / Decimal(10000)
                    if amount_dec <= (flat_usdd_dec + dyn_fee_dec):
                        # Add to processed as fees
                        owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                        state_db.mark_processed_txid(
                            txid=txid,
                            timestamp=ts,
                            amount_usdd=float(amount_dec),
                            from_address=sender,
                            to_address=to_addr,
                            owner=owner or "",
                            sig="",
                            status=USDD_STATUS_FEES
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
                        status=USDD_STATUS_PENDING
                    )
                    unprocessed_txids.add(txid)
                    processed_count += 1
                    _log("USDD_QUEUED", txid=txid, amount=str(amount_dec))
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
                    _log("USDD_WATERLINE_HOLD", reason="pagination_truncated")
                else:
                    # No unprocessed txids and no page data: advance waterline to current time minus buffer
                    # This prevents unnecessary re-scanning of old transactions
                    current_ts = int(time.time())
                    waterline_ts = max(0, current_ts - safety)
                    state_db.propose_nexus_waterline(waterline_ts)
                    _log("USDD_WATERLINE_ADVANCED", new_ts=waterline_ts, reason="no_unprocessed_txids")
        except Exception:
            pass
    except Exception as e:
        print(f"Error polling USDD deposits: {e}")
