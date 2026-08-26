"""Fail-closed reconciliation for completed Solana-to-Nexus mints.

Completed mint rows are the source of truth.  In particular, this module never joins a
completed row back to ``unprocessed_sigs``: that queue row is intentionally deleted after
confirmation and its absence is evidence loss, not evidence of a zero balance.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Tuple

from . import config, nexus_client, state_db

ELIGIBLE_SIG_STATUS_PREFIX = "debit_confirmed"


def _db() -> sqlite3.Connection:
    return sqlite3.connect(state_db.DB_PATH)


def _extract_nexus_address_from_memo(memo: str | None) -> str | None:
    if not memo:
        return None
    prefix = str(getattr(config, "DEPOSIT_MEMO_PREFIX", "nexus:"))
    value = str(memo)
    if value.lower().startswith(prefix.lower()):
        return value[len(prefix):].strip() or None
    return None


def _is_completed_mint_status(status: object) -> bool:
    return isinstance(status, str) and status.lower().startswith(ELIGIBLE_SIG_STATUS_PREFIX)


def _completed_mint_rows(waterline_ts: int) -> List[Tuple]:
    """Return durable completed-mint evidence in integer base units only."""
    conn = _db()
    try:
        return conn.execute(
            """
            SELECT sig, timestamp, amount_usdc_units, txid, amount_usdd_units,
                   status, reference, nexus_destination, memo
            FROM processed_sigs
            WHERE timestamp >= ? AND status LIKE 'debit_confirmed%'
            ORDER BY timestamp ASC
            """,
            (int(waterline_ts),),
        ).fetchall()
    finally:
        conn.close()


def _validate_mint_row(row: Tuple) -> Tuple[str | None, str | None]:
    """Return (destination, error).  No REAL values are accepted as evidence."""
    sig, _ts, solana_units, txid, nexus_units, _status, _reference, destination, memo = row
    if not sig:
        return None, "completed mint has no Solana signature"
    if not txid:
        return None, f"completed mint {sig} has no Nexus txid"
    if not destination:
        return None, f"completed mint {sig} has no durable Nexus destination"
    memo_destination = _extract_nexus_address_from_memo(memo)
    if memo_destination != destination:
        return None, f"completed mint {sig} has missing or mismatched durable memo"
    try:
        solana_units = int(solana_units)
        nexus_units = int(nexus_units)
    except (TypeError, ValueError):
        return None, f"completed mint {sig} has non-integer base-unit evidence"
    if solana_units < 0 or nexus_units < 0:
        return None, f"completed mint {sig} has negative base-unit evidence"
    expected = nexus_client.get_nexus_send_amount_units(solana_units)
    if nexus_units != expected:
        return None, (
            f"completed mint {sig} output {nexus_units} does not match production "
            f"fee calculation {expected}"
        )
    return str(destination), None


def _fetch_processed_sigs_for_account(nexus_account: str, waterline_ts: int) -> List[Tuple]:
    """Read durable completed-mint evidence for one Nexus recipient."""
    rows = _completed_mint_rows(waterline_ts)
    matching: List[Tuple] = []
    for row in rows:
        destination, error = _validate_mint_row(row)
        if error:
            raise ValueError(error)
        if destination == nexus_account:
            matching.append(row)
    return matching


def _fetch_processed_txids_for_account(
    nexus_account: str, treasury: str, waterline_ts: int
) -> Tuple[int, int]:
    """Return (credits account->treasury, debits treasury->account) in exact units.

    A legacy REAL-only row affecting this account makes reconciliation incomplete rather
    than being truncated or rounded to manufacture a green result.
    """
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT txid, amount_usdd_units, from_address, to_address
            FROM processed_txids
            WHERE timestamp >= ? AND from_address IS NOT NULL AND to_address IS NOT NULL
            """,
            (int(waterline_ts),),
        ).fetchall()
    finally:
        conn.close()

    credits = debits = 0
    for txid, amount_units, from_addr, to_addr in rows:
        relevant = ((from_addr == nexus_account and to_addr == treasury) or
                    (from_addr == treasury and to_addr == nexus_account))
        if not relevant:
            continue
        try:
            amount = int(amount_units)
        except (TypeError, ValueError):
            raise ValueError(f"processed Nexus credit {txid} lacks exact base-unit amount")
        if amount < 0:
            raise ValueError(f"processed Nexus credit {txid} has negative base-unit amount")
        if from_addr == nexus_account:
            credits += amount
        else:
            debits += amount
    return credits, debits


