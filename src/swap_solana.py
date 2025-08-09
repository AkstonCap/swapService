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
        sigs_resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, limit=20)
        sig_list = [r.get("signature") for r in (sigs_resp.get("result") or []) if r.get("signature")]
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
                        if not memo_data:
                            print("No memo found; refunding to sender")
                            refund_key = f"refund_usdc:{sig}"
                            if state.should_attempt(refund_key) and source_token_acc:
                                state.record_attempt(refund_key)
                                if solana_client.refund_usdc_to_source(source_token_acc, amount_usdc_units, "Missing memo nexus:<addr>"):
                                    print("Refunded USDC to sender token account")
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
                                    if solana_client.refund_usdc_to_source(source_token_acc, amount_usdc_units, "Invalid or wrong Nexus address"):
                                        print("Refunded USDC to sender token account")
                                    else:
                                        print("Refund failed")
                                        mark_processed = False
                                else:
                                    print("Skipping refund attempt (cooldown/max attempts)")
                                    mark_processed = False
                            else:
                                # Apply optional fee on USDC→USDD path, retained in USDC units
                                fee_usdc = (amount_usdc_units * max(0, config.FEE_BPS_USDC_TO_USDD)) // 10000
                                net_usdc = max(0, amount_usdc_units - fee_usdc)
                                if fee_usdc > 0:
                                    fees.add_usdc_fee(fee_usdc)
                                usdd_units = scale_amount(net_usdc, config.USDC_DECIMALS, config.USDD_DECIMALS)
                                mint_key = f"mint_usdd:{sig}"
                                if state.should_attempt(mint_key):
                                    state.record_attempt(mint_key)
                                    if nexus_client.debit_usdd(nexus_addr, usdd_units, f"USDC_TX:{sig}"):
                                        print(f"Minted/sent {usdd_units} USDD units to {nexus_addr}")
                                    else:
                                        print("USDD mint/send failed")
                                        attempts = int((state.attempt_state.get(mint_key) or {}).get("attempts", 0))
                                        if attempts >= 2 and source_token_acc:
                                            refund_key = f"refund_usdc:{sig}"
                                            if state.should_attempt(refund_key):
                                                state.record_attempt(refund_key)
                                                if solana_client.refund_usdc_to_source(source_token_acc, amount_usdc_units, "USDD mint failed after retries"):
                                                    print("Refunded USDC to sender after repeated USDD mint failures")
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
                                if solana_client.refund_usdc_to_source(source_token_acc, amount_usdc_units, "Invalid memo format; expected nexus:<addr>"):
                                    print("Refunded USDC to sender token account")
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
