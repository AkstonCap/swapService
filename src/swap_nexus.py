from decimal import Decimal, ROUND_DOWN, InvalidOperation
from . import config, state, solana_client, nexus_client, fees


def _parse_decimal_amount(val) -> Decimal:
    """Parse a Nexus token amount (string/number) into Decimal token units."""
    if val is None:
        return Decimal(0)
    try:
        return Decimal(str(val).strip())
    except (InvalidOperation, ValueError):
        try:
            return Decimal(float(val))
        except Exception:
            return Decimal(0)


def _format_token_amount(amount: Decimal, decimals: int) -> str:
    """Format a Decimal amount with given token decimals, rounded down, as plain string."""
    if amount < 0:
        amount = Decimal(0)
    q = amount.quantize(Decimal(10) ** -int(decimals), rounding=ROUND_DOWN)
    return format(q, 'f')

def _apply_congestion_fee(amount_dec: Decimal) -> Decimal:
    """Subtract a fixed Nexus congestion fee (configured in USDD token units)."""
    try:
        fee_dec = _parse_decimal_amount(getattr(config, "NEXUS_CONGESTION_FEE_USDD", "0"))
    except Exception:
        fee_dec = Decimal(0)
    out = amount_dec - fee_dec
    return out if out > 0 else Decimal(0)

