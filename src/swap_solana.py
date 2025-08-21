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


def poll_solana_deposits():
    from solana.rpc.api import Client
    from solders.signature import Signature
    try:
        client = Client(getattr(config, "SOLANA_RPC_URL", getattr(config, "RPC_URL", None)))
        limit = 100
        sigs_resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, limit=limit)
        sig_results = _normalize_get_sigs_response(sigs_resp)
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

        def _get_tx_result(sig_str: str):
            from typing import Any
            import json as _json

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
                try:
                    kwargs = {"encoding": "jsonParsed"}
                    if att["msv"] is not None:
                        kwargs["max_supported_transaction_version"] = att["msv"]
                    tx_resp = client.get_transaction(att["arg"], **kwargs)
                    tx_obj = _normalize_response(tx_resp)
                    if tx_obj is not None:
                        return tx_obj
                except Exception as e3:
                    last_exc = e3
                    continue
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

        def _extract_nexus_receival_from_memo(txobj) -> str | None:
            """Scan outer+inner instructions and logs for a memo like 'nexus:<USDD account>' and validate the account."""
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
                    m = memo_text.strip().strip('"').strip("'")
                    if not m.lower().startswith("nexus:"):
                        continue
                    addr = m.split(":", 1)[1].strip().split()[0]
                    if not addr:
                        continue
                    acct = nexus_client.get_account_info(addr)
                    if acct and nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                        return addr
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
                                    cand_clean = cand.strip().strip('"').strip("'")
                                    if not cand_clean.lower().startswith("nexus:"):
                                        continue
                                    addr = cand_clean.split(":", 1)[1].strip().split()[0]
                                    if addr:
                                        acct = nexus_client.get_account_info(addr)
                                        if acct and nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                                            return addr
            except Exception:
                pass
            return None

        # Load current unprocessed in-memory snapshot
        unprocessed = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
        # Also attempt to resolve receival_account for previously listed entries (via memo)
        try:
            for row in list(unprocessed):
                if row.get("receival_account") or row.get("comment") == "ready for processing":
                    continue
                sig0 = row.get("sig")
                try:
                    tx0 = _get_tx_result(sig0)
                except Exception:
                    tx0 = None
                if tx0:
                    recv = _extract_nexus_receival_from_memo(tx0)
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
        except Exception:
            pass

        for sig in sig_list:
            if _is_sig_processed(sig):
                continue

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
                print(f"Error fetching transaction {sig}: {et} {repr(e)}")
                print()
                continue

            all_instrs = list(_iter_all_instructions(tx))

            for instr in all_instrs:
                is_token_prog = (
                    instr.get("program") == "spl-token"
                    or instr.get("programId") == str(solana_client.TOKEN_PROGRAM_ID)
                )
                if is_token_prog and instr.get("parsed"):
                    p = instr["parsed"]
                    if p.get("type") in ("transfer", "transferChecked") and p.get("info", {}).get("destination") == str(config.VAULT_USDC_ACCOUNT):
                        found_deposit = True
                        info = p["info"]
                        if "amount" in info:
                            amount_usdc_units = int(info["amount"])
                        elif "tokenAmount" in info and isinstance(info["tokenAmount"], dict):
                            amount_usdc_units = int(info["tokenAmount"].get("amount", 0))
                        else:
                            continue

                        source_token_acc = info.get("source")
                        flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                        dynamic_bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
                        pre_dynamic_net = max(0, amount_usdc_units - flat_fee_units)
                        dynamic_fee_usdc = (pre_dynamic_net * dynamic_bps) // 10000 if dynamic_bps > 0 else 0
                        net_usdc_for_mint = max(0, pre_dynamic_net - dynamic_fee_usdc)

                        if net_usdc_for_mint <= 0:
                            fees.add_usdc_fee(amount_usdc_units)
                            _log("USDC_FEE_ONLY", sig=sig, amount=amount_usdc_units)
                            print()
                            entry = {
                                "sig": sig,
                                "amount_usdc_units": amount_usdc_units,
                                "ts": sig_bt.get(sig) or 0,
                                "from": source_token_acc,
                                "comment": "processed, smaller than fees",
                            }
                            state.append_jsonl(config.PROCESSED_SWAPS_FILE, entry)
                            mark_processed = True
                            break

                        entry = {
                            "sig": sig,
                            "amount_usdc_units": int(amount_usdc_units),
                            "ts": sig_bt.get(sig) or 0,
                            "from": source_token_acc,
                        }
                        if not any((r.get("sig") == sig) for r in unprocessed):
                            state.append_jsonl(config.UNPROCESSED_SIGS_FILE, entry)
                            unprocessed.append(entry)
                            _log("USDC_QUEUED", sig=sig, amount=amount_usdc_units, from_acct=source_token_acc)
                        recv = _extract_nexus_receival_from_memo(tx)
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
                            _log("USDC_READY", sig=sig, nexus=recv, amount=amount_usdc_units)
                        else:
                            # Missing/invalid memo -> refund (after flat fee)
                            refundable = max(0, amount_usdc_units - flat_fee_units)
                            if source_token_acc and refundable > 0 and solana_client.refund_usdc_to_source(source_token_acc, refundable, "missing/invalid memo"):
                                if flat_fee_units > 0:
                                    fees.add_usdc_fee(flat_fee_units)
                                out = {
                                    "sig": sig,
                                    "amount_usdc_units": amount_usdc_units,
                                    "ts": sig_bt.get(sig) or 0,
                                    "from": source_token_acc,
                                    "comment": "refunded",
                                }
                                state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                                state.append_jsonl(config.REFUNDED_SIGS_FILE, out)
                                # Remove from unprocessed lists / file
                                try:
                                    unprocessed = [r for r in unprocessed if r.get("sig") != sig]
                                    rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
                                    rows = [r for r in rows if r.get("sig") != sig]
                                    state.write_jsonl(config.UNPROCESSED_SIGS_FILE, rows)
                                except Exception:
                                    pass
                                mark_processed = True
                                _log("USDC_REFUNDED", sig=sig, amount=amount_usdc_units, reason="invalid_memo")
                            else:
                                page_has_unprocessed_deposit = True
                        break

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

                        if net_usdc_for_mint <= 0:
                            fees.add_usdc_fee(amount_usdc_units)
                            _log("USDC_FEE_ONLY", sig=sig, amount=amount_usdc_units, path="delta")
                            print()
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
                                state.append_jsonl(config.UNPROCESSED_SIGS_FILE, entry)
                                unprocessed.append(entry)
                                _log("USDC_QUEUED", sig=sig, amount=amount_usdc_units, from_acct=source_token_acc, path="delta")
                            recv = _extract_nexus_receival_from_memo(tx)
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
                            else:
                                refundable = max(0, amount_usdc_units - flat_fee_units)
                                if source_token_acc and refundable > 0 and solana_client.refund_usdc_to_source(source_token_acc, refundable, "missing/invalid memo"):
                                    if flat_fee_units > 0:
                                        fees.add_usdc_fee(flat_fee_units)
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
                                        unprocessed = [r for r in unprocessed if r.get("sig") != sig]
                                        rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
                                        rows = [r for r in rows if r.get("sig") != sig]
                                        state.write_jsonl(config.UNPROCESSED_SIGS_FILE, rows)
                                    except Exception:
                                        pass
                                    mark_processed = True
                                    _log("USDC_REFUNDED", sig=sig, amount=amount_usdc_units, reason="invalid_memo", path="delta")
                                else:
                                    page_has_unprocessed_deposit = True
                except Exception:
                    pass

            if mark_processed:
                ts_bt = sig_bt.get(sig) or 0
                state.mark_solana_processed(sig, ts=ts_bt, reason="deposit processed")
                if ts_bt:
                    confirmed_bt_candidates.append(int(ts_bt))
            elif found_deposit:
                page_has_unprocessed_deposit = True
            else:
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
                    vault_addr = str(config.VAULT_USDC_ACCOUNT)
                    pre_amt = 0
                    post_amt = 0
                    for e in pre:
                        try:
                            if e.get("mint") == str(config.USDC_MINT) and _akey(int(e.get("accountIndex"))) == vault_addr:
                                pre_amt = _amount(e)
                        except Exception:
                            pass
                    for e in post:
                        try:
                            if e.get("mint") == str(config.USDC_MINT) and _akey(int(e.get("accountIndex"))) == vault_addr:
                                post_amt = _amount(e)
                        except Exception:
                            pass
                    delta_final = post_amt - pre_amt
                    # Suppress non-deposit noise (no print)
                    if delta_final > 0:
                        page_has_unprocessed_deposit = True
                        continue
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
    except Exception as e:
        print(f"poll_solana_deposits error: {e}")
        print()
    finally:
        try:
            rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
            for r in rows:
                if r.get("comment") == "ready for processing" and r.get("receival_account"):
                    sig = r.get("sig")
                    recv = r.get("receival_account")
                    amt = int(r.get("amount_usdc_units") or 0)
                    flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                    dyn_bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
                    pre_dyn = max(0, amt - flat_fee_units)
                    dyn_fee = (pre_dyn * dyn_bps) // 10000 if dyn_bps > 0 else 0
                    net_usdc = max(0, pre_dyn - dyn_fee)
                    usdd_units = scale_amount(net_usdc, config.USDC_DECIMALS, config.USDD_DECIMALS)
                    ref = state.next_reference()
                    treas = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
                    if not treas:
                        ok, txid = (False, None)
                    else:
                        ok, txid = nexus_client.debit_account_with_txid(treas, recv, usdd_units, ref)
                    if ok:
                        total_fee = flat_fee_units + dyn_fee
                        if total_fee > 0:
                            fees.add_usdc_fee(total_fee)
                        def _pred(x):
                            return x.get("sig") == sig
                        def _upd(x):
                            x["txid"] = txid
                            x["reference"] = ref
                            x["comment"] = "debited, awaiting confirmations"
                            return x
                        state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred, _upd)
                    else:
                        # Debit failed: attempt USDC refund (retain flat fee only)
                        src = r.get("from")
                        if src:
                            refund_key = f"refund_usdc:{sig}"
                            if state.should_attempt(refund_key):
                                state.record_attempt(refund_key)
                                refundable = max(0, amt - flat_fee_units)
                                if refundable > 0 and solana_client.refund_usdc_to_source(src, refundable, "USDD debit failed"):
                                    if flat_fee_units > 0:
                                        fees.add_usdc_fee(flat_fee_units)
                                    out = dict(r)
                                    out["comment"] = "refunded"
                                    state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                                    state.append_jsonl(config.REFUNDED_SIGS_FILE, out)
                                    # Remove from unprocessed
                                    def _pred2(x):
                                        return x.get("sig") == sig
                                    # Filtered write below will drop it
                                else:
                                    # Quarantine after max attempts
                                    attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                    if attempts >= config.MAX_ACTION_ATTEMPTS:
                                        if solana_client.move_usdc_to_quarantine(refundable, note="FAILED_REFUND"):
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
        except Exception as e:
            print(f"processing ready entries error: {e}")
            print()

        try:
            rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
            new_unprocessed: list[dict] = []
            for r in rows:
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
                age_ok = (ts and (__import__("time").time() - ts) > config.REFUND_TIMEOUT_SEC)
                if age_ok:
                    src = r.get("from")
                    amt = int(r.get("amount_usdc_units") or 0)
                    flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                    refundable = max(0, amt - flat_fee_units)
                    should_refund = False
                    if comment == "ready for processing" and not r.get("receival_account"):
                        should_refund = True
                    elif comment == "debited, awaiting confirmations":
                        should_refund = True
                    if should_refund and src and refundable > 0:
                        refund_key = f"refund_usdc:{r.get('sig')}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            if solana_client.refund_usdc_to_source(src, refundable, "timeout or unresolved receival_account"):
                                if flat_fee_units > 0:
                                    fees.add_usdc_fee(flat_fee_units)
                                out = dict(r)
                                out["comment"] = "refunded"
                                state.append_jsonl(config.PROCESSED_SWAPS_FILE, out)
                                state.append_jsonl(config.REFUNDED_SIGS_FILE, out)
                                try:
                                    state.mark_solana_processed(r.get("sig"), ts=int(r.get("ts") or 0), reason="refunded")
                                except Exception:
                                    pass
                                continue
                            else:
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                if attempts >= config.MAX_ACTION_ATTEMPTS:
                                    if solana_client.move_usdc_to_quarantine(refundable, note="FAILED_REFUND"):
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
                                        continue
                new_unprocessed.append(r)
            state.write_jsonl(config.UNPROCESSED_SIGS_FILE, new_unprocessed)
        except Exception as e:
            print(f"confirm/move processed error: {e}")
            print()

        try:
            rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
            now = __import__("time").time()
            for r in rows:
                ts = int(r.get("ts") or 0)
                if (not r.get("receival_account") and ts and now - ts > config.REFUND_TIMEOUT_SEC):
                    def _pred(x):
                        return x.get("sig") == r.get("sig")
                    def _upd(x):
                        x["comment"] = "refund due"
                        return x
                    state.update_jsonl_row(config.UNPROCESSED_SIGS_FILE, _pred, _upd)
        except Exception as e:
            print(f"refund annotation error: {e}")
            print()

        # After updates, set waterline to min ts in unprocessed minus buffer
        try:
            rows = state.read_jsonl(config.UNPROCESSED_SIGS_FILE)
            if rows:
                min_ts = min(int(r.get("ts") or 0) for r in rows if int(r.get("ts") or 0) > 0)
                if min_ts:
                    state.propose_solana_waterline(int(max(0, min_ts - config.HEARTBEAT_WATERLINE_SAFETY_SEC)))
        except Exception:
            pass

        state.save_state()
