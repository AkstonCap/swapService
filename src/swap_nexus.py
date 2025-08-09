from decimal import Decimal
from solana.publickey import PublicKey
from . import config, state, solana_client, nexus_client


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

    cmd = [config.NEXUS_CLI, "finance/transaction/account", f"address={config.NEXUS_USDD_ACCOUNT}"]
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

        for tx in txs:
            tx_id = tx.get("txid")
            if not tx_id or tx_id in state.processed_nexus_txs:
                continue
            mark_processed = True

            confirmed = bool(tx.get("confirmed", False)) or tx.get("confirmation", 0) > 0 or tx.get("confirmations", 0) > 0
            if tx.get("type") == "CREDIT" and confirmed:
                usdd_units = parse_amount_to_base_units(tx.get("amount", 0), config.USDD_DECIMALS)
                usdc_units = int(usdd_units)  # same decimals by default; adjust if needed
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
                                if solana_client.ensure_send_usdc(sol_addr, usdc_units):
                                    print(f"Sent {usdc_units} USDC units to {sol_addr}")
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
