from decimal import Decimal
from solana.publickey import PublicKey
from . import config, state, solana_client


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
                        if solana_client.ensure_send_usdc(sol_addr, usdc_units):
                            print(f"Sent {usdc_units} USDC units to {sol_addr}")
                        else:
                            print("USDC send failed")
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
