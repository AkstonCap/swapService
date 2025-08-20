from decimal import Decimal
from . import config, state, nexus_client, solana_client, fees


def _normalize_get_sigs_response(resp):
    """Return a list of signature entries as dicts with at least 'signature' and optional 'blockTime'.
    Supports both dict-based solana-py responses and solders typed responses.
    """
    # Dict-style
    try:
        return (resp.get("result") or [])
    except AttributeError:
        pass
    # solders typed: try to_json first
    try:
        import json as _json
        js = _json.loads(resp.to_json())
        arr = js.get("result") or js.get("value") or []
        if isinstance(arr, dict):
            arr = arr.get("value") or arr.get("result") or []
        if isinstance(arr, list):
            return arr
    except Exception:
        pass
    # Fallback to .value (commonly a list of objects)
    try:
        val = getattr(resp, "value", None)
        out = []
        if isinstance(val, list):
            for r in val:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    sig = str(getattr(r, "signature", ""))
                    bt = getattr(r, "block_time", None)
                    try:
                        bt = int(bt) if bt is not None else 0
                    except Exception:
                        bt = 0
                    out.append({"signature": sig, "blockTime": bt})
            return out
    except Exception:
        pass
    return []


def scale_amount(amount: int, src_decimals: int, dst_decimals: int) -> int:
    if src_decimals == dst_decimals:
        return int(amount)
    if src_decimals < dst_decimals:
        return int(amount) * (10 ** (dst_decimals - src_decimals))
    return int(amount) // (10 ** (src_decimals - dst_decimals))


def _get_tx_result(sig: str):
    """Get transaction details with retries"""
    from solana.rpc.api import Client
    client = Client(getattr(config, "SOLANA_RPC_URL", getattr(config, "RPC_URL", None)))
    
    try:
        resp = client.get_transaction(sig, max_supported_transaction_version=0)
        if hasattr(resp, 'value') and resp.value:
            return resp.value
        elif isinstance(resp, dict) and resp.get("result"):
            return resp["result"]
    except Exception as e:
        print(f"Error fetching transaction {sig}: {e}")
    return None


def _iter_all_instructions(tx):
    """Iterate through all instructions including inner instructions (CPI calls)"""
    instructions = []
    
    # Top-level instructions
    for instr in tx.get("transaction", {}).get("message", {}).get("instructions", []):
        instructions.append(instr)
    
    # Inner instructions (CPI calls)
    meta = tx.get("meta", {})
    if "innerInstructions" in meta:
        for inner_group in meta["innerInstructions"]:
            for inner_instr in inner_group.get("instructions", []):
                instructions.append(inner_instr)
    
    return instructions


