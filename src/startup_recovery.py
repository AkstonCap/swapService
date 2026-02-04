"""Startup recovery & reconstruction utilities.

Responsibilities:
1. Fetch waterlines from Nexus heartbeat asset
2. Rebuild database from waterline timestamps (complete server wipeout recovery)
3. Reconstruct processed_txids markers for USDD->USDC sends using on-chain memos
4. Reconstruct refunded_sigs for USDC refunds via refundSig:<deposit_sig> memos
5. Seed reference counter if missing
6. Provide summary of all actions taken

Design notes:
 - We intentionally do NOT mutate historical database records except to add missing processed markers;
   reconstruction is additive and idempotent.
 - Waterline-based scanning allows full recovery from complete database loss.
 - Falls back to recent-only scan if waterlines unavailable or very old.
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
    """Scan Nexus USDD deposits from waterline and rebuild unprocessed_txids table.
    
    Returns dict with stats about deposits added.
    """
    treasury_addr = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
    if not treasury_addr:
        return {'nexus_deposits_added': 0, 'error': 'no_treasury_configured'}
    
    print(f"   Rebuilding Nexus deposits from waterline {waterline_timestamp}...")
    
    # Fetch all deposits since waterline
    deposits = nexus_client.fetch_deposits_since(treasury_addr, waterline_timestamp)
    
    # Get existing sets from database
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT txid FROM processed_txids")
    processed_txids = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT txid FROM refunded_txids")
    refunded_txids = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT txid FROM unprocessed_txids")
    unprocessed_txids = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    added_count = 0
    skipped_processed = 0
    skipped_fees = 0
    
    for tx in deposits:
        txid = tx.get("txid")
        ts = int(tx.get("timestamp") or 0)
        conf = int(tx.get("confirmations") or 0)
        
        if not txid:
            continue
        
        # Skip if already in database
        if txid in processed_txids or txid in refunded_txids or txid in unprocessed_txids:
            skipped_processed += 1
            continue
        
        # Extract contract details
        contracts = tx.get("contracts") or []
        for c in contracts:
            if not isinstance(c, dict):
                continue
            if str(c.get("OP") or "").upper() != "CREDIT":
                continue
            
            # Get sender
            from_field = c.get("from")
            sender = ""
            if isinstance(from_field, dict):
                sender = str(from_field.get("address") or from_field.get("name") or "")
            elif isinstance(from_field, str):
                sender = from_field
            
            # Get amount
            amount_dec = _parse_decimal_amount(c.get("amount"))
            if amount_dec <= 0:
                continue
            
            # Check minimum threshold
            min_threshold = getattr(config, "MIN_CREDIT_USDD_UNITS", 100101) / (10 ** config.USDD_DECIMALS)
            if amount_dec < min_threshold:
                continue
            
            # Check if fees only
            flat_fee = _parse_decimal_amount(getattr(config, "FLAT_FEE_USDD", "0.1"))
            dyn_bps = int(getattr(config, "DYNAMIC_FEE_BPS", 0))
            dyn_fee = (amount_dec * Decimal(dyn_bps)) / Decimal(10000)
            
            if amount_dec <= (flat_fee + dyn_fee):
                # Mark as processed fees
                owner = (nexus_client.get_account_info(sender) or {}).get("owner")
                state_db.mark_processed_txid(
                    txid=txid,
                    timestamp=ts,
                    amount_usdd=float(amount_dec),
                    from_address=sender,
                    to_address=treasury_addr,
                    owner=owner or "",
                    sig="",
                    status="processed as fees"
                )
                processed_txids.add(txid)
                skipped_fees += 1
                break
            
            # Add to unprocessed
            owner = (nexus_client.get_account_info(sender) or {}).get("owner")
            state_db.add_unprocessed_txid(
                txid=txid,
                timestamp=ts,
                amount_usdd=float(amount_dec),
                from_address=sender,
                to_address=treasury_addr,
                owner_from_address=owner,
                confirmations_credit=conf,
                status="pending_receival"
            )
            unprocessed_txids.add(txid)
            added_count += 1
            break
    
    return {
        'nexus_deposits_added': added_count,
        'nexus_from_timestamp': waterline_timestamp,
        'nexus_deposits_scanned': len(deposits),
        'nexus_skipped_processed': skipped_processed,
        'nexus_skipped_fees': skipped_fees,
    }


def _rebuild_solana_from_waterline(waterline_timestamp: int) -> dict:
    """Scan Solana USDC deposits from waterline and rebuild database tables.
    
    Rebuilds:
    - processed_txids markers (from nexus_txid: memos)
    - refunded_txids markers (from refundSig: memos)
    - quarantined_txids markers (from quarantinedSig: memos)
    
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
    cursor.execute("SELECT txid FROM quarantined_txids")
    quarantined_txids = {row[0] for row in cursor.fetchall()}
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
        if qsig in quarantined_txids:
            continue
        try:
            state_db.mark_quarantined_txid(txid=qsig, sig="")
            quarantined_txids.add(qsig)
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
    cursor.execute("SELECT txid FROM quarantined_txids")
    quarantined_txids = {row[0] for row in cursor.fetchall()}
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
            if qsig in quarantined_txids:
                continue
            try:
                state_db.mark_quarantined_txid(txid=qsig, sig="")
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
    
    # Fetch waterlines from heartbeat asset
    heartbeat = nexus_client.get_heartbeat_asset()
    
    if not heartbeat:
        print("   ⚠ No heartbeat asset found, using fallback scan")
        stats = _fallback_recent_scan()
        seeded = nexus_client.get_last_reference()
        return {
            'reference_seeded': seeded,
            **stats,
        }
    
    # Extract waterlines from heartbeat data field
    try:
        data_field = heartbeat.get("data") or "{}"
        if isinstance(data_field, str):
            import json
            data = json.loads(data_field)
        else:
            data = data_field
        
        nexus_waterline = int(data.get("nexus_waterline") or 0)
        solana_waterline = int(data.get("solana_waterline") or 0)
    except Exception:
        nexus_waterline = 0
        solana_waterline = 0
    
    # Safety: Don't scan too far back (avoid overwhelming recovery)
    max_lookback_sec = int(getattr(config, "MAX_WATERLINE_LOOKBACK_SEC", 7 * 24 * 3600))  # 7 days
    current_ts = int(time.time())
    min_allowed_waterline = current_ts - max_lookback_sec
    
    if nexus_waterline and nexus_waterline < min_allowed_waterline:
        print(f"   ⚠ Nexus waterline too old ({nexus_waterline}), limiting to {max_lookback_sec}s lookback")
        nexus_waterline = min_allowed_waterline
    
    if solana_waterline and solana_waterline < min_allowed_waterline:
        print(f"   ⚠ Solana waterline too old ({solana_waterline}), limiting to {max_lookback_sec}s lookback")
        solana_waterline = min_allowed_waterline
    
    # If no waterlines set, use fallback
    if not nexus_waterline and not solana_waterline:
        print("   ⚠ No waterlines set in heartbeat, using fallback scan")
        stats = _fallback_recent_scan()
        seeded = nexus_client.get_last_reference()
        return {
            'reference_seeded': seeded,
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
        **nexus_stats,
        **solana_stats,
    }

