"""Trade reconciliation between treasury USDD account and a specified user USDD account.

Objective
---------
After a given waterline timestamp (e.g. service last consistent checkpoint) compute the net
USDD value flow for an account participating in both swap directions and detect any surplus
or deficit (should be 0 if accounting is correct, ignoring in-flight, unconfirmed items).

Requested Components (interpreted):
 1. Credits to USDD from the account address (user -> treasury) representing USDD->USDC swaps
    (i.e. processed txids where from_address = user and to_address = treasury; or processed_txids
    recorded as such after confirmation).
 2. Debits from treasury to the account address (treasury -> user) representing completed
    USDC->USDD swaps or explicit refunds (processed_sigs joined to unprocessed_sigs on sig with memo nexus:<user>).
 3. Solana USDC deposit signatures (to vault) whose memo resolves to the user's Nexus address
    (from unprocessed_sigs & processed_sigs) – used to recompute expected minted USDD net of fees.
 4. Non-refunded deposit signatures (exclude any appearing in refunded_sigs) for that user.

We then derive:
    net_treasury_out = treasury_debits_to_user_usdd
    net_treasury_in  = user_credits_to_treasury_usdd
    swap_net_expected = net_minted_usdd_for_user (from USDC deposits net fees)
    trade_delta = (net_treasury_out - net_treasury_in) - swap_net_expected

Interpretation:
  trade_delta ~ 0  => balanced.
  trade_delta > 0  => treasury appears to have sent more USDD than covered by user deposits.
  trade_delta < 0  => user has over-contributed (or deposits not yet minted/refunded).

Assumptions / Notes:
 - processed_sigs lacks direct Nexus address; we recover it via the memo stored in unprocessed_sigs (format nexus:<addr>).
 - amount_usdd in processed_sigs is treated as net USDD delivered; if null we recompute using fee schedule.
 - For simplicity we ignore partially processed / awaiting confirmation statuses; only statuses starting
   with 'debit_confirmed' or equal to 'processed' are regarded as minted.
 - 'Refunds' on USDC side (returning USDC) reduce the effective deposit base automatically because
   the refunded sig won't appear with a minted status.
 - This module does NOT mutate state; it only reads DB and remote balances (optional).
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Iterable
from decimal import Decimal
import sqlite3
from . import config, state_db, nexus_client

ELIGIBLE_SIG_STATUSES = ("debit_confirmed", "processed")

# ---------------------------------------------------------------------------
# Low-level DB helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    return sqlite3.connect(state_db.DB_PATH)


def _extract_nexus_address_from_memo(memo: str | None) -> str | None:
    if not memo:
        return None
    if memo.lower().startswith("nexus:"):
        addr = memo.split(":", 1)[1].strip()
        return addr or None
    return None


def _fee_net_usdd(amount_usdc_units: int) -> int:
    flat_fee = max(0, int(getattr(config, 'FLAT_FEE_USDC_UNITS', 0)))
    dynamic_bps = max(0, int(getattr(config, 'DYNAMIC_FEE_BPS', 0)))
    pre_dynamic = max(0, amount_usdc_units - flat_fee)
    dynamic_fee = (pre_dynamic * dynamic_bps) // 10_000
    net_usdd = max(0, amount_usdc_units - (flat_fee + dynamic_fee)) * (10 ** config.USDC_DECIMALS)
    return net_usdd


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def _fetch_processed_sigs_for_account(usdd_account: str, waterline_ts: int) -> List[Tuple[str, int, int | None, str | None, float | None, str | None, int | None]]:
    """Join processed_sigs with unprocessed_sigs on sig to recover memos and filter for account."""
    q = """
        SELECT ps.sig, ps.timestamp, ps.amount_usdc_units, ps.txid, ps.amount_usdd, ps.status, ps.reference,
               us.memo
        FROM processed_sigs ps
        LEFT JOIN unprocessed_sigs us ON us.sig = ps.sig
        WHERE ps.timestamp >= ? AND ps.status IS NOT NULL
        ORDER BY ps.timestamp ASC
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute(q, (waterline_ts,))
    rows = cur.fetchall()
    conn.close()
    # Filter in python for matching memo nexus:usdd_account
    filtered: List[Tuple[str, int, int | None, str | None, float | None, str | None, int | None]] = []
    for sig, ts, amt_usdc, txid, amt_usdd, status, ref, memo in rows:
        addr = _extract_nexus_address_from_memo(memo)
        if addr == usdd_account and status and (status.lower().startswith(ELIGIBLE_SIG_STATUSES[0]) or status.lower() in ELIGIBLE_SIG_STATUSES):
            filtered.append((sig, ts, amt_usdc, txid, amt_usdd, status, ref))
    return filtered


