"""Startup recovery & reconstruction utilities.

Responsibilities:
1. Fetch waterlines from Nexus heartbeat asset
2. Rebuild database from waterline timestamps (complete server wipeout recovery)
3. Reconstruct processed_txids markers for Nexus->Solana sends using on-chain memos
4. Reconstruct refunded_sigs for Solana refunds via refundSig:<deposit_sig> memos
5. Seed reference counter if missing
6. Provide summary of all actions taken

Design notes:
 - We intentionally do NOT mutate historical database records except to add missing processed markers;
   reconstruction is additive and idempotent.
 - Waterline-based scanning allows full recovery from complete database loss.
 - Falls back to recent-only scan only if waterlines are unavailable or unset.
 - Reference seeding heuristic: choose max(reference found in database OR Nexus) + 1.
"""
from __future__ import annotations
from decimal import Decimal
from . import config, solana_client, nexus_client, state_db
import time

QUARANTINED_MEMO_PREFIX = "quarantinedSig:"


def _parse_decimal_amount(val) -> Decimal:
    """Parse a Nexus token amount into Decimal."""
    if val is None:
        return Decimal(0)
    try:
        return Decimal(str(val).strip())
    except Exception:
        try:
            return Decimal(float(val))
        except Exception:
            return Decimal(0)


def _rebuild_nexus_from_waterline(waterline_timestamp: int) -> dict:
    """Scan Nexus Nexus deposits from waterline and rebuild unprocessed_txids table.
    
    Returns dict with stats about deposits added.
    """
    # Startup recovery is a custody boundary: never let the legacy mutable alias choose
    # which account is treated as the bridge treasury.
    treasury_addr = config.SWAP_PAIR.nexus.treasury_account
    if not treasury_addr:
        return {'nexus_deposits_added': 0, 'error': 'no_treasury_configured'}

    print(f"   Rebuilding Nexus deposits from waterline {waterline_timestamp}...")

    # A partial history page is never a safe basis for a wipeout reconstruction.  In
    # particular, it must not create markers which could be mistaken for a complete
    # recovery after a pagination budget or transport failure.
    scan = nexus_client.fetch_deposits_since(treasury_addr, waterline_timestamp)
    if not scan.complete:
        return {
            'nexus_deposits_added': 0,
            'nexus_deposits_scanned': 0,
            'error': f'nexus_deposit_scan_incomplete:{scan.reason or "unknown"}',
        }
    deposits = scan.deposits

    # Get existing identities from database.  A transaction can fund more than one
    # independently payable treasury CREDIT, so txid alone is never a duplicate key.
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT txid, contract_id FROM processed_txids")
    processed_txids = {(row[0], row[1]) for row in cursor.fetchall()}
    cursor.execute("SELECT txid, contract_id FROM refunded_txids")
    refunded_txids = {(row[0], row[1]) for row in cursor.fetchall()}
    cursor.execute("SELECT txid, contract_id FROM unprocessed_txids")
    unprocessed_txids = {(row[0], row[1]) for row in cursor.fetchall()}
    conn.close()
    
    added_count = 0
    skipped_processed = 0
    skipped_fees = 0
    
    for tx in deposits:
        txid = tx.get("txid")
        ts = int(tx.get("timestamp") or 0)
        conf = int(tx.get("confirmations") or 0)
        
        if not txid:
            return {
                'nexus_deposits_added': 0,
                'nexus_deposits_scanned': 0,
                'error': 'nexus_deposit_scan_incomplete:missing_txid',
            }
        
        # Extract contract details. A transaction may carry multiple CREDITS; every
        # individual contract must independently prove that it targets the canonical
        # treasury. A sibling CREDIT never authorizes this one.
        contracts = tx.get("contracts") or []
        for c in contracts:
            if not isinstance(c, dict) or str(c.get("OP") or "").upper() != "CREDIT":
                continue
            to_address = nexus_client._parse_nexus_contract_address(c.get("to"))
            if to_address != treasury_addr:
                continue
            contract_id = c.get("id")
            if isinstance(contract_id, bool) or not isinstance(contract_id, int) or contract_id < 0:
                return {
                    'nexus_deposits_added': 0,
                    'nexus_deposits_scanned': 0,
                    'error': 'nexus_deposit_scan_incomplete:invalid_contract_id',
                }
            identity = (txid, contract_id)
            if identity in processed_txids or identity in refunded_txids or identity in unprocessed_txids:
                skipped_processed += 1
                continue
            sender = nexus_client._parse_nexus_contract_address(c.get("from")) or ""

            amount_dec = _parse_decimal_amount(c.get("amount"))
            classification = nexus_client.classify_nexus_credit(c.get("amount"))
            if classification.disposition == "invalid":
                if amount_dec.is_finite() and amount_dec > 0:
                    owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                    state_db.add_unprocessed_txid(
                        txid=txid, contract_id=contract_id, timestamp=ts, amount_usdd=float(amount_dec),
                        from_address=sender, to_address=to_address,
                        owner_from_address=owner, confirmations_credit=conf,
                        status="quarantined", amount_usdd_units=None,
                        hold_reason="invalid_exact_nexus_amount",
                    )
                    unprocessed_txids.add(identity)
                continue
            if classification.disposition == "dust":
                continue
            credit_units = classification.amount_nexus_units
            owner = (nexus_client.get_account_info(sender) or {}).get("owner")

            if classification.disposition in {"below_minimum", "fee_only"}:
                kind = (
                    "below_min_credit_nexus"
                    if classification.disposition == "below_minimum"
                    else "fee_only_nexus_credit"
                )
                state_db.add_fee_entry(
                    sig=None, txid=txid, kind=kind, amount_usdc_units=None,
                    amount_usdd_units=credit_units,
                )
                state_db.mark_processed_txid(
                    txid=txid, contract_id=contract_id, timestamp=ts, amount_usdd=float(amount_dec),
                    from_address=sender, to_address=treasury_addr, owner=owner or "", sig="",
                    status="processed as fees", amount_usdd_units=credit_units,
                )
                processed_txids.add(identity)
                skipped_fees += 1
                continue

            status = "refund pending" if classification.disposition == "over_cap" else "pending_receival"
            state_db.add_unprocessed_txid(
                txid=txid, contract_id=contract_id,
                timestamp=ts,
                amount_usdd=float(amount_dec),
                from_address=sender,
                to_address=treasury_addr,
                owner_from_address=owner,
                confirmations_credit=conf,
                status=status,
                amount_usdd_units=credit_units,
            )
            unprocessed_txids.add(identity)
            added_count += 1
    
    return {
        'nexus_deposits_added': added_count,
        'nexus_from_timestamp': waterline_timestamp,
        'nexus_deposits_scanned': len(deposits),
        'nexus_skipped_processed': skipped_processed,
        'nexus_skipped_fees': skipped_fees,
    }