def reconcile_account_trades(
    nexus_account: str, waterline_ts: int, include_remote_balance: bool = False
) -> Dict:
    treasury = getattr(config, "NEXUS_USDD_TREASURY_ACCOUNT", None)
    if not treasury:
        raise ValueError("NEXUS_USDD_TREASURY_ACCOUNT not configured")

    mint_rows = _fetch_processed_sigs_for_account(nexus_account, waterline_ts)
    credits, external_debits = _fetch_processed_txids_for_account(
        nexus_account, treasury, waterline_ts
    )

    minted = expected_from_deposits = 0
    details: List[Dict] = []
    for sig, ts, solana_units, txid, nexus_units, status, reference, destination, memo in mint_rows:
        input_units = int(solana_units)
        output_units = int(nexus_units)
        expected_units = nexus_client.get_nexus_send_amount_units(input_units)
        # _validate_mint_row already checked this; retain the invariant locally so this
        # function stays safe if its data source changes.
        if output_units != expected_units:
            raise ValueError(f"completed mint {sig} fails production-fee reconciliation")
        minted += output_units
        expected_from_deposits += expected_units
        details.append({
            "sig": sig,
            "ts": int(ts),
            "amount_usdc_units": input_units,
            "net_nexus_units": output_units,
            "txid": txid,
            "status": status,
            "reference": reference,
            "nexus_destination": destination,
            "memo": memo,
        })

    treasury_out = minted + external_debits
    treasury_in = credits
    trade_delta = (treasury_out - treasury_in) - expected_from_deposits

    remote_balance = None
    remote_error = None
    if include_remote_balance:
        try:
            account_info = nexus_client.get_account_info(nexus_account)
            if not isinstance(account_info, dict):
                raise ValueError("Nexus account lookup returned no object")
            balance = account_info.get("balance")
            if balance is None and isinstance(account_info.get("result"), dict):
                balance = account_info["result"].get("balance")
            if balance is None:
                raise ValueError("Nexus account lookup has no balance")
            # A remote API balance may be token-formatted, so it is display-only and
            # deliberately excluded from the base-unit delta calculation.
            remote_balance = str(balance)
        except Exception as exc:
            remote_error = str(exc)

    return {
        "account": nexus_account,
        "waterline_ts": int(waterline_ts),
        "minted_nexus_units": minted,
        "treasury_out_nexus_units": treasury_out,
        "treasury_in_nexus_units": treasury_in,
        "expected_net_from_deposits_nexus_units": expected_from_deposits,
        "processed_sig_count": len(details),
        "trade_delta_nexus_units": trade_delta,
        "remote_balance_nexus": remote_balance,
        "remote_balance_error": remote_error,
        "processed_sigs": details[:50],
    }


def print_account_reconciliation(summary: Dict) -> None:
    print(
        "[reconcile] account={account} minted={minted_nexus_units} "
        "treas_out={treasury_out_nexus_units} treas_in={treasury_in_nexus_units} "
        "expected={expected_net_from_deposits_nexus_units} "
        "delta={trade_delta_nexus_units}".format(**summary)
    )
    if summary.get("remote_balance_error"):
        print(f"[reconcile] remote balance incomplete: {summary['remote_balance_error']}")
    elif summary.get("remote_balance_nexus") is not None:
        print(f"[reconcile] remote_balance={summary['remote_balance_nexus']}")
    if summary["trade_delta_nexus_units"] != 0:
        print("[reconcile] WARNING non-zero trade delta (possible double mint or incomplete evidence)")


def reconcile_multiple(
    accounts: Iterable[str], waterline_ts: int, include_remote_balance: bool = False
) -> List[Dict]:
    results = []
    for account in accounts:
        result = reconcile_account_trades(account, waterline_ts, include_remote_balance)
        print_account_reconciliation(result)
        results.append(result)
    return results


def run_single(account: str, waterline_ts: int, include_remote_balance: bool = False) -> Dict:
    result = reconcile_account_trades(account, waterline_ts, include_remote_balance)
    print_account_reconciliation(result)
    return result


def _distinct_mint_recipient_accounts(waterline_ts: int) -> Tuple[List[str], List[str]]:
    """Discover recipients solely from valid durable completed-mint records."""
    accounts: set[str] = set()
    incomplete: List[str] = []
    for row in _completed_mint_rows(waterline_ts):
        destination, error = _validate_mint_row(row)
        if error:
            incomplete.append(error)
        elif destination:
            accounts.add(destination)
    return sorted(accounts), incomplete


def run_balance_reconciliation(
    dry_run: bool = True,
    waterline_ts: int | None = None,
    include_remote_balance: bool = False,
) -> Dict:
    """Run a read-only, fail-closed double-mint reconciliation.

    ``healthy`` is false if no mint recipient was checked, if any durable evidence is
    missing/malformed, or if any account calculation fails.  Callers must treat an
    unhealthy result as an operational safety event, separately from a confirmed surplus.
    """
    waterline = int(waterline_ts or 0)
    accounts, incomplete = _distinct_mint_recipient_accounts(waterline)
    discrepancies: List[Dict] = []
    account_errors: List[Dict] = []
    total_surplus = 0
    checked = 0

    for account in accounts:
        try:
            result = reconcile_account_trades(
                account, waterline, include_remote_balance=include_remote_balance
            )
            checked += 1
            delta = int(result["trade_delta_nexus_units"])
            if delta > 0:
                discrepancies.append({"account": account, "surplus_nexus_units": delta})
                total_surplus += delta
        except Exception as exc:
            account_errors.append({"account": account, "error": str(exc)})

    if checked == 0:
        incomplete.append("no completed mint recipients were checked")
    if account_errors:
        incomplete.append("one or more recipient calculations failed")

    healthy = not incomplete and not discrepancies
    return {
        "dry_run": dry_run,
        "waterline_ts": waterline,
        "healthy": healthy,
        "checked_addresses": checked,
        "discrepancies": discrepancies,
        "total_surplus_nexus_units": total_surplus,
        # Compatibility aliases for existing dashboard/callers; values remain integers.
        "total_surplus_nexus": total_surplus,
        "incomplete_reasons": incomplete,
        "account_errors": account_errors,
    }
