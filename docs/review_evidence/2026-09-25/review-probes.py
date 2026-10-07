"""Offline review probes for the 2026-09-25 documentation assessment.

These diagnostics use temporary SQLite databases and replace every external chain/send
boundary. A successful process exit means the documented defects were reproduced.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from src import alerts, config, dashboard, nexus_client, solana_client, startup_recovery, state_db


def _empty_solana_scan() -> dict:
    return {
        "complete": True,
        "reason": None,
        "legacy_nexus_txids": {},
        "malformed_nexus_memos": [],
        "nexus_payouts": {},
        "nexus_payout_timestamps": {},
        "refund_sigs": {},
        "quarantined_sigs": {},
        "solana_dispositions": {},
    }


def _remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _row(path: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, params).fetchone()


def partial_restore_probe(root: Path) -> dict:
    db = root / "partial-restore.db"
    source = ("lost-capacity-source", 1_000, "nexus:recipient", "source-token", 1_100)
    original_pair = config.SWAP_PAIR
    with ExitStack() as stack:
        stack.enter_context(patch.object(state_db, "DB_PATH", str(db)))
        stack.enter_context(patch.object(config, "SWAP_PAIR", replace(
            original_pair,
            fees=replace(
                original_pair.fees,
                flat_to_nexus_units=0,
                basis_points=0,
                refund_solana_units=10,
            ),
        )))
        stack.enter_context(patch.object(config, "MIN_DEPOSIT_SOLANA_UNITS", 1))
        stack.enter_context(patch.object(config, "MAX_SWAP_SOLANA_UNITS", 1_000))
        stack.enter_context(patch.object(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 50))
        state_db.init_db()
        state_db.add_unprocessed_sig(*source, "ready for processing", None)
        with patch.object(nexus_client, "is_valid_nexus_token_account", return_value=True), patch.object(
            solana_client, "_resolve_solana_token_destination", return_value="source-token"
        ), patch.object(solana_client, "send_solana_token_to_account_with_sig"):
            solana_client.process_unprocessed_solana_deposits(1, 10)
            solana_client.process_solana_deposits_refunding(1, 10)
        before = {
            "source": _row(
                db,
                "SELECT status, policy_decision, amount_usdc_units FROM unprocessed_sigs WHERE sig=?",
                (source[0],),
            ),
            "capacity": _row(
                db,
                "SELECT kind, needed_units, cap_units FROM solana_payout_capacity_holds WHERE source_signature=?",
                (source[0],),
            ),
            "liability_units": state_db.get_unresolved_solana_liability_units(),
        }

        # Model a stale/partial restore: the obligation is gone, but one unrelated
        # terminal source row survives. That one row is enough to bypass the narrow
        # empty-database latch.
        _remove_sqlite(db)
        state_db.init_db()
        state_db.mark_processed_sig(
            "retained-unrelated-source", 900, 1, None, 0, "processed", None
        )
        config.MAX_SWAP_SOLANA_UNITS = 2_000
        config.DAILY_PAYOUT_CAP_SOLANA_UNITS = 10_000
        heartbeat = {
            "last_safe_timestamp_nexus": "1",
            "last_safe_timestamp_solana": "900",
        }
        with patch.object(nexus_client, "get_heartbeat_asset", return_value=heartbeat), patch.object(
            solana_client, "scan_memos_since_timestamp", side_effect=lambda _ts: _empty_solana_scan()
        ), patch.object(
            nexus_client,
            "fetch_deposits_since",
            return_value=nexus_client.DepositScan([], True, None),
        ), patch.object(nexus_client, "get_last_reference", return_value=0):
            recovery = startup_recovery.perform_startup_recovery()

        admitted = state_db.commit_solana_deposit_scan_page(
            vault_account="vault",
            mint="mint",
            network="mainnet",
            commitment="finalized",
            query_identity="partial-restore-replay",
            lower_timestamp=900,
            request_before_signature=None,
            next_before_signature=None,
            upper_timestamp=1_900,
            previous_timestamp=None,
            page_last_timestamp=source[1],
            scanned_signature_count=1,
            deposits=[source],
            complete=True,
        )
        debit = Mock(return_value=(True, "changed-policy-debit"))
        with patch.object(nexus_client, "is_valid_nexus_token_account", return_value=True), patch.object(
            nexus_client, "debit_nexus_token_with_txid", debit
        ):
            worker = solana_client.process_unprocessed_solana_deposits(1, 10)
        result = {
            "before_loss": before,
            "after_partial_restore": {
                "startup_recovery_complete": recovery.get("recovery_complete"),
                "startup_recovery_error": recovery.get("error"),
                "admission_latch_rows": _row(
                    db, "SELECT COUNT(*) FROM recovery_admission_holds"
                )[0],
                "source_re_admitted": admitted,
                "deposit_worker": worker,
                "nexus_send_calls": debit.call_count,
                "nexus_send_amount": debit.call_args.args[1] if debit.call_args else None,
            },
        }
        assert result["after_partial_restore"] == {
            "startup_recovery_complete": True,
            "startup_recovery_error": None,
            "admission_latch_rows": 0,
            "source_re_admitted": 1,
            "deposit_worker": [1, 0, 0, 0],
            "nexus_send_calls": 1,
            "nexus_send_amount": 1_100,
        }
        return result


def malformed_oldest_probe(root: Path) -> dict:
    db = root / "malformed-oldest.db"
    original_pair = config.SWAP_PAIR
    pair = replace(
        original_pair,
        fees=replace(original_pair.fees, refund_solana_units=10),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(state_db, "DB_PATH", str(db)))
        stack.enter_context(patch.object(config, "SWAP_PAIR", pair))
        stack.enter_context(patch.object(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 100))
        stack.enter_context(patch.object(
            solana_client, "_resolve_solana_token_destination", return_value="destination-token"
        ))
        send = stack.enter_context(patch.object(
            solana_client,
            "send_solana_token_to_account_with_sig",
            return_value=(True, "must-not-send"),
        ))
        stack.enter_context(patch.object(alerts, "warning"))
        stack.enter_context(patch.object(alerts, "critical"))
        state_db.init_db()
        with patch.object(state_db.time, "time", return_value=1_000):
            assert state_db.reserve_solana_payout_budget(
                obligation_id="blocking", kind="nexus_payout",
                amount_usdc_units=60, cap_units=100,
            )
            state_db.add_unprocessed_sig(
                "old-malformed", 10, "source-memo", "sender", 60,
                "to be refunded", None,
            )
            assert solana_client.process_solana_deposits_refunding(100, 10) == 0
        with patch.object(state_db.time, "time", return_value=1_001):
            state_db.add_unprocessed_sig(
                "young-valid", 11, "source-memo", "sender", 60,
                "to be refunded", None,
            )
            assert solana_client.process_solana_deposits_refunding(100, 10) == 0
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE solana_payout_capacity_holds SET intent_evidence = '{' "
                "WHERE source_signature = 'old-malformed'"
            )
        with patch.object(state_db.time, "time", return_value=1_002):
            assert state_db.release_solana_payout_budget(
                "blocking", "known-unsent test blocker"
            )
            workers = [
                solana_client.process_solana_deposits_refunding(100, 10),
                solana_client.process_solana_deposits_refunding(100, 10),
            ]
        with sqlite3.connect(db) as conn:
            holds = conn.execute(
                "SELECT source_signature, reason, attempt_count "
                "FROM solana_payout_capacity_holds "
                "ORDER BY first_held_timestamp, source_signature"
            ).fetchall()
            sources = conn.execute(
                "SELECT sig, status, amount_usdc_units FROM unprocessed_sigs ORDER BY timestamp"
            ).fetchall()
        result = {
            "workers_after_release": workers,
            "send_calls": send.call_count,
            "liability_units": state_db.get_unresolved_solana_liability_units(),
            "holds": holds,
            "sources": sources,
        }
        assert result["workers_after_release"] == [0, 0]
        assert result["send_calls"] == 0
        assert result["liability_units"] == 120
        assert next(row for row in sources if row[0] == "young-valid")[1] == "refund capacity held"
        return result


def dashboard_failure_probe(root: Path) -> dict:
    db = root / "dashboard-recovery-failure.db"
    with patch.object(state_db, "DB_PATH", str(db)):
        state_db.init_db()
        state_db.save_metrics_snapshot(
            vault_usdc_units=200, circulating_usdd_units=100, paused=False,
            payouts_24h_units=0, fees_usdc_units=0, fees_usdd_units=0,
        )
        with patch.object(nexus_client, "get_heartbeat_asset", return_value={}):
            recovery = startup_recovery.perform_startup_recovery()
        summary = dashboard.api_summary()
        issues = dashboard.api_issues()
        result = {
            "startup_recovery_complete": recovery.get("recovery_complete"),
            "startup_recovery_error": recovery.get("error"),
            "dashboard_admission": summary["recovery_admission"],
            "dashboard_ratio_bps": summary["ratio_bps"],
            "dashboard_issue_count": issues["counts"]["issues"],
        }
        assert result == {
            "startup_recovery_complete": False,
            "startup_recovery_error": "heartbeat_missing",
            "dashboard_admission": {"status": "not_held", "liabilities_complete": None},
            "dashboard_ratio_bps": 20000,
            "dashboard_issue_count": 0,
        }
        return result


def dashboard_read_only_probe(root: Path) -> dict:
    db = root / "dashboard-missing.db"
    with patch.object(state_db, "DB_PATH", str(db)):
        assert not db.exists()
        summary = dashboard.api_summary()
        result = {
            "database_created_by_summary_read": db.exists(),
            "admission": summary["recovery_admission"],
        }
        assert result["database_created_by_summary_read"] is True
        assert result["admission"]["status"] == "unknown"
        return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="swap-review-2026-09-25-") as tmp:
        root = Path(tmp)
        output = {
            "partial_restore": partial_restore_probe(root),
            "malformed_oldest": malformed_oldest_probe(root),
            "dashboard_failed_recovery": dashboard_failure_probe(root),
            "dashboard_read_only": dashboard_read_only_probe(root),
        }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
