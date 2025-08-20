from decimal import Decimal
from . import config, state, nexus_client, solana_client, fees


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
        # Pass Pubkey (solders) as required by solana-py 0.36.x
        sigs_resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, limit=limit)
        sig_results = _normalize_get_sigs_response(sigs_resp)
        # Read heartbeat waterline and compute cutoff
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
            # If blockTime present, skip older than waterline cutoff
            try:
                bt = int(r.get("blockTime", 0) or 0)
            except Exception:
                bt = 0
            if wl_cutoff and bt and bt < wl_cutoff:
                continue
            sig_list.append(sig)
            sig_bt[sig] = bt

        # Helper: robust get_transaction with fallbacks for signature type and version flag
        def _get_tx_result(sig_str: str):
            from typing import Any
            import json as _json

            def _normalize_response(resp: Any):
                """Convert various response types to a transaction dict"""
                # If already a dict, check for result wrapper
                if isinstance(resp, dict):
                    return resp.get("result") or resp

                # Handle solders typed responses by unwrapping recursively
                val = getattr(resp, "value", None)
                if val is not None:
                    # Recurse so we don't return typed objects directly
                    return _normalize_response(val)

                # Try to_json() method
                tj = getattr(resp, "to_json", None)
                if callable(tj):
                    try:
                        parsed = _json.loads(tj())
                        # Some responses wrap in "result", others are direct
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
                    
                    # Normalize the response to a transaction dict
                    tx_obj = _normalize_response(tx_resp)
                    if tx_obj is not None:
                        return tx_obj
                        
                except Exception as e3:
                    last_exc = e3
                    continue

            if last_exc:
                raise last_exc
            return None

        for sig in sig_list:
            # Skip any signature already processed (state or local cache)
            if _is_sig_processed(sig):
                continue

            mark_processed = False
            found_deposit = False
            try:
                tx = _get_tx_result(sig)
                if not tx:
                    continue
                # Skip failed transactions
                try:
                    if (tx.get("meta") or {}).get("err") is not None:
                        continue
                except Exception:
                    pass
                # Do not add to waterline candidates yet; only after we know if it was processed or benign
            except Exception as e:
                # Print exception type and repr for better diagnostics across platforms
                try:
                    et = type(e).__name__
                except Exception:
                    et = "Exception"
                print(f"Error fetching transaction {sig}: {et} {repr(e)}")
                continue

            # Gather all instructions (outer + inner CPIs)
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
                        memo_data = solana_client.extract_memo_from_instructions(all_instrs)
                        if not memo_data:
                            # Fallback: try extracting from runtime logMessages (e.g., "Program log: Memo ...")
                            try:
                                logs = (tx.get("meta") or {}).get("logMessages") or []
                                if isinstance(logs, list):
                                    for ln in logs:
                                        if not isinstance(ln, str):
                                            continue
                                        # Common patterns seen in logs
                                        # - "Program log: Memo (len ...): <text>"
                                        # - "Program log: Memo: <text>"
                                        if "Program log: Memo" in ln:
                                            parts = ln.split(":", 2)
                                            if len(parts) >= 3:
                                                cand = parts[-1].strip()
                                                if cand:
                                                    memo_data = cand
                                                    break
                            except Exception:
                                pass
                        flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))

                        # Tiny deposit drop: treat <= flat fee threshold entirely as fee, do nothing else
                        if amount_usdc_units <= flat_fee_units:
                            fees.add_usdc_fee(amount_usdc_units)
                            print(f"USDC deposit {amount_usdc_units} <= flat fee; treated as fee, no action")
                            mark_processed = True
                            break

                        if not memo_data:
                            print("No memo found; refunding to sender")
                            refund_key = f"refund_usdc:{sig}"
                            if state.should_attempt(refund_key) and source_token_acc:
                                state.record_attempt(refund_key)
                                # Flat fee per attempt: compute remaining after taking this attempt's fee
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                remaining_before_fee = max(0, amount_usdc_units - max(0, (attempts - 1) * flat_fee_units))
                                fee_this_attempt = min(flat_fee_units, remaining_before_fee)
                                net_refund = max(0, remaining_before_fee - fee_this_attempt)
                                if net_refund <= 0:
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print("Amount entirely consumed by flat fee; no refund sent")
                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, f"Missing memo nexus:<addr> USDC_TX:{sig}"):
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                else:
                                    print("Refund failed")
                                    # If we've hit max attempts, quarantine and log
                                    max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                    if attempts >= max_tries:
                                        # Keep the USDC out of vault backing by moving to quarantine
                                        if solana_client.move_usdc_to_quarantine(net_refund, note=f"FAILED_REFUND USDC_TX:{sig}"):
                                            state.log_failed_refund({
                                                "type": "refund_failure",
                                                "sig": sig,
                                                "reason": "Missing memo",
                                                "source_token_acc": source_token_acc,
                                                "amount_units": net_refund,
                                            })
                                            print("Quarantined failed refund amount and logged for manual inspection")
                                        mark_processed = True
                                    else:
                                        mark_processed = False
                            else:
                                print("Skipping refund attempt (cooldown/max attempts)")
                                mark_processed = False
                            break

                        if isinstance(memo_data, str) and memo_data.lower().startswith("nexus:"):
                            # Accept case-insensitive prefix; keep address case as-is
                            nexus_addr = memo_data[memo_data.find(":") + 1 :].strip()
                            acct = nexus_client.get_account_info(nexus_addr)
                            if not acct or not nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                                print("Invalid Nexus address or token; require USDD account")
                                refund_key = f"refund_usdc:{sig}"
                                if state.should_attempt(refund_key) and source_token_acc:
                                    state.record_attempt(refund_key)
                                    attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                    remaining_before_fee = max(0, amount_usdc_units - max(0, (attempts - 1) * flat_fee_units))
                                    fee_this_attempt = min(flat_fee_units, remaining_before_fee)
                                    net_refund = max(0, remaining_before_fee - fee_this_attempt)
                                    if net_refund <= 0:
                                        if fee_this_attempt > 0:
                                            fees.add_usdc_fee(fee_this_attempt)
                                        print("Amount entirely consumed by flat fee; no refund sent")
                                    elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, f"Invalid or wrong Nexus address USDC_TX:{sig}"):
                                        if fee_this_attempt > 0:
                                            fees.add_usdc_fee(fee_this_attempt)
                                        print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                    else:
                                        print("Refund failed")
                                        max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                        if attempts >= max_tries:
                                            if solana_client.move_usdc_to_quarantine(net_refund, note=f"FAILED_REFUND USDC_TX:{sig}"):
                                                state.log_failed_refund({
                                                    "type": "refund_failure",
                                                    "sig": sig,
                                                    "reason": "Invalid Nexus address",
                                                    "source_token_acc": source_token_acc,
                                                    "amount_units": net_refund,
                                                })
                                                print("Quarantined failed refund amount and logged for manual inspection")
                                            mark_processed = True
                                        else:
                                            mark_processed = False
                                else:
                                    print("Skipping refund attempt (cooldown/max attempts)")
                                    mark_processed = False
                            else:
                                # Idempotency: skip if we already debited treasury for this Solana signature
                                if nexus_client.was_usdd_debited_from_treasury_for_sig(sig, lookback=60, min_confirmations=0):
                                    print("Detected prior USDD debit from treasury (pending/confirmed); waiting for confirmations")
                                    break
                                # Apply fees on USDC→USDD path:
                                # - Always retain a flat fee (on success or refund)
                                # - Apply dynamic fee BPS only on successful mint
                                dynamic_bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
                                # Net amount available for mint before dynamic fee
                                pre_dynamic_net = max(0, amount_usdc_units - flat_fee_units)
                                dynamic_fee_usdc = (pre_dynamic_net * dynamic_bps) // 10000 if dynamic_bps > 0 else 0
                                net_usdc_for_mint = max(0, pre_dynamic_net - dynamic_fee_usdc)
                                usdd_units = scale_amount(net_usdc_for_mint, config.USDC_DECIMALS, config.USDD_DECIMALS)
                                mint_key = f"mint_usdd:{sig}"
                                if state.should_attempt(mint_key):
                                    state.record_attempt(mint_key)
                                    if nexus_client.debit_usdd(nexus_addr, usdd_units, f"USDC_TX:{sig}"):
                                        # Accrue fees only after success: flat + dynamic
                                        total_fee = flat_fee_units + dynamic_fee_usdc
                                        if total_fee > 0:
                                            fees.add_usdc_fee(total_fee)
                                        print(f"Minted/sent {usdd_units} USDD units to {nexus_addr} (fees retained: {total_fee})")
                                        # Explicit success marker for USDC->USDD path
                                        try:
                                            print(f"SWAP SUCCESS USDC->USDD sig={sig} usdc_units={net_usdc_for_mint} usdd_units={usdd_units} to={nexus_addr}")
                                        except Exception:
                                            pass
                                        mark_processed = True
                                    else:
                                        print("USDD mint/send failed")
                                        attempts = int((state.attempt_state.get(mint_key) or {}).get("attempts", 0))
                                        if attempts >= 2 and source_token_acc:
                                            refund_key = f"refund_usdc:{sig}"
                                            if state.should_attempt(refund_key):
                                                state.record_attempt(refund_key)
                                                net_refund = max(0, amount_usdc_units - flat_fee_units)
                                                if net_refund <= 0:
                                                    fees.add_usdc_fee(amount_usdc_units)
                                                    print("Amount entirely consumed by flat fee; no refund sent")
                                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, f"USDD mint failed after retries USDC_TX:{sig}"):
                                                    # Accrue only flat fee on refund path
                                                    if flat_fee_units > 0:
                                                        fees.add_usdc_fee(flat_fee_units)
                                                    print(f"Refunded {net_refund} USDC units to sender after retries (flat fee retained)")
                                                else:
                                                    print("USDC refund failed")
                                                    mark_processed = False
                                            else:
                                                print("Skipping refund attempt (cooldown/max attempts)")
                                                mark_processed = False
                                        else:
                                            mark_processed = False
                                else:
                                    print("Skipping USDD mint attempt (cooldown/max attempts)")
                                    mark_processed = False
                        else:
                            print("Bad memo format:", memo_data)
                            refund_key = f"refund_usdc:{sig}"
                            if state.should_attempt(refund_key) and source_token_acc:
                                state.record_attempt(refund_key)
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                remaining_before_fee = max(0, amount_usdc_units - max(0, (attempts - 1) * flat_fee_units))
                                fee_this_attempt = min(flat_fee_units, remaining_before_fee)
                                net_refund = max(0, remaining_before_fee - fee_this_attempt)
                                if net_refund <= 0:
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print("Amount entirely consumed by flat fee; no refund sent")
                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, f"Invalid memo format; expected nexus:<addr> USDC_TX:{sig}"):
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                else:
                                    print("Refund failed")
                                    max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                    if attempts >= max_tries:
                                        if solana_client.move_usdc_to_quarantine(net_refund, note=f"FAILED_REFUND USDC_TX:{sig}"):
                                            state.log_failed_refund({
                                                "type": "refund_failure",
                                                "sig": sig,
                                                "reason": "Bad memo format",
                                                "source_token_acc": source_token_acc,
                                                "amount_units": net_refund,
                                            })
                                            print("Quarantined failed refund amount and logged for manual inspection")
                                        mark_processed = True
                                    else:
                                        mark_processed = False
                            else:
                                print("Skipping refund attempt (cooldown/max attempts)")
                                mark_processed = False
                        break

            # Fallback: detect deposit via balance deltas when parsed instructions are inconclusive
            if not found_deposit:
                try:
                    meta = tx.get("meta") or {}
                    pre = meta.get("preTokenBalances") or []
                    post = meta.get("postTokenBalances") or []
                    # Build account index -> pubkey map
                    acct_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    def _akey(i):
                        try:
                            k = acct_keys[i]
                            if isinstance(k, str):
                                return k
                            if isinstance(k, dict):
                                # jsonParsed may use {"pubkey": "..."}
                                return k.get("pubkey") or k.get("pubKey") or ""
                        except Exception:
                            return ""
                        return ""
                    def _amount(entry):
                        try:
                            return int(((entry.get("uiTokenAmount") or {}).get("amount")) or 0)
                        except Exception:
                            return 0
                    # Map account addr -> amount for USDC mint only
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
                        # Find a source account with the opposite delta
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
                        memo_data = solana_client.extract_memo_from_instructions(all_instrs)
                        if not memo_data:
                            try:
                                logs = (tx.get("meta") or {}).get("logMessages") or []
                                if isinstance(logs, list):
                                    for ln in logs:
                                        if not isinstance(ln, str):
                                            continue
                                        if "Program log: Memo" in ln:
                                            parts = ln.split(":", 2)
                                            if len(parts) >= 3:
                                                cand = parts[-1].strip()
                                                if cand:
                                                    memo_data = cand
                                                    break
                            except Exception:
                                pass
                        flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                        found_deposit = True

                        # Tiny deposit drop
                        if amount_usdc_units <= flat_fee_units:
                            fees.add_usdc_fee(amount_usdc_units)
                            print(f"USDC deposit {amount_usdc_units} <= flat fee; treated as fee, no action [delta]")
                            mark_processed = True
                        elif not memo_data:
                            print("No memo found [delta]; refunding to sender if known")
                            refund_key = f"refund_usdc:{sig}"
                            if state.should_attempt(refund_key) and source_token_acc:
                                state.record_attempt(refund_key)
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                remaining_before_fee = max(0, amount_usdc_units - max(0, (attempts - 1) * flat_fee_units))
                                fee_this_attempt = min(flat_fee_units, remaining_before_fee)
                                net_refund = max(0, remaining_before_fee - fee_this_attempt)
                                if net_refund <= 0:
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print("Amount entirely consumed by flat fee; no refund sent")
                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, f"Missing memo nexus:<addr> USDC_TX:{sig}"):
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                else:
                                    print("Refund failed")
                                    max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                    if attempts >= max_tries:
                                        if solana_client.move_usdc_to_quarantine(net_refund, note=f"FAILED_REFUND USDC_TX:{sig}"):
                                            state.log_failed_refund({
                                                "type": "refund_failure",
                                                "sig": sig,
                                                "reason": "Missing memo [delta]",
                                                "source_token_acc": source_token_acc,
                                                "amount_units": net_refund,
                                            })
                                            print("Quarantined failed refund amount and logged for manual inspection")
                                        mark_processed = True
                                    else:
                                        mark_processed = False
                            else:
                                print("Skipping refund attempt (cooldown/max attempts) or unknown source [delta]")
                                mark_processed = False
                        elif isinstance(memo_data, str) and memo_data.lower().startswith("nexus:"):
                            nexus_addr = memo_data[memo_data.find(":") + 1 :].strip()
                            acct = nexus_client.get_account_info(nexus_addr)
                            if not acct or not nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                                print("Invalid Nexus address or token [delta]; require USDD account")
                                refund_key = f"refund_usdc:{sig}"
                                if state.should_attempt(refund_key) and source_token_acc:
                                    state.record_attempt(refund_key)
                                    attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                    remaining_before_fee = max(0, amount_usdc_units - max(0, (attempts - 1) * flat_fee_units))
                                    fee_this_attempt = min(flat_fee_units, remaining_before_fee)
                                    net_refund = max(0, remaining_before_fee - fee_this_attempt)
                                    if net_refund <= 0:
                                        if fee_this_attempt > 0:
                                            fees.add_usdc_fee(fee_this_attempt)
                                        print("Amount entirely consumed by flat fee; no refund sent")
                                    elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, f"Invalid or wrong Nexus address USDC_TX:{sig}"):
                                        if fee_this_attempt > 0:
                                            fees.add_usdc_fee(fee_this_attempt)
                                        print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                    else:
                                        print("Refund failed")
                                        max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                        if attempts >= max_tries:
                                            if solana_client.move_usdc_to_quarantine(net_refund, note=f"FAILED_REFUND USDC_TX:{sig}"):
                                                state.log_failed_refund({
                                                    "type": "refund_failure",
                                                    "sig": sig,
                                                    "reason": "Invalid Nexus address [delta]",
                                                    "source_token_acc": source_token_acc,
                                                    "amount_units": net_refund,
                                                })
                                                print("Quarantined failed refund amount and logged for manual inspection")
                                            mark_processed = True
                                        else:
                                            mark_processed = False
                                else:
                                    print("Skipping refund attempt (cooldown/max attempts) [delta]")
                                    mark_processed = False
                            else:
                                if nexus_client.was_usdd_debited_from_treasury_for_sig(sig, lookback=60, min_confirmations=0):
                                    print("Detected prior USDD debit from treasury (pending/confirmed); waiting for confirmations [delta]")
                                else:
                                    dynamic_bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
                                    pre_dynamic_net = max(0, amount_usdc_units - flat_fee_units)
                                    dynamic_fee_usdc = (pre_dynamic_net * dynamic_bps) // 10000 if dynamic_bps > 0 else 0
                                    net_usdc_for_mint = max(0, pre_dynamic_net - dynamic_fee_usdc)
                                    usdd_units = scale_amount(net_usdc_for_mint, config.USDC_DECIMALS, config.USDD_DECIMALS)
                                    mint_key = f"mint_usdd:{sig}"
                                    if state.should_attempt(mint_key):
                                        state.record_attempt(mint_key)
                                        if nexus_client.debit_usdd(nexus_addr, usdd_units, f"USDC_TX:{sig}"):
                                            total_fee = flat_fee_units + dynamic_fee_usdc
                                            if total_fee > 0:
                                                fees.add_usdc_fee(total_fee)
                                            print(f"Minted/sent {usdd_units} USDD units to {nexus_addr} (fees retained: {total_fee}) [delta]")
                                            try:
                                                print(f"SWAP SUCCESS USDC->USDD sig={sig} usdc_units={net_usdc_for_mint} usdd_units={usdd_units} to={nexus_addr}")
                                            except Exception:
                                                pass
                                            mark_processed = True
                                        else:
                                            print("USDD mint/send failed [delta]")
                                            mark_processed = False
                                    else:
                                        print("Skipping USDD mint attempt (cooldown/max attempts) [delta]")
                                        mark_processed = False
                        else:
                            print("Bad memo format [delta]:", memo_data)
                            refund_key = f"refund_usdc:{sig}"
                            if state.should_attempt(refund_key) and source_token_acc:
                                state.record_attempt(refund_key)
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                remaining_before_fee = max(0, amount_usdc_units - max(0, (attempts - 1) * flat_fee_units))
                                fee_this_attempt = min(flat_fee_units, remaining_before_fee)
                                net_refund = max(0, remaining_before_fee - fee_this_attempt)
                                if net_refund <= 0:
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print("Amount entirely consumed by flat fee; no refund sent")
                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, f"Invalid memo format; expected nexus:<addr> USDC_TX:{sig}"):
                                    if fee_this_attempt > 0:
                                        fees.add_usdc_fee(fee_this_attempt)
                                    print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                else:
                                    print("Refund failed")
                                    max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                    if attempts >= max_tries:
                                        if solana_client.move_usdc_to_quarantine(net_refund, note=f"FAILED_REFUND USDC_TX:{sig}"):
                                            state.log_failed_refund({
                                                "type": "refund_failure",
                                                "sig": sig,
                                                "reason": "Bad memo format [delta]",
                                                "source_token_acc": source_token_acc,
                                                "amount_units": net_refund,
                                            })
                                            print("Quarantined failed refund amount and logged for manual inspection")
                                        mark_processed = True
                                    else:
                                        mark_processed = False
                            else:
                                print("Skipping refund attempt (cooldown/max attempts) [delta]")
                                mark_processed = False
                except Exception:
                    # If delta analysis fails, we leave found_deposit as-is
                    pass

            if mark_processed:
                # Record processed with best-known timestamp for pruning later
                ts_bt = sig_bt.get(sig) or 0
                state.mark_solana_processed(sig, ts=ts_bt, reason="deposit processed")
                if ts_bt:
                    confirmed_bt_candidates.append(int(ts_bt))
            elif found_deposit:
                # There is a deposit but it wasn't processed yet (e.g., awaiting refund/memo/attempt cooldown)
                page_has_unprocessed_deposit = True
            else:
                # No relevant deposit found touching the vault; treat as benign (e.g., account creation)
                # Extra certainty: log vault USDC delta before classifying. If we still see a positive
                # delta here, treat it as an unprocessed deposit and do not mark as not-a-deposit.
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
                    print(f"Classifying NOT A DEPOSIT sig={sig} vault_pre={pre_amt} vault_post={post_amt} delta={delta_final}")
                    if delta_final > 0:
                        # Defensive: do not mark as not-a-deposit if a positive delta is observed here.
                        page_has_unprocessed_deposit = True
                        continue
                except Exception:
                    pass
                ts_bt = sig_bt.get(sig) or 0
                state.mark_solana_processed(sig, ts=ts_bt, reason="not a deposit")
                _processed_sig_cache.add(sig)
                if ts_bt:
                    confirmed_bt_candidates.append(int(ts_bt))

        # Propose a conservative waterline only if page wasn't full and only using confirmed txs we inspected
        try:
            if (
                isinstance(sig_results, list)
                and len(sig_results) < limit
                and confirmed_bt_candidates
                and not page_has_unprocessed_deposit
            ):
                state.propose_solana_waterline(int(min(confirmed_bt_candidates)))
            elif page_has_unprocessed_deposit:
                # Optional: log that we are holding waterline due to pending items
                print("Holding Solana waterline: unprocessed deposit(s) in current page")
        except Exception:
            pass
    except Exception as e:
        print(f"poll_solana_deposits error: {e}")
    finally:
        state.save_state()
