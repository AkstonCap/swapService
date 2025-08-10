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

    # Use Treasury for main credits; tiny deposits can be routed to LOCAL later.
    treasury_addr = config.NEXUS_USDD_TREASURY_ACCOUNT
    cmd = [config.NEXUS_CLI, "finance/transaction/account", f"address={treasury_addr}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            print("Error fetching USDD transactions:", res.stderr)
            return
        txs = json.loads(res.stdout)
        if not isinstance(txs, list):
            txs = [txs]
        def _get_sender_addr(t: dict):
            if not isinstance(t, dict):
                return None
            # Common fields
            for key in ("from", "sender", "source", "addressFrom", "origin"):
                val = t.get(key) or t.get(key.lower())
                if isinstance(val, str) and val:
                    return val
            # Nested
            for c in ("result", "tx", "transaction", "data", "details"):
                inner = t.get(c)
                if isinstance(inner, dict):
                    addr = _get_sender_addr(inner)
                    if addr:
                        return addr
            return None

        # Read heartbeat waterline and apply a safety margin
        from .main import read_heartbeat_waterlines
        _, wl_nexus = read_heartbeat_waterlines()
        wl_cutoff = int(max(0, wl_nexus - config.HEARTBEAT_WATERLINE_SAFETY_SEC)) if config.HEARTBEAT_WATERLINE_ENABLED else 0

        for tx in txs:
            tx_id = tx.get("txid")
            if not tx_id or tx_id in state.processed_nexus_txs:
                continue
            mark_processed = True
            ts = int(tx.get("timestamp", 0) or tx.get("time", 0) or tx.get("created", 0) or 0)
            if wl_cutoff and ts and ts < wl_cutoff:
                # Older than waterline; skip safely (idempotency protects if we later re-check)
                continue

            confirmed = bool(tx.get("confirmed", False)) or tx.get("confirmation", 0) > 0 or tx.get("confirmations", 0) > 0
            if tx.get("type") == "CREDIT" and confirmed:
                usdd_units = parse_amount_to_base_units(tx.get("amount", 0), config.USDD_DECIMALS)
                usdc_units = int(usdd_units)  # same decimals by default; adjust if needed
                # Tiny USDD routing: if <= flat fee threshold, route to local account (no USDC sent)
                if usdd_units <= max(0, int(config.FLAT_FEE_USDD_UNITS)):
                    route_key = f"route_tiny_usdd:{tx_id}"
                    if state.should_attempt(route_key):
                        state.record_attempt(route_key)
                        if nexus_client.send_tiny_usdd_to_local(usdd_units, note=f"TINY_USDD:{tx_id}"):
                            print(f"Routed tiny USDD {usdd_units} to local account; marked processed")
                            state.processed_nexus_txs.add(tx_id)
                            continue
                        else:
                            print("Tiny USDD routing failed; will retry after cooldown")
                            mark_processed = False
                    else:
                        print("Skipping tiny USDD routing attempt (cooldown/max attempts)")
                        mark_processed = False
                reference = tx.get("reference", "") or ""
                if reference.startswith("solana:"):
                    sol_addr = reference.split("solana:", 1)[1].strip()
                    try:
                        _ = PublicKey(sol_addr)
                        valid = True
                    except Exception:
                        valid = False
                    if valid:
                        # Require recipient to have USDC ATA; if not, refund USDD with explanation
                        if not solana_client.has_usdc_ata(sol_addr):
                            print("Recipient lacks USDC ATA; attempting USDD refund to sender")
                            sender_addr = _get_sender_addr(tx)
                            if sender_addr:
                                reason = f"Missing USDC ATA for {sol_addr}"
                                refund_key = f"refund_usdd:{tx_id}"
                                if state.should_attempt(refund_key):
                                    state.record_attempt(refund_key)
                                    if nexus_client.refund_usdd(sender_addr, usdd_units, reason):
                                        print("Refunded USDD to sender due to missing ATA")
                                    else:
                                        print("USDD refund failed")
                                        mark_processed = False
                                else:
                                    print("Skipping refund attempt (cooldown/max attempts)")
                                    mark_processed = False
                            else:
                                print("Cannot determine sender Nexus address; skipping refund")
                                mark_processed = False
                        else:
                            send_key = f"send_usdc:{tx_id}"
                            if state.should_attempt(send_key):
                                state.record_attempt(send_key)
                                # Apply optional fee on USDD→USDC path before sending
                                fee_usdc = (usdc_units * max(0, config.FEE_BPS_USDD_TO_USDC)) // 10000
                                net_usdc = max(0, usdc_units - fee_usdc)
                                # Idempotency: skip if already sent for this Nexus txid
                                if solana_client.was_usdc_sent_for_nexus_tx(tx_id, sol_addr):
                                    print("Detected prior USDC send for this Nexus tx; marking processed")
                                    state.processed_nexus_txs.add(tx_id)
                                    continue
                                memo = f"NEXUS_TX:{tx_id}"
                                if solana_client.ensure_send_usdc(sol_addr, net_usdc, memo=memo):
                                    print(f"Sent {net_usdc} USDC units to {sol_addr}")
                                    if fee_usdc > 0:
                                        fees.add_usdc_fee(fee_usdc)
                                else:
                                    print("USDC send failed")
                                    attempts = int((state.attempt_state.get(send_key) or {}).get("attempts", 0))
                                    if attempts >= 2:
                                        sender_addr = _get_sender_addr(tx)
                                        if sender_addr:
                                            reason = f"USDC send failed after retries to {sol_addr}"
                                            refund_key = f"refund_usdd:{tx_id}"
                                            if state.should_attempt(refund_key):
                                                state.record_attempt(refund_key)
                                                if nexus_client.refund_usdd(sender_addr, usdd_units, reason):
                                                    print("Refunded USDD to sender after repeated send failures")
                                                else:
                                                    print("USDD refund failed")
                                                    mark_processed = False
                                            else:
                                                print("Skipping refund attempt (cooldown/max attempts)")
                                                mark_processed = False
                                        else:
                                            print("Cannot determine sender Nexus address; skipping refund")
                                            mark_processed = False
                                    else:
                                        mark_processed = False
                            else:
                                print("Skipping send attempt (cooldown/max attempts)")
                                mark_processed = False
                    else:
                        print("Invalid Solana address in reference")
                        mark_processed = False
                else:
                    print(f"No Solana address in reference: {reference}")
            if mark_processed:
                state.processed_nexus_txs.add(tx_id)
    except Exception as e:
        print(f"Error polling USDD deposits: {e}")
    finally:
        state.save_state()
