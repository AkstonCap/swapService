"""Startup recovery & reconstruction utilities.

Responsibilities:
1. Seed reference counter if missing by scanning existing counter file or highest reference
   appearing in unprocessed / processed swap logs OR last reference in reference file.
2. Reconstruct processed_nexus_txs markers for USDD->USDC sends using on-chain memos nexus_txid:<txid>.
3. Reconstruct refunded_sigs for USDC refunds via refundSig:<deposit_sig> memos (best effort).
4. Provide a summary so caller can log actions taken.

Design notes:
 - We intentionally do NOT mutate historical JSONL lines except to append missing processed markers;
   reconstruction is additive and idempotent.
 - We cap Solana scan to a configurable SEARCH_LIMIT to avoid long startup delays.
 - If reference counter file already exists we leave it untouched.
 - Reference seeding heuristic: choose max(reference found in any row/reference field) + 1.
"""
from __future__ import annotations
import json, os
from . import config, solana_client, nexus_client, state_db

QUARANTINED_MEMO_PREFIX = "quarantinedSig:"


def reconstruct_processed_from_memos(scan_limit: int | None = None) -> dict:
    
    if scan_limit is None:
        scan_limit = int(getattr(config, 'STARTUP_SCAN_SIGNATURE_LIMIT', 300))
    memo_map = solana_client.scan_recent_memos(search_limit=scan_limit)
    added_nexus = 0
    added_refunds = 0
    # Add nexus_txid processed markers
    for txid, sig in memo_map.get('nexus_txids', {}).items():
        if txid in state_db.processed_txids:
            continue
        try:
            state_db.mark_processed_txid(txid, sig)
            added_nexus += 1
        except Exception:
            pass
    # Add refunded sig markers (append to refunded_sigs file if absent)
    for dep_sig, refund_sig in memo_map.get('refund_sigs', {}).items():
        if state_db.is_refunded(dep_sig):
            continue
        try:
            state_db.mark_refunded_sig(dep_sig, refund_sig)
            added_refunds += 1
        except Exception:
            pass
    # Quarantined signatures inclusion (if scan_recent_memos later extended) – ignore silently if absent
    found_quarantine = len(memo_map.get('quarantined_sigs', {})) if isinstance(memo_map, dict) else 0
    if found_quarantine:
        for qsig in memo_map.get('quarantined_sigs', {}).keys():
            if qsig in state_db.processed_sigs:
                continue
            try:
                state_db.mark_quarantined_sig(qsig, reason="quarantined_startup")
            except Exception:
                pass
    return {
        'added_nexus_processed': added_nexus,
        'added_refunded_sigs': added_refunds,
        'scan_limit': scan_limit,
        'found_nexus_memos': len(memo_map.get('nexus_txids', {})),
        'found_refund_memos': len(memo_map.get('refund_sigs', {})),
        'found_quarantined_memos': found_quarantine,
    }

def perform_startup_recovery() -> dict:
    seeded = nexus_client.get_last_reference()
    memo_stats = reconstruct_processed_from_memos()
    return {
        'reference_seeded': seeded,
        **memo_stats,
    }