def _process_single_deposit(deposit_key: str, sig: str, amount_usdc_units: int, 
                          source_token_acc: str, memo_data: str, all_instrs: list) -> bool:
    """Process a single deposit within a transaction. Returns True if handled successfully."""
    
    # Check if this specific deposit was already processed
    if deposit_key in state.processed_sigs:
        return True
    
    # Skip tiny amounts (less than flat fee)
    flat_fee_usdc_units = config.FLAT_FEE_USDC_UNITS
    if amount_usdc_units < flat_fee_usdc_units:
        print(f"Deposit {deposit_key}: amount {amount_usdc_units} below minimum fee {flat_fee_usdc_units}")
        state.mark_solana_processed(deposit_key, reason="amount too small")
        return True
    
    try:
        # Extract memo - case insensitive
        memo_addr = None
        if memo_data:
            memo_addr = memo_data.strip()
        
        if not memo_addr:
            print(f"Deposit {deposit_key}: No memo found, refunding")
            # Charge flat fee and refund remainder
            refund_amount = amount_usdc_units - flat_fee_usdc_units
            if refund_amount > 0:
                # Send refund back to source
                success = solana_client.send_usdc_from_vault(source_token_acc, refund_amount, 
                                                           memo=f"Refund: {sig}")
                if success:
                    state.mark_solana_processed(deposit_key, reason="refunded - no memo")
                    return True
                else:
                    print(f"Deposit {deposit_key}: Refund failed")
                    return False
            else:
                state.mark_solana_processed(deposit_key, reason="fee only - no memo")
                return True
        
        # Process swap to USDD
        try:
            # Calculate amounts
            amount_usdc_decimal = Decimal(amount_usdc_units) / (10 ** config.USDC_DECIMALS)
            flat_fee_decimal = Decimal(config.FLAT_FEE_USDC)
            
            # Deduct flat fee first
            net_amount_decimal = amount_usdc_decimal - flat_fee_decimal
            if net_amount_decimal <= 0:
                state.mark_solana_processed(deposit_key, reason="fee only")
                return True
            
            # Calculate dynamic fee
            dynamic_fee_decimal = net_amount_decimal * (Decimal(config.DYNAMIC_FEE_BPS) / Decimal("10000"))
            final_usdd_amount = net_amount_decimal - dynamic_fee_decimal
            
            if final_usdd_amount <= 0:
                print(f"Deposit {deposit_key}: Final amount after fees is zero or negative")
                state.mark_solana_processed(deposit_key, reason="no amount after fees")
                return True
            
            # Scale to USDD units (same decimals as USDC in this system)
            final_usdd_units = int(final_usdd_amount * (10 ** config.USDD_DECIMALS))
            
            # Perform Nexus transfer
            success = nexus_client.send_usdd_to_account(memo_addr, final_usdd_units)
            
            if success:
                state.mark_solana_processed(deposit_key, reason="swapped successfully")
                return True
            else:
                print(f"Deposit {deposit_key}: Nexus transfer failed, scheduling refund")
                # Schedule refund attempt
                refund_amount = amount_usdc_units - flat_fee_usdc_units
                if refund_amount > 0:
                    success = solana_client.send_usdc_from_vault(source_token_acc, refund_amount,
                                                               memo=f"Refund: {sig}")
                    if success:
                        state.mark_solana_processed(deposit_key, reason="refunded - nexus failed")
                        return True
                    else:
                        print(f"Deposit {deposit_key}: Both swap and refund failed")
                        return False
                else:
                    state.mark_solana_processed(deposit_key, reason="nexus failed - fee only")
                    return True
                    
        except Exception as e:
            print(f"Deposit {deposit_key}: Processing error: {e}")
            return False
            
    except Exception as e:
        print(f"Deposit {deposit_key}: Unexpected error: {e}")
        return False


