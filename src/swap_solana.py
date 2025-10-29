from decimal import Decimal
from . import config, state_db, nexus_client, solana_client, fees

# Lightweight structured logging for deposit lifecycle only
def _log(event: str, **fields):
    parts = [f"{event}"]
    for k, v in fields.items():
        if v is not None:
            parts.append(f"{k}={v}")
    print(" ".join(parts))


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
        import time as _time
        heartbeat = nexus_client.get_heartbeat_asset()
        if not heartbeat:
            return
        wline_sol = heartbeat.get("last_safe_timestamp_solana")
        if wline_sol is None:
            return
        
        poll_start = _time.time()
        current_bal = solana_client.get_token_account_balance(config.VAULT_USDC_ACCOUNT)
        last_bal = state_db.load_last_vault_balance()
        delta = current_bal - last_bal
        
        # Pre-balance micro batch skip
        if delta < getattr(config, "MIN_DEPOSIT_USDC_UNITS", 0):
            # Advance waterline opportunistically using recent signatures
            state_db.propose_solana_waterline(int(poll_start))                    
            state_db.save_last_vault_balance(current_bal)
            nexus_client.update_heartbeat_asset(int(poll_start), None, int(poll_start))
            _log("USDC_MICRO_BATCH_SKIPPED", delta_units=delta, threshold=getattr(config, 'MIN_DEPOSIT_USDC_UNITS', 0))
            # Still process existing unprocessed entries before returning
            return
        
        client = Client(getattr(config, "RPC_URL", None))
       
        #usdc_deposits = solana_client.fetch_filtered_token_account_transaction_history(
        #    config.VAULT_USDC_ACCOUNT, wline_sol, config.MIN_DEPOSIT_USDC_UNITS, 10.0
        #    )
        
        # Prefer Helius enriched RPC to batch-fetch txs + memos in 1–2 calls; fallback to existing scanner.
        usdc_deposits = solana_client.fetch_incoming_usdc_deposits_via_helius(
            str(config.VAULT_USDC_ACCOUNT),
            since_ts=int(wline_sol),
            min_units=getattr(config, "MIN_DEPOSIT_USDC_UNITS", 0),
            limit=getattr(config, "POLL_HELIUS_LIMIT", 200),
        )
        
        unprocessed_deposits_added = solana_client.process_filtered_deposits(usdc_deposits, True)
        print(f"New deposits fetched and added for processing: {unprocessed_deposits_added}\n")

        [proc_count_swap, proc_count_refund, proc_count_quar, proc_count_mic] = solana_client.process_unprocessed_usdc_deposits(1000, 8.0)
        print(f"Debited, awaiting confirmation: {proc_count_swap}, \nTo be refunded: {proc_count_refund}, \nTo be quarantined: {proc_count_quar}, \nMicro-sigs found: {proc_count_mic}\n")

        refunds = solana_client.process_usdc_deposits_refunding(1000, 8.0)
        print(f"Processed refunds, awaiting confirmation: {refunds}\n") if refunds > 0 else None

        quarantines = solana_client.process_usdc_deposits_quarantine(1000, 8.0)
        print(f"Processed quarantines: {quarantines}\n") if quarantines > 0 else None

        confirmed_ref = solana_client.check_sig_confirmations(100, 8.0)
        print(f"Confirmed refunds: {confirmed_ref}\n") if confirmed_ref > 0 else None

        confirmed_debits = nexus_client.check_unconfirmed_debits(10, 8.0)
        print(f"Confirmed debits: {confirmed_debits}\n") if confirmed_debits > 0 else None

        new_waterline = solana_client.check_timestamp_unpr_sigs()
        if new_waterline and new_waterline > wline_sol:
            _log("WATERLINE_ADVANCED", old_ts=wline_sol, new_ts=new_waterline, reason="signatures processed")
            nexus_client.update_heartbeat_asset(new_waterline, None, poll_start)

    except Exception:
        # Suppress top-level poll errors to keep terminal focused on deposit lifecycle
        pass
    
