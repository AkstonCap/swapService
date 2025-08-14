from decimal import Decimal, ROUND_DOWN, InvalidOperation
from solders.pubkey import Pubkey as PublicKey
from . import config, state, solana_client, nexus_client, fees


def parse_amount_to_base_units(val, decimals: int) -> int:
    """
    Convert a token-denominated amount (e.g., "1.23") into base units (int),
    rounding down to avoid over-sending. Accepts str/int/float/Decimal.
    Returns 0 on invalid/negative input.
    """
    if val is None:
        return 0
    try:
        dec = Decimal(str(val).strip())
    except (InvalidOperation, ValueError):
        try:
            dec = Decimal(float(val))
        except Exception:
            return 0
    try:
        d = int(decimals)
    except Exception:
        d = 0
    if d < 0:
        d = 0
    scale = Decimal(10) ** d
    base = (dec * scale).quantize(Decimal(1), rounding=ROUND_DOWN)
    if base < 0:
        return 0
    return int(base)


def poll_nexus_usdd_deposits():
    import json
    import subprocess

    # Use the configured treasury account name consistently
    treasury_addr = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None) or getattr(config, "NEXUS_USDD_ACCOUNT")

    # Query finance/transaction/account and explicitly select fields we need.
    # Include contracts.id and contracts.to so we can filter correctly and key per-contract.
    cmd = [
        config.NEXUS_CLI,
        "finance/transaction/account/"
        "txid,timestamp,confirmations,"
        "contracts.id,contracts.OP,contracts.from,contracts.to,contracts.amount,contracts.reference,contracts.ticker,contracts.token",
        f"address={treasury_addr}",
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            print("Error fetching USDD transactions:", (res.stderr or res.stdout).strip())
            return

        # Parse leniently to ignore trailing "[Completed in ...]" footer
        try:
            txs = nexus_client._parse_json_lenient(res.stdout)
        except Exception:
            txs = None
        if txs is None:
            print("Failed to parse Nexus CLI JSON output for finance/transaction/account")
            return

        if not isinstance(txs, list):
            txs = [txs]

        # Optional: read on-chain heartbeat waterline to skip very old items.
        wl_cutoff = 0
        if getattr(config, "HEARTBEAT_WATERLINE_ENABLED", False):
            try:
                from .main import read_heartbeat_waterlines
                # read_heartbeat_waterlines returns (lastPollSolanaUnix, lastPollNexusUnix)
                _, wl_nexus = read_heartbeat_waterlines()
                safety = int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0))
                wl_cutoff = int(max(0, int(wl_nexus) - safety))
            except Exception:
                wl_cutoff = 0

        for tx in txs:
            tx_id = (tx or {}).get("txid")
            if not tx_id:
                continue

            ts = int((tx.get("timestamp") or 0) or 0)
            if wl_cutoff and ts and ts < wl_cutoff:
                # Too old relative to heartbeat waterline
                continue

            confirmations = int(tx.get("confirmations") or 0)
            if confirmations <= 0:
                # Require at least 1 confirmation (tuneable if desired)
                continue

            contracts = tx.get("contracts") or []
            if not isinstance(contracts, list):
                continue

            for c in contracts:
                if not isinstance(c, dict):
                    continue

                # Per-contract processed key (idempotency)
                cid = c.get("id")
                processed_key = f"{tx_id}:{cid if cid is not None else 'x'}"
                if processed_key in state.processed_nexus_txs:
                    continue

                # Only treat as incoming deposit if destination is our treasury.
                if c.get("to") != treasury_addr:
                    continue

                # Amount in base units
                usdd_units = parse_amount_to_base_units(c.get("amount"), config.USDD_DECIMALS)
                if usdd_units <= 0:
                    state.processed_nexus_txs.add(processed_key)
                    continue

                # Optional tiny routing policy (unchanged)
                tiny_fee_units = parse_amount_to_base_units(getattr(config, "FLAT_FEE_USDD_UNITS", 0), config.USDD_DECIMALS)
                if usdd_units <= max(0, int(tiny_fee_units or 0)):
                    route_key = f"route_tiny_usdd:{processed_key}"
                    if state.should_attempt(route_key):
                        state.record_attempt(route_key)
                        if nexus_client.send_tiny_usdd_to_local(usdd_units, note=f"TINY_USDD:{tx_id}:{cid}"):
                            print(f"Routed tiny USDD {usdd_units} to local; processed ({processed_key})")
                            state.processed_nexus_txs.add(processed_key)
                            continue
                        else:
                            print(f"Tiny USDD routing failed; will retry ({processed_key})")
                            continue  # leave unprocessed to retry
                    else:
                        print(f"Skipping tiny USDD routing attempt (cooldown/max attempts) ({processed_key})")
                        continue  # leave unprocessed

                # Parse reference; accept "solana:<ADDR>" (case-insensitive). Numeric refs are invalid for swaps.
                ref_raw = c.get("reference", "")
                ref_str = str(ref_raw).strip() if ref_raw is not None else ""
                is_solana_ref = ref_str.lower().startswith("solana:")
                sol_addr = ref_str.split(":", 1)[1].strip() if is_solana_ref else ""

                # Sender for refunds (from field of the credit-to-treasury contract)
                sender_addr = c.get("from")

                # Compute optional refund fee (base units) for USDD refunds
                refund_usdd_fee_units = parse_amount_to_base_units(getattr(config, "REFUND_USDD_FEE_BASE_UNITS", 0), config.USDD_DECIMALS)
                refund_amount_units = max(0, usdd_units - int(refund_usdd_fee_units or 0))

                if not is_solana_ref:
                    # Missing/invalid reference -> refund USDD
                    if not sender_addr:
                        print(f"Invalid/missing reference and unknown sender; skipping ({processed_key})")
                        continue
                    reason = "Missing or invalid reference; expected 'solana:<address>'"
                    refund_key = f"refund_usdd:{processed_key}"
                    if state.should_attempt(refund_key):
                        state.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender_addr, refund_amount_units, reason):
                            print(f"Refunded USDD due to invalid/missing reference ({processed_key})")
                            state.processed_nexus_txs.add(processed_key)
                        else:
                            print(f"USDD refund failed ({processed_key})")
                    else:
                        print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                    continue

                # Validate Solana address format
                valid_sol = True
                try:
                    _ = PublicKey.from_string(sol_addr)
                except Exception:
                    valid_sol = False

                if not valid_sol:
                    # Refund USDD due to invalid Solana address
                    if not sender_addr:
                        print(f"Cannot determine sender Nexus address; skipping refund ({processed_key})")
                        continue
                    reason = f"Invalid Solana address: {sol_addr}"
                    refund_key = f"refund_usdd:{processed_key}"
                    if state.should_attempt(refund_key):
                        state.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender_addr, refund_amount_units, reason):
                            print(f"Refunded USDD due to invalid Solana address ({processed_key})")
                            state.processed_nexus_txs.add(processed_key)
                        else:
                            print(f"USDD refund failed ({processed_key})")
                    else:
                        print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                    continue

                # Convert USDD base units to USDC base units if decimals differ
                usdc_units = usdd_units
                if config.USDD_DECIMALS != config.USDC_DECIMALS:
                    # Re-scale amounts between token decimals
                    pow_diff = config.USDC_DECIMALS - config.USDD_DECIMALS
                    if pow_diff > 0:
                        usdc_units = usdd_units * (10 ** pow_diff)
                    else:
                        usdc_units = usdd_units // (10 ** (-pow_diff))

                # Apply optional USDD->USDC fee (bps)
                fee_bps = int(getattr(config, "FEE_BPS_USDD_TO_USDC", 0))
                fee_usdc = (usdc_units * max(0, fee_bps)) // 10_000
                net_usdc = max(0, usdc_units - fee_usdc)

                if net_usdc <= 0:
                    if not sender_addr:
                        print(f"Cannot determine sender Nexus address to refund zero-net; skipping ({processed_key})")
                        continue
                    reason = "Net USDC after fee is zero"
                    refund_key = f"refund_usdd:{processed_key}"
                    if state.should_attempt(refund_key):
                        state.record_attempt(refund_key)
                        if nexus_client.refund_usdd(sender_addr, refund_amount_units, reason):
                            print(f"Refunded USDD due to zero-net after fee ({processed_key})")
                            state.processed_nexus_txs.add(processed_key)
                        else:
                            print(f"USDD refund failed ({processed_key})")
                    else:
                        print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                    continue

                # Ensure recipient ATA exists and send USDC
                send_key = f"send_usdc:{processed_key}"
                if state.should_attempt(send_key):
                    state.record_attempt(send_key)
                    memo = f"NEXUS_TX:{tx_id}:{cid}"
                    # Idempotency shortcut
                    if solana_client.was_usdc_sent_for_nexus_tx(processed_key, sol_addr):
                        print(f"Detected prior USDC send for {processed_key}; marking processed")
                        state.processed_nexus_txs.add(processed_key)
                        continue

                    # Ensure ATA and send
                    if solana_client.ensure_send_usdc_ata(sol_addr, net_usdc, memo=memo):
                        print(f"Sent {net_usdc} USDC units to {sol_addr} ({processed_key})")
                        if fee_usdc > 0:
                            fees.add_usdc_fee(fee_usdc)
                        state.processed_nexus_txs.add(processed_key)
                    else:
                        print(f"USDC send failed ({processed_key})")
                        # Optional: after several attempts, refund USDD
                        attempts = int((state.attempt_state.get(send_key) or {}).get("attempts", 0))
                        if attempts >= 2 and sender_addr:
                            reason = f"USDC send failed after retries to {sol_addr}"
                            refund_key = f"refund_usdd:{processed_key}"
                            if state.should_attempt(refund_key):
                                state.record_attempt(refund_key)
                                if nexus_client.refund_usdd(sender_addr, refund_amount_units, reason):
                                    print(f"Refunded USDD after repeated send failures ({processed_key})")
                                    state.processed_nexus_txs.add(processed_key)
                                else:
                                    print(f"USDD refund failed ({processed_key})")
                            else:
                                print(f"Skipping refund attempt (cooldown/max attempts) ({processed_key})")
                else:
                    print(f"Skipping send attempt (cooldown/max attempts) ({processed_key})")

    except Exception as e:
        print(f"Error polling USDD deposits: {e}")
    finally:
        state.save_state()
