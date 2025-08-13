from decimal import Decimal
from solana.publickey import PublicKey
from . import config, state, solana_client, nexus_client, fees


def parse_amount_to_base_units(val, decimals: int) -> int:
    try:
        s = str(val)
        if "." in s:
            return int((Decimal(s) * (Decimal(10) ** decimals)).to_integral_value())
        return int(s)
    except Exception:
        return 0


def poll_nexus_usdd_deposits():
    import json
    import subprocess

    treasury_addr = config.NEXUS_USDD_TREASURY_ACCOUNT
    cmd = [
        config.NEXUS_CLI, 
           "finance/transaction/account/"
           "txid,timestamp,confirmations,"
           "contracts.OP,contracts.from,contracts.amount,contracts.reference", 
           f"address={treasury_addr}"
           ]
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            print("Error fetching USDD transactions:", res.stderr)
            return
        
        try:
            txs = json.loads(res.stdout)
        except json.JSONDecodeError:
            print("Failed to parse Nexus CLI JSON output for finance/transaction/account")
            return

        if not isinstance(txs, list):
            txs = [txs]

        # Optional: read on-chain heartbeat waterline to skip very old items
        wl_cutoff = 0
        if getattr(config, "HEARTBEAT_WATERLINE_ENABLED", False):
            try:
                from .main import read_heartbeat_waterlines
                _, wl_nexus = read_heartbeat_waterlines()
                wl_cutoff = int(max(0, wl_nexus - config.HEARTBEAT_WATERLINE_SAFETY_SEC))
            except Exception:
                wl_cutoff = 0

        for tx in txs:
            tx_id = tx.get("txid")
            if not tx_id or tx_id in state.processed_nexus_txs:
                continue
            processed_key = f"{tx_id}"
            mark_processed = True
            ts = int(tx.get("timestamp", 0) )
            if wl_cutoff and ts and ts < wl_cutoff:
                continue
            confirmations = int(tx.get("confirmations") or 0)
            if confirmations <= 1:
                continue

            contracts = tx.get("contracts") or []
            if not isinstance(contracts, list):
                continue

            confirmed = tx.get("confirmations", 0) > 1

            for c in contracts:
                if not isinstance(c, dict):
                    continue

                contract_op = c.get("OP")
                if contract_op != "CREDIT":
                    # Don't handle debit (outgoing) contracts, only credit (incoming) contracts
                    continue

                from_addr = c.get("from")

                usdd_units = parse_amount_to_base_units(c.get("amount"), config.USDD_DECIMALS)
                if usdd_units <= 0:
                    state.processed_nexus_txs.add(processed_key)
                    continue

                # Tiny USDD routing (policy)
                if usdd_units <= max(0, int(getattr(config, "FLAT_FEE_USDD_UNITS", 0))):
                    route_key = f"route_tiny_usdd:{processed_key}"
                    if state.should_attempt(route_key):
                        state.record_attempt(route_key)
                        if nexus_client.send_tiny_usdd_to_local(usdd_units, note=f"TINY_USDD:{tx_id}"):
                            print(f"Routed tiny USDD {usdd_units} to local account; marked processed ({processed_key})")
                            state.processed_nexus_txs.add(processed_key)
                            continue
                        else:
                            print(f"Tiny USDD routing failed; will retry ({processed_key})")
                            continue  # leave unprocessed to retry after cooldown
                    else:
                        print(f"Skipping tiny USDD routing attempt (cooldown/max attempts) ({processed_key})")
                        continue  # leave unprocessed


                # Parse and validate reference: expect "USDC_SOL:<WALLET_OR_ATA>"
                ref_raw = c.get("reference", "")
                ref_str = str(ref_raw).strip() if ref_raw is not None else ""
                if ref_str.upper().startswith("USDC_SOL:"):
                    sol_addr = ref_str.split(":", 1)[1].strip()
                    # Validate Solana address
                    valid_sol = True
                    try:
                        _ = PublicKey(sol_addr)
                    except Exception:
                        valid_sol = False

                    if not valid_sol:
                        # Attempt to refund USDD due to invalid Solana address
                        sender_addr = from_addr
                        if not sender_addr:
                            print(f"Cannot determine sender Nexus address; skipping refund ({processed_key})")
                            continue
                        reason = f"Invalid Solana address: {sol_addr}"
                        refund_key = f"refund_usdd:{processed_key}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            if nexus_client.refund_usdd(sender_addr, usdd_units, reason):
                                print(f"Refunded USDD to sender due to invalid Solana address ({processed_key})")
                                state.processed_nexus_txs.add(processed_key)
                            else:
                                print(f"USDD refund failed ({processed_key})")
                        else:
                            print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                        continue

                    # Send USDC to provided Solana address (apply optional fee)
                    usdc_units = int(usdd_units)  # adjust if decimals differ
                    fee_usdc = (usdc_units * max(0, getattr(config, "FEE_BPS_USDD_TO_USDC", 0))) // 10000
                    net_usdc = max(0, usdc_units - fee_usdc)

                    if net_usdc <= 0:
                        # Nothing to send; refund USDD
                        sender_addr = from_addr
                        refund_amount = usdd_units - 0.01
                        if not sender_addr:
                            print(f"Cannot determine sender Nexus address to refund zero-net; skipping ({processed_key})")
                            continue
                        reason = "Net USDC after fee is zero"
                        refund_key = f"refund_usdd:{processed_key}"
                        if state.should_attempt(refund_key):
                            state.record_attempt(refund_key)
                            if nexus_client.refund_usdd(sender_addr, refund_amount, reason):
                                print(f"Refunded USDD due to zero-net after fee ({processed_key})")
                                state.processed_nexus_txs.add(processed_key)
                            else:
                                print(f"USDD refund failed ({processed_key})")
                        else:
                            print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                        continue

                    send_key = f"send_usdc:{processed_key}"
                    if state.should_attempt(send_key):
                        state.record_attempt(send_key)
                        memo = f"NEXUS_TX:{tx_id}"
                        # Idempotency: check if already sent for this tx/contract
                        if solana_client.was_usdc_sent_for_nexus_tx(processed_key, sol_addr):
                            print(f"Detected prior USDC send for {processed_key}; marking processed")
                            state.processed_nexus_txs.add(processed_key)
                            continue
                        if solana_client.ensure_send_usdc(sol_addr, net_usdc, memo=memo):
                            print(f"Sent {net_usdc} USDC units to {sol_addr} ({processed_key})")
                            if fee_usdc > 0:
                                fees.add_usdc_fee(fee_usdc)
                            state.processed_nexus_txs.add(processed_key)
                        else:
                            print(f"USDC send failed ({processed_key})")
                            # If repeated failures, attempt USDD refund to sender
                            attempts = int((state.attempt_state.get(send_key) or {}).get("attempts", 0))
                            if attempts >= 2:
                                sender_addr = from_addr
                                if sender_addr:
                                    reason = f"USDC send failed after retries to {sol_addr}"
                                    refund_key = f"refund_usdd:{processed_key}"
                                    if state.should_attempt(refund_key):
                                        state.record_attempt(refund_key)
                                        if nexus_client.refund_usdd(sender_addr, usdd_units, reason):
                                            print(f"Refunded USDD to sender after repeated send failures ({processed_key})")
                                            state.processed_nexus_txs.add(processed_key)
                                        else:
                                            print(f"USDD refund failed ({processed_key})")
                                    else:
                                        print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                                else:
                                    print(f"Cannot determine sender Nexus address; skipping refund ({processed_key})")
                    else:
                        print(f"Skipping send attempt (cooldown/max attempts) ({processed_key})")

                else:
                    # Missing or invalid reference — policy: refund USDD to sender
                    sender_addr = from_addr
                    if not sender_addr:
                        print(f"Invalid/missing reference and unknown sender; skipping ({processed_key})")
                        continue
                    reason = "Missing or invalid reference; expected 'USDC_SOL:<address>'"
                    refund_key = f"refund_usdd:{processed_key}"
                    if state.should_attempt(refund_key):
                        state.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender_addr, refund_amount, reason):
                            print(f"Refunded USDD to sender due to invalid/missing reference ({processed_key})")
                            state.processed_nexus_txs.add(processed_key)
                        else:
                            print(f"USDD refund failed ({processed_key})")
                    else:
                        print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")

    except Exception as e:
        print(f"Error polling USDD deposits: {e}")
    finally:
        state.save_state()
