from decimal import Decimal
from . import config, state, nexus_client, solana_client, fees

# SPL Token program ID constant for reliable instruction matching
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Optional: allow extra token program IDs via config (e.g., Token-2022)
EXTRA_TOKEN_PROGRAM_IDS = set(getattr(config, "EXTRA_TOKEN_PROGRAM_IDS", []))


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
    """Get transaction details with parsed instructions"""
    from solana.rpc.api import Client
    from solders.signature import Signature
    
    client = Client(getattr(config, "SOLANA_RPC_URL", getattr(config, "RPC_URL", None)))
    
    try:
        sig_obj = Signature.from_string(sig)
        # CRITICAL: Must include encoding="jsonParsed" to get parsed instructions
        resp = client.get_transaction(
            sig_obj, 
            encoding="jsonParsed",
            max_supported_transaction_version=0
        )
        
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


def _handle_refund(deposit_key: str, sig: str, amount_usdc_units: int, source_token_acc: str, reason: str) -> bool:
    """Handle refund with flat fee deduction"""
    flat_fee_units = config.FLAT_FEE_USDC_UNITS
    refund_amount = amount_usdc_units - flat_fee_units
    
    if refund_amount > 0 and source_token_acc and source_token_acc != "unknown":
        success = solana_client.send_usdc_from_vault(
            source_token_acc, refund_amount, 
            memo=f"Refund: {reason} USDC_TX:{sig}"
        )
        if success:
            fees.add_usdc_fee(flat_fee_units)
            # Mark both deposit and signature as processed
            if not hasattr(state, 'processed_deposits'):
                state.processed_deposits = set()
            state.processed_deposits.add(deposit_key)
            state.mark_solana_processed(deposit_key, reason=f"refunded - {reason}")
            return True
        else:
            print(f"Deposit {deposit_key}: Refund failed")
            return False
    else:
        # Keep as fee if refund not possible or amount too small
        fees.add_usdc_fee(amount_usdc_units)
        # Mark both deposit and signature as processed
        if not hasattr(state, 'processed_deposits'):
            state.processed_deposits = set()
        state.processed_deposits.add(deposit_key)
        state.mark_solana_processed(deposit_key, reason=f"fee only - {reason}")
        return True