def _rebuild_solana_from_waterline(waterline_timestamp: int) -> dict:
    """Scan Solana deposits from waterline and rebuild database tables.
    
    Rebuilds:
    - processed_txids markers (from nexus_txid: memos)
    - refunded_txids markers (from refundSig: memos)
    - quarantined_sigs markers (from quarantinedSig: memos - Solana side, keyed by deposit sig)
    
    Returns dict with stats.
    """
    print(f"   Rebuilding Solana markers from waterline {waterline_timestamp}...")
    
    # Scan all memos since waterline
    memo_map = solana_client.scan_memos_since_timestamp(waterline_timestamp)
    
    # Get existing sets from database
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT txid FROM processed_txids")
    processed_txids = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT sig FROM refunded_sigs")
    refunded_sigs = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT sig FROM quarantined_sigs")
    quarantined_sig_set = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    # Rebuild processed markers
    added_processed = 0
    for txid, sig in memo_map.get('nexus_txids', {}).items():
        if txid in processed_txids:
            continue
        try:
            # We don't have full tx details, just mark as processed
            state_db.mark_processed_txid(
                txid=txid,
                timestamp=int(time.time()),
                amount_usdd=0.0,
                from_address="",
                to_address="",
                owner="",
                sig=sig,
                status="processed"
            )
            processed_txids.add(txid)
            added_processed += 1
        except Exception:
            pass
    
    # Rebuild refunded markers
    added_refunds = 0
    for dep_sig, refund_sig in memo_map.get('refund_sigs', {}).items():
        if dep_sig in refunded_sigs:
            continue
        try:
            state_db.mark_refunded_sig(
                sig=dep_sig,
                timestamp=int(time.time()),
                from_address="",
                amount_usdc_units=0,
                memo=None,
                refund_sig=refund_sig,
                refunded_units=None,
                status="refunded"
            )
            refunded_sigs.add(dep_sig)
            added_refunds += 1
        except Exception:
            pass
    
    # Rebuild quarantined markers
    added_quarantine = 0
    for qsig in memo_map.get('quarantined_sigs', {}).keys():
        if qsig in quarantined_sig_set:
            continue
        try:
            # Solana-side quarantine: belongs in quarantined_sigs, keyed by deposit signature.
            state_db.mark_quarantined_sig(
                sig=qsig, timestamp=int(time.time()), from_address="",
                amount_usdc_units=0, memo=None, quarantine_sig=None,
                quarantined_units=None, status="reconstructed from on-chain memo",
            )
            quarantined_sig_set.add(qsig)
            added_quarantine += 1
        except Exception:
            pass
    
    return {
        'solana_processed_markers_added': added_processed,
        'solana_refunded_markers_added': added_refunds,
        'solana_quarantined_markers_added': added_quarantine,
        'solana_from_timestamp': waterline_timestamp,
        'solana_found_processed_memos': len(memo_map.get('nexus_txids', {})),
        'solana_found_refund_memos': len(memo_map.get('refund_sigs', {})),
        'solana_found_quarantined_memos': len(memo_map.get('quarantined_sigs', {})),
    }