def _fetch_processed_txids_for_account(usdd_account: str, treasury: str, waterline_ts: int) -> Tuple[int, int]:
    """Return (credits_from_account_to_treasury_usdd, debits_from_treasury_to_account_usdd) from processed_txids."""
    q = """
        SELECT timestamp, amount_usdd, from_address, to_address
        FROM processed_txids
        WHERE timestamp >= ? AND from_address IS NOT NULL AND to_address IS NOT NULL
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute(q, (waterline_ts,))
    credits = 0  # account -> treasury
    debits = 0   # treasury -> account
    for ts, amt_usdd, from_addr, to_addr in cur.fetchall():
        try:
            amt = int(amt_usdd or 0)
        except Exception:
            amt = 0
        if from_addr == usdd_account and to_addr == treasury:
            credits += amt
        elif from_addr == treasury and to_addr == usdd_account:
            debits += amt
    conn.close()
    return credits, debits


def _fetch_refunded_sigs_for_account(usdd_account: str, waterline_ts: int) -> set[str]:
    q = """
        SELECT sig, timestamp, from_address, memo FROM refunded_sigs WHERE timestamp >= ?
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute(q, (waterline_ts,))
    refunded = set()
    for sig, ts, from_addr, memo in cur.fetchall():
        if not memo:
            continue
        addr = _extract_nexus_address_from_memo(memo)
        if addr == usdd_account:
            refunded.add(sig)
    conn.close()
    return refunded


