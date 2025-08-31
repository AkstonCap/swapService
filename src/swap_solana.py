from decimal import Decimal
from . import config, state, nexus_client, solana_client, fees

# Lightweight structured logging for deposit lifecycle only
def _log(event: str, **fields):
    parts = [f"{event}"]
    for k, v in fields.items():
        if v is not None:
            parts.append(f"{k}={v}")
    print(" ".join(parts))



def _normalize_get_sigs_response(resp):
    """Return a list of signature entries as dicts with at least 'signature' and optional 'blockTime'.
    Supports both dict-based solana-py responses and solders typed responses.
    """
    # Dict-style
    try:
        return (resp.get("result") or [])
    except AttributeError:
        pass
    # solders typed: try to_json first
    try:
        import json as _json
        js = _json.loads(resp.to_json())
        arr = js.get("result") or js.get("value") or []
        if isinstance(arr, dict):
            arr = arr.get("value") or arr.get("result") or []
        if isinstance(arr, list):
            return arr
    except Exception:
        pass
    # Fallback to .value (commonly a list of objects)
    try:
        val = getattr(resp, "value", None)
        out = []
        if isinstance(val, list):
            for r in val:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    sig = str(getattr(r, "signature", ""))
                    bt = getattr(r, "block_time", None)
                    try:
                        bt = int(bt) if bt is not None else 0
                    except Exception:
                        bt = 0
                    out.append({"signature": sig, "blockTime": bt})
            return out
    except Exception:
        pass
    return []


def scale_amount(amount: int, src_decimals: int, dst_decimals: int) -> int:
    if src_decimals == dst_decimals:
        return int(amount)
    if src_decimals < dst_decimals:
        return int(amount) * (10 ** (dst_decimals - src_decimals))
    return int(amount) // (10 ** (src_decimals - dst_decimals))


