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
from . import config, state, solana_client

QUARANTINED_MEMO_PREFIX = "quarantinedSig:"

def _read_jsonl_fast(path: str):
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except Exception:
                    continue
    except Exception:
        return rows
    return rows

def seed_reference_counter_if_needed() -> int | None:
    path = config.REFERENCE_COUNTER_FILE
    if os.path.exists(path):
        # Already present; trust existing sequence
        return None
    candidates: list[int] = []
    # Scan unprocessed & processed USDC->USDD (Solana deposits) logs for 'reference'
    for p in (config.UNPROCESSED_SIGS_FILE, config.PROCESSED_SWAPS_FILE):
        for row in _read_jsonl_fast(p):
            try:
                ref = row.get('reference')
                if isinstance(ref, int):
                    candidates.append(ref)
                elif isinstance(ref, str) and ref.isdigit():
                    candidates.append(int(ref))
            except Exception:
                continue
    # Also look at unprocessed Nexus txids list for stored references
    for p in ("unprocessed_txids.json", "processed_txids.json"):
        for row in _read_jsonl_fast(p):
            try:
                ref = row.get('reference')
                if isinstance(ref, int):
                    candidates.append(ref)
                elif isinstance(ref, str) and ref.isdigit():
                    candidates.append(int(ref))
            except Exception:
                continue
    next_val = (max(candidates) + 1) if candidates else 1
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'next': next_val}, f)
        return next_val
    except Exception:
        return None

def reconstruct_processed_from_memos(scan_limit: int | None = None) -> dict:
    if scan_limit is None:
        scan_limit = int(getattr(config, 'STARTUP_SCAN_SIGNATURE_LIMIT', 300))
    memo_map = solana_client.scan_recent_memos(search_limit=scan_limit)
    added_nexus = 0
    added_refunds = 0
    # Add nexus_txid processed markers
    for txid, sig in memo_map.get('nexus_txids', {}).items():
        key = f"nexus_txid:{txid}"
        if key in state.processed_nexus_txs:
            continue
        try:
            state.mark_nexus_processed(key, reason="startup_recover_memo")
            added_nexus += 1
        except Exception:
            pass
    # Add refunded sig markers (append to refunded_sigs file if absent)
    for dep_sig, refund_sig in memo_map.get('refund_sigs', {}).items():
        if state.is_refunded(dep_sig):
            continue
        try:
            state.atomic_add_refunded_sig(dep_sig)
            added_refunds += 1
        except Exception:
            pass
    # Quarantined signatures inclusion (if scan_recent_memos later extended) – ignore silently if absent
    found_quarantine = len(memo_map.get('quarantined_sigs', {})) if isinstance(memo_map, dict) else 0
    if found_quarantine:
        for qsig in memo_map.get('quarantined_sigs', {}).keys():
            if qsig in state.processed_sigs:
                continue
            try:
                state.mark_solana_processed(qsig, reason="quarantined_startup")
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
    seeded = seed_reference_counter_if_needed()
    memo_stats = reconstruct_processed_from_memos()
    return {
        'reference_seeded': seeded,
        **memo_stats,
    }