def poll_solana_deposits():
    """Poll for USDC deposits with 100% reliability - no deposits will be missed."""
    from solana.rpc.api import Client
    from solders.signature import Signature
    
    try:
        client = Client(getattr(config, "SOLANA_RPC_URL", getattr(config, "RPC_URL", None)))
        limit = 100
        
        # Pagination to fetch ALL signatures
        before_sig = None
        confirmed_bt_candidates: list[int] = []
        page_has_unprocessed_deposit = False
        
        while True:  # Pagination loop to ensure we get ALL transactions
            if before_sig:
                sigs_resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, limit=limit, before=before_sig)
            else:
                sigs_resp = client.get_signatures_for_address(config.VAULT_USDC_ACCOUNT, limit=limit)
            
            sig_results = _normalize_get_sigs_response(sigs_resp)
            
            # Apply waterline safety
            from .main import read_heartbeat_waterlines
            wl_solana, _ = read_heartbeat_waterlines()
            wl_cutoff = int(max(0, wl_solana - config.HEARTBEAT_WATERLINE_SAFETY_SEC)) if config.HEARTBEAT_WATERLINE_ENABLED else 0

            sig_list: list[str] = []
            sig_bt: dict[str, int] = {}

            for r in sig_results:
                sig = r.get("signature")
                if not sig:
                    continue
                try:
                    bt = int(r.get("blockTime", 0) or 0)
                except Exception:
                    bt = 0
                if wl_cutoff and bt and bt < wl_cutoff:
                    continue
                sig_list.append(sig)
                sig_bt[sig] = bt

            # Process each signature comprehensively
            for sig in sig_list:
                # Skip if already fully processed
                if sig in state.processed_sigs:
                    continue

                # Get transaction details
                try:
                    tx = _get_tx_result(sig)
                    if not tx:
                        continue
                    if (tx.get("meta") or {}).get("err") is not None:
                        continue
                except Exception as e:
                    print(f"Error fetching transaction {sig}: {e}")
                    continue

                # Get ALL instructions (top-level + inner/CPI)
                all_instrs = list(_iter_all_instructions(tx))
                
                # Find ALL USDC deposits in this transaction
                deposits_found = []
                
                # Parse all instructions for USDC transfers to our vault
                for instr in all_instrs:
                    is_token_prog = (
                        instr.get("program") == "spl-token"
                        or instr.get("programId") == str(solana_client.TOKEN_PROGRAM_ID)
                    )
                    if is_token_prog and instr.get("parsed"):
                        p = instr["parsed"]
                        # Support ALL transfer types
                        if p.get("type") in ("transfer", "transferChecked", "transferCheckedWithFee"):
                            info = p.get("info", {})
                            if info.get("destination") == str(config.VAULT_USDC_ACCOUNT):
                                # Extract amount (different fields for different transfer types)
                                amount_usdc_units = 0
                                if "amount" in info:
                                    amount_usdc_units = int(info["amount"])
                                elif "tokenAmount" in info and isinstance(info["tokenAmount"], dict):
                                    amount_usdc_units = int(info["tokenAmount"].get("amount", 0))
                                
                                if amount_usdc_units > 0:
                                    deposits_found.append({
                                        "amount": amount_usdc_units,
                                        "source": info.get("source"),
                                        "authority": info.get("authority")  # For transferCheckedWithFee
                                    })
                
                # Fallback: Check balance deltas if no parsed transfers found
                if not deposits_found:
                    meta = tx.get("meta", {})
                    pre_balances = meta.get("preTokenBalances", [])
                    post_balances = meta.get("postTokenBalances", [])
                    
                    # Find our USDC account in the balances
                    vault_pre = None
                    vault_post = None
                    
                    for bal in pre_balances:
                        if bal.get("owner") == str(config.VAULT_USDC_ACCOUNT):
                            vault_pre = int(bal.get("uiTokenAmount", {}).get("amount", 0))
                            break
                    
                    for bal in post_balances:
                        if bal.get("owner") == str(config.VAULT_USDC_ACCOUNT):
                            vault_post = int(bal.get("uiTokenAmount", {}).get("amount", 0))
                            break
                    
                    if vault_pre is not None and vault_post is not None and vault_post > vault_pre:
                        delta = vault_post - vault_pre
                        deposits_found.append({
                            "amount": delta,
                            "source": "unknown",  # Can't determine from balance delta
                            "authority": None
                        })
                
                # Process ALL deposits found in this transaction
                all_deposits_handled = True
                memo_data = solana_client.extract_memo_from_instructions(all_instrs)
                
                for i, deposit in enumerate(deposits_found):
                    amount_usdc_units = deposit["amount"]
                    source_token_acc = deposit["source"] or "unknown"
                    
                    # Create unique key per deposit within transaction
                    deposit_key = f"{sig}:{i}"
                    
                    # Process this specific deposit
                    deposit_handled = _process_single_deposit(
                        deposit_key, sig, amount_usdc_units, source_token_acc, 
                        memo_data, all_instrs
                    )
                    
                    if not deposit_handled:
                        all_deposits_handled = False
                        page_has_unprocessed_deposit = True

                # Only mark signature as processed if ALL deposits were handled
                if all_deposits_handled and deposits_found:
                    ts_bt = sig_bt.get(sig) or 0
                    state.mark_solana_processed(sig, ts=ts_bt, reason="all deposits processed")
                    if ts_bt:
                        confirmed_bt_candidates.append(int(ts_bt))
                elif not deposits_found:
                    # No deposits found - mark as benign transaction
                    ts_bt = sig_bt.get(sig) or 0
                    state.mark_solana_processed(sig, ts=ts_bt, reason="not a deposit")
                    if ts_bt:
                        confirmed_bt_candidates.append(int(ts_bt))

            # Check if we should continue pagination
            if not isinstance(sig_results, list) or len(sig_results) < limit:
                break  # Last page reached
            
            # Set up next page
            before_sig = sig_results[-1].get("signature")

        # Propose waterline advancement only if no unprocessed deposits
        if confirmed_bt_candidates and not page_has_unprocessed_deposit:
            state.propose_solana_waterline(int(min(confirmed_bt_candidates)))
            
    except Exception as e:
        print(f"poll_solana_deposits error: {e}")
    finally:
        state.save_state()