def process_unprocessed_entries():
    """Process queued USDC deposits independent of new signature polling.
    Prioritizes ready entries and handles confirmations, refunds, and quarantine.
    """
    import time as _time
    
    process_start = _time.time()
    PROCESS_BUDGET_SEC = getattr(config, "UNPROCESSED_PROCESS_BUDGET_SEC", 30)
    
    try:
        # Priority 1: Process ready entries (has receival_account)
        rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
        for r in rows:
            if _time.time() - process_start > PROCESS_BUDGET_SEC:
                _log("UNPROCESSED_BUDGET_EXCEEDED", budget_sec=PROCESS_BUDGET_SEC)
                break
                
            if r.get("comment") == "ready for processing" and r.get("receival_account"):
                if r.get("txid"):
                    continue
                sig = r.get("sig")
                # Reservation to prevent concurrent debit
                if not state.reserve_action("debit", sig):
                    continue
                sig = r.get("sig")
                recv = r.get("receival_account")
                amt = int(r.get("amount_usdc_units") or 0)
                flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                dyn_bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
                pre_dyn = max(0, amt - flat_fee_units)
                dyn_fee = (pre_dyn * dyn_bps) // 10000 if dyn_bps > 0 else 0
                net_usdc = max(0, pre_dyn - dyn_fee)
                # Convert net USDC units to decimal amount for 1:1 USDC->USDD swap
                net_usdc_decimal = Decimal(net_usdc) / (10 ** config.USDC_DECIMALS)
                ref = state.next_reference()
                treas = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
                if not treas:
                    ok, txid = (False, None)
                else:
                    # Pass decimal amount directly; nexus_client will format appropriately
                    ok, txid = nexus_client.debit_account_with_txid(treas, recv, net_usdc_decimal, ref)
                if ok and txid:
                    total_fee = flat_fee_units + dyn_fee
                    # Record fee components separately for audit clarity.
                    if flat_fee_units > 0:
                        fees.add_usdc_fee(flat_fee_units, sig=sig, kind="flat")
                    if dyn_fee > 0:
                        fees.add_usdc_fee(dyn_fee, sig=sig, kind="dynamic")
                    def _pred(x):
                        return x.get("sig") == sig
                    def _upd(x):
                        x["txid"] = txid
                        x["reference"] = ref
                        x["comment"] = "debited, awaiting confirmations"
                        return x
                    state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred, _upd)
                    try:
                        print(f"[USDC_DEBITED] sig={sig} txid={txid} ref={ref} net_usdc={net_usdc} usdd_decimal={net_usdc_decimal}")
                        print()
                    except Exception:
                        pass
                else:
                    # Release reservation on failure
                    state.release_reservation("debit", sig)
                    try:
                        print(f"[USDC_DEBIT_FAIL] sig={sig} net_usdc={net_usdc} reason=debit_failed")
                        print()
                    except Exception:
                        pass
                    # Debit failed: attempt USDC refund (retain flat fee only)
                    src = r.get("from")
                    if src:
                        refund_key = f"refund_usdc:{sig}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            refundable = max(0, amt - flat_fee_units)
                            if refundable > 0 and solana_client.refund_usdc_to_source(src, refundable, "USDD debit failed", deposit_sig=sig):
                                if flat_fee_units > 0:
                                    fees.add_usdc_fee(flat_fee_units, sig=sig, kind="refund_flat")
                                state.finalize_refund(dict(r), reason="debit_failed")
                            else:
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                if attempts >= config.MAX_ACTION_ATTEMPTS:
                                    if solana_client.move_usdc_to_quarantine(refundable, note="FAILED_REFUND", deposit_sig=sig):
                                        state.log_failed_refund({
                                            "type": "refund_failure",
                                            "sig": sig,
                                            "reason": "Debit failed",
                                            "source_token_acc": src,
                                            "amount_units": refundable,
                                        })
                                        out = dict(r)
                                        out["comment"] = "quarantined"
                                        state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
    except Exception:
        # Suppress processing ready entries error noise
        pass

    try:
        # Priority 2: Check confirmations for debited entries
        rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
        new_unprocessed: list[dict] = []
        for r in rows:
            if _time.time() - process_start > PROCESS_BUDGET_SEC:
                new_unprocessed.append(r)  # Keep remaining entries
                continue
                
            comment = r.get("comment")
            if comment == "debited, awaiting confirmations" and r.get("txid"):
                st = nexus_client.get_debit_status(r["txid"]) or {}
                conf = int((st.get("confirmations") or 0))
                if conf > 1:
                    out = dict(r)
                    out["comment"] = "processed"
                    state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                    try:
                        state.mark_solana_processed(r.get("sig"), ts=int(r.get("ts") or 0), reason="processed")
                    except Exception:
                        pass
                    continue
            # Refund path on timeout or invalid receival account
            ts = int(r.get("ts") or 0)
            now_time = _time.time()
            age_ok = (ts and (now_time - ts) > config.REFUND_TIMEOUT_SEC)
            stale_age = (ts and (now_time - ts) > getattr(config, "STALE_DEPOSIT_QUARANTINE_SEC", 86400))
            if age_ok:
                src = r.get("from")
                amt = int(r.get("amount_usdc_units") or 0)
                flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                refundable = max(0, amt - flat_fee_units)
                should_refund = False
                if comment in ("ready for processing", "memo unresolved") and not r.get("receival_account"):
                    should_refund = True
                elif comment == "debited, awaiting confirmations":
                    # Never refund after a debit has been initiated and txid recorded; rely on confirmations.
                    should_refund = False
                if stale_age and comment in ("ready for processing", "memo unresolved"):
                    # Quarantine stale unprocessed deposit (>24h) instead of refunding again
                    refundable = max(0, amt - flat_fee_units)
                    if refundable > 0 and solana_client.move_usdc_to_quarantine(refundable, note="STALE_UNPROCESSED", deposit_sig=r.get("sig")):
                        state.log_failed_refund({
                            "type": "stale_quarantine",
                            "sig": r.get("sig"),
                            "reason": "stale_unprocessed",
                            "age_sec": int(now_time - ts),
                            "source_token_acc": src,
                            "amount_units": refundable,
                        })
                        out = dict(r)
                        out["comment"] = "quarantined"
                        state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                        try:
                            state.mark_solana_processed(r.get("sig"), ts=int(r.get("ts") or 0), reason="stale_quarantined")
                        except Exception:
                            pass
                        _log("USDC_QUARANTINED", sig=r.get("sig"), reason="stale_unprocessed", age=int(now_time - ts))
                        try:
                            print(f"[USDC_QUARANTINED] sig={r.get('sig')} amount_units={refundable} reason=stale_unprocessed age={int(now_time - ts)}")
                            print()
                        except Exception:
                            pass
                        try:
                            state.release_reservation("debit", r.get("sig"))
                        except Exception:
                            pass
                        continue
                    else:
                        # If we cannot quarantine (e.g., no refundable), mark processed to avoid infinite looping
                        try:
                            state.mark_solana_processed(r.get("sig"), ts=int(r.get("ts") or 0), reason="stale_no_refund")
                        except Exception:
                            pass
                        _log("USDC_STALE_NO_QUARANTINE", sig=r.get("sig"), age=int(now_time - ts))
                        continue
                if stale_age and comment == "debited, awaiting confirmations":
                    # Debit initiated but still unconfirmed after stale threshold: log for manual review (no refund)
                    state.log_failed_refund({
                        "type": "stale_debit",
                        "sig": r.get("sig"),
                        "reason": "stale_debit_no_confirm",
                        "age_sec": int(now_time - ts),
                        "txid": r.get("txid"),
                        "receival_account": r.get("receival_account"),
                        "amount_units": amt,
                    })
                    _log("USDC_STALE_DEBIT", sig=r.get("sig"), age=int(now_time - ts), txid=r.get("txid"))
                    try:
                        print(f"[USDC_QUARANTINED] sig={r.get('sig')} amount_units={amt} reason=stale_debit_no_confirm age={int(now_time - ts)}")
                        print()
                    except Exception:
                        pass
                    # Keep it in list for potential later confirmation, do not refund/quarantine.
                    new_unprocessed.append(r)
                    continue
                if should_refund and src and refundable > 0:
                    refund_key = f"refund_usdc:{r.get('sig')}"
                    if state.should_attempt(refund_key):
                        state.record_attempt(refund_key)
                        sig = r.get("sig")  # Fix undefined sig variable
                        if solana_client.refund_usdc_to_source(src, refundable, "timeout or unresolved receival_account", deposit_sig=sig):
                            if flat_fee_units > 0:
                                fees.add_usdc_fee(flat_fee_units, sig=r.get('sig'), kind="refund_flat")
                            state.finalize_refund(dict(r), reason="timeout_or_unresolved")
                            # release possible stale reservation
                            try:
                                state.release_reservation("debit", r.get("sig"))
                            except Exception:
                                pass
                            continue
                        else:
                            attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                            if attempts >= config.MAX_ACTION_ATTEMPTS:
                                if solana_client.move_usdc_to_quarantine(refundable, note="FAILED_REFUND", deposit_sig=r.get("sig")):
                                    state.log_failed_refund({
                                        "type": "refund_failure",
                                        "sig": r.get("sig"),
                                        "reason": "timeout/unconfirmed",
                                        "source_token_acc": src,
                                        "amount_units": refundable,
                                    })
                                    out = dict(r)
                                    out["comment"] = "quarantined"
                                    state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                                    try:
                                        state.mark_solana_processed(r.get("sig"), ts=int(r.get("ts") or 0), reason="quarantined")
                                    except Exception:
                                        pass
                                    try:
                                        state.release_reservation("debit", r.get("sig"))
                                    except Exception:
                                        pass
                                    continue
            new_unprocessed.append(r)
        state.write_jsonl(config.UNPROCESSED_SIGS_FILE, new_unprocessed)
    except Exception:
        # Suppress confirm/move processed error noise
        pass

    try:
        # Priority 3: Mark refund due entries and retry memo resolution
        rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
        now = _time.time()
        for r in rows:
            if _time.time() - process_start > PROCESS_BUDGET_SEC:
                break
                
            ts = int(r.get("ts") or 0)
            if (not r.get("receival_account") and ts and now - ts > config.REFUND_TIMEOUT_SEC):
                def _pred(x):
                    return x.get("sig") == r.get("sig")
                def _upd(x):
                    x["comment"] = "refund due"
                    return x
                state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred, _upd)
    except Exception:
        # Suppress refund annotation error noise
        pass

    # Cleanup stale rows and update waterline
    try:
        rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
        now_ts = _time.time()
        active_ts = []
        for r in rows:
            rts = int(r.get("ts") or 0)
            if not rts:
                continue
            age = now_ts - rts
            if age > getattr(config, "STALE_ROW_SEC", 86400):
                # Stale row -> mark for manual review and move to processed to unblock waterline
                out = dict(r)
                out["comment"] = "stale_manual_review"
                state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                try:
                    state.mark_solana_processed(r.get("sig"), ts=rts, reason="stale_manual_review")
                except Exception:
                    pass
            else:
                active_ts.append(rts)
        # Rewrite unprocessed dropping stale rows
        new_rows = [r for r in rows if r.get("comment") != "stale_manual_review"]
        state.write_jsonl(config.UNPROCESSED_SIGS_FILE, new_rows)
        if active_ts:
            min_ts = min(active_ts)
            state.propose_solana_waterline(int(max(0, min_ts - config.HEARTBEAT_WATERLINE_SAFETY_SEC)))
        else:
            # No unprocessed entries left: advance waterline to current time minus buffer
            # This prevents unnecessary re-scanning of old signatures
            current_ts = int(now_ts)
            waterline_ts = max(0, current_ts - config.HEARTBEAT_WATERLINE_SAFETY_SEC)
            state.propose_solana_waterline(waterline_ts)
            _log("WATERLINE_ADVANCED", new_ts=waterline_ts, reason="no_unprocessed_entries")
    except Exception:
        pass

    state.save_state()


