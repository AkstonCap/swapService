#!/usr/bin/env python3
"""Regression tests for the 2026-08-24 Critical fund-safety findings."""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub(name, **attrs):
    module = type(sys)(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class _PublicKey:
    @staticmethod
    def from_string(value):
        return value

    @staticmethod
    def find_program_address(seeds, program_id):
        return ("ATA", 0)

    def __init__(self, *args):
        pass


_stub("solana")
_stub("solana.rpc")
_stub("solana.rpc.api", Client=lambda *args, **kwargs: None)
_stub("solders")
_stub("solders.pubkey", Pubkey=_PublicKey)
_stub("solders.keypair", Keypair=object)
_stub("solders.signature", Signature=_PublicKey)
_stub("solders.hash", Hash=object)
_stub("solders.instruction", Instruction=object, AccountMeta=object)
_stub("solders.transaction", Transaction=object, VersionedTransaction=object)
_stub("solders.message", Message=object)
_stub("requests", post=lambda *args, **kwargs: None, get=lambda *args, **kwargs: None)
_stub("dotenv", load_dotenv=lambda *args, **kwargs: None)

os.environ.setdefault("SOLANA_RPC_URL", "http://127.0.0.1:8899")
os.environ.setdefault("VAULT_KEYPAIR", "/tmp/nonexistent-keypair.json")
os.environ.setdefault("VAULT_USDC_ACCOUNT", "VAULT")
os.environ.setdefault("USDC_MINT", "MINT")
os.environ.setdefault("SOL_MINT", "SOL")
os.environ.setdefault("NEXUS_PIN", "1234")
os.environ.setdefault("NEXUS_USDD_TREASURY_ACCOUNT", "TREASURY")
os.environ.setdefault("SOL_MAIN_ACCOUNT", "OWNER")
os.environ.setdefault("NEXUS_CLI_PATH", "/bin/false")

from src import balance_reconciler, fees, nexus_client, solana_client, state_db, swap_nexus  # noqa: E402


class CriticalSafetyTests(unittest.TestCase):
    @patch.object(nexus_client.state_db, "update_unprocessed_sig_status")
    @patch.object(nexus_client.state_db, "filter_unprocessed_sigs")
    @patch.object(nexus_client, "_run")
    @patch.object(nexus_client.time, "time", return_value=2_000)
    def test_failed_confirmation_lookup_never_authorizes_refund(
        self, _time, run, pending_rows, update_status
    ):
        pending_rows.return_value = [
            ("deposit-sig", 1_000, "nexus:user", "sender", 2_000_000,
             "debited, awaiting confirmation", "nexus-txid")
        ]
        run.return_value = (1, "", "node unavailable")

        processed = nexus_client.check_unconfirmed_debits(
            min_confirmations=2, timeout=1
        )

        self.assertEqual(processed, 0)
        update_status.assert_not_called()

    @patch.object(nexus_client.state_db, "get_attempt_last_timestamp", return_value=1_000)
    @patch.object(nexus_client.state_db, "update_unprocessed_sig_status")
    @patch.object(nexus_client.state_db, "filter_unprocessed_sigs")
    @patch.object(nexus_client, "_run", return_value=(0, "[]", ""))
    @patch.object(nexus_client.time, "time", return_value=2_000)
    def test_empty_confirmation_scan_never_authorizes_refund(
        self, _time, _run, pending_rows, update_status, _attempted_at
    ):
        pending_rows.return_value = [
            ("deposit-sig", 1_000, "nexus:user", "sender", 2_000_000,
             "debited, awaiting confirmation", "missing-txid")
        ]

        processed = nexus_client.check_unconfirmed_debits(2, 1)

        self.assertEqual(processed, 0)
        update_status.assert_not_called()

    @patch.object(nexus_client.state_db, "update_unprocessed_sig_status")
    @patch.object(nexus_client.state_db, "filter_unprocessed_sigs")
    @patch.object(nexus_client.state_db, "get_attempt_last_timestamp", return_value=1_000)
    @patch.object(nexus_client.time, "time", return_value=2_000)
    def test_missing_txid_never_authorizes_refund(
        self, _time, _attempted_at, pending_rows, update_status
    ):
        pending_rows.return_value = [
            ("deposit-sig", 1_000, "nexus:user", "sender", 2_000_000,
             "debited, awaiting confirmation", None)
        ]

        processed = nexus_client.check_unconfirmed_debits(2, 1)

        self.assertEqual(processed, 0)
        update_status.assert_not_called()

    @patch.object(nexus_client.state_db, "release_reservation")
    @patch.object(nexus_client.state_db, "get_attempt_count", return_value=1)
    @patch.object(nexus_client.state_db, "get_attempt_last_timestamp", return_value=1_000)
    @patch.object(nexus_client.state_db, "update_unprocessed_sig_status")
    @patch.object(nexus_client.state_db, "get_sigs_pending_debit_verification")
    @patch.object(nexus_client, "_run")
    @patch.object(nexus_client.time, "time", return_value=2_000)
    def test_failed_reference_lookup_never_authorizes_retry_or_refund(
        self, _time, run, pending_rows, update_status, _attempted_at,
        _attempt_count, _release
    ):
        pending_rows.return_value = [
            ("deposit-sig", 1_000, "nexus:user", "sender", 2_000_000,
             "debit unverified", None, 77)
        ]
        run.return_value = (1, "", "node unavailable")

        resolved = nexus_client.resolve_unverified_debits()

        self.assertEqual(resolved, 0)
        update_status.assert_not_called()

    @patch.object(nexus_client.state_db, "release_reservation")
    @patch.object(nexus_client.state_db, "get_attempt_count", return_value=1)
    @patch.object(nexus_client.state_db, "get_attempt_last_timestamp", return_value=1_000)
    @patch.object(nexus_client.state_db, "update_unprocessed_sig_status")
    @patch.object(nexus_client.state_db, "get_sigs_pending_debit_verification")
    @patch.object(nexus_client, "_run", return_value=(0, "[]", ""))
    @patch.object(nexus_client.time, "time", return_value=2_000)
    def test_empty_reference_scan_never_authorizes_retry_or_refund(
        self, _time, _run, pending_rows, update_status, _attempted_at,
        _attempt_count, _release
    ):
        pending_rows.return_value = [
            ("deposit-sig", 1_000, "nexus:user", "sender", 2_000_000,
             "debit unverified", None, 77)
        ]

        resolved = nexus_client.resolve_unverified_debits()

        self.assertEqual(resolved, 0)
        update_status.assert_not_called()

    @patch.object(nexus_client, "_run", return_value=(1, "", "node down"))
    def test_failed_receival_asset_lookup_is_incomplete(self, _run):
        lookup = nexus_client.find_asset_receival_account_by_txid_and_owner(
            "txid", "owner"
        )

        self.assertFalse(lookup.complete)
        self.assertIsNone(lookup.asset)

    @patch.object(
        nexus_client,
        "_run",
        return_value=(
            0,
            json.dumps({"txid_toService": "txid", "receival_account": "solana"}),
            "",
        ),
    )
    def test_malformed_receival_asset_lookup_is_incomplete(self, _run):
        lookup = nexus_client.find_asset_receival_account_by_txid_and_owner(
            "txid", "owner"
        )

        self.assertFalse(lookup.complete)
        self.assertIsNone(lookup.asset)

    @patch.object(swap_nexus.state_db, "update_unprocessed_txid")
    @patch.object(swap_nexus.nexus_client, "refund_nexus_token")
    @patch.object(
        swap_nexus.nexus_client,
        "find_asset_receival_account_by_txid_and_owner",
        return_value=nexus_client.AssetLookup(None, False, "cli_error"),
    )
    @patch.object(
        swap_nexus.state_db,
        "get_unprocessed_txids_as_dicts",
        return_value=[{
            "txid": "credit-tx", "ts": 1, "comment": swap_nexus.NEXUS_STATUS_PENDING,
            "confirmations": 2, "owner": "owner", "from": "sender",
            "amount_usdd_units": 1_000_000,
        }],
    )
    def test_incomplete_receival_lookup_cannot_trigger_timeout_refund(
        self, _rows, _lookup, refund, update_txid
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                swap_nexus.time, "time", return_value=10_000
            ), patch.object(swap_nexus, "_log") as log:
                state_db.init_db()
                swap_nexus.process_unprocessed_txids()

        refund.assert_not_called()
        update_txid.assert_not_called()
        self.assertFalse(any(call.args and call.args[0] == "NEXUS_PROCESS_ERROR" for call in log.call_args_list))

    @patch.object(swap_nexus.alerts, "critical")
    @patch.object(swap_nexus.solana_client, "is_valid_solana_token_account", return_value=False)
    @patch.object(swap_nexus.state_db, "update_unprocessed_txid")
    @patch.object(swap_nexus.nexus_client, "refund_nexus_token")
    @patch.object(swap_nexus.nexus_client, "find_asset_receival_account_by_txid_and_owner")
    @patch.object(swap_nexus.state_db, "get_unprocessed_txids_as_dicts")
    @patch.object(swap_nexus.time, "time", return_value=10_000)
    def test_every_nexus_refund_path_holds_and_alerts_without_transfer(
        self, _time, rows, lookup, refund, update_txid, _valid_account, alert
    ):
        cases = (
            (
                "invalid receival account",
                swap_nexus.NEXUS_STATUS_PENDING,
                nexus_client.AssetLookup(
                    {"receival_account": "invalid", "owner": "owner"}, True, ""
                ),
                9_999,
            ),
            (
                "unresolved receival account timeout",
                swap_nexus.NEXUS_STATUS_PENDING,
                nexus_client.AssetLookup(None, True, ""),
                1,
            ),
            (
                "collecting refund",
                swap_nexus.NEXUS_STATUS_COLLECTING_REFUND,
                nexus_client.AssetLookup(None, False, "cli_error"),
                9_999,
            ),
            (
                "refund pending",
                swap_nexus.NEXUS_STATUS_REFUND_PENDING,
                nexus_client.AssetLookup(None, False, "cli_error"),
                9_999,
            ),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                for reason, status, asset_lookup, timestamp in cases:
                    with self.subTest(reason=reason):
                        rows.return_value = [{
                            "txid": f"credit-{reason}", "ts": timestamp,
                            "comment": status, "confirmations": 2, "owner": "owner",
                            "from": "sender", "amount_usdd_units": 1_000_000,
                        }]
                        lookup.return_value = asset_lookup
                        swap_nexus.process_unprocessed_txids()

                        refund.assert_not_called()
                        update_txid.assert_any_call(
                            txid=f"credit-{reason}",
                            status=swap_nexus.NEXUS_STATUS_REFUND_HOLD,
                            hold_reason=reason,
                        )
                        alert.assert_called_with(
                            "nexus_refund_held",
                            "Automatic Nexus refund disabled; manual operator review required",
                            txid=f"credit-{reason}", sender="sender", amount_units=1_000_000,
                            reason=reason, age_sec=10_000 - timestamp,
                        )
                        update_txid.reset_mock()
                        alert.reset_mock()

    @patch.object(swap_nexus.state_db, "update_unprocessed_txid")
    @patch.object(
        swap_nexus.nexus_client,
        "find_asset_receival_account_by_txid_and_owner",
        return_value=nexus_client.AssetLookup(None, False, "invalid_response"),
    )
    @patch.object(
        swap_nexus.state_db,
        "get_unprocessed_txids_as_dicts",
        return_value=[{
            "txid": "credit-tx", "ts": 1,
            "comment": swap_nexus.NEXUS_STATUS_TRADE_BAL_CHECK,
            "confirmations": 2, "owner": "owner", "from": "sender",
            "amount_usdd_units": 1_000_000,
        }],
    )
    def test_incomplete_receival_recheck_cannot_enter_refund_state(
        self, _rows, _lookup, update_txid
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                swap_nexus, "_log"
            ) as log:
                state_db.init_db()
                swap_nexus.process_unprocessed_txids()

        update_txid.assert_not_called()
        self.assertFalse(any(call.args and call.args[0] == "NEXUS_PROCESS_ERROR" for call in log.call_args_list))

    @patch.object(fees.config, "nexus_units_to_solana", side_effect=lambda units: units)
    @patch.object(fees.state_db, "get_unresolved_solana_liability_units", return_value=3_000_000)
    def test_unresolved_refund_liability_is_subtracted_from_surplus(
        self, _liability, _convert
    ):
        surplus = fees.available_backing_surplus_solana_units(
            vault_solana_units=12_000_000,
            circulating_nexus_units=10_000_000,
        )

        self.assertEqual(surplus, 0)

    @patch.object(fees.config, "FEE_CONVERSION_ENABLED", True)
    @patch.object(fees.config, "BACKING_SURPLUS_MINT_THRESHOLD_SOLANA_UNITS", 0)
    @patch.object(fees, "available_backing_surplus_solana_units", return_value=10_000_000)
    @patch.object(solana_client, "get_token_account_balance", return_value=20_000_000)
    @patch.object(solana_client, "get_vault_sol_balance", return_value=0)
    @patch.object(nexus_client, "get_circulating_nexus_units", return_value=10_000_000)
    @patch.object(nexus_client, "get_nxs_default_balance_units", return_value=0)
    @patch.object(nexus_client, "mint_nexus_to_local", return_value=True)
    @patch.object(solana_client, "swap_token_for_sol_via_jupiter", return_value=True)
    def test_automatic_fee_conversion_is_disabled_without_durable_intent(
        self, swap_sol, mint_nexus, _nxs_balance, _circ, _sol_balance,
        _vault, _surplus
    ):
        fees.process_fee_conversions()

        mint_nexus.assert_not_called()
        swap_sol.assert_not_called()

    @patch.object(fees.config, "BACKING_DEFICIT_PAUSE_PCT", 90)
    @patch.object(fees.config, "nexus_units_to_solana", side_effect=lambda units: units)
    @patch.object(fees.state_db, "get_unresolved_solana_liability_units", return_value=4_000_000)
    @patch.object(solana_client, "get_token_account_balance", return_value=12_000_000)
    @patch.object(nexus_client, "get_circulating_nexus_units", return_value=10_000_000)
    def test_unresolved_liabilities_cannot_mask_backing_deficit(
        self, _circ, _vault, _liability, _convert
    ):
        should_pause = fees.maintain_backing_and_bounds()

        self.assertTrue(should_pause)

    @patch.object(solana_client, "get_token_account_balance", side_effect=RuntimeError("rpc down"))
    def test_backing_check_error_fails_closed(self, _vault):
        self.assertTrue(fees.maintain_backing_and_bounds())

    @patch.object(nexus_client, "_run", return_value=(1, "", "node down"))
    def test_supply_lookup_failure_is_not_reported_as_zero(self, _run):
        with self.assertRaises(RuntimeError):
            nexus_client.get_circulating_nexus_units()

    def test_liability_ledger_includes_every_unprocessed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "refund", 1, "", "sender", 3_000_000, "to be refunded", None
                )
                state_db.add_unprocessed_sig(
                    "quarantine", 2, "", "sender", 4_000_000,
                    "quarantine sent, awaiting confirmation", None
                )
                state_db.add_unprocessed_sig(
                    "mint", 3, "", "sender", 5_000_000,
                    "debited, awaiting confirmation", "txid"
                )

                liability = state_db.get_unresolved_solana_liability_units()

        self.assertEqual(liability, 12_000_000)

    @patch.object(nexus_client.state_db, "update_unprocessed_sig_status")
    @patch.object(nexus_client.state_db, "filter_unprocessed_sigs")
    @patch.object(nexus_client, "_run")
    @patch.object(nexus_client.time, "time", return_value=2_000)
    def test_truncated_confirmation_lookup_never_authorizes_refund(
        self, _time, run, pending_rows, update_status
    ):
        pending_rows.return_value = [
            ("deposit-sig", 1_000, "nexus:user", "sender", 2_000_000,
             "debited, awaiting confirmation", "missing-txid")
        ]
        page = [
            {"txid": f"unrelated-{index}", "confirmations": 10}
            for index in range(200)
        ]
        run.return_value = (0, json.dumps(page), "")

        processed = nexus_client.check_unconfirmed_debits(2, 1)

        self.assertEqual(processed, 0)
        update_status.assert_not_called()

    @patch.object(swap_nexus.config, "USE_NEXUS_WHERE_FILTER_USDD", True, create=True)
    @patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    def test_nexus_poller_never_uses_heuristic_server_side_amount_filter(self, run):
        """A Nexus scan must be complete even when the legacy flag is enabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        command = run.call_args.args[0]
        self.assertFalse(any(str(argument).startswith("where=") for argument in command))

    @patch.object(nexus_client, "_run", return_value=(0, "[]", ""))
    def test_recovery_enumeration_never_uses_heuristic_server_side_amount_filter(self, run):
        """Recovery must never skip credits through an unverified nested WHERE clause."""
        nexus_client.fetch_deposits_since("TREASURY", since_timestamp=0)

        command = run.call_args.args[0]
        self.assertFalse(any(str(argument).startswith("where=") for argument in command))

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    @patch("subprocess.run", side_effect=TimeoutError("node timeout"))
    def test_nexus_enumeration_failure_holds_waterline(self, _run, propose_waterline):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        propose_waterline.assert_not_called()

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    @patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout='{"unexpected": true}', stderr=""),
    )
    def test_malformed_nexus_enumeration_response_holds_waterline(
        self, _run, propose_waterline
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        propose_waterline.assert_not_called()

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    @patch(
        "subprocess.run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{
                "txid": "credit-tx",
                "timestamp": 1_000,
                "contracts": [{"OP": "CREDIT", "from": "sender", "to": "TREASURY"}],
            }]),
            stderr="",
        ),
    )
    def test_malformed_credit_contract_holds_waterline(self, _run, propose_waterline):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        propose_waterline.assert_not_called()

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    def test_processing_pass_never_advances_nexus_waterline_without_scan_evidence(
        self, propose_waterline
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.process_unprocessed_txids()

        propose_waterline.assert_not_called()

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    @patch.object(
        swap_nexus.state_db,
        "get_unprocessed_txids_as_dicts",
        return_value=[{"txid": "held", "ts": 100, "comment": "manual hold"}],
    )
    def test_processing_pass_with_active_rows_never_advances_waterline(
        self, _rows, propose_waterline
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.process_unprocessed_txids()

        propose_waterline.assert_not_called()

    @patch.object(swap_nexus.config, "NEXUS_MAX_PAGES", 1, create=True)
    @patch.object(
        swap_nexus.state_db,
        "get_unprocessed_txids_as_dicts",
        return_value=[{"txid": "held", "ts": 100, "comment": "manual hold"}],
    )
    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    def test_pagination_truncation_holds_waterline_even_with_active_rows(
        self, propose_waterline, _rows
    ):
        transactions = [
            {"txid": f"tx-{index}", "timestamp": 1_000 + index, "contracts": []}
            for index in range(100)
        ]
        completed = SimpleNamespace(
            returncode=0, stdout=json.dumps(transactions), stderr=""
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch(
                "subprocess.run", return_value=completed
            ):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        propose_waterline.assert_not_called()

    def test_reconciliation_uses_durable_completed_mint_evidence_after_queue_removal(self):
        """A completed mint remains checkable after its transient queue row is gone."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                solana_units = 2_000_000
                nexus_units = nexus_client.get_nexus_send_amount_units(solana_units)
                state_db.mark_processed_sig(
                    "mint-sig", 100, solana_units, "mint-tx", 0.0,
                    "debit_confirmed", 77,
                    amount_usdd_units=nexus_units,
                    nexus_destination="recipient",
                    memo="nexus:recipient",
                )
                self.assertFalse(state_db.is_unprocessed_sig("mint-sig"))

                healthy = balance_reconciler.run_balance_reconciliation(waterline_ts=0)
                self.assertTrue(healthy["healthy"])
                self.assertEqual(healthy["checked_addresses"], 1)
                self.assertEqual(healthy["total_surplus_nexus_units"], 0)

                # A second treasury debit to the same recipient is observable as a
                # positive exact-base-unit discrepancy rather than a false green.
                state_db.mark_processed_txid(
                    "duplicate-mint", 101, 0.0, "TREASURY", "recipient", "", "",
                    "processed", amount_usdd_units=nexus_units,
                )
                duplicate = balance_reconciler.run_balance_reconciliation(waterline_ts=0)
                self.assertFalse(duplicate["healthy"])
                self.assertEqual(duplicate["total_surplus_nexus_units"], nexus_units)
                self.assertEqual(duplicate["discrepancies"], [{
                    "account": "recipient", "surplus_nexus_units": nexus_units,
                }])

    def test_reconciliation_fails_closed_when_completed_mint_lacks_durable_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.mark_processed_sig(
                    "legacy-mint", 100, 2_000_000, "mint-tx", 1.0,
                    "debit_confirmed", 1,
                )
                result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        self.assertEqual(result["checked_addresses"], 0)
        self.assertTrue(any("durable Nexus destination" in reason
                            for reason in result["incomplete_reasons"]))

    def test_nexus_transfer_intent_is_durable_and_reuses_its_unique_reference(self):
        """A refund/quarantine transfer is uniquely identified before the CLI can run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                first = state_db.create_nexus_transfer_intent(
                    kind="refund",
                    source_txid="credit-1",
                    from_address="TREASURY",
                    to_address="sender",
                    amount_usdd_units=1_000_000,
                )
                second = state_db.create_nexus_transfer_intent(
                    kind="refund",
                    source_txid="credit-1",
                    from_address="TREASURY",
                    to_address="sender",
                    amount_usdd_units=1_000_000,
                )

                self.assertEqual(first["status"], "prepared")
                self.assertEqual(first["reference"], second["reference"])
                self.assertEqual(first["id"], second["id"])
                self.assertEqual(
                    state_db.get_nexus_transfer_intent(first["id"]), first
                )

    @patch.object(nexus_client, "_run", return_value=(0, '{"txid":"refund-tx"}', ""))
    def test_nexus_transfer_intent_executes_once_and_persists_remote_txid(self, run):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-2", from_address="TREASURY",
                    to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )

                result = nexus_client.execute_nexus_transfer_intent(intent["id"])
                repeated = nexus_client.execute_nexus_transfer_intent(intent["id"])
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertTrue(result.executed)
        self.assertEqual(result.status, "submitted")
        self.assertFalse(repeated.executed)
        self.assertEqual(stored["status"], "submitted")
        self.assertEqual(stored["remote_txid"], "refund-tx")
        self.assertEqual(run.call_count, 1)

    def test_completed_nexus_transfer_intent_cannot_be_regressed_to_an_ambiguous_state(self):
        """Terminal chain evidence must not be overwritten by a later recovery path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-terminal", from_address="TREASURY",
                    to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.claim_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(
                    intent["id"], status="submitted", remote_txid="chain-tx"
                )
                state_db.update_nexus_transfer_intent(
                    intent["id"], status="completed", remote_txid="chain-tx", resolved=True
                )

                with self.assertRaises(ValueError):
                    state_db.update_nexus_transfer_intent(
                        intent["id"], status="outcome_unknown"
                    )
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["remote_txid"], "chain-tx")
        self.assertIsNotNone(stored["resolved_timestamp"])

    @patch.object(nexus_client, "find_nexus_debits_by_references")
    @patch.object(nexus_client, "_run", side_effect=TimeoutError("node timed out"))
    def test_unknown_nexus_transfer_outcome_holds_until_positive_reference_resolution(
        self, run, find_by_reference
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="quarantine", source_txid="credit-3", from_address="TREASURY",
                    to_address="QUARANTINE", amount_usdd_units=2_000_000,
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                result = nexus_client.execute_nexus_transfer_intent(intent["id"])
                again = nexus_client.execute_nexus_transfer_intent(intent["id"])
                find_by_reference.return_value = nexus_client.BatchLookup({}, False, "timeout")
                unresolved = nexus_client.resolve_nexus_transfer_intents()
                find_by_reference.return_value = nexus_client.BatchLookup(
                    {intent["reference"]: "chain-txid"}, True
                )
                resolved = nexus_client.resolve_nexus_transfer_intents()
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertEqual(result.status, "outcome_unknown")
        self.assertFalse(again.executed)
        self.assertEqual(unresolved, 0)
        self.assertEqual(resolved, 1)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["remote_txid"], "chain-txid")
        self.assertEqual(run.call_count, 1)

    @patch.object(nexus_client, "transfer_nexus_between_accounts", return_value=True)
    def test_legacy_refund_wrapper_only_prepares_durable_intent(self, raw_transfer):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                refunded = nexus_client.refund_nexus_token(
                    "sender", 1_000_000, "missing mapping txid: credit-4"
                )
                intents = state_db.get_nexus_transfer_intents_by_status(("prepared",))

        self.assertFalse(refunded)
        raw_transfer.assert_not_called()
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["kind"], "refund")
        self.assertEqual(intents[0]["source_txid"], "credit-4")

    @patch.object(nexus_client, "_run", return_value=(0, '{"txid":"refund-tx"}', ""))
    def test_nexus_transfer_requires_explicit_authorization_and_audited_disposition(self, run):
        """Only a named operator can release a held credit after durable chain evidence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.add_unprocessed_txid(
                    txid="credit-operator", timestamp=1, amount_usdd=1.0,
                    from_address="sender", to_address="TREASURY", owner_from_address="owner",
                    confirmations_credit=2, status=swap_nexus.NEXUS_STATUS_REFUND_HOLD,
                    amount_usdd_units=1_000_000, hold_reason="missing mapping",
                )
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-operator", from_address="TREASURY",
                    to_address="sender", amount_usdd_units=1_000_000,
                )

                blocked = nexus_client.execute_nexus_transfer_intent(intent["id"])
                with self.assertRaises(ValueError):
                    state_db.authorize_nexus_transfer_intent(
                        intent["id"], actor="alice", rationale="reviewed", expected_reference="wrong"
                    )
                authorized = state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="alice", rationale="mapping absent; refund approved",
                    expected_reference=intent["reference"],
                )
                executed = nexus_client.execute_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(
                    intent["id"], status="completed", remote_txid="refund-tx", resolved=True
                )
                self.assertFalse(state_db.finalize_nexus_transfer_disposition(
                    intent["id"], actor="alice", rationale="wrong txid rejected", expected_remote_txid="wrong"
                ))
                finalized = state_db.finalize_nexus_transfer_disposition(
                    intent["id"], actor="alice", rationale="reference confirmed on target node",
                    expected_remote_txid="refund-tx",
                )
                source_rows = state_db.get_unprocessed_txids_as_dicts()
                events = state_db.get_nexus_transfer_audit_events(intent["id"])
                is_refunded = state_db.is_refunded_txid("credit-operator")

        self.assertFalse(blocked.executed)
        self.assertEqual(blocked.status, "prepared")
        self.assertEqual(authorized["status"], "authorized")
        self.assertTrue(executed.executed)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(finalized)
        self.assertTrue(is_refunded)
        self.assertEqual(source_rows, [])
        self.assertEqual([event["action"] for event in events], [
            "authorized_execution", "finalized_refund",
        ])
        self.assertTrue(all(event["actor"] == "alice" for event in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
