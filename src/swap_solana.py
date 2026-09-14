import logging
from decimal import Decimal
from . import config, state_db, nexus_client, solana_client, fees, alerts, structured_logging

_LOG = structured_logging.get_logger("swapService.solana")


def _log(event: str, **fields):
    """Best-effort Solana lifecycle diagnostics that cannot stop custody processing."""
    try:
        structured_logging.emit(_LOG, logging.INFO, event, **fields)
    except Exception:
        # A logging outage must never interrupt durable state transitions or poller work.
        pass


def scale_amount(amount: int, src_decimals: int, dst_decimals: int) -> int:
    """Deprecated alias for `config.rescale_units`, kept so the two implementations of
    cross-decimal scaling cannot drift apart."""
    return config.rescale_units(amount, src_decimals, dst_decimals)


def _advance_solana_waterline(current_wline, poll_start, fetch_ok: bool, deferred_ts=None,
                              covered_through_ts=None) -> None:
    """Move `last_safe_timestamp_solana` forward only to a point proven safe.

    Invariant: the waterline must never pass a deposit that is not durably recorded,
    because bounded provider scans exclude `ts < since_ts` - anything left behind
    the waterline is never seen again.

    - Enumeration failed this cycle -> update the heartbeat only, never the waterline.
    - Deposits still unprocessed  -> pin the waterline behind the oldest one.
    - Nothing pending             -> the completed cursor range, not wall-clock time,
                                     bounds the waterline.  A newer RPC head can arrive
                                     while an older range is paginated.
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
        if type(covered_through_ts) is not int or covered_through_ts <= 0:
            _log("WATERLINE_HELD", reason="missing_completed_cursor_coverage")
            nexus_client.update_heartbeat_asset(int(poll_start), None, None)
            return
        candidate = int(covered_through_ts) - safety
        reason = "completed_cursor_range_persisted"

    # A deposit withheld pending finalization is NOT in the DB, so nothing above accounts
    # for it. The waterline must stay behind it or it would be hidden forever.
    if deferred_ts:
        deferred_floor = int(deferred_ts) - safety - 1
        if deferred_floor < candidate:
            candidate = deferred_floor
            reason = "pinned_behind_deferred_unfinalized_deposit"

    # Durable parser/finality holds are not guaranteed to survive total local DB loss.
    # Keep the externally published recovery checkpoint behind their earliest source.
    earliest_hold = state_db.get_earliest_solana_deposit_hold_timestamp()
    if earliest_hold is not None:
        hold_floor = int(earliest_hold) - safety - 1
        if hold_floor < candidate:
            candidate = hold_floor
            reason = "pinned_behind_oldest_durable_deposit_hold"

    if candidate > int(current_wline):
        _log("WATERLINE_ADVANCED", old_ts=int(current_wline), new_ts=candidate, reason=reason)
        nexus_client.update_heartbeat_asset(int(poll_start), None, candidate)
    else:
        # Never move backwards; still refresh the liveness heartbeat.
        nexus_client.update_heartbeat_asset(int(poll_start), None, None)


def poll_solana_deposits(paused: bool = False):
    """Poll Solana. When `paused` (backing deficit), stop taking on NEW swap exposure
    but keep returning money: refunds, quarantines and confirmations still run."""
    from solana.rpc.api import Client
    from solders.signature import Signature
    try:
        import time as _time
        heartbeat = nexus_client.get_heartbeat_asset()
        if not heartbeat:
            # Fall back to the last known-good local waterline instead of halting
            # silently - a Nexus outage previously stopped Solana ingestion entirely
            # while the service still looked healthy.
            hb_row = state_db.get_heartbeat(getattr(config, "NEXUS_HEARTBEAT_ASSET_NAME", "") or "")
            local_wl = hb_row[2] if hb_row and len(hb_row) > 2 else None
            if local_wl is None:
                alerts.critical("heartbeat_unreadable",
                                "heartbeat asset unreadable and no local waterline; "
                                "Solana ingestion is HALTED")
                return
            alerts.warning("heartbeat_unreadable_fallback",
                           "heartbeat asset unreadable; using last known-good local waterline",
                           waterline=local_wl)
            wline_sol = local_wl
        else:
            try:
                wline_sol = nexus_client.parse_heartbeat_waterlines(heartbeat).solana
            except ValueError as exc:
                alerts.critical(
                    "heartbeat_schema_incompatible",
                    "Solana ingestion halted: heartbeat waterline schema is incompatible",
                    error=str(exc),
                )
                return

        poll_start = _time.time()

        # Clamp a waterline that is somehow ahead of now (corrupt or hand-edited asset):
        # left alone it would skip every future deposit.
        if int(wline_sol) > int(poll_start):
            alerts.warning("waterline_in_future",
                           "Solana waterline is ahead of now; clamping",
                           waterline=int(wline_sol), now=int(poll_start))
            wline_sol = int(poll_start) - int(getattr(config, "HEARTBEAT_WATERLINE_SAFETY_SEC", 120))

        # Deposit ingestion is NEVER gated on the vault balance delta.
        # The vault is debited by Nexus->Solana payouts, refunds and quarantine moves as
        # well as credited by deposits, so a small or negative delta does not mean
        # "no deposits arrived" - it usually means outflows >= inflows. The previous
        # code skipped fetching on a small delta AND advanced the waterline to now,
        # which permanently hid every deposit that landed in the skipped window.
        fetch_ok = False
        unprocessed_deposits_added = 0
        deferred_ts = None  # oldest deposit withheld pending finalization, if any
        covered_through_ts = None
        if paused:
            # Backing deficit: take on no NEW exposure, but still run the refund,
            # quarantine and confirmation passes below so user funds keep moving.
            _log("SOLANA_INGEST_PAUSED", reason="backing deficit")
        else:
            try:
                # Promote previously held exact candidates independently of the public
                # timestamp waterline.  A confirmed large deposit remains durable while
                # waiting for finalization and therefore cannot be stranded when a later
                # bounded range completes.
                try:
                    promoted_holds = solana_client.replay_solana_deposit_holds()
                    if promoted_holds:
                        _log("SOLANA_DEPOSIT_HOLDS_PROMOTED", count=promoted_holds)
                except Exception as exc:
                    _log("SOLANA_DEPOSIT_HOLD_REPLAY_FAILED", error=str(exc))

                # Helius is the trusted primary when configured. Its full transaction
                # pages and provider token are bound to this fixed poll range and committed
                # atomically. Core RPC remains the no-Helius operational fallback; an
                # in-flight cursor is always resumed by the provider that created it.
                progress = solana_client.scan_incoming_deposits_with_durable_cursor(
                    str(config.VAULT_USDC_ACCOUNT),
                    since_ts=int(wline_sol),
                    page_size=min(1000, max(1, int(getattr(config, "POLL_HELIUS_LIMIT", 200)))),
                    upper_timestamp=int(poll_start),
                )
                unprocessed_deposits_added = progress.admitted_count
                fetch_ok = progress.complete
                covered_through_ts = progress.upper_timestamp
                _log("SOLANA_DEPOSITS_INGESTED", count=unprocessed_deposits_added,
                     cursor_complete=fetch_ok)
            except Exception as e:
                # A failed enumeration must not advance the waterline (see _advance_solana_waterline).
                _log("SOLANA_FETCH_FAILED", error=str(e))

        if not paused:
            [proc_count_swap, proc_count_refund, proc_count_quar, proc_count_mic] = solana_client.process_unprocessed_solana_deposits(1000, 8.0)
            _log(
                "SOLANA_PROCESSING_SUMMARY",
                swap_debits=proc_count_swap,
                refunds_pending=proc_count_refund,
                quarantines_pending=proc_count_quar,
                micro_deposits=proc_count_mic,
            )

        refunds = solana_client.process_solana_deposits_refunding(1000, 8.0)
        if refunds > 0:
            _log("SOLANA_REFUNDS_SUBMITTED", count=refunds)

        quarantines = solana_client.process_solana_deposits_quarantine(1000, 8.0)
        if quarantines > 0:
            _log("SOLANA_QUARANTINES_SUBMITTED", count=quarantines)

        confirmed_ref = solana_client.check_sig_confirmations(100, 8.0)
        if confirmed_ref > 0:
            _log("SOLANA_REFUNDS_CONFIRMED", count=confirmed_ref)

        # Bug #8 fix: Check quarantine confirmations (mirrors refund confirmation pattern)
        confirmed_quar = solana_client.check_quarantine_confirmations(100, 8.0)
        if confirmed_quar > 0:
            _log("SOLANA_QUARANTINES_CONFIRMED", count=confirmed_quar)

        # Resolve any debit whose outcome is unknown (crash, timeout, unparsable CLI
        # response) against the chain BEFORE the confirmation pass, so an executed
        # debit is never mistaken for a failure and refunded.
        resolved = nexus_client.resolve_unverified_debits()
        if resolved > 0:
            _log("NEXUS_DEBITS_RESOLVED", count=resolved)

        confirmed_debits = nexus_client.check_unconfirmed_debits(
            config.get_nexus_transfer_min_confirmations(), 8.0
        )
        if confirmed_debits > 0:
            _log("NEXUS_DEBITS_CONFIRMED", count=confirmed_debits)

        _advance_solana_waterline(wline_sol, poll_start, fetch_ok, deferred_ts,
                                  covered_through_ts if not paused else None)

        # Refresh the public registration record (status + current terms) alongside the
        # heartbeat, so a client reading it on-chain sees whether we are paused and what
        # fees/minimums are actually in force right now.
        try:
            nexus_client.publish_service_record(
                status="paused" if paused else "online", last_poll=int(poll_start))
        except Exception as e:
            _log("SERVICE_RECORD_UPDATE_FAILED", error=str(e))

        # Retained for observability only; this value no longer gates ingestion.
        current_bal_after = solana_client.get_token_account_balance(config.VAULT_USDC_ACCOUNT)
        state_db.save_last_vault_balance(current_bal_after)

    except Exception as e:
        # Log poll errors so they are not silently swallowed
        _log("POLL_SOLANA_ERROR", error=str(e))
    
