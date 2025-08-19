from decimal import Decimal, ROUND_DOWN, InvalidOperation
from . import config, state, solana_client, nexus_client, fees


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
    import subprocess

    # Use the configured treasury account name consistently
    treasury_addr = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)

    # Query recent transactions affecting the treasury account and parse nested parties.
    # Use finance/transactions/token and filter by name to avoid ambiguity.
    cmd = [
        config.NEXUS_CLI,
        "finance/transactions/token/"
        "txid,timestamp,confirmations,"
        "contracts.id,contracts.OP,contracts.from,contracts.to,contracts.amount,contracts.reference,contracts.ticker,contracts.token",
        f"name={treasury_addr}",
        "sort=timestamp",
        "order=desc",
        "limit=100",
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            print("Error fetching USDD transactions:", (res.stderr or res.stdout).strip())
            return

        txs = nexus_client._parse_json_lenient(res.stdout)
        if txs is None:
            print("Failed to parse Nexus CLI JSON output for finance/transactions/token")
            return
        if not isinstance(txs, list):
            txs = [txs]

        wl_cutoff = 0
        if getattr(config, "HEARTBEAT_WATERLINE_ENABLED", False):
            try:
                from .main import read_heartbeat_waterlines
                _, wl_nexus = read_heartbeat_waterlines()
                wl_cutoff = max(0, int(wl_nexus) - int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0)))
            except Exception:
                wl_cutoff = 0

        page_ts_candidates: list[int] = []
        for tx in txs:
            try:
                tx_id = (tx or {}).get("txid")
                if not tx_id:
                    continue
                ts = int((tx.get("timestamp") or 0) or 0)
                if ts:
                    page_ts_candidates.append(ts)
                if wl_cutoff and ts and ts < wl_cutoff:
                    continue
                confirmations = int(tx.get("confirmations") or 0)
                if confirmations <= 0:
                    continue
                contracts = tx.get("contracts") or []
                if not isinstance(contracts, list):
                    continue
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("id")
                    processed_key = f"{tx_id}:{cid if cid is not None else 'x'}"
                    if processed_key in state.processed_nexus_txs:
                        continue
                    # Extract nested 'to' and 'from' addresses robustly
                    def _addr(obj) -> str:
                        if isinstance(obj, dict):
                            a = obj.get("address")
                            if isinstance(a, str):
                                return a
                            # Some outputs might just include name strings
                            n = obj.get("name")
                            return str(n) if n else ""
                        if isinstance(obj, str):
                            return obj
                        return ""
                    
                    usdd_amount_dec = _parse_decimal_amount(c.get("amount"))
                    if usdd_amount_dec <= 0:
                        state.mark_nexus_processed(processed_key, ts=ts)
                        continue
                    # Tiny routing threshold compared in token units (Decimal); uses FLAT_FEE_USDD as threshold
                    tiny_threshold_dec = _parse_decimal_amount(getattr(config, "FLAT_FEE_USDD", "0"))
                    if usdd_amount_dec <= tiny_threshold_dec:
                        route_key = f"route_tiny_usdd:{processed_key}"
                        if state.should_attempt(route_key):
                            state.record_attempt(route_key)
                            amt_str = _format_token_amount(usdd_amount_dec, config.USDD_DECIMALS)
                            if nexus_client.send_tiny_usdd_to_local(amt_str, note=f"TINY_USDD:{tx_id}:{cid}"):
                                print(f"Routed tiny USDD {amt_str} to local; processed ({processed_key})")
                                state.mark_nexus_processed(processed_key, ts=ts)
                                continue
                            else:
                                print(f"Tiny USDD routing failed; will retry ({processed_key})")
                                continue
                        else:
                            print(f"Skipping tiny USDD routing attempt (cooldown/max attempts) ({processed_key})")
                            continue

                    ref_raw = c.get("reference", "")
                    ref_str = str(ref_raw).strip() if ref_raw is not None else ""
                    is_solana_ref = isinstance(ref_str, str) and ref_str.lower().startswith("solana:")
                    sol_addr = (ref_str[ref_str.find(":") + 1:].strip() if is_solana_ref else "")
                    sender_addr = _addr(c.get("from"))
                    # Base refund equals full USDD unless adjusted by congestion fee at send time
                    refund_amount_dec = usdd_amount_dec

                    if not is_solana_ref:
                        if not sender_addr:
                            print(f"Invalid/missing reference and unknown sender; skipping ({processed_key})")
                            continue
                        reason = "Missing or invalid reference; expected 'solana:<address>'"
                        refund_key = f"refund_usdd:{processed_key}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            fee_dec = _parse_decimal_amount(getattr(config, "NEXUS_CONGESTION_FEE_USDD", "0"))
                            net_refund_dec = refund_amount_dec - fee_dec
                            if net_refund_dec < 0:
                                net_refund_dec = Decimal(0)
                            ok_fee = True
                            if fee_dec > 0 and getattr(config, "NEXUS_USDD_LOCAL_ACCOUNT", None):
                                fee_str = _format_token_amount(fee_dec, config.USDD_DECIMALS)
                                ok_fee = nexus_client.transfer_usdd_between_accounts(
                                    config.NEXUS_USDD_TREASURY_ACCOUNT,
                                    config.NEXUS_USDD_LOCAL_ACCOUNT,
                                    fee_str,
                                    f"CONGESTION_FEE:{tx_id}:{cid}"
                                )
                            amt_str = _format_token_amount(net_refund_dec, config.USDD_DECIMALS)
                            if ok_fee and nexus_client.refund_usdd(sender_addr, amt_str, reason):
                                print(f"Refunded USDD due to invalid/missing reference ({processed_key})")
                                state.mark_nexus_processed(processed_key, ts=ts)
                            else:
                                # If we've reached max attempts, quarantine remaining refund and log
                                max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                if attempts >= max_tries and getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", None):
                                    q_ok = nexus_client.transfer_usdd_between_accounts(
                                        config.NEXUS_USDD_TREASURY_ACCOUNT,
                                        config.NEXUS_USDD_QUARANTINE_ACCOUNT,
                                        amt_str,
                                        f"FAILED_REFUND:{tx_id}:{cid}"
                                    )
                                    if q_ok:
                                        state.log_failed_refund({
                                            "type": "usdd_refund_failure",
                                            "key": processed_key,
                                            "reason": reason,
                                            "amount_usdd": amt_str,
                                        })
                                        print(f"Quarantined failed USDD refund ({processed_key}) and logged for manual inspection")
                                        state.mark_nexus_processed(processed_key, ts=ts)
                        else:
                            print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                        continue

                    valid_sol = True
                    try:
                        _ = solana_client.PublicKey.from_string(sol_addr)
                    except Exception:
                        valid_sol = False
                    if not valid_sol:
                        if not sender_addr:
                            print(f"Cannot determine sender Nexus address; skipping refund ({processed_key})")
                            continue
                        reason = f"Invalid Solana address: {sol_addr}"
                        refund_key = f"refund_usdd:{processed_key}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            fee_dec = _parse_decimal_amount(getattr(config, "NEXUS_CONGESTION_FEE_USDD", "0"))
                            net_refund_dec = refund_amount_dec - fee_dec
                            if net_refund_dec < 0:
                                net_refund_dec = Decimal(0)
                            ok_fee = True
                            if fee_dec > 0 and getattr(config, "NEXUS_USDD_LOCAL_ACCOUNT", None):
                                fee_str = _format_token_amount(fee_dec, config.USDD_DECIMALS)
                                ok_fee = nexus_client.transfer_usdd_between_accounts(
                                    config.NEXUS_USDD_TREASURY_ACCOUNT,
                                    config.NEXUS_USDD_LOCAL_ACCOUNT,
                                    fee_str,
                                    f"CONGESTION_FEE:{tx_id}:{cid}"
                                )
                            amt_str = _format_token_amount(net_refund_dec, config.USDD_DECIMALS)
                            if ok_fee and nexus_client.refund_usdd(sender_addr, amt_str, reason):
                                print(f"Refunded USDD due to invalid Solana address ({processed_key})")
                                state.mark_nexus_processed(processed_key, ts=ts)
                            else:
                                max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                if attempts >= max_tries and getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", None):
                                    q_ok = nexus_client.transfer_usdd_between_accounts(
                                        config.NEXUS_USDD_TREASURY_ACCOUNT,
                                        config.NEXUS_USDD_QUARANTINE_ACCOUNT,
                                        amt_str,
                                        f"FAILED_REFUND:{tx_id}:{cid}"
                                    )
                                    if q_ok:
                                        state.log_failed_refund({
                                            "type": "usdd_refund_failure",
                                            "key": processed_key,
                                            "reason": reason,
                                            "amount_usdd": amt_str,
                                        })
                                        print(f"Quarantined failed USDD refund ({processed_key}) and logged for manual inspection")
                                        state.mark_nexus_processed(processed_key, ts=ts)
                        else:
                            print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                        continue

                    # Convert Decimal token amount (USDD) to USDC base units for Solana 1:1
                    usdc_units = int((usdd_amount_dec * (Decimal(10) ** config.USDC_DECIMALS)).quantize(Decimal(1), rounding=ROUND_DOWN))

                    # Apply flat + dynamic fees (both in USDC base units)
                    flat_fee_units = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
                    fee_bps = int(getattr(config, "DYNAMIC_FEE_BPS", 0))
                    pre_dynamic = max(0, usdc_units - flat_fee_units)
                    dynamic_fee_usdc = (pre_dynamic * max(0, fee_bps)) // 10_000
                    net_usdc = max(0, pre_dynamic - dynamic_fee_usdc)
                    if net_usdc <= 0:
                        if not sender_addr:
                            print(f"Cannot determine sender Nexus address to refund zero-net; skipping ({processed_key})")
                            continue
                        reason = "Net USDC after fee is zero"
                        refund_key = f"refund_usdd:{processed_key}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            final_refund_dec = _apply_congestion_fee(refund_amount_dec)
                            amt_str = _format_token_amount(final_refund_dec, config.USDD_DECIMALS)
                            if nexus_client.refund_usdd(sender_addr, amt_str, reason):
                                print(f"Refunded USDD due to zero-net after fee ({processed_key})")
                                state.mark_nexus_processed(processed_key, ts=ts)
                            else:
                                max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                if attempts >= max_tries and getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", None):
                                    q_ok = nexus_client.transfer_usdd_between_accounts(
                                        config.NEXUS_USDD_TREASURY_ACCOUNT,
                                        config.NEXUS_USDD_QUARANTINE_ACCOUNT,
                                        amt_str,
                                        f"FAILED_REFUND:{tx_id}:{cid}"
                                    )
                                    if q_ok:
                                        state.log_failed_refund({
                                            "type": "usdd_refund_failure",
                                            "key": processed_key,
                                            "reason": reason,
                                            "amount_usdd": amt_str,
                                        })
                                        print(f"Quarantined failed USDD refund ({processed_key}) and logged for manual inspection")
                                        state.mark_nexus_processed(processed_key, ts=ts)
                        else:
                            print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                        continue

                    send_key = f"send_usdc:{processed_key}"
                    if state.should_attempt(send_key):
                        state.record_attempt(send_key)
                        memo = f"NEXUS_TX:{tx_id}:{cid}"
                        if solana_client.was_usdc_sent_for_nexus_tx(processed_key, sol_addr):
                            print(f"Detected prior USDC send for {processed_key}; marking processed")
                            state.mark_nexus_processed(processed_key, ts=ts)
                            continue
                        if solana_client.ensure_send_usdc_owner_or_ata(sol_addr, net_usdc, memo=memo):
                            print(f"Sent {net_usdc} USDC units to {sol_addr} ({processed_key})")
                            total_fee_units = flat_fee_units + dynamic_fee_usdc
                            if total_fee_units > 0:
                                fees.add_usdc_fee(total_fee_units)
                            state.mark_nexus_processed(processed_key, ts=ts)
                        else:
                            print(f"USDC send failed ({processed_key})")
                            attempts = int((state.attempt_state.get(send_key) or {}).get("attempts", 0))
                            if attempts >= 2 and sender_addr:
                                reason = f"USDC send failed after retries to {sol_addr}"
                                refund_key = f"refund_usdd:{processed_key}"
                                if state.should_attempt(refund_key):
                                    state.record_attempt(refund_key)
                                    # Always subtract congestion fee; additionally subtract dynamic fee if send was likely submitted
                                    fee_dec = _parse_decimal_amount(getattr(config, "NEXUS_CONGESTION_FEE_USDD", "0"))
                                    submitted_likely = False
                                    try:
                                        # If provided is a valid USDC token account, or it's an owner with existing ATA, we would submit
                                        if solana_client.is_valid_usdc_token_account(sol_addr):
                                            submitted_likely = True
                                        else:
                                            # Treat as owner address and check if ATA exists
                                            if solana_client.has_usdc_ata(sol_addr):
                                                submitted_likely = True
                                    except Exception:
                                        submitted_likely = False

                                    net_refund_dec = refund_amount_dec
                                    if submitted_likely:
                                        try:
                                            dynamic_fee_dec = (Decimal(dynamic_fee_usdc) / (Decimal(10) ** config.USDC_DECIMALS))
                                            flat_fee_dec = (Decimal(flat_fee_units) / (Decimal(10) ** config.USDC_DECIMALS))
                                        except Exception:
                                            dynamic_fee_dec = Decimal(0)
                                            flat_fee_dec = Decimal(0)
                                        net_refund_dec = net_refund_dec - dynamic_fee_dec - flat_fee_dec
                                    net_refund_dec = net_refund_dec - fee_dec
                                    if net_refund_dec < 0:
                                        net_refund_dec = Decimal(0)
                                    ok_fee = True
                                    if fee_dec > 0 and getattr(config, "NEXUS_USDD_LOCAL_ACCOUNT", None):
                                        fee_str = _format_token_amount(fee_dec, config.USDD_DECIMALS)
                                        ok_fee = nexus_client.transfer_usdd_between_accounts(
                                            config.NEXUS_USDD_TREASURY_ACCOUNT,
                                            config.NEXUS_USDD_LOCAL_ACCOUNT,
                                            fee_str,
                                            f"CONGESTION_FEE:{tx_id}:{cid}"
                                        )
                                    amt_str = _format_token_amount(net_refund_dec, config.USDD_DECIMALS)
                                    if ok_fee and nexus_client.refund_usdd(sender_addr, amt_str, reason):
                                        print(f"Refunded USDD after repeated send failures ({processed_key})")
                                        state.mark_nexus_processed(processed_key, ts=ts)
                                    else:
                                        max_tries = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
                                        attempts = int((state.attempt_state.get(refund_key) or {}).get("attempts", 1))
                                        if attempts >= max_tries and getattr(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", None):
                                            q_ok = nexus_client.transfer_usdd_between_accounts(
                                                config.NEXUS_USDD_TREASURY_ACCOUNT,
                                                config.NEXUS_USDD_QUARANTINE_ACCOUNT,
                                                amt_str,
                                                f"FAILED_REFUND:{tx_id}:{cid}"
                                            )
                                            if q_ok:
                                                state.log_failed_refund({
                                                    "type": "usdd_refund_failure",
                                                    "key": processed_key,
                                                    "reason": reason,
                                                    "amount_usdd": amt_str,
                                                })
                                                print(f"Quarantined failed USDD refund ({processed_key}) and logged for manual inspection")
                                                state.mark_nexus_processed(processed_key, ts=ts)
                                else:
                                    print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                    else:
                        print(f"Skipping send attempt (cooldown/max attempts) ({processed_key})")
            except Exception as e:
                print(f"Error processing Nexus tx: {e}")

        # Propose conservative Nexus waterline only if page isn't full
        try:
            # The API default limit is 100; if fewer than 100, we likely reached the end of the page
            if isinstance(txs, list) and len(txs) < 100 and page_ts_candidates:
                state.propose_nexus_waterline(int(min(page_ts_candidates)))
        except Exception:
            pass

    except Exception as e:
        print(f"Error polling USDD deposits: {e}")
    finally:
        state.save_state()
