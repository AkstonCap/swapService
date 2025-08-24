from decimal import Decimal, ROUND_DOWN, InvalidOperation
from . import config, state, solana_client, nexus_client, fees

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

def poll_nexus_usdd_deposits():
    """USDD->USDC pipeline per spec:
    1) Detect USDD credits to treasury; append new entries to unprocessed_txids.json
    2) For each unprocessed entry, resolve receival_account via assets WHERE txid_toService AND owner
    3) If valid USDC token account, mark ready
    4) Send USDC minus fees from vault with memo containing a unique integer; record sig and reference
    5) Confirm sig>2 and move to processed_txids.json with reference
    6) Move waterline to min ts of unprocessed - buffer
    Refund USDD if invalid receival_account, send fails, or not confirmed within timeout.
    """
    import subprocess, time
    from solana.rpc.api import Client as SolClient

    unprocessed_path = "unprocessed_txids.json"
    processed_path = "processed_txids.json"

    treasury_addr = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
    # Build base command. We use a WHERE clause to restrict to CREDIT contracts TO treasury and amount >= threshold.
    base_cmd = [config.NEXUS_CLI]
    projection = (
        "finance/transactions/token/"
        "txid,timestamp,confirmations,contracts.id,contracts.OP,contracts.from,contracts.to,contracts.amount"
    )
    base_cmd.append(projection)
    base_cmd.append(f"name={treasury_addr}")
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

    # Load current sets
    unprocessed = state.read_jsonl(unprocessed_path)
    processed = state.read_jsonl(processed_path)
    processed_txids = {r.get("txid") for r in processed}
    refunded_txids = {r.get("txid") for r in processed if (r.get("comment") == "refunded")}
    unprocessed_txids = {r.get("txid") for r in unprocessed}

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
                    for existing in unprocessed:
                        if existing.get("txid") == txid:
                            if existing.get("comment") == USDD_STATUS_PENDING:
                                existing["confirmations"] = conf
                                if conf > 1:
                                    _log("USDD_CONF_THRESHOLD", txid=txid, confirmations=conf)
                            break
                contracts = tx.get("contracts") or []
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    if str(c.get("OP") or "").upper() != "CREDIT":
                        continue
                    to = c.get("to")
                    def _addr(obj) -> str:
                        if isinstance(obj, dict):
                            a = obj.get("address") or obj.get("name")
                            return str(a) if a else ""
                        if isinstance(obj, str):
                            return obj
                        return ""
                    if _addr(to) != treasury_addr:
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
                        row = {
                            "txid": txid,
                            "ts": ts,
                            "from": sender,
                            "owner": (nexus_client.get_account_info(sender) or {}).get("owner"),
                            "amount_usdd": str(amount_dec),
                            "comment": USDD_STATUS_FEES,
                        }
                        state.append_jsonl(processed_path, row)
                        processed_txids.add(txid)
                        processed_count += 1
                        continue
                    if txid in unprocessed_txids:
                        continue
                    # Owner lookup only for non-micro credits
                    owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                    row = {
                        "txid": txid,
                        "ts": ts,
                        "from": sender,
                        "owner": owner,
                        "amount_usdd": str(amount_dec),
                        "comment": USDD_STATUS_PENDING,
                        "confirmations": conf,
                    }
                    state.append_jsonl(unprocessed_path, row)
                    unprocessed.append(row)
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

        # Step 3: resolve receival_account for unprocessed (skip already refunded)
        for r in list(unprocessed):
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
                def _pred(x):
                    return x.get("txid") == txid
                def _upd(x):
                    x["receival_account"] = recv
                    x["comment"] = USDD_STATUS_READY
                    return x
                state.update_jsonl_row(unprocessed_path, _pred, _upd)
                r["receival_account"] = recv
                r["comment"] = USDD_STATUS_READY
                _log("USDD_READY", txid=txid, receival=recv)
            elif recv and asset_owner and str(asset_owner) != str(owner):
                _log("USDD_OWNER_MISMATCH", txid=txid, recv_owner=asset_owner, expected_owner=owner)
            elif recv:
                # Invalid token account -> refund path with retry/quarantine
                amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                sender = r.get("from")
                if sender:
                    refund_key = f"usdd_refund:{txid}"
                    if state.should_attempt(refund_key):
                        state.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender, amt_str, "invalid receival_account"):
                            row = dict(r)
                            row["comment"] = USDD_STATUS_REFUNDED
                            state.append_jsonl(processed_path, row)
                            unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                            state.write_jsonl(unprocessed_path, unprocessed)
                        else:
                            attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                            if attempts >= config.MAX_ACTION_ATTEMPTS:
                                row = dict(r)
                                row["comment"] = USDD_STATUS_QUARANTINED
                                state.append_jsonl(processed_path, row)
                                _log("USDD_REFUND_QUARANTINED", txid=txid, reason="invalid_receival_account")
                                unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                                state.write_jsonl(unprocessed_path, unprocessed)
                            else:
                                def _pred_rp(x):
                                    return x.get("txid") == txid
                                def _upd_rp(x):
                                    if x.get("comment") not in (USDD_STATUS_REFUNDED, USDD_STATUS_QUARANTINED):
                                        x["comment"] = USDD_STATUS_REFUND_PENDING
                                    return x
                                state.update_jsonl_row(unprocessed_path, _pred_rp, _upd_rp)
            else:
                # No receival asset yet; if age exceeds REFUND_TIMEOUT_SEC attempt refund; else leave pending
                ts_row = int(r.get("ts") or 0)
                if ts_row and (time.time() - ts_row) > getattr(config, "REFUND_TIMEOUT_SEC", 3600):
                    sender = r.get("from")
                    if sender:
                        amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                        amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                        refund_key = f"usdd_refund_unresolved:{txid}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            if nexus_client.refund_usdd(sender, amt_str, "unresolved receival_account"):
                                row = dict(r)
                                row["comment"] = USDD_STATUS_REFUNDED
                                state.append_jsonl(processed_path, row)
                                unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                                state.write_jsonl(unprocessed_path, unprocessed)
                            else:
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                if attempts >= config.MAX_ACTION_ATTEMPTS:
                                    row = dict(r)
                                    row["comment"] = USDD_STATUS_QUARANTINED
                                    state.append_jsonl(processed_path, row)
                                    _log("USDD_REFUND_QUARANTINED", txid=txid, reason="unresolved_timeout")
                                    unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                                    state.write_jsonl(unprocessed_path, unprocessed)
                                else:
                                    def _pred_rp3(x):
                                        return x.get("txid") == txid
                                    def _upd_rp3(x):
                                        if x.get("comment") not in (USDD_STATUS_REFUNDED, USDD_STATUS_QUARANTINED, USDD_STATUS_TRADE_BAL_CHECK, USDD_STATUS_COLLECTING_REFUND):
                                            x["comment"] = USDD_STATUS_TRADE_BAL_CHECK
                                        return x
                                    state.update_jsonl_row(unprocessed_path, _pred_rp3, _upd_rp3)

        # Step 4: send USDC for ready entries
        for r in list(unprocessed):
            # Immediate recovery: if SENDING with reference but no sig, attempt memo lookup before any timeout logic
            if r.get("comment") == USDD_STATUS_SENDING and r.get("reference") and not r.get("sig"):
                try:
                    memo_ref = str(r.get("reference"))
                    found_sig = solana_client.find_signature_with_memo(memo_ref)
                    if found_sig:
                        def _pred_found(x):
                            return x.get("txid") == r.get("txid") and x.get("comment") == USDD_STATUS_SENDING
                        def _upd_found(x):
                            x["sig"] = found_sig
                            x["comment"] = USDD_STATUS_AWAITING
                            return x
                        if state.update_jsonl_row(unprocessed_path, _pred_found, _upd_found):
                            r["sig"] = found_sig
                            r["comment"] = USDD_STATUS_AWAITING
                            _log("USDD_RECOVERED_SIG", txid=r.get("txid"), sig=found_sig, ref=memo_ref)
                except Exception:
                    pass
            # Allow recovery of stale 'sending' reservations older than 300s only if no reference or no sig recovered
            if r.get("comment") == USDD_STATUS_SENDING and not r.get("sig"):
                try:
                    if (time.time() - int(r.get("reservation_ts") or 0)) > 300:
                        # Only revert if we failed to recover signature above
                        def _pred_rev(x):
                            return x.get("txid") == r.get("txid") and x.get("comment") == USDD_STATUS_SENDING and not x.get("sig")
                        def _upd_rev(x):
                            if x.get("comment") == USDD_STATUS_SENDING and not x.get("sig"):
                                x["comment"] = USDD_STATUS_READY
                            return x
                        if state.update_jsonl_row(unprocessed_path, _pred_rev, _upd_rev):
                            r["comment"] = USDD_STATUS_READY
                except Exception:
                    pass
            if r.get("comment") != USDD_STATUS_READY or not r.get("receival_account"):
                continue
            txid = r.get("txid")
            if txid in refunded_txids:
                continue
            def _pred_res(x):
                return x.get("txid") == txid and x.get("comment") == USDD_STATUS_READY and not x.get("sig")
            def _upd_res(x):
                x["comment"] = USDD_STATUS_SENDING
                x["reservation_ts"] = int(time.time())
                return x
            if not state.update_jsonl_row(unprocessed_path, _pred_res, _upd_res):
                continue
            recv_acct = r.get("receival_account")
            valid_recv = False
            if recv_acct and solana_client.is_valid_usdc_token_account(recv_acct):
                valid_recv = True
            else:
                if recv_acct and solana_client.has_usdc_ata(recv_acct):
                    ata = solana_client.derive_usdc_ata(recv_acct)
                    if ata and solana_client.is_valid_usdc_token_account(ata):
                        recv_acct = ata
                        valid_recv = True
                        def _pred_fix(x):
                            return x.get("txid") == txid and x.get("comment") == USDD_STATUS_SENDING
                        def _upd_fix(x):
                            x["receival_account"] = recv_acct
                            return x
                        state.update_jsonl_row(unprocessed_path, _pred_fix, _upd_fix)
            if not valid_recv:
                def _pred_rev2(x):
                    return x.get("txid") == txid and x.get("comment") == USDD_STATUS_SENDING
                def _upd_rev2(x):
                    x["comment"] = USDD_STATUS_PENDING
                    return x
                state.update_jsonl_row(unprocessed_path, _pred_rev2, _upd_rev2)
                _log("USDD_REVALIDATION_FAIL", txid=txid, receival=r.get("receival_account"))
                continue
            for x in unprocessed:
                if x.get("txid") == txid:
                    x["comment"] = USDD_STATUS_SENDING
                    x["reservation_ts"] = int(time.time())
                    if recv_acct and recv_acct != x.get("receival_account"):
                        x["receival_account"] = recv_acct
                    break
            if recv_acct and recv_acct != r.get("receival_account"):
                r["receival_account"] = recv_acct
            amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
            usdc_units = int((amt_dec * (Decimal(10) ** config.USDC_DECIMALS)).quantize(Decimal(1), rounding=ROUND_DOWN))
            flat_fee = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
            bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
            pre_dyn = max(0, usdc_units - flat_fee)
            dyn_fee = (pre_dyn * bps) // 10000
            net_usdc = max(0, pre_dyn - dyn_fee)
            if net_usdc <= 0:
                row = dict(r)
                row["comment"] = USDD_STATUS_FEES
                state.append_jsonl(processed_path, row)
                unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                state.write_jsonl(unprocessed_path, unprocessed)
                continue
            # Allocate reference (unique integer) BUT memo must carry nexus txid per spec: nexus_txid:<txid>
            ref = state.next_reference()  # still stored for internal auditing
            memo = f"nexus_txid:{txid}"
            # Persist send intent (idempotency anchor)
            def _pred_intent(x):
                return x.get("txid") == txid and x.get("comment") == USDD_STATUS_SENDING and not x.get("reference")
            def _upd_intent(x):
                if not x.get("reference"):
                    x["reference"] = ref
                    x["intent_ts"] = int(time.time())
                return x
            state.update_jsonl_row(unprocessed_path, _pred_intent, _upd_intent)
            # Crash recovery: if a prior send with this memo already landed, capture it instead of resending
            existing_sig = solana_client.find_signature_with_memo(memo)
            if existing_sig:
                ok, sig = True, existing_sig
            else:
                ok, sig = solana_client.send_usdc_to_token_account_with_sig(r["receival_account"], net_usdc, memo)
            if ok and sig:
                if (flat_fee + dyn_fee) > 0:
                    fees.add_usdc_fee(flat_fee + dyn_fee)
                def _pred(x):
                    return x.get("txid") == txid and x.get("comment") == USDD_STATUS_SENDING
                def _upd(x):
                    x["sig"] = sig
                    x["reference"] = ref
                    x["comment"] = USDD_STATUS_AWAITING
                    return x
                state.update_jsonl_row(unprocessed_path, _pred, _upd)
                for x in unprocessed:
                    if x.get("txid") == txid:
                        x["sig"] = sig
                        x["reference"] = ref
                        x["comment"] = USDD_STATUS_AWAITING
                        break
            else:
                sender = r.get("from")
                amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                if sender and (txid not in refunded_txids):
                    refund_key = f"usdd_refund:{txid}"
                    if state.should_attempt(refund_key):
                        state.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender, amt_str, "USDC send failed"):
                            row = dict(r)
                            row["comment"] = USDD_STATUS_REFUNDED
                            state.append_jsonl(processed_path, row)
                            refunded_txids.add(txid)
                            unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                            state.write_jsonl(unprocessed_path, unprocessed)
                        else:
                            attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                            if attempts >= config.MAX_ACTION_ATTEMPTS:
                                row = dict(r)
                                row["comment"] = USDD_STATUS_QUARANTINED
                                state.append_jsonl(processed_path, row)
                                _log("USDD_REFUND_QUARANTINED", txid=txid, reason="send_failed")
                                unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                                state.write_jsonl(unprocessed_path, unprocessed)
                            else:
                                def _pred_rp2(x):
                                    return x.get("txid") == txid
                                def _upd_rp2(x):
                                    if x.get("comment") not in (USDD_STATUS_REFUNDED, USDD_STATUS_QUARANTINED):
                                        x["comment"] = USDD_STATUS_REFUND_PENDING
                                    return x
                                state.update_jsonl_row(unprocessed_path, _pred_rp2, _upd_rp2)
                def _pred_clear_send(x):
                    return x.get("txid") == txid and x.get("comment") == USDD_STATUS_SENDING
                def _upd_clear_send(x):
                    if x.get("comment") == USDD_STATUS_SENDING and x.get("txid") == txid and not x.get("sig"):
                        if x.get("comment") != USDD_STATUS_REFUND_PENDING:
                            x["comment"] = USDD_STATUS_REFUND_PENDING
                    return x
                state.update_jsonl_row(unprocessed_path, _pred_clear_send, _upd_clear_send)

        # Step 5: confirmations, timeouts, refund retries
        try:
            client = SolClient(getattr(config, "SOLANA_RPC_URL", getattr(config, "RPC_URL", None)))
        except Exception:
            client = None
        now = time.time()
        new_unprocessed: list[dict] = []
        for r in state.read_jsonl(unprocessed_path):
            if r.get("comment") == USDD_STATUS_AWAITING and r.get("sig") and client:
                try:
                    st = client.get_signature_statuses([r["sig"]])
                    js = getattr(st, "to_json", None)
                    if callable(js):
                        import json as _json
                        val = _json.loads(st.to_json()).get("result", {}).get("value", [None])[0] or {}
                    else:
                        val = (st.get("result", {}).get("value") or [None])[0] if isinstance(st, dict) else {}
                    conf = int((val or {}).get("confirmations") or 0)
                except Exception:
                    conf = 0
                if conf > 2:
                    row = dict(r)
                    row["comment"] = USDD_STATUS_PROCESSED
                    state.append_jsonl(processed_path, row)
                    continue
                ts = int(r.get("ts") or 0)
                if ts and (now - ts) > getattr(config, "USDC_CONFIRM_TIMEOUT_SEC", 600):
                    sender = r.get("from")
                    amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                    amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                    if sender and (r.get("txid") not in refunded_txids):
                        refund_key = f"usdd_refund:{r.get('txid')}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            if nexus_client.refund_usdd(sender, amt_str, "timeout"):
                                row = dict(r)
                                row["comment"] = USDD_STATUS_REFUNDED
                                state.append_jsonl(processed_path, row)
                                refunded_txids.add(r.get("txid"))
                                continue
                            else:
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                if attempts >= config.MAX_ACTION_ATTEMPTS:
                                    row = dict(r)
                                    row["comment"] = USDD_STATUS_QUARANTINED
                                    state.append_jsonl(processed_path, row)
                                    _log("USDD_REFUND_QUARANTINED", txid=r.get("txid"), reason="timeout")
                                    continue
                                else:
                                    def _pred_timeout_rp(x):
                                        return x.get("txid") == r.get("txid")
                                    def _upd_timeout_rp(x):
                                        if x.get("comment") not in (USDD_STATUS_REFUNDED, USDD_STATUS_QUARANTINED):
                                            x["comment"] = USDD_STATUS_REFUND_PENDING
                                        return x
                                    state.update_jsonl_row(unprocessed_path, _pred_timeout_rp, _upd_timeout_rp)
                                    continue
            if r.get("comment") == USDD_STATUS_REFUND_PENDING:
                sender = r.get("from")
                if sender:
                    refund_key = f"usdd_refund:{r.get('txid')}"
                    if state.should_attempt(refund_key):
                        amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                        amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                        state.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender, amt_str, "retry_refund"):
                            row = dict(r)
                            row["comment"] = USDD_STATUS_REFUNDED
                            state.append_jsonl(processed_path, row)
                            refunded_txids.add(r.get("txid"))
                            continue
                        else:
                            attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                            if attempts >= config.MAX_ACTION_ATTEMPTS:
                                row = dict(r)
                                row["comment"] = USDD_STATUS_QUARANTINED
                                state.append_jsonl(processed_path, row)
                                _log("USDD_REFUND_QUARANTINED", txid=r.get("txid"), reason="retry_fail")
                                continue
            if r.get("comment") == USDD_STATUS_TRADE_BAL_CHECK:
                new_unprocessed.append(r)
                continue
            new_unprocessed.append(r)
        state.write_jsonl(unprocessed_path, new_unprocessed)

        # Aggregate trade balance refunds: promote earliest per sender, aggregate others
        try:
            rows_tb = state.read_jsonl(unprocessed_path)
            per_sender: dict[str, list[dict]] = {}
            for row in rows_tb:
                if row.get("comment") == USDD_STATUS_TRADE_BAL_CHECK and row.get("from"):
                    per_sender.setdefault(row["from"], []).append(row)
            changed = False
            for sender, lst in per_sender.items():
                if len(lst) <= 1:
                    continue
                lst.sort(key=lambda x: int(x.get("ts") or x.get("timestamp") or 0))
                collector = lst[0]
                def _pred_coll(x):
                    return x.get("txid") == collector.get("txid") and x.get("comment") == USDD_STATUS_TRADE_BAL_CHECK
                def _upd_coll(x):
                    x["comment"] = USDD_STATUS_COLLECTING_REFUND
                    return x
                if state.update_jsonl_row(unprocessed_path, _pred_coll, _upd_coll):
                    changed = True
                for extra in lst[1:]:
                    txid_ex = extra.get("txid")
                    if not txid_ex:
                        continue
                    row_out = dict(extra)
                    row_out["comment"] = "processed (aggregated trade balance)"
                    state.append_jsonl(processed_path, row_out)
                    try:
                        state.mark_nexus_processed(txid_ex, ts=int(extra.get("ts") or extra.get("timestamp") or 0), reason="aggregated_trade_balance")
                    except Exception:
                        pass
                    changed = True
            if changed:
                # Remove aggregated rows from unprocessed
                keep_rows = [r for r in state.read_jsonl(unprocessed_path) if r.get("comment") != "processed (aggregated trade balance)"]
                state.write_jsonl(unprocessed_path, keep_rows)
        except Exception:
            pass

        # Execute consolidated refund for collecting trade balance rows
        try:
            rows_collect = state.read_jsonl(unprocessed_path)
            processed_rows_cache = None
            changed_collect = False
            for r in rows_collect:
                if r.get("comment") != USDD_STATUS_COLLECTING_REFUND:
                    continue
                sender = r.get("from")
                if not sender:
                    continue
                # Idempotency: skip if already refunded
                if r.get("txid") in refunded_txids:
                    continue
                refund_key = f"usdd_collect_refund:{r.get('txid')}"
                if not state.should_attempt(refund_key):
                    continue
                state.record_attempt(refund_key)
                # Lazy load processed rows once
                if processed_rows_cache is None:
                    try:
                        processed_rows_cache = state.read_jsonl(processed_path)
                    except Exception:
                        processed_rows_cache = []
                from decimal import Decimal as _D
                total_dec = _parse_decimal_amount(r.get("amount_usdd"))
                # Include aggregated entries already moved to processed for this sender
                for pr in processed_rows_cache:
                    if pr.get("from") == sender and pr.get("comment") == "processed (aggregated trade balance)":
                        try:
                            total_dec += _parse_decimal_amount(pr.get("amount_usdd"))
                        except Exception:
                            continue
                amt_str = _format_token_amount(total_dec, config.USDD_DECIMALS)
                reason = "aggregated_refund"
                if nexus_client.refund_usdd(sender, amt_str, reason):
                    # Mark collecting row refunded
                    row_out = dict(r)
                    row_out["comment"] = USDD_STATUS_REFUNDED
                    row_out["aggregated_refund_total"] = amt_str
                    state.append_jsonl(processed_path, row_out)
                    refunded_txids.add(r.get("txid"))
                    changed_collect = True
                else:
                    attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                    if attempts >= config.MAX_ACTION_ATTEMPTS:
                        row_q = dict(r)
                        row_q["comment"] = USDD_STATUS_QUARANTINED
                        row_q["aggregated_refund_total_attempted"] = amt_str
                        state.append_jsonl(processed_path, row_q)
                        changed_collect = True
            if changed_collect:
                # Rewrite unprocessed without refunded/quarantined collecting rows
                keep_rows = []
                for r in rows_collect:
                    if r.get("comment") == USDD_STATUS_COLLECTING_REFUND:
                        if r.get("txid") in refunded_txids:
                            continue
                        # If moved to quarantined we added processed row, drop from unprocessed
                        # Determine if quarantined by searching processed cache for txid with QUARANTINED
                        if processed_rows_cache is not None:
                            if any((p.get("txid") == r.get("txid") and p.get("comment") == USDD_STATUS_QUARANTINED) for p in processed_rows_cache):
                                continue
                    keep_rows.append(r)
                state.write_jsonl(unprocessed_path, keep_rows)
        except Exception:
            pass

        # Prune stale (> STALE_ROW_SEC) rows to processed for manual review so they do not block waterline
        try:
            rows_all = state.read_jsonl(unprocessed_path)
            now_cut = time.time()
            active_rows: list[dict] = []
            for r in rows_all:
                tsr = int(r.get("ts") or 0)
                if tsr and (now_cut - tsr) > getattr(config, "STALE_ROW_SEC", 86400):
                    out = dict(r)
                    out["comment"] = "stale_manual_review"
                    state.append_jsonl(processed_path, out)
                    try:
                        state.mark_nexus_processed(r.get("txid"), ts=tsr, reason="stale_manual_review")
                    except Exception:
                        pass
                else:
                    active_rows.append(r)
            if len(active_rows) != len(rows_all):
                state.write_jsonl(unprocessed_path, active_rows)
        except Exception:
            pass

        # Step 6: waterline proposal (only advance when safe)
        try:
            safety = int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0))
            rows = state.read_jsonl(unprocessed_path)
            if rows:
                ts_candidates = [int(x.get("ts") or 0) for x in rows if int(x.get("ts") or 0) > 0]
                if ts_candidates:
                    min_ts = min(ts_candidates)
                    wl = max(0, min_ts - safety)
                    state.propose_nexus_waterline(int(wl))
            else:
                if page_ts_candidates and not backlog_truncated:
                    min_page_ts = min(int(t) for t in page_ts_candidates if int(t) > 0)
                    wl = max(0, min_page_ts - safety)
                    state.propose_nexus_waterline(int(wl))
                elif backlog_truncated:
                    _log("USDD_WATERLINE_HOLD", reason="pagination_truncated")
        except Exception:
            pass
    except Exception as e:
        print(f"Error polling USDD deposits: {e}")
    finally:
        state.save_state()