def _fetch_deposit_sigs_for_account(usdd_account: str, waterline_ts: int) -> List[Tuple[str, int, int]]:
    """All Solana deposit signatures (unprocessed + processed) after waterline that target the account via memo.
    Return list of (sig, timestamp, amount_usdc_units)."""
    q = """
        SELECT sig, timestamp, memo, amount_usdc_units FROM unprocessed_sigs WHERE timestamp >= ?
        UNION ALL
        SELECT ps.sig, ps.timestamp, us.memo, ps.amount_usdc_units
        FROM processed_sigs ps
        LEFT JOIN unprocessed_sigs us ON us.sig = ps.sig
        WHERE ps.timestamp >= ?
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute(q, (waterline_ts, waterline_ts))
    out: List[Tuple[str, int, int]] = []
    for sig, ts, memo, amt in cur.fetchall():
        addr = _extract_nexus_address_from_memo(memo)
        if addr == usdd_account:
            try:
                out.append((sig, int(ts), int(amt or 0)))
            except Exception:
                continue
    conn.close()
    return out


def reconcile_account_trades(usdd_account: str, waterline_ts: int, include_remote_balance: bool = False) -> Dict:
    treasury = getattr(config, 'NEXUS_USDD_TREASURY_ACCOUNT', None)
    if not treasury:
        raise ValueError("NEXUS_USDD_TREASURY_ACCOUNT not configured")

    processed_sigs_rows = _fetch_processed_sigs_for_account(usdd_account, waterline_ts)
    credits_from_acct_txids, debits_to_acct_txids = _fetch_processed_txids_for_account(usdd_account, treasury, waterline_ts)
    deposit_sigs = _fetch_deposit_sigs_for_account(usdd_account, waterline_ts)
    refunded_sig_set = _fetch_refunded_sigs_for_account(usdd_account, waterline_ts)

    # Net minted USDD for processed sigs
    minted_usdd = 0
    details_processed: List[Dict] = []
    for sig, ts, amt_usdc, txid, amt_usdd, status, ref in processed_sigs_rows:
        if amt_usdd is not None:
            net_usdd = int(amt_usdd)
        else:
            net_usdd = _fee_net_usdd(int(amt_usdc or 0))
        minted_usdd += net_usdd
        details_processed.append({
            'sig': sig,
            'ts': ts,
            'amount_usdc_units': amt_usdc,
            'net_usdd': net_usdd,
            'txid': txid,
            'status': status,
            'reference': ref,
        })

    # Expected net from deposit signatures (exclude those refunded)
    expected_net_from_deposits = 0
    non_refunded_deposits_usdc = 0
    for sig, ts, amt_usdc in deposit_sigs:
        if sig in refunded_sig_set:
            continue
        non_refunded_deposits_usdc += amt_usdc
        expected_net_from_deposits += _fee_net_usdd(amt_usdc)

    # Compose USDD flow summary
    # treasury_out = minted_usdd (swaps) + debits_to_acct_txids (generic processed_txids from treasury)
    treasury_out = minted_usdd + debits_to_acct_txids
    treasury_in = credits_from_acct_txids  # user -> treasury

    # trade_delta definition
    trade_delta = (treasury_out - treasury_in) - expected_net_from_deposits

    remote_balance = None
    if include_remote_balance:
        try:
            acct_info = nexus_client.get_account_info(usdd_account)
            if acct_info and isinstance(acct_info, dict):
                bal = acct_info.get('balance')
                if bal is None and isinstance(acct_info.get('result'), dict):
                    bal = acct_info['result'].get('balance')
                if bal is not None:
                    remote_balance = int(Decimal(str(bal)))
        except Exception:
            remote_balance = None

    return {
        'account': usdd_account,
        'waterline_ts': waterline_ts,
        'minted_usdd': minted_usdd,
        'treasury_out_usdd': treasury_out,
        'treasury_in_usdd': treasury_in,
        'expected_net_from_deposits_usdd': expected_net_from_deposits,
        'non_refunded_deposits_usdc_units': non_refunded_deposits_usdc,
        'processed_sig_count': len(details_processed),
        'deposit_sig_count': len(deposit_sigs),
        'refunded_sig_count': len(refunded_sig_set),
        'trade_delta_usdd': trade_delta,
        'remote_balance_usdd': remote_balance,
        'processed_sigs': details_processed[:50],  # cap for readability
    }


def print_account_reconciliation(summary: Dict):
    acct = summary['account']
    delta = summary['trade_delta_usdd']
    print(f"[reconcile] account={acct} minted={summary['minted_usdd']} treas_out={summary['treasury_out_usdd']} treas_in={summary['treasury_in_usdd']} expected_net_deposits={summary['expected_net_from_deposits_usdd']} delta={delta}")
    if summary.get('remote_balance_usdd') is not None:
        print(f"[reconcile] remote_balance={summary['remote_balance_usdd']}")
    if delta != 0:
        print(f"[reconcile] WARNING non-zero trade delta (possible imbalance or in-flight operations)")


def reconcile_multiple(accounts: Iterable[str], waterline_ts: int, include_remote_balance: bool = False) -> List[Dict]:
    results = []
    for acct in accounts:
        try:
            res = reconcile_account_trades(acct, waterline_ts, include_remote_balance=include_remote_balance)
            print_account_reconciliation(res)
            results.append(res)
        except Exception as e:
            print(f"[reconcile] error for {acct}: {e}")
    return results


def run_single(account: str, waterline_ts: int, include_remote_balance: bool = False) -> Dict:
    res = reconcile_account_trades(account, waterline_ts, include_remote_balance=include_remote_balance)
    print_account_reconciliation(res)
    return res
