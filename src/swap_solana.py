from decimal import Decimal
from . import config, state, nexus_client, solana_client, fees


def scale_amount(amount: int, src_decimals: int, dst_decimals: int) -> int:
    if src_decimals == dst_decimals:
        return int(amount)
    if src_decimals < dst_decimals:
        return int(amount) * (10 ** (dst_decimals - src_decimals))
    return int(amount) // (10 ** (src_decimals - dst_decimals))


def poll_solana_deposits():
    from solana.rpc.api import Client
    try:
        client = Client(config.RPC_URL)
        sigs_resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, limit=100)
        sig_results = (sigs_resp.get("result") or [])
        # Read heartbeat waterline and compute cutoff
        from .main import read_heartbeat_waterlines
        wl_solana, _ = read_heartbeat_waterlines()
        wl_cutoff = int(max(0, wl_solana - config.HEARTBEAT_WATERLINE_SAFETY_SEC)) if config.HEARTBEAT_WATERLINE_ENABLED else 0
        sig_list = []
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
        for sig in sig_list:
            if sig in state.processed_sigs:
                continue

            mark_processed = True
            try:
                tx = client.get_transaction(sig, encoding="jsonParsed").get("result")
                if not tx:
                    continue
            except Exception as e:
                print(f"Error fetching transaction {sig}: {e}")
                continue

            for instr in tx["transaction"]["message"]["instructions"]:
                if instr.get("program") == "spl-token" and instr.get("parsed"):
                    p = instr["parsed"]
                    if p.get("type") in ("transfer", "transferChecked") and p.get("info", {}).get(
                        "destination"
                    ) == str(config.VAULT_USDC_ACCOUNT):
                        info = p["info"]
                        if "amount" in info:
                            amount_usdc_units = int(info["amount"])
                        elif "tokenAmount" in info and isinstance(info["tokenAmount"], dict):
                            amount_usdc_units = int(info["tokenAmount"].get("amount", 0))
                        else:
                            continue

                        source_token_acc = info.get("source")

                        memo_data = solana_client.extract_memo_from_instructions(
                            tx["transaction"]["message"]["instructions"]
                        )
                        flat_fee_units = max(0, int(config.FLAT_FEE_USDC_UNITS))
                        # Tiny deposit drop: treat <= flat fee threshold entirely as fee, do nothing else
                        if amount_usdc_units <= flat_fee_units:
                            fees.add_usdc_fee(amount_usdc_units)
                            print(f"USDC deposit {amount_usdc_units} <= flat fee; treated as fee, no action")
                            break
                        if not memo_data:
                            print("No memo found; refunding to sender")
                            refund_key = f"refund_usdc:{sig}"
                            if state.should_attempt(refund_key) and source_token_acc:
                                state.record_attempt(refund_key)
                                net_refund = max(0, amount_usdc_units - flat_fee_units)
                                if net_refund <= 0:
                                    fees.add_usdc_fee(amount_usdc_units)
                                    print("Amount entirely consumed by flat fee; no refund sent")
                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, "Missing memo nexus:<addr>"):
                                    fees.add_usdc_fee(flat_fee_units)
                                    print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                else:
                                    print("Refund failed")
                                    mark_processed = False
                            else:
                                print("Skipping refund attempt (cooldown/max attempts)")
                                mark_processed = False
                            break

                        if memo_data.startswith("nexus:"):
                            nexus_addr = memo_data.split("nexus:", 1)[1].strip()
                            acct = nexus_client.get_account_info(nexus_addr)
                            if not acct or not nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                                print("Invalid Nexus address or token; require USDD account")
                                refund_key = f"refund_usdc:{sig}"
                                if state.should_attempt(refund_key) and source_token_acc:
                                    state.record_attempt(refund_key)
                                    net_refund = max(0, amount_usdc_units - flat_fee_units)
                                    if net_refund <= 0:
                                        fees.add_usdc_fee(amount_usdc_units)
                                        print("Amount entirely consumed by flat fee; no refund sent")
                                    elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, "Invalid or wrong Nexus address"):
                                        fees.add_usdc_fee(flat_fee_units)
                                        print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                    else:
                                        print("Refund failed")
                                        mark_processed = False
                                else:
                                    print("Skipping refund attempt (cooldown/max attempts)")
                                    mark_processed = False
                            else:
                                # Idempotency: skip if we already minted for this Solana signature
                                if nexus_client.was_usdd_minted_for_sig(nexus_addr, sig):
                                    print("Detected prior USDD mint for this Solana tx; marking processed")
                                    break
                                # Apply fees on USDC→USDD path:
                                # - Always retain a flat fee (on success or refund)
                                # - Apply dynamic fee BPS only on successful mint
                                dynamic_bps = max(0, int(config.FEE_BPS_USDC_TO_USDD))
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
                                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, "USDD mint failed after retries"):
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
                                net_refund = max(0, amount_usdc_units - flat_fee_units)
                                if net_refund <= 0:
                                    fees.add_usdc_fee(amount_usdc_units)
                                    print("Amount entirely consumed by flat fee; no refund sent")
                                elif solana_client.refund_usdc_to_source(source_token_acc, net_refund, "Invalid memo format; expected nexus:<addr>"):
                                    if flat_fee_units > 0:
                                        fees.add_usdc_fee(flat_fee_units)
                                    print(f"Refunded {net_refund} USDC units to sender (flat fee retained)")
                                else:
                                    print("Refund failed")
                                    mark_processed = False
                            else:
                                print("Skipping refund attempt (cooldown/max attempts)")
                                mark_processed = False
                        break

            if mark_processed:
                state.processed_sigs.add(sig)
    finally:
        state.save_state()
