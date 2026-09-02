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


def _exact_db_integer(value: object, description: str) -> int:
    """Accept only SQLite INTEGER evidence; never truncate REAL/TEXT values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{description} is not stored as an exact integer")
    return value


def _completed_mint_rows(
    waterline_ts: int, conn: sqlite3.Connection | None = None
) -> List[Tuple]:
    """Return durable completed-mint evidence in integer base units only."""
    owns_connection = conn is None
    conn = conn or _db()
    try:
        return conn.execute(
            """
            SELECT sig, timestamp, amount_usdc_units, txid, amount_usdd_units,
                   status, reference, nexus_destination, memo, contract_id
            FROM processed_sigs
            WHERE timestamp >= ? AND status LIKE 'debit_confirmed%'
            ORDER BY timestamp ASC
            """,
            (int(waterline_ts),),
        ).fetchall()
    finally:
        if owns_connection:
            conn.close()


def _validate_mint_row(row: Tuple) -> Tuple[str | None, str | None]:
    """Return (destination, error).  No REAL values are accepted as evidence."""
    sig, _ts, solana_units, txid, nexus_units, _status, reference, destination, memo, contract_id = row
    if not sig:
        return None, "completed mint has no Solana signature"
    if not txid:
        return None, f"completed mint {sig} has no Nexus txid"
    if reference is None or not str(reference).strip():
        return None, f"completed mint {sig} has no durable Nexus reference"
    if not destination:
        return None, f"completed mint {sig} has no durable Nexus destination"
    memo_destination = _extract_nexus_address_from_memo(memo)
    if memo_destination != destination:
        return None, f"completed mint {sig} has missing or mismatched durable memo"
    try:
        solana_units = _exact_db_integer(solana_units, f"completed mint {sig} Solana units")
        nexus_units = _exact_db_integer(nexus_units, f"completed mint {sig} Nexus units")
    except ValueError:
        return None, f"completed mint {sig} has non-integer base-unit evidence"
    if solana_units <= 0 or nexus_units <= 0:
        return None, f"completed mint {sig} has non-positive base-unit evidence"
    try:
        exact_contract_id = _exact_db_integer(contract_id, f"completed mint {sig} Nexus contract id")
    except ValueError:
        return None, f"completed mint {sig} has no exact Nexus contract identity"
    if exact_contract_id < 0:
        return None, f"completed mint {sig} has negative Nexus contract identity"
    # This amount was committed with the debit reference before the Nexus CLI call.
    # Fee policy is mutable, so re-running today's calculation against a historical
    # debit would reject otherwise exact on-chain evidence after a legitimate change.
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
            amount = _exact_db_integer(
                amount_units, f"processed Nexus credit {txid} amount"
            )
        except ValueError:
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
    for sig, ts, solana_units, txid, nexus_units, status, reference, destination, memo, contract_id in mint_rows:
        input_units = int(solana_units)
        output_units = int(nexus_units)
        # The confirmed output is the immutable amount fixed with this debit intent.
        # Do not reprice an historical bridge mint under the current fee configuration.
        expected_units = output_units
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


def _distinct_mint_recipient_accounts(
    waterline_ts: int, completed_rows: List[Tuple] | None = None
) -> Tuple[List[str], List[str]]:
    """Discover recipients solely from valid durable completed-mint records."""
    accounts: set[str] = set()
    incomplete: List[str] = []
    rows = completed_rows if completed_rows is not None else _completed_mint_rows(waterline_ts)
    for row in rows:
        destination, error = _validate_mint_row(row)
        if error:
            incomplete.append(error)
        elif destination:
            accounts.add(destination)
    return sorted(accounts), incomplete


def _active_mint_rows(
    waterline_ts: int, conn: sqlite3.Connection | None = None
) -> List[Tuple]:
    """Read active Solana-to-Nexus debit intents from one optional DB snapshot."""
    owns_connection = conn is None
    conn = conn or _db()
    try:
        return conn.execute(
            """
            SELECT sig, timestamp, memo, amount_usdc_units, amount_usdd_units, status, txid, reference
            FROM unprocessed_sigs
            WHERE timestamp >= ? AND status IN (?, ?, ?)
            ORDER BY timestamp ASC
            """,
            (
                int(waterline_ts),
                "debit in flight",
                "debit unverified",
                "debited, awaiting confirmation",
            ),
        ).fetchall()
    finally:
        if owns_connection:
            conn.close()


def _mint_reconciliation_snapshot(waterline_ts: int) -> Tuple[List[Tuple], List[Tuple]]:
    """Capture completed mints and active debit intents from one SQLite snapshot.

    A confirmation worker moves a row between these tables. Mixing pre- and post-move
    reads would leave its remote debit unmatched and manufacture a false surplus.
    """
    conn = _db()
    try:
        conn.execute("BEGIN")
        completed_rows = _completed_mint_rows(waterline_ts, conn)
        active_rows = _active_mint_rows(waterline_ts, conn)
        conn.rollback()
        return completed_rows, active_rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _active_mint_expectations(
    waterline_ts: int, active_rows: List[Tuple] | None = None
) -> Tuple[List[Dict], List[str]]:
    """Return exact queued debit intents so in-flight work is not called a duplicate."""
    rows = active_rows if active_rows is not None else _active_mint_rows(waterline_ts)

    expectations: List[Dict] = []
    incomplete: List[str] = []
    for sig, _ts, memo, solana_units, nexus_units, status, txid, reference in rows:
        destination = _extract_nexus_address_from_memo(memo)
        if not destination:
            incomplete.append(f"active mint {sig} has missing or malformed Nexus destination memo")
            continue
        if reference is None or not str(reference).strip():
            incomplete.append(f"active mint {sig} has no durable Nexus reference")
            continue
        try:
            _exact_db_integer(solana_units, f"active mint {sig} Solana units")
            output_units = _exact_db_integer(
                nexus_units, f"active mint {sig} Nexus output units"
            )
        except ValueError:
            incomplete.append(f"active mint {sig} has invalid exact base-unit evidence")
            continue
        if output_units <= 0:
            incomplete.append(f"active mint {sig} has invalid Nexus output units")
            continue
        expectations.append({
            "sig": str(sig),
            "status": str(status),
            "txid": str(txid) if txid else None,
            "reference": str(reference).strip(),
            "destination": destination,
            "amount_usdd_units": output_units,
        })
    return expectations, incomplete


def _reconcile_remote_mint_history(
    accounts: List[str], waterline_ts: int,
    completed_rows: List[Tuple] | None = None,
    active_rows: List[Tuple] | None = None,
) -> Tuple[Dict[str, int], List[str]]:
    """Compare local completed/active intents with authoritative Nexus token history."""
    lookup = nexus_client.find_nexus_mint_debits_since(accounts, waterline_ts)
    if not lookup.complete:
        return {}, [
            "remote Nexus mint history incomplete: " + (lookup.reason or "unknown")
        ]
    if not isinstance(lookup.values, dict):
        return {}, ["remote Nexus mint history returned invalid evidence"]

    remote: List[nexus_client.NexusMintDebitEvidence] = []
    for candidates in lookup.values.values():
        if not isinstance(candidates, list) or not all(
            isinstance(item, nexus_client.NexusMintDebitEvidence) for item in candidates
        ):
            return {}, ["remote Nexus mint history returned invalid evidence"]
        remote.extend(candidates)

    consumed: set[tuple[str, int]] = set()
    verified_mint_sources: set[str] = set()
    incomplete: List[str] = []
    completed = completed_rows if completed_rows is not None else _completed_mint_rows(waterline_ts)
    configured_token_register = str(getattr(config, "NEXUS_TOKEN_REGISTER_ADDRESS", "") or "").strip()
    if not configured_token_register:
        return {}, ["configured Nexus token register address is missing"]
    for row in completed:
        sig, _ts, _solana_units, txid, nexus_units, _status, reference, destination, _memo, contract_id = row
        _valid_destination, error = _validate_mint_row(row)
        if error:
            continue  # already reported by _distinct_mint_recipient_accounts
        candidates = lookup.values.get(str(txid), [])
        exact = [
            evidence for evidence in candidates
            if (evidence.remote_txid, evidence.contract_id) not in consumed
            and evidence.from_address == configured_token_register
            and evidence.contract_id == contract_id
            and evidence.to_address == str(destination)
            and evidence.amount_usdd_units == int(nexus_units)
            and evidence.reference == str(reference).strip()
            and evidence.confirmations >= config.get_nexus_transfer_min_confirmations()
        ]
        if len(exact) != 1:
            incomplete.append(
                f"completed mint {sig} has no unique exact confirmed Nexus txid/reference/amount match"
            )
            continue
        consumed.add((exact[0].remote_txid, exact[0].contract_id))
        verified_mint_sources.add(exact[0].from_address)

    active, active_errors = _active_mint_expectations(waterline_ts, active_rows)
    incomplete.extend(active_errors)
    for intent in active:
        exact = [
            evidence for evidence in remote
            if (evidence.remote_txid, evidence.contract_id) not in consumed
            and evidence.from_address in verified_mint_sources
            and evidence.to_address == intent["destination"]
            and evidence.amount_usdd_units == intent["amount_usdd_units"]
            and evidence.reference == intent["reference"]
            and (intent["txid"] is None or evidence.remote_txid == intent["txid"])
        ]
        if exact:
            consumed.add((exact[0].remote_txid, exact[0].contract_id))
        incomplete.append(
            f"active mint {intent['sig']} remains {intent['status']}; remote outcome is not terminal"
        )

    surplus_by_account: Dict[str, int] = {}
    for evidence in remote:
        identity = (evidence.remote_txid, evidence.contract_id)
        if identity in consumed:
            continue
        # finance/transactions/token also contains account-to-account movements.
        # Classify an unmatched DEBIT as a duplicate mint only when its source is the
        # same token-supply register proven by an exact completed local mint.
        if evidence.from_address not in verified_mint_sources:
            continue
        surplus_by_account[evidence.to_address] = (
            surplus_by_account.get(evidence.to_address, 0) + evidence.amount_usdd_units
        )
    return surplus_by_account, incomplete


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
    completed_rows, active_rows = _mint_reconciliation_snapshot(waterline)
    accounts, incomplete = _distinct_mint_recipient_accounts(waterline, completed_rows)
    discrepancy_units: Dict[str, int] = {}
    discrepancies: List[Dict] = []
    account_errors: List[Dict] = []
    checked = 0

    if accounts or active_rows:
        try:
            remote_surplus, remote_incomplete = _reconcile_remote_mint_history(
                accounts, waterline, completed_rows, active_rows
            )
            incomplete.extend(remote_incomplete)
            for account, units in remote_surplus.items():
                discrepancy_units[account] = discrepancy_units.get(account, 0) + int(units)
        except Exception as exc:
            incomplete.append(f"remote Nexus mint history reconciliation failed: {exc}")

    for account in accounts:
        try:
            result = reconcile_account_trades(
                account, waterline, include_remote_balance=include_remote_balance
            )
            checked += 1
            delta = int(result["trade_delta_nexus_units"])
            if delta > 0:
                discrepancy_units[account] = discrepancy_units.get(account, 0) + delta
        except Exception as exc:
            account_errors.append({"account": account, "error": str(exc)})

    for account in sorted(discrepancy_units):
        units = int(discrepancy_units[account])
        if units > 0:
            discrepancies.append({"account": account, "surplus_nexus_units": units})
    total_surplus = sum(item["surplus_nexus_units"] for item in discrepancies)

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