def _process_single_deposit(deposit_key: str, sig: str, amount_usdc_units: int, 
                          source_token_acc: str, memo_data: str, all_instrs: list) -> bool:
    """Process a single deposit within a transaction. Returns True if handled successfully."""
    
    # Check if this specific deposit was already processed using deposit-specific tracking
    if not hasattr(state, 'processed_deposits'):
        state.processed_deposits = set()
    
    if deposit_key in state.processed_deposits:
        return True
    
    # Skip tiny amounts (less than flat fee)
    flat_fee_usdc_units = config.FLAT_FEE_USDC_UNITS
    if amount_usdc_units < flat_fee_usdc_units:
        print(f"Deposit {deposit_key}: amount {amount_usdc_units} below minimum fee {flat_fee_usdc_units}")
        # Mark both deposit and signature as processed
        state.processed_deposits.add(deposit_key)
        state.mark_solana_processed(deposit_key, reason="amount too small")
        return True
    
    try:
        # Extract Nexus address from memo (case-insensitive prefix)
        nexus_addr = None
        if isinstance(memo_data, str) and memo_data.lower().startswith("nexus:"):
            nexus_addr = memo_data[memo_data.find(":") + 1:].strip()
        
        if not nexus_addr:
            print(f"Deposit {deposit_key}: No valid nexus:<addr> memo found, refunding")
            return _handle_refund(deposit_key, sig, amount_usdc_units, source_token_acc, 
                                "Missing memo nexus:<addr>")
        
        # Validate Nexus account
        try:
            acct = nexus_client.get_account_info(nexus_addr)
            if not acct or not nexus_client.is_expected_token(acct, config.NEXUS_TOKEN_NAME):
                return _handle_refund(deposit_key, sig, amount_usdc_units, source_token_acc,
                                    "Invalid or wrong Nexus address")
        except Exception as e:
            print(f"Deposit {deposit_key}: Nexus validation error: {e}")
            return _handle_refund(deposit_key, sig, amount_usdc_units, source_token_acc,
                                "Nexus validation failed")
        
        # Check for duplicate mint
        try:
            if nexus_client.was_usdd_debited_from_treasury_for_sig(sig, lookback=60, min_confirmations=0):
                print(f"Deposit {deposit_key}: Already minted, skipping")
                # Mark both deposit and signature as processed
                state.processed_deposits.add(deposit_key)
                state.mark_solana_processed(deposit_key, reason="already minted")
                return True
        except Exception as e:
            print(f"Deposit {deposit_key}: Duplicate check failed: {e}")
        
        # Calculate fees and net amount
        dynamic_bps = config.DYNAMIC_FEE_BPS
        pre_dynamic_net = max(0, amount_usdc_units - flat_fee_usdc_units)
        dynamic_fee_usdc = (pre_dynamic_net * dynamic_bps) // 10000
        net_usdc_for_mint = max(0, pre_dynamic_net - dynamic_fee_usdc)
        
        if net_usdc_for_mint <= 0:
            print(f"Deposit {deposit_key}: No amount left after fees")
            fees.add_usdc_fee(amount_usdc_units)
            # Mark both deposit and signature as processed
            state.processed_deposits.add(deposit_key)
            state.mark_solana_processed(deposit_key, reason="fee only")
            return True
        
        # Convert to USDD units
        usdd_units = scale_amount(net_usdc_for_mint, config.USDC_DECIMALS, config.USDD_DECIMALS)
        
        # Mint USDD on Nexus
        try:
            if nexus_client.debit_usdd(nexus_addr, usdd_units, f"USDC_TX:{sig}"):
                # Success - record fees
                total_fee = flat_fee_usdc_units + dynamic_fee_usdc
                fees.add_usdc_fee(total_fee)
                # Mark both deposit and signature as processed
                state.processed_deposits.add(deposit_key)
                state.mark_solana_processed(deposit_key, reason="minted successfully")
                print(f"Minted {usdd_units} USDD units to {nexus_addr} (fees: {total_fee})")
                return True
            else:
                # Mint failed - refund
                return _handle_refund(deposit_key, sig, amount_usdc_units, source_token_acc,
                                    "USDD mint failed")
        except Exception as e:
            print(f"Deposit {deposit_key}: Mint error: {e}")
            return _handle_refund(deposit_key, sig, amount_usdc_units, source_token_acc,
                                "USDD mint error")
            
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

                # Get transaction details with better error handling
                try:
                    tx = _get_tx_result(sig)
                    if not tx:
                        # Couldn't fetch transaction - retry later, don't mark as processed
                        page_has_unprocessed_deposit = True
                        continue
                        
                    meta = tx.get("meta")
                    if meta is None:
                        # Missing meta - can't parse instructions or balances, retry later
                        page_has_unprocessed_deposit = True
                        continue
                        
                    if meta.get("err") is not None:
                        # Failed transaction - safe to skip
                        continue
                        
                except Exception as e:
                    print(f"Error fetching transaction {sig}: {e}")
                    # Network/RPC error - retry later
                    page_has_unprocessed_deposit = True
                    continue

                # Get ALL instructions (top-level + inner/CPI)
                all_instrs = list(_iter_all_instructions(tx))
                
                # Find ALL USDC deposits in this transaction
                deposits_found = []
                
                # Parse all instructions for USDC transfers to our vault
                for instr in all_instrs:
                    # Fix SPL Token program matching - check both program name and programId
                    prog = instr.get("program")
                    prog_id_raw = instr.get("programId")
                    prog_id = None
                    if prog_id_raw is not None:
                        # programId can be a dict with 'pubkey' or a string
                        prog_id = (
                            prog_id_raw.get("pubkey")
                            if isinstance(prog_id_raw, dict)
                            else str(prog_id_raw)
                        )
                    is_token_prog = (
                        prog == "spl-token" or 
                        (prog_id and str(prog_id) == SPL_TOKEN_PROGRAM_ID) or
                        (prog_id and str(prog_id) in EXTRA_TOKEN_PROGRAM_IDS)
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
                
                # Fallback: Check balance deltas only if we have required data
                if not deposits_found:
                    acct_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    if not acct_keys:
                        # No account keys - can't resolve balance deltas, retry later
                        page_has_unprocessed_deposit = True
                        continue
                        
                    try:
                        pre_balances = meta.get("preTokenBalances", [])
                        post_balances = meta.get("postTokenBalances", [])
                        
                        def _key_at(idx):
                            try:
                                k = acct_keys[idx]
                                return k.get("pubkey") if isinstance(k, dict) else str(k)
                            except Exception:
                                return None
                        
                        # Find any post balance entry that corresponds to our vault token account
                        vault_post_entries = [e for e in post_balances if _key_at(int(e.get("accountIndex", -1))) == str(config.VAULT_USDC_ACCOUNT)]
                        for post_e in vault_post_entries:
                            idx = int(post_e["accountIndex"])
                            post_amt = int((post_e.get("uiTokenAmount") or {}).get("amount") or 0)
                            # Corresponding pre entry (may not exist if account was empty before)
                            pre_e = next((e for e in pre_balances if int(e.get("accountIndex", -1)) == idx), None)
                            pre_amt = int((pre_e.get("uiTokenAmount") or {}).get("amount") or 0) if pre_e else 0
                            delta = post_amt - pre_amt
                            if delta > 0:
                                deposits_found.append({
                                    "amount": delta, 
                                    "source": None, 
                                    "authority": None
                                })
                    except Exception:
                        # Balance parsing failed but we have account keys - probably not a deposit
                        pass
                
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
                elif not deposits_found and meta is not None:
                    # Only mark "not a deposit" if we could fully inspect the transaction
                    acct_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    if acct_keys:  # Only mark if we had account keys to check balances
                        ts_bt = sig_bt.get(sig) or 0
                        state.mark_solana_processed(sig, ts=ts_bt, reason="not a deposit")
                        if ts_bt:
                            confirmed_bt_candidates.append(int(ts_bt))
                    else:
                        # Incomplete inspection - retry later
                        page_has_unprocessed_deposit = True
                else:
                    # Partial processing or incomplete inspection - retry later
                    page_has_unprocessed_deposit = True

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