def poll_solana_deposits():
    from solana.rpc.api import Client
    from solders.signature import Signature
    try:
        import time as _time
        heartbeat = nexus_client.get_heartbeat_asset()
        if not heartbeat:
            return
        wline_sol = heartbeat.get("last_safe_timestamp_solana")
        if wline_sol is None:
            return
        
        poll_start = _time.time()
        current_bal = solana_client.get_token_account_balance(config.VAULT_USDC_ACCOUNT)
        last_bal = state.load_last_vault_balance()
        delta = current_bal - last_bal
        
        # Pre-balance micro batch skip
        if delta < getattr(config, "MIN_DEPOSIT_USDC_UNITS", 0):
            # Advance waterline opportunistically using recent signatures
            state.propose_solana_waterline(poll_start)                    
            state.save_last_vault_balance(current_bal)
            nexus_client.update_heartbeat_asset(poll_start, None, poll_start)
            _log("USDC_MICRO_BATCH_SKIPPED", delta_units=delta, threshold=getattr(config, 'MIN_DEPOSIT_USDC_UNITS', 0))
            # Still process existing unprocessed entries before returning
            return
        
        client = Client(getattr(config, "RPC_URL", None))
       
        usdc_deposits = solana_client.fetch_filtered_token_account_transaction_history(
            config.USDC_MINT, wline_sol, config.MIN_DEPOSIT_USDC_UNITS, 10.0
            )
        unprocessed_deposits_added = solana_client.process_filtered_deposits(usdc_deposits, True)
        print(f"New deposits fetched and added for processing: {unprocessed_deposits_added}\n")

        [proc_count_swap, proc_count_refund, proc_count_quar, proc_count_mic] = solana_client.process_unprocessed_usdc_deposits(1000, 8.0)
        print(f"Debited, awaiting confirmation: {proc_count_swap}, \nTo be refunded: {proc_count_refund}, \nTo be quarantined: {proc_count_quar}, \nMicro-sigs found: {proc_count_mic}\n")

        refunds = solana_client.process_usdc_deposits_refunding(1000, 8.0)
        print(f"Processed refunds, awaiting confirmation: {refunds}\n") if refunds > 0 else None

        quarantines = solana_client.process_usdc_deposits_quarantine(1000, 8.0)
        print(f"Processed quarantines: {quarantines}\n") if quarantines > 0 else None

        confirmed_ref = solana_client.check_sig_confirmations(100, 8.0)
        print(f"Confirmed refunds: {confirmed_ref}\n") if confirmed_ref > 0 else None

        confirmed_debits = nexus_client.check_unconfirmed_debits(10, 8.0)
        print(f"Confirmed debits: {confirmed_debits}\n") if confirmed_debits > 0 else None

        new_waterline = solana_client.check_timestamp_unprocessed_sigs()
        if new_waterline and new_waterline > wline_sol:
            _log("WATERLINE_ADVANCED", old_ts=wline_sol, new_ts=new_waterline, reason="signatures processed")
            nexus_client.update_heartbeat_asset(new_waterline, None, poll_start)

    except Exception:
        # Suppress top-level poll errors to keep terminal focused on deposit lifecycle
        pass
    
