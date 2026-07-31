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


def _advance_solana_waterline(current_wline, poll_start, fetch_ok: bool) -> None:
    """Move `last_safe_timestamp_solana` forward only to a point proven safe.

    Invariant: the waterline must never pass a deposit that is not durably recorded,
    because `_fetch_deposits_helius` stops at `ts <= since_ts` - anything left behind
    the waterline is never seen again.

    - Enumeration failed this cycle -> update the heartbeat only, never the waterline.
    - Deposits still unprocessed  -> pin the waterline behind the oldest one.
    - Nothing pending             -> everything up to poll_start is persisted, so the
                                     waterline may advance to poll_start (less safety).
    """
    safety = int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 120))
    if not fetch_ok:
        _log("WATERLINE_HELD", reason="deposit_fetch_failed")
        nexus_client.update_heartbeat_asset(int(poll_start), None, None)
        return

    oldest_pending = solana_client.check_timestamp_unpr_sigs()  # oldest unprocessed ts - 1, else None
    if oldest_pending:
        candidate = int(oldest_pending) - safety
        reason = "pinned_behind_oldest_unprocessed"
    else:
        candidate = int(poll_start) - safety
        reason = "all_fetched_deposits_persisted"

    if candidate > int(current_wline):
        _log("WATERLINE_ADVANCED", old_ts=int(current_wline), new_ts=candidate, reason=reason)
        nexus_client.update_heartbeat_asset(int(poll_start), None, candidate)
    else:
        # Never move backwards; still refresh the liveness heartbeat.
        nexus_client.update_heartbeat_asset(int(poll_start), None, None)


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

        # Deposit ingestion is NEVER gated on the vault balance delta.
        # The vault is debited by USDD->USDC payouts, refunds and quarantine moves as
        # well as credited by deposits, so a small or negative delta does not mean
        # "no deposits arrived" - it usually means outflows >= inflows. The previous
        # code skipped fetching on a small delta AND advanced the waterline to now,
        # which permanently hid every deposit that landed in the skipped window.
        fetch_ok = False
        unprocessed_deposits_added = 0
        try:
            # Prefer Helius enriched RPC to batch-fetch txs + memos in 1–2 calls; fallback to existing scanner.
            usdc_deposits = solana_client.fetch_incoming_usdc_deposits_via_helius(
                str(config.VAULT_USDC_ACCOUNT),
                since_ts=int(wline_sol),
                min_units=getattr(config, "MIN_DEPOSIT_USDC_UNITS", 0),
                limit=getattr(config, "POLL_HELIUS_LIMIT", 200),
            )

            # Consume the enriched tuples directly (memo/from/amount already present);
            # no per-deposit re-fetch, so the Helius fast path stays 1-2 RPC calls.
            unprocessed_deposits_added = solana_client.process_helius_deposits(usdc_deposits, True)
            fetch_ok = True
            print(f"New deposits fetched and added for processing: {unprocessed_deposits_added}\n")
        except Exception as e:
            # A failed enumeration must not advance the waterline (see _advance_solana_waterline).
            _log("USDC_FETCH_FAILED", error=str(e))

        [proc_count_swap, proc_count_refund, proc_count_quar, proc_count_mic] = solana_client.process_unprocessed_usdc_deposits(1000, 8.0)
        print(f"Debited, awaiting confirmation: {proc_count_swap}, \nTo be refunded: {proc_count_refund}, \nTo be quarantined: {proc_count_quar}, \nMicro-sigs found: {proc_count_mic}\n")

        refunds = solana_client.process_usdc_deposits_refunding(1000, 8.0)
        print(f"Processed refunds, awaiting confirmation: {refunds}\n") if refunds > 0 else None

        quarantines = solana_client.process_usdc_deposits_quarantine(1000, 8.0)
        print(f"Processed quarantines: {quarantines}\n") if quarantines > 0 else None

        confirmed_ref = solana_client.check_sig_confirmations(100, 8.0)
        print(f"Confirmed refunds: {confirmed_ref}\n") if confirmed_ref > 0 else None

        # Bug #8 fix: Check quarantine confirmations (mirrors refund confirmation pattern)
        confirmed_quar = solana_client.check_quarantine_confirmations(100, 8.0)
        print(f"Confirmed quarantines: {confirmed_quar}\n") if confirmed_quar > 0 else None

        confirmed_debits = nexus_client.check_unconfirmed_debits(10, 8.0)
        print(f"Confirmed debits: {confirmed_debits}\n") if confirmed_debits > 0 else None

        _advance_solana_waterline(wline_sol, poll_start, fetch_ok)

        # Retained for observability only; this value no longer gates ingestion.
        current_bal_after = solana_client.get_token_account_balance(config.VAULT_USDC_ACCOUNT)
        state_db.save_last_vault_balance(current_bal_after)

    except Exception as e:
        # Log poll errors so they are not silently swallowed
        _log("POLL_SOLANA_ERROR", error=str(e))
    
