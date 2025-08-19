from decimal import Decimal
from . import config, state, nexus_client, solana_client, fees


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
            last_exc = None
            # Build Signature object once; if invalid, we still try string mode
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
                    # Parse result from dict or typed response
                    try:
                        tx = tx_resp.get("result")
                        return tx
                    except AttributeError:
                        try:
                            import json as _json
                            js = _json.loads(tx_resp.to_json())
                            return js.get("result")
                        except Exception as e2:
                            last_exc = e2
                            continue
                except Exception as e3:
                    last_exc = e3
                    continue
            if last_exc:
                raise last_exc
            return None

        for sig in sig_list:
            if sig in state.processed_sigs:
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
                # Consider this tx confirmed enough for waterline (we fetched it and it wasn't failed)
                bt_val = sig_bt.get(sig)
                if bt_val:
                    confirmed_bt_candidates.append(int(bt_val))
            except Exception as e:
                # Print exception type and repr for better diagnostics across platforms
                try:
                    et = type(e).__name__
                except Exception:
                    et = "Exception"
                print(f"Error fetching transaction {sig}: {et} {repr(e)}")
                continue

            for instr in tx["transaction"]["message"]["instructions"]:
                if instr.get("program") == "spl-token" and instr.get("parsed"):
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
                        memo_data = solana_client.extract_memo_from_instructions(tx["transaction"]["message"]["instructions"])
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

            if mark_processed:
                # Record processed with best-known timestamp for pruning later
                state.mark_solana_processed(sig, ts=sig_bt.get(sig) or 0)
            else:
                # No relevant deposit found touching the vault; treat as benign (e.g., account creation)
                if not found_deposit:
                    state.mark_solana_processed(sig, ts=sig_bt.get(sig) or 0)

        # Propose a conservative waterline only if page wasn't full and only using confirmed txs we inspected
        try:
            if isinstance(sig_results, list) and len(sig_results) < limit and confirmed_bt_candidates:
                state.propose_solana_waterline(int(min(confirmed_bt_candidates)))
        except Exception:
            pass
    except Exception as e:
        print(f"poll_solana_deposits error: {e}")
    finally:
        state.save_state()