def _fallback_recent_scan() -> dict:
    """Fallback to scanning only recent signatures (no waterline available)."""
    print("   No waterline available, scanning recent signatures only...")
    
    scan_limit = int(getattr(config, 'STARTUP_SCAN_SIGNATURE_LIMIT', 300))
    memo_map = solana_client.scan_recent_memos(search_limit=scan_limit)
    
    # Get existing sets from database
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT txid FROM processed_txids")
    processed_txids = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT sig FROM refunded_sigs")
    refunded_sigs = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT sig FROM quarantined_sigs")
    quarantined_sig_set = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    added_nexus = 0
    added_refunds = 0
    added_quarantine = 0
    
    # Add nexus_txid processed markers
    for txid, sig in memo_map.get('nexus_txids', {}).items():
        if txid in processed_txids:
            continue
        try:
            state_db.mark_processed_txid(
                txid=txid,
                timestamp=int(time.time()),
                amount_usdd=0.0,
                from_address="",
                to_address="",
                owner="",
                sig=sig,
                status="processed"
            )
            added_nexus += 1
        except Exception:
            pass
    
    # Add refunded sig markers
    for dep_sig, refund_sig in memo_map.get('refund_sigs', {}).items():
        if dep_sig in refunded_sigs:
            continue
        try:
            state_db.mark_refunded_sig(
                sig=dep_sig,
                timestamp=int(time.time()),
                from_address="",
                amount_usdc_units=0,
                memo=None,
                refund_sig=refund_sig,
                refunded_units=None,
                status="refunded"
            )
            added_refunds += 1
        except Exception:
            pass
    
    # Quarantined signatures
    found_quarantine = len(memo_map.get('quarantined_sigs', {})) if isinstance(memo_map, dict) else 0
    if found_quarantine:
        for qsig in memo_map.get('quarantined_sigs', {}).keys():
            if qsig in quarantined_sig_set:
                continue
            try:
                state_db.mark_quarantined_sig(
                    sig=qsig, timestamp=int(time.time()), from_address="",
                    amount_usdc_units=0, memo=None, quarantine_sig=None,
                    quarantined_units=None, status="reconstructed from on-chain memo",
                )
                quarantined_sig_set.add(qsig)
                added_quarantine += 1
            except Exception:
                pass
    
    return {
        'fallback_mode': True,
        'added_nexus_processed': added_nexus,
        'added_refunded_sigs': added_refunds,
        'added_quarantined_sigs': added_quarantine,
        'scan_limit': scan_limit,
        'found_nexus_memos': len(memo_map.get('nexus_txids', {})),
        'found_refund_memos': len(memo_map.get('refund_sigs', {})),
        'found_quarantined_memos': found_quarantine,
    }