def poll_nexus_usdd_deposits():
    """USDD->USDC pipeline per spec:
    1) Detect USDD credits to treasury; append new entries to unprocessed_txids.json
    2) For each unprocessed entry, resolve receival_account via assets WHERE txid_toService AND owner
    3) If valid USDC token account, mark ready
    4) Send USDC minus fees from vault with memo containing a unique integer; record sig and reference
    5) Confirm sig>2 and move to processed_txids.json with reference
    6) Move waterline to min ts of unprocessed - buffer
    Refund USDD if invalid receival_account, send fails, or not confirmed within timeout.
    """
    import subprocess, time
    from solana.rpc.api import Client as SolClient

    unprocessed_path = "unprocessed_txids.json"
    processed_path = "processed_txids.json"

    treasury_addr = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
    cmd = [
        config.NEXUS_CLI,
        "finance/transactions/token/"
        "txid,timestamp,confirmations,contracts.id,contracts.OP,contracts.from,contracts.to,contracts.amount",
        f"name={treasury_addr}",
        "sort=timestamp",
        "order=desc",
        "limit=100",
    ]

    # Load current sets
    unprocessed = state.read_jsonl(unprocessed_path)
    processed = state.read_jsonl(processed_path)
    processed_txids = {r.get("txid") for r in processed}
    unprocessed_txids = {r.get("txid") for r in unprocessed}

    wl_cutoff = 0
    if getattr(config, "HEARTBEAT_WATERLINE_ENABLED", False):
        try:
            from .main import read_heartbeat_waterlines
            _, wl_nexus = read_heartbeat_waterlines()
            wl_cutoff = max(0, int(wl_nexus) - int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 0)))
        except Exception:
            wl_cutoff = 0

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            print("Error fetching USDD transactions:", (res.stderr or res.stdout).strip())
            return
        txs = nexus_client._parse_json_lenient(res.stdout)
        if not isinstance(txs, list):
            txs = [txs]

        # Step 1+2: collect new credits to treasury
        page_ts_candidates: list[int] = []
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            txid = tx.get("txid")
            ts = int(tx.get("timestamp") or 0)
            if ts:
                page_ts_candidates.append(ts)
            if wl_cutoff and ts and ts < wl_cutoff:
                continue
            if not txid or txid in processed_txids:
                continue
            contracts = tx.get("contracts") or []
            for c in contracts:
                if not isinstance(c, dict):
                    continue
                if str(c.get("OP") or "").upper() != "CREDIT":
                    continue
                # Validate credit to treasury
                to = c.get("to")
                def _addr(obj) -> str:
                    if isinstance(obj, dict):
                        a = obj.get("address") or obj.get("name")
                        return str(a) if a else ""
                    if isinstance(obj, str):
                        return obj
                    return ""
                if _addr(to) != treasury_addr:
                    continue
                # Extract sender and owner
                sender = _addr(c.get("from"))
                amount_dec = _parse_decimal_amount(c.get("amount"))
                if amount_dec <= 0:
                    continue
                # Fees-as-fees threshold in USDD token units
                flat_usdd_dec = _parse_decimal_amount(getattr(config, "FLAT_FEE_USDD", "0.1"))
                dyn_bps = int(getattr(config, "DYNAMIC_FEE_BPS", 0))
                dyn_fee_dec = (amount_dec * Decimal(max(0, dyn_bps))) / Decimal(10000)
                if amount_dec <= (flat_usdd_dec + dyn_fee_dec):
                    row = {
                        "txid": txid,
                        "ts": ts,
                        "from": sender,
                        "owner": (nexus_client.get_account_info(sender) or {}).get("owner"),
                        "amount_usdd": str(amount_dec),
                        "comment": "processed as fees",
                    }
                    state.append_jsonl(processed_path, row)
                    processed_txids.add(txid)
                    continue
                if txid in unprocessed_txids:
                    continue
                owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                row = {
                    "txid": txid,
                    "ts": ts,
                    "from": sender,
                    "owner": owner,
                    "amount_usdd": str(amount_dec),
                }
                state.append_jsonl(unprocessed_path, row)
                unprocessed.append(row)
                unprocessed_txids.add(txid)

        # Step 3: resolve receival_account for unprocessed
        for r in list(unprocessed):
            if r.get("receival_account") or r.get("comment") == "ready for processing":
                continue
            txid = r.get("txid")
            owner = r.get("owner")
            asset = nexus_client.find_asset_receival_account_by_txid_and_owner(txid, owner)
            recv = (asset or {}).get("receival_account")
            if recv and solana_client.is_valid_usdc_token_account(recv):
                def _pred(x):
                    return x.get("txid") == txid
                def _upd(x):
                    x["receival_account"] = recv
                    x["comment"] = "ready for processing"
                    return x
                state.update_jsonl_row(unprocessed_path, _pred, _upd)
                r["receival_account"] = recv
                r["comment"] = "ready for processing"
            elif recv:
                # Invalid token account -> refund immediately
                amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                sender = r.get("from")
                if sender and nexus_client.refund_usdd(sender, amt_str, "invalid receival_account"):
                    row = dict(r)
                    row["comment"] = "refunded"
                    state.append_jsonl(processed_path, row)
                    # remove from unprocessed
                    unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                    state.write_jsonl(unprocessed_path, unprocessed)

        # Step 4: send USDC for ready entries
        for r in list(unprocessed):
            if r.get("comment") != "ready for processing" or not r.get("receival_account"):
                continue
            txid = r.get("txid")
            amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
            usdc_units = int((amt_dec * (Decimal(10) ** config.USDC_DECIMALS)).quantize(Decimal(1), rounding=ROUND_DOWN))
            flat_fee = max(0, int(getattr(config, "FLAT_FEE_USDC_UNITS", 0)))
            bps = max(0, int(getattr(config, "DYNAMIC_FEE_BPS", 0)))
            pre_dyn = max(0, usdc_units - flat_fee)
            dyn_fee = (pre_dyn * bps) // 10000
            net_usdc = max(0, pre_dyn - dyn_fee)
            if net_usdc <= 0:
                row = dict(r)
                row["comment"] = "processed as fees"
                state.append_jsonl(processed_path, row)
                unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                state.write_jsonl(unprocessed_path, unprocessed)
                continue
            ref = state.next_reference()
            memo = str(ref)
            ok, sig = solana_client.send_usdc_to_token_account_with_sig(r["receival_account"], net_usdc, memo)
            if ok and sig:
                if (flat_fee + dyn_fee) > 0:
                    fees.add_usdc_fee(flat_fee + dyn_fee)
                def _pred(x):
                    return x.get("txid") == txid
                def _upd(x):
                    x["sig"] = sig
                    x["reference"] = ref
                    x["comment"] = "sig created, awaiting confirmations"
                    return x
                state.update_jsonl_row(unprocessed_path, _pred, _upd)
                for x in unprocessed:
                    if x.get("txid") == txid:
                        x["sig"] = sig
                        x["reference"] = ref
                        x["comment"] = "sig created, awaiting confirmations"
                        break
            else:
                # Send failed -> refund USDD
                sender = r.get("from")
                amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                if sender and nexus_client.refund_usdd(sender, amt_str, "USDC send failed"):
                    row = dict(r)
                    row["comment"] = "refunded"
                    state.append_jsonl(processed_path, row)
                    unprocessed = [x for x in unprocessed if x.get("txid") != txid]
                    state.write_jsonl(unprocessed_path, unprocessed)

        # Step 5: confirmations and timeouts
        try:
            client = SolClient(getattr(config, "SOLANA_RPC_URL", getattr(config, "RPC_URL", None)))
        except Exception:
            client = None
        now = time.time()
        new_unprocessed: list[dict] = []
        for r in state.read_jsonl(unprocessed_path):
            if r.get("comment") == "sig created, awaiting confirmations" and r.get("sig") and client:
                try:
                    st = client.get_signature_statuses([r["sig"]])
                    js = getattr(st, "to_json", None)
                    if callable(js):
                        import json as _json
                        val = _json.loads(st.to_json()).get("result", {}).get("value", [None])[0] or {}
                    else:
                        val = (st.get("result", {}).get("value") or [None])[0] if isinstance(st, dict) else {}
                    conf = int((val or {}).get("confirmations") or 0)
                except Exception:
                    conf = 0
                if conf > 2:
                    row = dict(r)
                    row["comment"] = "processed"
                    state.append_jsonl(processed_path, row)
                    continue
                # timeout refund
                ts = int(r.get("ts") or 0)
                if ts and (now - ts) > getattr(config, "USDC_CONFIRM_TIMEOUT_SEC", 600):
                    sender = r.get("from")
                    amt_dec = _parse_decimal_amount(r.get("amount_usdd"))
                    amt_str = _format_token_amount(amt_dec, config.USDD_DECIMALS)
                    if sender and nexus_client.refund_usdd(sender, amt_str, "timeout"):
                        row = dict(r)
                        row["comment"] = "refunded"
                        state.append_jsonl(processed_path, row)
                        continue
            new_unprocessed.append(r)
        state.write_jsonl(unprocessed_path, new_unprocessed)

        # Step 6: waterline proposal
        try:
            rows = state.read_jsonl(unprocessed_path)
            if rows:
                min_ts = min(int(x.get("ts") or 0) for x in rows if int(x.get("ts") or 0) > 0)
                if min_ts:
                    state.propose_nexus_waterline(int(max(0, min_ts - config.HEARTBEAT_WATERLINE_SAFETY_SEC)))
            elif page_ts_candidates:
                state.propose_nexus_waterline(int(min(page_ts_candidates)))
        except Exception:
            pass
    except Exception as e:
        print(f"Error polling USDD deposits: {e}")
    finally:
        state.save_state()
