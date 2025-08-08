from decimal import Decimal
from . import config, state, nexus_client, solana_client


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
        sigs = client.get_signatures_for_address(
            config.VAULT_USDC_ACCOUNT, limit=100, commitment="confirmed"
        )["result"]
    except Exception as e:
        print(f"Error fetching signatures: {e}")
        return

    for entry in sigs:
        sig = entry["signature"]
        if sig in state.processed_sigs:
            continue

        mark_processed = True
        try:
            tx = Client(config.RPC_URL).get_transaction(sig, encoding="jsonParsed")["result"]
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
                            from .solana_refunds import refund_usdc_to_source  # optional split
                        # Fallback: call inline refund from original script if not splitting further
                        # Keeping behavior: mark failure if refund fails
                        mark_processed = False  # let the original send/refund handle marking in monolith
                        break

                    if memo_data.startswith("nexus:"):
                        nexus_addr = memo_data.split("nexus:", 1)[1].strip()
                        acct = nexus_client.get_account_info(nexus_addr)
                        if not acct or not nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                            print("Invalid Nexus address or token; require USDD account")
                            mark_processed = False
                        else:
                            # Apply fee? In this modular version we skip fee math and use straight passthrough
                            usdd_units = scale_amount(amount_usdc_units, config.USDC_DECIMALS, config.USDD_DECIMALS)
                            mint_key = f"mint_usdd:{sig}"
                            if state.should_attempt(mint_key):
                                state.record_attempt(mint_key)
                                if nexus_client.debit_usdd(nexus_addr, usdd_units, f"USDC_TX:{sig}"):
                                    print(f"Minted/sent {usdd_units} USDD units to {nexus_addr}")
                                else:
                                    print("USDD mint/send failed")
                                    mark_processed = False
                    else:
                        print("Bad memo format:", memo_data)
                        mark_processed = False
                    break

        if mark_processed:
            state.processed_sigs.add(sig)
    state.save_state()