def perform_startup_recovery() -> dict:
    """Perform complete startup recovery using waterlines.
    
    Steps:
    1. Fetch waterlines from Nexus heartbeat asset
    2. Rebuild Nexus deposits (unprocessed_txids) from waterline
    3. Rebuild Solana markers (processed/refunded/quarantined) from waterline
    4. Seed reference counter if needed
    
    Falls back to recent-only scan if waterlines unavailable or too old.
    """
    print("🔧 Starting recovery...")

    # A process can die after atomically claiming the sole permitted CLI execution
    # but before recording its result. Convert that state to an explicit hold before
    # any scans run; later resolution must find positive chain evidence and can never
    # consume the authorization a second time.
    interrupted_nexus_transfers_held = state_db.recover_interrupted_nexus_transfer_intents()
    
    # Fetch waterlines from heartbeat asset
    heartbeat = nexus_client.get_heartbeat_asset()
    
    if not heartbeat:
        print("   ⚠ No heartbeat asset found, using fallback scan")
        stats = _fallback_recent_scan()
        seeded = nexus_client.get_last_reference()
        return {
            'reference_seeded': seeded,
            'interrupted_nexus_transfers_held': interrupted_nexus_transfers_held,
            **stats,
        }
    
    # Every runtime heartbeat is a top-level Nexus basic-asset record.  Never interpret
    # legacy nested ``data`` payloads as an empty checkpoint: that would bypass Nexus
    # wipeout reconstruction and silently continue with only a recent Solana memo scan.
    try:
        waterlines = nexus_client.parse_heartbeat_waterlines(heartbeat)
    except ValueError as exc:
        print(f"   ⚠ Incompatible heartbeat waterline schema: {exc}")
        seeded = nexus_client.get_last_reference()
        return {
            'reference_seeded': seeded,
            'interrupted_nexus_transfers_held': interrupted_nexus_transfers_held,
            'recovery_incomplete': True,
            'error': f'heartbeat_waterline_schema_incompatible:{exc}',
        }
    nexus_waterline = waterlines.nexus
    solana_waterline = waterlines.solana

    # The heartbeat waterlines are custody checkpoints, not workload hints.  Advancing an
    # old checkpoint would make liabilities in the omitted interval unrecoverable after a
    # database wipe.  Recovery must enumerate the entire published range (or return an
    # explicit incomplete result); callers decide how to schedule the work, never which
    # credits may be skipped.
    # If no waterlines set, use fallback
    if not nexus_waterline and not solana_waterline:
        print("   ⚠ No waterlines set in heartbeat, using fallback scan")
        stats = _fallback_recent_scan()
        seeded = nexus_client.get_last_reference()
        return {
            'reference_seeded': seeded,
            'interrupted_nexus_transfers_held': interrupted_nexus_transfers_held,
            **stats,
        }
    
    print(f"   Waterlines: Nexus={nexus_waterline}, Solana={solana_waterline}")
    
    # Rebuild from waterlines
    nexus_stats = {}
    solana_stats = {}
    
    if nexus_waterline:
        try:
            nexus_stats = _rebuild_nexus_from_waterline(nexus_waterline)
        except Exception as e:
            print(f"   Error rebuilding Nexus: {e}")
            nexus_stats = {'error': str(e)}
    
    if solana_waterline:
        try:
            solana_stats = _rebuild_solana_from_waterline(solana_waterline)
        except Exception as e:
            print(f"   Error rebuilding Solana: {e}")
            solana_stats = {'error': str(e)}
    
    # Seed reference counter
    seeded = nexus_client.get_last_reference()
    
    return {
        'waterline_mode': True,
        'nexus_waterline': nexus_waterline,
        'solana_waterline': solana_waterline,
        'reference_seeded': seeded,
        'interrupted_nexus_transfers_held': interrupted_nexus_transfers_held,
        **nexus_stats,
        **solana_stats,
    }

