from decimal import Decimal
from . import config, state, nexus_client, solana_client, fees

# Lightweight structured logging for deposit lifecycle only
def _log(event: str, **fields):
    parts = [f"{event}"]
    for k, v in fields.items():
        if v is not None:
            parts.append(f"{k}={v}")
    print(" ".join(parts))


# Local in-process cache so signatures marked processed (especially non-deposits)
# are not revisited within the same runtime even if state lookups miss.
_processed_sig_cache: set[str] = set()


def _is_sig_processed(sig: str) -> bool:
    """Check if a signature has been processed, consulting state and a local cache."""
    # Preferred explicit API
    try:
        is_proc = getattr(state, "is_solana_processed", None)
        if callable(is_proc) and is_proc(sig):
            return True
    except Exception:
        pass
    # Common shapes for in-memory set/dict/list
    try:
        ps = getattr(state, "processed_sigs", None)
        if isinstance(ps, dict) and sig in ps:
            return True
        if isinstance(ps, (set, list)) and sig in ps:
            return True
    except Exception:
        pass
    # Fallback to local cache
    return sig in _processed_sig_cache


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
        poll_start = _time.time()
        fetch_count = 0
        processed_count = 0
        client = Client(getattr(config, "RPC_URL", None))
        # Pre-balance micro batch skip
        if getattr(config, "IGNORE_MICRO_USDC", True):
            try:
                current_bal = solana_client.get_vault_usdc_balance_units(client)
                last_bal = state.load_last_vault_balance()
                delta = current_bal - last_bal
                if delta < getattr(config, "MIN_DEPOSIT_USDC_UNITS", 0):
                    # Advance waterline opportunistically using recent signatures
                    state.propose_solana_waterline(poll_start)
                    
                    state.save_last_vault_balance(current_bal)
                    _log("USDC_MICRO_BATCH_SKIPPED", delta_units=delta, threshold=getattr(config, 'MIN_DEPOSIT_USDC_UNITS', 0))
                    # Still process existing unprocessed entries before returning
                    #process_unprocessed_entries()
                    return
            except Exception:
                pass
        limit = 100
        # --- Pagination over signatures to avoid missing older deposits ---
        MAX_PAGES = int(getattr(config, "SOLANA_PAGINATION_PAGES", 5))
        # Anti-DoS: Limit processing per loop iteration
        MAX_PROCESS_PER_LOOP = getattr(config, "MAX_DEPOSITS_PER_LOOP", 100)
        before_sig = None
        sig_results: list = []
        pages = 0
        while pages < MAX_PAGES:
            kwargs = {"limit": limit}
            if before_sig:
                kwargs["before"] = before_sig
            # Fetch each page with timeout to avoid hangs
            import queue as _q, threading as _th
            _result_q: "_q.Queue[tuple[bool, object]]" = _q.Queue(maxsize=1)
            def _fetch_page():
                try:
                    resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, **kwargs)
                    _result_q.put((True, resp))
                except Exception as e:  # pragma: no cover
                    _result_q.put((False, e))
            _t = _th.Thread(target=_fetch_page, daemon=True)
            _t.start()
            try:
                ok, page_resp = _result_q.get(timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))
            except Exception:
                _log("USDC_SIG_PAGE_TIMEOUT", page=pages)
                break
            if not ok:
                break
            page_list = _normalize_get_sigs_response(page_resp)
            if not page_list:
                break
            sig_results.extend(page_list)
            pages += 1
            if len(page_list) < limit:
                break
            # Prepare for next page
            last = page_list[-1]
            before_sig = last.get("signature") if isinstance(last, dict) else None
            # Stop early if we already crossed waterline cutoff after we compute it below
            # (wl_cutoff not yet known here; handled after gathering blockTimes)
        from .main import read_heartbeat_waterlines
        wl_solana, _ = read_heartbeat_waterlines()
        wl_cutoff = int(max(0, wl_solana - config.HEARTBEAT_WATERLINE_SAFETY_SEC)) if config.HEARTBEAT_WATERLINE_ENABLED else 0

        sig_list: list[str] = []
        sig_bt: dict[str, int] = {}
        confirmed_bt_candidates: list[int] = []
        page_has_unprocessed_deposit = False

        for r in sig_results:
            sig = r.get("signature")
            if not sig:
                continue
            try:
                bt = int(r.get("blockTime", 0) or 0)
            except Exception:
                bt = 0
            if wl_cutoff and bt and bt < wl_cutoff:
                continue
            sig_list.append(sig)
            sig_bt[sig] = bt

        # Backlog signal if we paged fully and still might have more (oldest bt still above cutoff)
        try:
            if pages == MAX_PAGES and sig_list:
                oldest_bt = min([b for b in sig_bt.values() if b]) if any(sig_bt.values()) else 0
                if oldest_bt and (not wl_cutoff or oldest_bt >= wl_cutoff):
                    _log("USDC_PAGINATION_BACKLOG", pages=pages, oldest_bt=oldest_bt, wl_cutoff=wl_cutoff)
        except Exception:
            pass

        def _get_tx_result(sig_str: str):
            from typing import Any
            import json as _json
            import threading, queue, time as _time
            fetch_timeout = getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))

            def _normalize_response(resp: Any):
                if isinstance(resp, dict):
                    return resp.get("result") or resp
                val = getattr(resp, "value", None)
                if val is not None:
                    return _normalize_response(val)
                tj = getattr(resp, "to_json", None)
                if callable(tj):
                    try:
                        parsed = _json.loads(tj())
                        return parsed.get("result") or parsed.get("value") or parsed
                    except Exception:
                        pass
                return None

            last_exc = None
            sig_obj = None
            try:
                sig_obj = Signature.from_string(sig_str)
            except Exception as e:
                last_exc = e
                sig_obj = None

            attempts = []
            if sig_obj is not None:
                attempts.append({"arg": sig_obj, "msv": 0})
                attempts.append({"arg": sig_obj, "msv": None})
            attempts.append({"arg": sig_str, "msv": 0})
            attempts.append({"arg": sig_str, "msv": None})

            for att in attempts:
                result_q: "queue.Queue[Any]" = queue.Queue(maxsize=1)
                def _call():
                    nonlocal last_exc
                    try:
                        kwargs = {"encoding": "jsonParsed"}
                        if att["msv"] is not None:
                            kwargs["max_supported_transaction_version"] = att["msv"]
                        tx_resp = client.get_transaction(att["arg"], **kwargs)
                        tx_obj = _normalize_response(tx_resp)
                        result_q.put((True, tx_obj))
                    except Exception as e3:
                        last_exc = e3
                        result_q.put((False, None))
                th = threading.Thread(target=_call, daemon=True)
                th.start()
                try:
                    ok, val = result_q.get(timeout=fetch_timeout)
                except Exception:
                    # Timed out: leave thread to die, continue attempts
                    continue
                if ok and val is not None:
                    return val
            if last_exc:
                raise last_exc
            return None

        def _iter_all_instructions(txobj):
            try:
                for ix in txobj.get("transaction", {}).get("message", {}).get("instructions", []) or []:
                    yield ix
            except Exception:
                pass
            try:
                for inner in (txobj.get("meta", {}) or {}).get("innerInstructions", []) or []:
                    for ix in inner.get("instructions", []) or []:
                        yield ix
            except Exception:
                pass

        MEMO_PROGRAM_IDS = {
            "Memo111111111111111111111111111111111111111",
            "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        }

        def _extract_raw_memo_candidates(txobj):
            """Return (validated_addr, raw_candidates_set). validated_addr is first that passes token validation."""
            validated = None
            raw_candidates = set()

            import re as _re

            def _consider(memo_text: str):
                if not isinstance(memo_text, str):
                    return
                m_clean = memo_text.strip().strip('"').strip("'")
                if "nexus:" not in m_clean.lower():
                    return
                # regex extract
                for match in _re.finditer(r"(?i)nexus:([A-Za-z0-9]{20,})", m_clean):
                    addr = match.group(1)
                    if addr:
                        try:
                            acct = nexus_client.get_account_info(addr)
                            if acct and nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                                if not validated:
                                    validated = addr
                            else:
                                raw_candidates.add(addr)
                        except Exception:
                            raw_candidates.add(addr)

            def _decode_memo_data(data) -> str | None:
                import base64 as _b64
                # data can be str or [payload, encoding]
                try:
                    if isinstance(data, list) and data:
                        payload = data[0]
                        enc = (data[1] if len(data) > 1 else "").lower()
                        if isinstance(payload, str):
                            if enc == "base64":
                                try:
                                    return _b64.b64decode(payload).decode("utf-8", errors="ignore").strip() or None
                                except Exception:
                                    return None
                            if enc == "base58":
                                try:
                                    from base58 import b58decode  # type: ignore
                                    return b58decode(payload).decode("utf-8", errors="ignore").strip() or None
                                except Exception:
                                    return None
                            # unknown encoding: try base64, then base58, then utf-8
                            try:
                                return _b64.b64decode(payload).decode("utf-8", errors="ignore").strip() or None
                            except Exception:
                                try:
                                    from base58 import b58decode  # type: ignore
                                    return b58decode(payload).decode("utf-8", errors="ignore").strip() or None
                                except Exception:
                                    s = payload.strip()
                                    return s or None
                    elif isinstance(data, str):
                        # try base64, then base58, then utf-8 plain text
                        try:
                            return _b64.b64decode(data).decode("utf-8", errors="ignore").strip() or None
                        except Exception:
                            try:
                                from base58 import b58decode  # type: ignore
                                return b58decode(data).decode("utf-8", errors="ignore").strip() or None
                            except Exception:
                                s = data.strip()
                                return s or None
                except Exception:
                    return None
                return None

            # 1) Inspect instructions (outer + inner)
            for instr in _iter_all_instructions(txobj):
                try:
                    prog = instr.get("program")
                    pid = instr.get("programId")
                    if not (prog == "spl-memo" or (isinstance(pid, str) and pid in MEMO_PROGRAM_IDS)):
                        continue
                    memo_text = None
                    p = instr.get("parsed")
                    if isinstance(p, str):
                        memo_text = p
                    elif isinstance(p, dict):
                        info = p.get("info") or {}
                        if isinstance(info, dict):
                            memo_text = info.get("memo") or info.get("message") or info.get("text")
                        if not memo_text:
                            memo_text = p.get("message") or p.get("memo") or p.get("text")
                    if not memo_text:
                        memo_text = _decode_memo_data(instr.get("data"))
                    if not isinstance(memo_text, str):
                        continue
                    _consider(memo_text)
                except Exception:
                    continue

            # 2) Fallback: parse runtime logs for memo lines
            try:
                logs = (txobj.get("meta") or {}).get("logMessages") or []
                if isinstance(logs, list):
                    for ln in logs:
                        if not isinstance(ln, str):
                            continue
                        if "Program log: Memo" in ln or "Program data: " in ln:
                            parts = ln.split(":", 2)
                            if len(parts) >= 3:
                                cand = parts[-1].strip()
                                if isinstance(cand, str):
                                    _consider(cand)
            except Exception:
                pass
            return validated, raw_candidates

        def _extract_nexus_receival_from_memo(txobj) -> tuple[str | None, set[str]]:
            return _extract_raw_memo_candidates(txobj)

        # Load current unprocessed in-memory snapshot
        unprocessed = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
        # Also attempt to resolve receival_account for previously listed entries (via memo)
        try:
            for row in list(unprocessed):
                if row.get("receival_account") or row.get("comment") in ("ready for processing", "debited, awaiting confirmations"):
                    continue
                sig0 = row.get("sig")
                try:
                    tx0 = _get_tx_result(sig0)
                except Exception:
                    tx0 = None
                if tx0:
                    recv, raw_cands = _extract_nexus_receival_from_memo(tx0)
                    if recv:
                        def _pred(r):
                            return r.get("sig") == sig0
                        def _upd(r):
                            r["receival_account"] = recv
                            r["comment"] = "ready for processing"
                            return r
                        state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred, _upd)
                        row["receival_account"] = recv
                        row["comment"] = "ready for processing"
                    elif raw_cands:
                        # keep unresolved memo for later validation
                        def _pred2(r):
                            return r.get("sig") == sig0
                        def _upd2(r):
                            r["raw_memo_candidates"] = list(raw_cands)
                            r["comment"] = "memo unresolved"
                            return r
                        state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred2, _upd2)
                        row["raw_memo_candidates"] = list(raw_cands)
                        row["comment"] = "memo unresolved"
        except Exception:
            pass

        for sig in sig_list:
            # Check if we've hit processing limit for this loop iteration
            if processed_count >= MAX_PROCESS_PER_LOOP:
                _log("USDC_LOOP_LIMIT_REACHED", processed=processed_count, remaining=len(sig_list) - sig_list.index(sig))
                break
                
            # Cooperative stop: respect global stop event if present
            try:
                from .main import _stop_event as _global_stop
                if _global_stop and _global_stop.is_set():
                    break
            except Exception:
                pass
            # Time budget / fetch limit guard
            if (fetch_count >= getattr(config, "SOLANA_MAX_TX_FETCH_PER_POLL", 120)) or ((_time.time() - poll_start) > getattr(config, "SOLANA_POLL_TIME_BUDGET_SEC", 10)):
                _log("USDC_POLL_BUDGET_EXHAUSTED", fetched=fetch_count, elapsed=round(_time.time()-poll_start,2))
                break
            if _is_sig_processed(sig):
                continue
            try:
                if state.is_refunded(sig):
                    continue
            except Exception:
                pass

            mark_processed = False
            found_deposit = False
            try:
                tx = _get_tx_result(sig)
                if not tx:
                    continue
                try:
                    if (tx.get("meta") or {}).get("err") is not None:
                        continue
                except Exception:
                    pass
            except Exception as e:
                try:
                    et = type(e).__name__
                except Exception:
                    et = "Exception"
                # Suppress noisy fetch errors for non-deposit focus; could optionally _log an error event
                continue

            all_instrs = list(_iter_all_instructions(tx))
            fetch_count += 1

            # Collect all token transfers to the vault in this tx (instruction path)
            transfer_events = []
            for instr in all_instrs:
                is_token_prog = (
                    instr.get("program") == "spl-token"
                    or instr.get("programId") == str(solana_client.TOKEN_PROGRAM_ID)
                )
                if is_token_prog and instr.get("parsed"):
                    p = instr["parsed"]
                    if p.get("type") in ("transfer", "transferChecked") and p.get("info", {}).get("destination") == str(config.VAULT_USDC_ACCOUNT):
                        info = p["info"]
                        # Optional mint sanity check if present
                        mint_ok = True
                        try:
                            mint_val = info.get("mint") or (info.get("tokenAmount") or {}).get("mint")
                            if mint_val and str(mint_val) != str(config.USDC_MINT):
                                mint_ok = False
                        except Exception:
                            pass
                        if not mint_ok:
                            continue
                        if "amount" in info:
                            amt_units = int(info["amount"])
                        elif "tokenAmount" in info and isinstance(info["tokenAmount"], dict):
                            amt_units = int(info["tokenAmount"].get("amount", 0))
                        else:
                            continue
                        transfer_events.append({
                            "amount": amt_units,
                            "source": info.get("source"),
                        })

            if transfer_events:
                found_deposit = True
                # Aggregate (assumes single logical deposit per tx) – sum amounts; choose source if unique
                amount_usdc_units = sum(e["amount"] for e in transfer_events)
                sources = {e.get("source") for e in transfer_events if e.get("source")}
                source_token_acc = next(iter(sources)) if len(sources) == 1 else None
                flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                dynamic_bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
                pre_dynamic_net = max(0, amount_usdc_units - flat_fee_units)
                dynamic_fee_usdc = (pre_dynamic_net * dynamic_bps) // 10000 if dynamic_bps > 0 else 0
                net_usdc_for_mint = max(0, pre_dynamic_net - dynamic_fee_usdc)

                # Anti-DoS: Check minimum deposit threshold (apply to GROSS amount, not net after fees)
                min_deposit_threshold = getattr(config, "MIN_DEPOSIT_USDC_UNITS", 100101)  # e.g. 0.100101 USDC in base units
                if amount_usdc_units < min_deposit_threshold:
                    # Ignore true micro deposit (below threshold) entirely (design choice per user request)
                    # Do NOT treat as fees; simply skip so attacker waste SOL fees without service work.
                    # Continue scanning remaining signatures instead of returning early.
                    continue
                elif net_usdc_for_mint <= 0:
                    # Entire deposit consumed by fees (flat + dynamic). Tag as fee_only.
                    fees.add_usdc_fee(amount_usdc_units, sig=sig, kind="fee_only")
                    _log("USDC_FEE_ONLY", sig=sig, amount=amount_usdc_units, multi=len(transfer_events))
                    entry = {
                        "sig": sig,
                        "amount_usdc_units": amount_usdc_units,
                        "ts": sig_bt.get(sig) or 0,
                        "from": source_token_acc,
                        "comment": "processed, smaller than fees",
                    }
                    state.append_jsonl(config.PROCESSED_SWAPS_FILE, entry)
                    mark_processed = True
                else:
                    entry = {
                        "sig": sig,
                        "amount_usdc_units": int(amount_usdc_units),
                        "ts": sig_bt.get(sig) or 0,
                        "from": source_token_acc,
                    }
                    if not any((r.get("sig") == sig) for r in unprocessed):
                        entry["reservation_ts"] = int(__import__("time").time())
                        state.append_jsonl(config.UNPROCESSED_SIGS_FILE, entry)
                        unprocessed.append(entry)
                        _log("USDC_QUEUED", sig=sig, amount=amount_usdc_units, from_acct=source_token_acc, multi=len(transfer_events))
                    recv, raw_cands = _extract_nexus_receival_from_memo(tx)
                    if recv:
                        def _pred(r):
                            return r.get("sig") == sig
                        def _upd(r):
                            r["receival_account"] = recv
                            r["comment"] = "ready for processing"
                            return r
                        state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred, _upd)
                        for r in unprocessed:
                            if r.get("sig") == sig:
                                r["receival_account"] = recv
                                r["comment"] = "ready for processing"
                                break
                        _log("USDC_READY", sig=sig, nexus=recv, amount=amount_usdc_units, multi=len(transfer_events))
                        try:
                            print(f"[USDC_READY] sig={sig} receival_account={recv} amount_usdc_units={amount_usdc_units}")
                            print()
                        except Exception:
                            pass
                    elif raw_cands:
                        def _pred2(r):
                            return r.get("sig") == sig
                        def _upd2(r):
                            r["raw_memo_candidates"] = list(raw_cands)
                            r["comment"] = "memo unresolved"
                            return r
                        state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred2, _upd2)
                        for r in unprocessed:
                            if r.get("sig") == sig:
                                r["raw_memo_candidates"] = list(raw_cands)
                                r["comment"] = "memo unresolved"
                                break
                        _log("USDC_MEMO_UNRESOLVED", sig=sig, amount=amount_usdc_units, candidates=len(raw_cands))
                    else:
                        # Missing memo entirely: refund path after fees
                        refundable = max(0, amount_usdc_units - flat_fee_units)
                        if source_token_acc and refundable > 0 and solana_client.refund_usdc_to_source(source_token_acc, refundable, "missing memo", deposit_sig=sig):
                            if flat_fee_units > 0:
                                fees.add_usdc_fee(flat_fee_units, sig=sig, kind="refund_flat")
                            out = {
                                "sig": sig,
                                "amount_usdc_units": amount_usdc_units,
                                "ts": sig_bt.get(sig) or 0,
                                "from": source_token_acc,
                                "comment": "refunded",
                            }
                            state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                            state.append_jsonl(config.REFUNDED_SIGS_FILE, out)
                            try:
                                state.add_refunded_sig(sig)
                            except Exception:
                                pass
                            try:
                                unprocessed = [r for r in unprocessed if r.get("sig") != sig]
                                rows2 = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
                                rows2 = [r for r in rows2 if r.get("sig") != sig]
                                state.write_jsonl(config.UNPROCESSED_SIGS_FILE, rows2)
                            except Exception:
                                pass
                            mark_processed = True
                            _log("USDC_REFUNDED", sig=sig, amount=amount_usdc_units, reason="missing_memo")
                            try:
                                refundable = max(0, amount_usdc_units - flat_fee_units)
                                print(f"[USDC_REFUNDED] sig={sig} refundable_units={refundable} reason=missing_memo")
                                print()
                            except Exception:
                                pass
                        else:
                            page_has_unprocessed_deposit = True

            if not found_deposit:
                try:
                    meta = tx.get("meta") or {}
                    pre = meta.get("preTokenBalances") or []
                    post = meta.get("postTokenBalances") or []
                    acct_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    def _akey(i):
                        try:
                            k = acct_keys[i]
                            if isinstance(k, str):
                                return k
                            if isinstance(k, dict):
                                return k.get("pubkey") or k.get("pubKey") or ""
                        except Exception:
                            return ""
                        return ""
                    def _amount(entry):
                        try:
                            return int(((entry.get("uiTokenAmount") or {}).get("amount")) or 0)
                        except Exception:
                            return 0
                    pre_map = {}
                    for e in pre:
                        try:
                            if e.get("mint") == str(config.USDC_MINT):
                                addr = _akey(int(e.get("accountIndex")))
                                pre_map[addr] = _amount(e)
                        except Exception:
                            continue
                    post_map = {}
                    for e in post:
                        try:
                            if e.get("mint") == str(config.USDC_MINT):
                                addr = _akey(int(e.get("accountIndex")))
                                post_map[addr] = _amount(e)
                        except Exception:
                            continue
                    vault_addr = str(config.VAULT_USDC_ACCOUNT)
                    pre_amt = pre_map.get(vault_addr, 0)
                    post_amt = post_map.get(vault_addr, 0)
                    delta_in = post_amt - pre_amt
                    if delta_in > 0:
                        src_addr = None
                        for addr, pre_a in pre_map.items():
                            if addr == vault_addr:
                                continue
                            post_a = post_map.get(addr, 0)
                            if pre_a - post_a == delta_in:
                                src_addr = addr
                                break
                        amount_usdc_units = int(delta_in)
                        source_token_acc = src_addr
                        flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                        found_deposit = True

                        dynamic_bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
                        pre_dynamic_net = max(0, amount_usdc_units - flat_fee_units)
                        dynamic_fee_usdc = (pre_dynamic_net * dynamic_bps) // 10000 if dynamic_bps > 0 else 0
                        net_usdc_for_mint = max(0, pre_dynamic_net - dynamic_fee_usdc)

                        # Anti-DoS: Check minimum deposit threshold on gross amount
                        min_deposit_threshold = getattr(config, "MIN_DEPOSIT_USDC_UNITS", 100101)
                        if amount_usdc_units < min_deposit_threshold:
                            # Skip micro (gross below threshold) and continue scanning
                            continue
                        elif net_usdc_for_mint <= 0:
                            fees.add_usdc_fee(amount_usdc_units, sig=sig, kind="fee_only")
                            _log("USDC_FEE_ONLY", sig=sig, amount=amount_usdc_units, path="delta")
                            # blank line suppressed
                            entry = {
                                "sig": sig,
                                "amount_usdc_units": amount_usdc_units,
                                "ts": sig_bt.get(sig) or 0,
                                "from": source_token_acc,
                                "comment": "processed, smaller than fees",
                            }
                            state.append_jsonl(config.PROCESSED_SWAPS_FILE, entry)
                            mark_processed = True
                        else:
                            entry = {
                                "sig": sig,
                                "amount_usdc_units": int(amount_usdc_units),
                                "ts": sig_bt.get(sig) or 0,
                                "from": source_token_acc,
                            }
                            if not any((r.get("sig") == sig) for r in unprocessed):
                                entry["reservation_ts"] = int(__import__("time").time())
                                state.append_jsonl(config.UNPROCESSED_SIGS_FILE, entry)
                                unprocessed.append(entry)
                                _log("USDC_QUEUED", sig=sig, amount=amount_usdc_units, from_acct=source_token_acc, path="delta")
                            recv, raw_cands = _extract_nexus_receival_from_memo(tx)
                            if recv:
                                def _pred(r):
                                    return r.get("sig") == sig
                                def _upd(r):
                                    r["receival_account"] = recv
                                    r["comment"] = "ready for processing"
                                    return r
                                state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred, _upd)
                                for r in unprocessed:
                                    if r.get("sig") == sig:
                                        r["receival_account"] = recv
                                        r["comment"] = "ready for processing"
                                        break
                                _log("USDC_READY", sig=sig, nexus=recv, amount=amount_usdc_units, path="delta")
                                try:
                                    print(f"[USDC_READY] sig={sig} receival_account={recv} amount_usdc_units={amount_usdc_units}")
                                    print()
                                except Exception:
                                    pass
                            elif raw_cands:
                                def _pred2(r):
                                    return r.get("sig") == sig
                                def _upd2(r):
                                    r["raw_memo_candidates"] = list(raw_cands)
                                    r["comment"] = "memo unresolved"
                                    return r
                                state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred2, _upd2)
                                for r in unprocessed:
                                    if r.get("sig") == sig:
                                        r["raw_memo_candidates"] = list(raw_cands)
                                        r["comment"] = "memo unresolved"
                                        break
                                _log("USDC_MEMO_UNRESOLVED", sig=sig, amount=amount_usdc_units, path="delta", candidates=len(raw_cands))
                            else:
                                refundable = max(0, amount_usdc_units - flat_fee_units)
                                if source_token_acc and refundable > 0 and solana_client.refund_usdc_to_source(source_token_acc, refundable, "missing/invalid memo", deposit_sig=sig):
                                    if flat_fee_units > 0:
                                        fees.add_usdc_fee(flat_fee_units, sig=sig, kind="refund_flat")
                                    out = {
                                        "sig": sig,
                                        "amount_usdc_units": amount_usdc_units,
                                        "ts": sig_bt.get(sig) or 0,
                                        "from": source_token_acc,
                                        "comment": "refunded",
                                    }
                                    state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                                    state.append_jsonl(config.REFUNDED_SIGS_FILE, out)
                                    try:
                                        state.add_refunded_sig(sig)
                                    except Exception:
                                        pass
                                else:
                                    page_has_unprocessed_deposit = True
                except Exception:
                    pass
                ts_bt = sig_bt.get(sig) or 0
                state.append_jsonl(config.NON_DEPOSITS_FILE, {"sig": sig, "ts": ts_bt})
                state.mark_solana_processed(sig, ts=ts_bt, reason="not a deposit")
                _processed_sig_cache.add(sig)
                if ts_bt:
                    confirmed_bt_candidates.append(int(ts_bt))

        try:
            if (
                isinstance(sig_results, list)
                and len(sig_results) < limit
                and confirmed_bt_candidates
                and not page_has_unprocessed_deposit
            ):
                state.propose_solana_waterline(int(min(confirmed_bt_candidates)))
            # Silence waterline hold message; lifecycle events already show pending state
        except Exception:
            pass
    except Exception:
        # Suppress top-level poll errors to keep terminal focused on deposit lifecycle
        pass
    finally:
        # Process unprocessed entries regardless of signature polling results
        process_unprocessed_entries()
