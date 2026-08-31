#!/usr/bin/env python3
"""Regression tests for the 2026-08-24 Critical fund-safety findings."""

import importlib
import json
import logging
import os
import runpy
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from typing import cast
from unittest.mock import call, patch

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
os.environ.setdefault("NEXUS_TOKEN_REGISTER_ADDRESS", "TOKEN-REGISTER")
os.environ.setdefault("SOL_MAIN_ACCOUNT", "OWNER")
os.environ.setdefault("NEXUS_CLI_PATH", "/bin/false")

from src import (  # noqa: E402
    alerts, balance_reconciler, config, fees, main, nexus_client, solana_client,
    startup_recovery, state_db, swap_nexus, swap_solana,
)


class CriticalSafetyTests(unittest.TestCase):
    def test_solana_poll_money_path_summaries_are_structured_events(self):
        """A Nexus→Solana payout operator must not have to parse console prose."""
        with patch.object(swap_solana.nexus_client, "get_heartbeat_asset", return_value={
            "last_safe_timestamp_solana": 100,
        }), patch.object(
            swap_solana.solana_client, "fetch_incoming_deposits_via_helius", return_value=[]
        ), patch.object(
            swap_solana.solana_client, "process_helius_deposits", return_value=(2, None)
        ), patch.object(
            swap_solana.solana_client, "process_unprocessed_solana_deposits",
            return_value=[3, 4, 5, 6],
        ), patch.object(
            swap_solana.solana_client, "process_solana_deposits_refunding", return_value=7
        ), patch.object(
            swap_solana.solana_client, "process_solana_deposits_quarantine", return_value=8
        ), patch.object(
            swap_solana.solana_client, "check_sig_confirmations", return_value=9
        ), patch.object(
            swap_solana.solana_client, "check_quarantine_confirmations", return_value=10
        ), patch.object(
            swap_solana.nexus_client, "resolve_unverified_debits", return_value=11
        ), patch.object(
            swap_solana.nexus_client, "check_unconfirmed_debits", return_value=12
        ), patch.object(
            swap_solana.nexus_client, "update_heartbeat_asset"
        ), patch.object(
            swap_solana.nexus_client, "publish_service_record"
        ), patch.object(
            swap_solana.solana_client, "get_token_account_balance", return_value=100
        ), patch.object(
            swap_solana.state_db, "save_last_vault_balance"
        ), patch.object(
            swap_solana, "_advance_solana_waterline"
        ), patch.object(swap_solana, "_log") as log:
            swap_solana.poll_solana_deposits()

        self.assertIn(
            call("SOLANA_DEPOSITS_INGESTED", count=2),
            log.call_args_list,
        )
        self.assertIn(
            call(
                "SOLANA_PROCESSING_SUMMARY",
                swap_debits=3,
                refunds_pending=4,
                quarantines_pending=5,
                micro_deposits=6,
            ),
            log.call_args_list,
        )
        for event, count in (
            ("SOLANA_REFUNDS_SUBMITTED", 7),
            ("SOLANA_QUARANTINES_SUBMITTED", 8),
            ("SOLANA_REFUNDS_CONFIRMED", 9),
            ("SOLANA_QUARANTINES_CONFIRMED", 10),
            ("NEXUS_DEBITS_RESOLVED", 11),
            ("NEXUS_DEBITS_CONFIRMED", 12),
        ):
            self.assertIn(call(event, count=count), log.call_args_list)

    def test_poller_lifecycle_logging_failure_cannot_abort_either_chain_poller(self):
        """Observability outages must not turn Nexus or Solana custody work into a stopped poller."""
        for poller in (swap_nexus, swap_solana):
            with patch.object(
                poller.structured_logging, "emit", side_effect=RuntimeError("logger failed")
            ):
                poller._log("TEST_LIFECYCLE_EVENT", chain="test")

    def test_operator_alerts_use_structured_events_not_terminal_prose(self):
        """Critical Nexus/Solana custody events must be parseable by incident tooling."""
        with patch.object(alerts.structured_logging, "emit") as emit:
            alerts.alert(
                alerts.CRITICAL,
                "nexus_debit_held",
                "Nexus debit needs resolution",
                intent_id="intent-1",
                solana_signature="sig-1",
            )

        emit.assert_called_once_with(
            alerts._LOG,
            __import__("logging").CRITICAL,
            "nexus_debit_held",
            "Nexus debit needs resolution",
            intent_id="intent-1",
            solana_signature="sig-1",
        )

    def test_ambiguous_nexus_debits_have_no_expiring_negative_lookup_window(self):
        """An uncertain Nexus debit must hold for resolution, not age into a false negative."""
        self.assertFalse(hasattr(config, "DEBIT_VERIFY_GRACE_SEC"))

    def test_ambiguous_nexus_debits_expose_only_batch_lookup_apis(self):
        """No stale single-item Nexus history scan may be revived for a money decision."""
        for legacy_helper in (
            "get_transaction_confirmations",
            "find_nexus_debit_by_reference",
            "was_nexus_debited_to_account_for_amount",
        ):
            self.assertFalse(hasattr(nexus_client, legacy_helper), legacy_helper)
        self.assertTrue(callable(nexus_client.get_transactions_confirmations))
        self.assertTrue(callable(nexus_client.find_nexus_debits_by_references))

    def test_dormant_nexus_dex_and_rebalance_paths_are_not_available_to_runtime(self):
        """The bridge must not retain unaudited automatic money-movement helpers."""
        self.assertFalse(hasattr(config, "SOL_MINT"))
        for legacy_helper in (
            "mint_nexus_to_local",
            "token_nxs_market",
            "nxs_token_market",
            "list_market_bids",
            "list_market_asks",
            "execute_market_order",
            "buy_nxs_with_token_budget",
        ):
            self.assertFalse(hasattr(nexus_client, legacy_helper), legacy_helper)

    def test_invalid_production_mode_in_environment_fails_configuration_loading(self):
        """A typo must never silently downgrade a production process to development mode."""
        try:
            with patch.dict(os.environ, {"SWAP_PRODUCTION_MODE": "treu"}):
                with self.assertRaisesRegex(ValueError, "SWAP_PRODUCTION_MODE.*treu"):
                    importlib.reload(config)
        finally:
            # Reload the normal test configuration after the intentional failed import.
            importlib.reload(config)

    def test_production_mode_parser_accepts_only_documented_spellings(self):
        """The documented switch values stay explicit across Nexus/Solana deployments."""
        for value in ("1", "true", "yes", "on", " TrUe "):
            self.assertTrue(config.parse_strict_boolean("SWAP_PRODUCTION_MODE", value))
        for value in ("0", "false", "no", "off", " FaLsE "):
            self.assertFalse(config.parse_strict_boolean("SWAP_PRODUCTION_MODE", value))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config.parse_strict_boolean("SWAP_PRODUCTION_MODE", default=False))
        with self.assertRaisesRegex(ValueError, "SWAP_PRODUCTION_MODE.*''"):
            config.parse_strict_boolean("SWAP_PRODUCTION_MODE", "")

    @patch.object(main.alerts, "critical")
    def test_production_controls_reject_disabled_caps_and_alerting(self, critical):
        """A production process must not start with disabled loss-limiting controls."""
        with (
            patch.object(config, "PRODUCTION_MODE", True),
            patch.object(config, "MAX_SWAP_SOLANA_UNITS", 0),
            patch.object(config, "MAX_SWAP_NEXUS_UNITS", 0),
            patch.object(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 0),
            patch.object(config, "ALERT_WEBHOOK_URL", ""),
            patch.object(config, "ALERT_COMMAND", ""),
            patch.object(config, "NEXUS_API_URL", "https://127.0.0.1:8443"),
            patch.object(config, "NEXUS_API_USER", "api-user"),
            patch.object(config, "NEXUS_API_PASSWORD", "api-password"),
            patch.object(config, "USDC_QUARANTINE_ACCOUNT", "SOLANA_QUARANTINE"),
            patch.object(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", "NEXUS_QUARANTINE"),
        ):
            self.assertFalse(main.validate_production_controls())

        critical.assert_called_once_with(
            "production_controls_missing",
            "refusing production startup because mandatory exposure controls are disabled",
            missing_controls=[
                "MAX_SWAP_USDC",
                "MAX_SWAP_USDD",
                "DAILY_PAYOUT_CAP_USDC",
                "ALERT_WEBHOOK_URL or ALERT_COMMAND",
            ],
        )

    @patch.object(main.alerts, "critical")
    def test_production_controls_require_https_nexus_api_transport(self, critical):
        """A live bridge must not place Nexus credentials in a child-process argv."""
        with (
            patch.object(config, "PRODUCTION_MODE", True),
            patch.object(config, "MAX_SWAP_SOLANA_UNITS", 1),
            patch.object(config, "MAX_SWAP_NEXUS_UNITS", 1),
            patch.object(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 1),
            patch.object(config, "ALERT_COMMAND", "/usr/local/bin/bridge-alert"),
            patch.object(config, "ALERT_WEBHOOK_URL", ""),
            patch.object(config, "USDC_QUARANTINE_ACCOUNT", "SOLANA_QUARANTINE"),
            patch.object(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", "NEXUS_QUARANTINE"),
            patch.object(config, "NEXUS_API_URL", "", create=True),
            patch.object(config, "NEXUS_API_USER", "", create=True),
            patch.object(config, "NEXUS_API_PASSWORD", "", create=True),
        ):
            self.assertFalse(main.validate_production_controls())

        critical.assert_called_once_with(
            "production_controls_missing",
            "refusing production startup because mandatory exposure controls are disabled",
            missing_controls=[
                "NEXUS_API_URL (HTTPS)",
                "NEXUS_API_USER",
                "NEXUS_API_PASSWORD",
            ],
        )

    @patch.object(main.alerts, "critical")
    def test_production_controls_require_multiuser_session(self, critical):
        """A multiuser Nexus node cannot admit a bridge without its session credential."""
        with (
            patch.object(config, "PRODUCTION_MODE", True),
            patch.object(config, "MAX_SWAP_SOLANA_UNITS", 1),
            patch.object(config, "MAX_SWAP_NEXUS_UNITS", 1),
            patch.object(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 1),
            patch.object(config, "ALERT_COMMAND", "/usr/local/bin/bridge-alert"),
            patch.object(config, "ALERT_WEBHOOK_URL", ""),
            patch.object(config, "USDC_QUARANTINE_ACCOUNT", "SOLANA_QUARANTINE"),
            patch.object(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", "NEXUS_QUARANTINE"),
            patch.object(config, "NEXUS_API_URL", "https://127.0.0.1:8443"),
            patch.object(config, "NEXUS_API_USER", "api-user"),
            patch.object(config, "NEXUS_API_PASSWORD", "api-password"),
            patch.object(config, "NEXUS_MULTIUSER", True),
            patch.object(config, "NEXUS_SESSION", ""),
        ):
            self.assertFalse(main.validate_production_controls())

        critical.assert_called_once_with(
            "production_controls_missing",
            "refusing production startup because mandatory exposure controls are disabled",
            missing_controls=["NEXUS_SESSION (required when NEXUS_MULTIUSER=true)"],
        )

    @patch.object(main.alerts, "critical")
    def test_production_controls_require_both_quarantine_destinations(self, critical):
        """A live bridge cannot strand either chain's failed-payout funds in its vault."""
        with (
            patch.object(config, "PRODUCTION_MODE", True),
            patch.object(config, "MAX_SWAP_SOLANA_UNITS", 1),
            patch.object(config, "MAX_SWAP_NEXUS_UNITS", 1),
            patch.object(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 1),
            patch.object(config, "ALERT_COMMAND", "/usr/local/bin/bridge-alert"),
            patch.object(config, "ALERT_WEBHOOK_URL", ""),
            patch.object(config, "NEXUS_API_URL", "https://127.0.0.1:8443"),
            patch.object(config, "NEXUS_API_USER", "api-user"),
            patch.object(config, "NEXUS_API_PASSWORD", "api-password"),
            patch.object(config, "USDC_QUARANTINE_ACCOUNT", ""),
            patch.object(config, "NEXUS_USDD_QUARANTINE_ACCOUNT", ""),
        ):
            self.assertFalse(main.validate_production_controls())

        critical.assert_called_once_with(
            "production_controls_missing",
            "refusing production startup because mandatory exposure controls are disabled",
            missing_controls=[
                "USDC_QUARANTINE_ACCOUNT",
                "NEXUS_USDD_QUARANTINE_ACCOUNT",
            ],
        )

    @patch.object(main, "acquire_singleton_lock", return_value=False)
    @patch.object(main.state_db, "init_db")
    def test_run_rejects_production_controls_before_opening_state(self, init_db, _lock):
        """A rejected production configuration must not acquire state or enter the loop."""
        with patch.object(main, "validate_production_controls", return_value=False, create=True):
            self.assertIs(main.run(), False)

        init_db.assert_not_called()

    @patch.object(main.state_db, "init_db")
    def test_entrypoint_exits_nonzero_when_startup_is_rejected(self, init_db):
        """A service supervisor must observe a failed production admission as an error."""
        entrypoint = os.path.join(ROOT, "swapService.py")
        with patch.object(main, "validate_production_controls", return_value=False):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(entrypoint, run_name="__main__")

        self.assertEqual(raised.exception.code, 1)
        init_db.assert_not_called()

    def test_production_nexus_transport_rejects_malformed_or_ambiguous_urls(self):
        """A production URL must unambiguously name one HTTPS Nexus API origin."""
        invalid_urls = (
            "http://127.0.0.1:8080",
            "https://:8443",
            "https://127.0.0.1?",
            "https://127.0.0.1#",
            "https://127.0.0.1:not-a-port",
            "https://user:pass@127.0.0.1:8443",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with (
                    patch.object(config, "NEXUS_API_URL", url),
                    patch.object(config, "NEXUS_API_USER", "api-user"),
                    patch.object(config, "NEXUS_API_PASSWORD", "api-password"),
                ):
                    self.assertEqual(
                        nexus_client.nexus_api_transport_errors(),
                        ["NEXUS_API_URL (HTTPS)"],
                    )

    @patch("src.nexus_client.build_opener")
    def test_nexus_api_transport_disables_http_redirects(self, build_opener):
        """Basic API credentials must never follow a redirect to another origin."""
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def read(self):
                return b'{}'

        build_opener.return_value.open.return_value = Response()
        with (
            patch.object(config, "NEXUS_API_URL", "https://127.0.0.1:8443"),
            patch.object(config, "NEXUS_API_USER", "api-user"),
            patch.object(config, "NEXUS_API_PASSWORD", "api-password"),
        ):
            self.assertEqual(nexus_client._run([config.NEXUS_CLI, "system/get/info"]), (0, "{}", ""))

        handlers = build_opener.call_args.args
        self.assertTrue(any(isinstance(handler, nexus_client._NoRedirect) for handler in handlers))
        build_opener.return_value.open.assert_called_once()

    @patch("src.nexus_client.build_opener")
    def test_nexus_api_transport_sends_nexus_credentials_only_in_post_body(self, build_opener):
        """Production Nexus calls must not expose PIN/session through process argv."""
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def read(self):
                return b'{"txid":"remote-tx"}'

        build_opener.return_value.open.return_value = Response()
        with (
            patch.object(config, "NEXUS_API_URL", "https://127.0.0.1:8443"),
            patch.object(config, "NEXUS_API_USER", "api-user"),
            patch.object(config, "NEXUS_API_PASSWORD", "api-password"),
        ):
            code, out, err = nexus_client._run([
                config.NEXUS_CLI,
                "finance/debit/token",
                "from=USDD",
                "to=recipient",
                "amount=1",
                "pin=PIN123",
                "session=SESSION-ABC",
            ])

        self.assertEqual((code, out, err), (0, '{"txid":"remote-tx"}', ""))
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://127.0.0.1:8443/finance/debit/token")
        payload = request.data.decode("utf-8")
        self.assertIn("pin=PIN123", payload)
        self.assertIn("session=SESSION-ABC", payload)
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

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

    def test_unverified_debit_resolution_rejects_reference_match_with_wrong_terms(self):
        """A reference collision cannot attach an unrelated Nexus mint to a Solana deposit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "deposit-sig", 1_000, "nexus:intended-recipient", "sender",
                    2_000_000, "debit in flight", None,
                )
                state_db.set_unprocessed_sig_debit_intent("deposit-sig", 77, 1_898_000)
                state_db.update_unprocessed_sig_status("deposit-sig", "debit unverified")
                self.assertTrue(state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, "deposit-sig"))
                response = [{
                    "txid": "unrelated-debit",
                    "contracts": [{
                        "OP": "DEBIT", "reference": 77, "from": config.NEXUS_TOKEN_REGISTER_ADDRESS,
                        "to": "wrong-recipient", "amount": "1.898",
                    }],
                }]
                with patch.object(nexus_client, "_run", return_value=(0, json.dumps(response), "")):
                    resolved = nexus_client.resolve_unverified_debits()
                row = state_db.get_unprocessed_sigs()[0]

                self.assertEqual(resolved, 0)
                self.assertEqual(row[5], "debit unverified")
                self.assertIsNone(row[6])
                self.assertTrue(state_db.is_reserved(state_db.DEBIT_RESERVATION_KIND, "deposit-sig"))

    def test_unverified_debit_resolution_holds_two_exact_contracts_in_one_transaction(self):
        """One txid with two matching contracts is ambiguous, not one completed mint."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "deposit-sig", 1_000, "nexus:intended-recipient", "sender",
                    2_000_000, "debit in flight", None,
                )
                state_db.set_unprocessed_sig_debit_intent("deposit-sig", 77, 1_898_000)
                state_db.update_unprocessed_sig_status("deposit-sig", "debit unverified")
                self.assertTrue(state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, "deposit-sig"))
                response = [{
                    "txid": "ambiguous-debit",
                    "contracts": [
                        {"id": 0, "OP": "DEBIT", "reference": 77,
                         "from": config.NEXUS_TOKEN_REGISTER_ADDRESS, "to": "intended-recipient",
                         "amount": "1.898"},
                        {"id": 1, "OP": "DEBIT", "reference": 77,
                         "from": config.NEXUS_TOKEN_REGISTER_ADDRESS, "to": "intended-recipient",
                         "amount": "1.898"},
                    ],
                }]
                with patch.object(nexus_client, "_run", return_value=(0, json.dumps(response), "")):
                    resolved = nexus_client.resolve_unverified_debits()
                row = state_db.get_unprocessed_sigs()[0]

        self.assertEqual(resolved, 0)
        self.assertEqual(row[5], "debit unverified")
        self.assertIsNone(row[6])

    def test_unverified_debit_resolution_holds_single_candidate_from_incomplete_lookup(self):
        """A bounded reference scan cannot prove a lone observed mint is unique."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                nexus_client, "find_nexus_transfer_debits_by_references"
            ) as lookup:
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "deposit-sig", 1_000, "nexus:intended-recipient", "sender",
                    2_000_000, "debit in flight", None,
                )
                state_db.set_unprocessed_sig_debit_intent("deposit-sig", 77, 1_898_000)
                state_db.update_unprocessed_sig_status("deposit-sig", "debit unverified")
                self.assertTrue(state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, "deposit-sig"))
                lookup.return_value = nexus_client.BatchLookup({"77": [
                    nexus_client.TransferDebitEvidence(
                        remote_txid="observed-only-tx", contract_id=0,
                        from_address=config.NEXUS_TOKEN_REGISTER_ADDRESS,
                        to_address="intended-recipient", amount_usdd_units=1_898_000,
                    )
                ]}, False, "pagination_truncated")

                resolved = nexus_client.resolve_unverified_debits()
                row = state_db.get_unprocessed_sigs()[0]
                reservation_retained = state_db.is_reserved(
                    state_db.DEBIT_RESERVATION_KIND, "deposit-sig"
                )

        self.assertEqual(resolved, 0)
        self.assertEqual(row[5], "debit unverified")
        self.assertIsNone(row[6])
        self.assertTrue(reservation_retained)

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

    def test_unsafe_automatic_fee_conversion_path_is_removed_not_feature_gated(self):
        """No configuration edit may revive unintentional Nexus/Solana value movement."""
        for setting in (
            "FEE_CONVERSION_ENABLED",
            "FEE_CONVERSION_MIN_USDC",
            "SOL_TOPUP_MIN_LAMPORTS",
            "SOL_TOPUP_TARGET_LAMPORTS",
            "NEXUS_NXS_TOPUP_MIN",
        ):
            self.assertFalse(hasattr(config, setting), setting)
        for helper in (
            "automatic_surplus_actions_enabled",
            "process_fee_conversions",
            "reconcile_fees_to_fee_account",
        ):
            self.assertFalse(hasattr(fees, helper), helper)

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
    @patch.object(nexus_client, "_run", return_value=(0, "[]", ""))
    def test_nexus_poller_never_uses_heuristic_server_side_amount_filter(self, run):
        """A Nexus scan must be complete even when the legacy flag is enabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        command = run.call_args.args[0]
        self.assertFalse(any(str(argument).startswith("where=") for argument in command))

    @patch.object(nexus_client, "get_heartbeat_asset", return_value=None)
    @patch.object(nexus_client, "_run", return_value=(0, "[]", ""))
    def test_nexus_poller_enumeration_uses_common_transport_wrapper(self, run, _heartbeat):
        """Deposit enumeration must share the Nexus transport's timeout/error semantics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], config.NEXUS_CLI)
        self.assertTrue(command[1].startswith("register/transactions/finance:token/"))
        self.assertIn("limit=100", command)
        self.assertIn("offset=0", command)
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            config.NEXUS_CLI_TIMEOUT_SEC,
        )

    @patch.object(nexus_client, "_run", return_value=(0, "[]", ""))
    def test_recovery_enumeration_never_uses_heuristic_server_side_amount_filter(self, run):
        """Recovery must never skip credits through an unverified nested WHERE clause."""
        nexus_client.fetch_deposits_since("TREASURY", since_timestamp=0)

        command = run.call_args.args[0]
        self.assertFalse(any(str(argument).startswith("where=") for argument in command))

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    @patch.object(nexus_client, "_run", side_effect=TimeoutError("node timeout"))
    def test_nexus_enumeration_failure_holds_waterline(self, _run, propose_waterline):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        propose_waterline.assert_not_called()

    @patch.object(swap_nexus.nexus_client, "get_heartbeat_asset", return_value=None)
    @patch.object(nexus_client, "_run", side_effect=TimeoutError("node timeout"))
    def test_nexus_poller_enumeration_error_is_structured_event(self, _run, _heartbeat):
        """A Nexus enumeration failure must be machine-readable and keep its page context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                swap_nexus, "_log"
            ) as log:
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        self.assertIn(
            call(
                "NEXUS_ENUMERATION_FAILED",
                page=0,
                reason="exception",
                error="node timeout",
            ),
            log.call_args_list,
        )

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    @patch.object(nexus_client, "_run", return_value=(0, "[]", ""))
    def test_empty_successful_nexus_enumeration_holds_waterline(
        self, _run, propose_waterline
    ):
        """An empty page cannot prove that the live Nexus history is complete."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        propose_waterline.assert_not_called()

    @patch.object(swap_nexus.state_db, "propose_nexus_waterline")
    @patch.object(nexus_client, "_run", return_value=(0, '{"unexpected": true}', ""))
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
    @patch.object(
        nexus_client,
        "_run",
        return_value=(0, json.dumps([{
            "txid": "credit-tx",
            "timestamp": 1_000,
            "contracts": [{"OP": "CREDIT", "from": "sender", "to": "TREASURY"}],
        }]), ""),
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
        poll_output = json.dumps(transactions)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                nexus_client, "_run", return_value=(0, poll_output, "")
            ):
                state_db.init_db()
                swap_nexus.poll_nexus_deposits()

        propose_waterline.assert_not_called()

    def test_confirmed_txid_with_wrong_debit_terms_remains_held(self):
        """Confirmation count alone must not archive an unrelated Nexus debit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                nexus_client,
                "get_transactions_confirmations",
                return_value=nexus_client.BatchLookup({"unrelated-confirmed-tx": 10}, True),
            ), patch.object(
                nexus_client,
                "find_nexus_transfer_debits_by_references",
                return_value=nexus_client.BatchLookup({"77": [
                    nexus_client.TransferDebitEvidence(
                        remote_txid="unrelated-confirmed-tx", contract_id=0,
                        from_address=config.NEXUS_TOKEN_REGISTER_ADDRESS,
                        to_address="wrong-recipient", amount_usdd_units=1_898_000,
                    )
                ]}, True),
            ):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "mint-sig", 100, "nexus:recipient", "sender", 2_000_000,
                    "debited, awaiting confirmation", "unrelated-confirmed-tx",
                )
                state_db.set_unprocessed_sig_debit_intent("mint-sig", 77, 1_898_000)

                self.assertEqual(nexus_client.check_unconfirmed_debits(10, 8), 0)
                self.assertTrue(state_db.is_unprocessed_sig("mint-sig"))
                self.assertFalse(state_db.is_processed_sig("mint-sig"))

    def test_confirmed_mint_archives_its_own_persisted_reference(self):
        """A concurrent later debit must not replace this mint's on-chain identity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                nexus_client,
                "get_transactions_confirmations",
                return_value=nexus_client.BatchLookup({"mint-tx": 10}, True),
            ), patch.object(
                nexus_client,
                "find_nexus_transfer_debits_by_references",
                return_value=nexus_client.BatchLookup({"77": [
                    nexus_client.TransferDebitEvidence(
                        remote_txid="mint-tx", contract_id=0,
                        from_address=config.NEXUS_TOKEN_REGISTER_ADDRESS, to_address="recipient",
                        amount_usdd_units=nexus_client.get_nexus_send_amount_units(2_000_000),
                    )
                ]}, True),
            ):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "mint-sig", 100, "nexus:recipient", "sender", 2_000_000,
                    "debited, awaiting confirmation", "mint-tx",
                )
                state_db.set_unprocessed_sig_debit_intent(
                    "mint-sig", 77, nexus_client.get_nexus_send_amount_units(2_000_000)
                )
                # Simulate another worker advancing the global counter/reference history.
                state_db.mark_processed_sig(
                    "later-mint", 101, 2_000_000, "later-tx", 0.0,
                    "debit_confirmed", 99,
                    amount_usdd_units=nexus_client.get_nexus_send_amount_units(2_000_000),
                    nexus_destination="other",
                    memo="nexus:other",
                )

                self.assertEqual(nexus_client.check_unconfirmed_debits(10, 8), 1)
                conn = sqlite3.connect(db_path)
                try:
                    archived_reference = conn.execute(
                        "SELECT reference FROM processed_sigs WHERE sig = 'mint-sig'"
                    ).fetchone()[0]
                finally:
                    conn.close()

        self.assertEqual(archived_reference, 77)

    def test_persisted_mint_reference_cannot_be_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "mint-sig", 100, "nexus:recipient", "sender", 2_000_000,
                    "debit in flight", None,
                )
                state_db.set_unprocessed_sig_reference("mint-sig", 77)
                with self.assertRaisesRegex(ValueError, "different Nexus reference"):
                    state_db.set_unprocessed_sig_reference("mint-sig", 78)
                self.assertEqual(state_db.get_unprocessed_sig_reference("mint-sig"), 77)

    def test_persisting_debit_intent_atomically_enters_restart_recovery_state(self):
        """A crash after intent persistence must be resolved, never retried as ready."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    "crash-window-sig", 100, "nexus:recipient", "sender", 2_000_000,
                    "ready for processing", None,
                )
                state_db.set_unprocessed_sig_debit_intent(
                    "crash-window-sig", 77, 1_898_000
                )

                self.assertEqual(
                    state_db.get_unprocessed_sig_status("crash-window-sig"),
                    "debit in flight",
                )
                pending = state_db.get_sigs_pending_debit_verification(
                    nexus_client.DEBIT_UNVERIFIED_STATUSES
                )

        self.assertEqual([row[0] for row in pending], ["crash-window-sig"])

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
                remote = nexus_client.NexusMintDebitEvidence(
                    remote_txid="mint-tx", timestamp=100, confirmations=10,
                    from_address="TOKEN", to_address="recipient", amount_usdd_units=nexus_units,
                    reference="77", contract_id=0,
                )

                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({"mint-tx": [remote]}, True),
                ):
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
                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({"mint-tx": [remote]}, True),
                ):
                    duplicate = balance_reconciler.run_balance_reconciliation(waterline_ts=0)
                self.assertFalse(duplicate["healthy"])
                self.assertEqual(duplicate["total_surplus_nexus_units"], nexus_units)
                self.assertEqual(duplicate["discrepancies"], [{
                    "account": "recipient", "surplus_nexus_units": nexus_units,
                }])

    def test_reconciliation_detects_unrecorded_duplicate_in_remote_nexus_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                solana_units = 2_000_000
                nexus_units = nexus_client.get_nexus_send_amount_units(solana_units)
                state_db.mark_processed_sig(
                    "mint-sig", 100, solana_units, "mint-tx", 0.0,
                    "debit_confirmed", 77, amount_usdd_units=nexus_units,
                    nexus_destination="recipient", memo="nexus:recipient",
                )
                remote = [
                    nexus_client.NexusMintDebitEvidence(
                        remote_txid=txid, timestamp=timestamp, confirmations=10,
                        from_address="TOKEN", to_address="recipient",
                        amount_usdd_units=nexus_units,
                        reference="77", contract_id=0,
                    )
                    for txid, timestamp in (("mint-tx", 100),)
                ]
                remote.append(nexus_client.NexusMintDebitEvidence(
                    remote_txid="duplicate-tx", timestamp=101, confirmations=10,
                    from_address="TOKEN", to_address="attacker",
                    amount_usdd_units=nexus_units, reference="88", contract_id=0,
                ))
                # Token history also carries account-to-account movements. A treasury
                # transfer to the same recipient is not a second token-supply mint.
                remote.append(nexus_client.NexusMintDebitEvidence(
                    remote_txid="account-transfer", timestamp=102, confirmations=10,
                    from_address="TREASURY", to_address="recipient",
                    amount_usdd_units=123_000, reference="900", contract_id=0,
                ))
                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({
                        evidence.remote_txid: [evidence] for evidence in remote
                    }, True),
                ):
                    result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        self.assertEqual(result["total_surplus_nexus_units"], nexus_units)
        self.assertEqual(result["discrepancies"], [{
            "account": "attacker", "surplus_nexus_units": nexus_units,
        }])

    def test_incomplete_remote_nexus_history_cannot_reconcile_green(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                nexus_units = nexus_client.get_nexus_send_amount_units(2_000_000)
                state_db.mark_processed_sig(
                    "mint-sig", 100, 2_000_000, "mint-tx", 0.0,
                    "debit_confirmed", 77, amount_usdd_units=nexus_units,
                    nexus_destination="recipient", memo="nexus:recipient",
                )
                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({}, False, "pagination_truncated"),
                    create=True,
                ):
                    result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        self.assertTrue(any("pagination_truncated" in reason
                            for reason in result["incomplete_reasons"]))

    def test_active_mint_with_malformed_destination_cannot_be_skipped_green(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                nexus_units = nexus_client.get_nexus_send_amount_units(2_000_000)
                state_db.mark_processed_sig(
                    "mint-sig", 100, 2_000_000, "mint-tx", 0.0,
                    "debit_confirmed", 77, amount_usdd_units=nexus_units,
                    nexus_destination="recipient", memo="nexus:recipient",
                )
                state_db.add_unprocessed_sig(
                    "active-sig", 101, "malformed", "sender", 2_000_000,
                    "debit in flight", None,
                )
                state_db.set_unprocessed_sig_reference("active-sig", 88)
                remote = nexus_client.NexusMintDebitEvidence(
                    remote_txid="mint-tx", timestamp=100, confirmations=10,
                    from_address="TOKEN", to_address="recipient",
                    amount_usdd_units=nexus_units, reference="77", contract_id=0,
                )
                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({"mint-tx": [remote]}, True),
                ):
                    result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        self.assertTrue(any("active mint active-sig" in reason
                            for reason in result["incomplete_reasons"]))

    def test_active_first_time_recipient_uses_its_persisted_debit_amount(self):
        """A fee change after submission cannot turn an active mint into a false surplus.

        The first completed recipient establishes the Nexus token-supply source.  A
        second, first-time recipient is then still awaiting terminal confirmation. Its
        exact output must be the amount persisted before the Nexus debit, rather than
        a fee calculation recomputed under later operator configuration.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                completed_units = 1_111_000
                submitted_units = 2_222_000
                state_db.mark_processed_sig(
                    "completed-sig", 100, 2_000_000, "completed-tx", 0.0,
                    "debit_confirmed", 77, amount_usdd_units=completed_units,
                    nexus_destination="completed-recipient", memo="nexus:completed-recipient",
                )
                state_db.add_unprocessed_sig(
                    "active-sig", 101, "nexus:first-time-recipient", "sender", 3_000_000,
                    "debit in flight", None,
                )
                state_db.set_unprocessed_sig_debit_intent(
                    "active-sig", 88, submitted_units
                )
                remote = [
                    nexus_client.NexusMintDebitEvidence(
                        remote_txid="completed-tx", timestamp=100, confirmations=10,
                        from_address="TOKEN", to_address="completed-recipient",
                        amount_usdd_units=completed_units, reference="77", contract_id=0,
                    ),
                    nexus_client.NexusMintDebitEvidence(
                        remote_txid="active-tx", timestamp=101, confirmations=10,
                        from_address="TOKEN", to_address="first-time-recipient",
                        amount_usdd_units=submitted_units, reference="88", contract_id=1,
                    ),
                ]
                with (
                    patch.object(nexus_client, "get_nexus_send_amount_units", return_value=completed_units),
                    patch.object(
                        nexus_client, "find_nexus_mint_debits_since",
                        return_value=nexus_client.BatchLookup({
                            evidence.remote_txid: [evidence] for evidence in remote
                        }, True),
                    ),
                ):
                    result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        self.assertEqual(result["total_surplus_nexus_units"], 0)
        self.assertEqual(result["discrepancies"], [])
        self.assertTrue(any("active mint active-sig remains debit in flight" in reason
                            for reason in result["incomplete_reasons"]))

    def test_first_active_mint_still_scans_remote_history(self):
        """The first submitted mint is observed even before any completed recipient exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                issued_units = nexus_client.get_nexus_send_amount_units(2_000_000)
                state_db.add_unprocessed_sig(
                    "first-sig", 100, "nexus:first-recipient", "sender", 2_000_000,
                    "debited, awaiting confirmation", "first-tx",
                )
                state_db.set_unprocessed_sig_debit_intent("first-sig", 77, issued_units)
                remote = nexus_client.NexusMintDebitEvidence(
                    remote_txid="first-tx", timestamp=100, confirmations=10,
                    from_address="TOKEN", to_address="first-recipient",
                    amount_usdd_units=issued_units, reference="77", contract_id=0,
                )
                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({"first-tx": [remote]}, True),
                ) as history:
                    result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        history.assert_called_once_with([], 0)
        self.assertTrue(any("active mint first-sig remains" in reason
                            for reason in result["incomplete_reasons"]))

    def test_completed_mint_uses_its_immutable_output_after_fee_change(self):
        """A historically confirmed mint remains reconcilable after fee policy changes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                issued_units = 2_222_000
                state_db.mark_processed_sig(
                    "completed-sig", 100, 2_000_000, "completed-tx", 0.0,
                    "debit_confirmed", 77, amount_usdd_units=issued_units,
                    nexus_destination="recipient", memo="nexus:recipient",
                )
                remote = nexus_client.NexusMintDebitEvidence(
                    remote_txid="completed-tx", timestamp=100, confirmations=10,
                    from_address="TOKEN", to_address="recipient",
                    amount_usdd_units=issued_units, reference="77", contract_id=0,
                )
                # The mutable current fee calculation is deliberately different from
                # the output that was durably fixed before the historical debit.
                with (
                    patch.object(nexus_client, "get_nexus_send_amount_units", return_value=1_111_000),
                    patch.object(
                        nexus_client, "find_nexus_mint_debits_since",
                        return_value=nexus_client.BatchLookup({"completed-tx": [remote]}, True),
                    ),
                ):
                    result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertTrue(result["healthy"])
        self.assertEqual(result["total_surplus_nexus_units"], 0)

    def test_reconciliation_snapshot_keeps_transitioning_active_mint_consumed(self):
        """An active→completed transition cannot expose its remote debit as a surplus."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                completed_units = nexus_client.get_nexus_send_amount_units(2_000_000)
                active_units = nexus_client.get_nexus_send_amount_units(3_000_000)
                state_db.mark_processed_sig(
                    "completed-sig", 100, 2_000_000, "completed-tx", 0.0,
                    "debit_confirmed", 77, amount_usdd_units=completed_units,
                    nexus_destination="completed-recipient", memo="nexus:completed-recipient",
                )
                state_db.add_unprocessed_sig(
                    "active-sig", 101, "nexus:first-time-recipient", "sender", 3_000_000,
                    "debited, awaiting confirmation", "active-tx",
                )
                state_db.set_unprocessed_sig_debit_intent("active-sig", 88, active_units)
                completed_rows, active_rows = balance_reconciler._mint_reconciliation_snapshot(0)

                # Simulate the confirmation worker committing after reconciliation has
                # captured its local evidence but before remote history is reconciled.
                state_db.mark_processed_sig(
                    "active-sig", 101, 3_000_000, "active-tx", 0.0,
                    "debit_confirmed", 88, amount_usdd_units=active_units,
                    nexus_destination="first-time-recipient", memo="nexus:first-time-recipient",
                )
                state_db.remove_unprocessed_sig("active-sig")
                remote = [
                    nexus_client.NexusMintDebitEvidence(
                        remote_txid="completed-tx", timestamp=100, confirmations=10,
                        from_address="TOKEN", to_address="completed-recipient",
                        amount_usdd_units=completed_units, reference="77", contract_id=0,
                    ),
                    nexus_client.NexusMintDebitEvidence(
                        remote_txid="active-tx", timestamp=101, confirmations=10,
                        from_address="TOKEN", to_address="first-time-recipient",
                        amount_usdd_units=active_units, reference="88", contract_id=1,
                    ),
                ]
                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({
                        evidence.remote_txid: [evidence] for evidence in remote
                    }, True),
                ):
                    surplus, incomplete = balance_reconciler._reconcile_remote_mint_history(
                        ["completed-recipient"], 0,
                        completed_rows=completed_rows, active_rows=active_rows,
                    )

        self.assertEqual(surplus, {})
        self.assertTrue(any("active mint active-sig remains" in reason for reason in incomplete))

    def test_one_remote_mint_cannot_satisfy_two_completed_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                nexus_units = nexus_client.get_nexus_send_amount_units(2_000_000)
                for sig in ("mint-a", "mint-b"):
                    state_db.mark_processed_sig(
                        sig, 100, 2_000_000, "shared-tx", 0.0,
                        "debit_confirmed", 77, amount_usdd_units=nexus_units,
                        nexus_destination="recipient", memo="nexus:recipient",
                    )
                remote = nexus_client.NexusMintDebitEvidence(
                    remote_txid="shared-tx", timestamp=100, confirmations=10,
                    from_address="TOKEN", to_address="recipient",
                    amount_usdd_units=nexus_units, reference="77", contract_id=0,
                )
                with patch.object(
                    nexus_client, "find_nexus_mint_debits_since",
                    return_value=nexus_client.BatchLookup({"shared-tx": [remote]}, True),
                ):
                    result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        self.assertTrue(any("no unique exact" in reason
                            for reason in result["incomplete_reasons"]))

    @patch.object(nexus_client, "_run")
    def test_remote_nexus_mint_history_preserves_exact_contract_evidence(self, run):
        run.return_value = (0, json.dumps([{
            "txid": "mint-tx",
            "timestamp": 100,
            "confirmations": 12,
            "contracts": [{
                "id": 3,
                "OP": "DEBIT",
                "from": "TOKEN",
                "to": "recipient",
                "amount": "1.898",
                "reference": 77,
            }],
        }]), "")

        lookup = nexus_client.find_nexus_mint_debits_since({"recipient"}, 0)

        self.assertTrue(lookup.complete)
        self.assertEqual(lookup.values, {"mint-tx": [
            nexus_client.NexusMintDebitEvidence(
                remote_txid="mint-tx", timestamp=100, confirmations=12,
                from_address="TOKEN", to_address="recipient",
                amount_usdd_units=1_898_000,
                reference="77", contract_id=3,
            )
        ]})
        command = run.call_args.args[0]
        self.assertIn("contracts.reference", command[1])
        self.assertIn("contracts.amount", command[1])
        self.assertFalse(any(str(argument).startswith("where=") for argument in command))

    @patch.object(nexus_client, "_run")
    def test_conflicting_remote_contract_identity_is_incomplete(self, run):
        transactions = []
        for destination in ("recipient", "attacker"):
            transactions.append({
                "txid": "same-tx",
                "timestamp": 100,
                "confirmations": 10,
                "contracts": [{
                    "id": 0, "OP": "DEBIT", "from": "TOKEN",
                    "to": destination, "amount": "1.0", "reference": 77,
                }],
            })
        run.return_value = (0, json.dumps(transactions), "")

        lookup = nexus_client.find_nexus_mint_debits_since({"recipient"}, 0)

        self.assertFalse(lookup.complete)
        self.assertEqual(lookup.reason, "conflicting_contract_identity")

    @patch.object(nexus_client, "_run")
    def test_missing_remote_contract_id_is_incomplete(self, run):
        run.return_value = (0, json.dumps([{
            "txid": "mint-tx", "timestamp": 100, "confirmations": 10,
            "contracts": [{
                "OP": "DEBIT", "from": "TOKEN", "to": "recipient",
                "amount": "1.0", "reference": 77,
            }],
        }]), "")

        lookup = nexus_client.find_nexus_mint_debits_since({"recipient"}, 0)

        self.assertFalse(lookup.complete)
        self.assertEqual(lookup.reason, "invalid_contract_id")

    @patch.object(nexus_client, "_run")
    def test_truncated_remote_mint_history_is_explicitly_incomplete(self, run):
        run.return_value = (0, json.dumps([{
            "txid": f"tx-{index}",
            "timestamp": 1_000 - index,
            "confirmations": 10,
            "contracts": [],
        } for index in range(100)]), "")

        lookup = nexus_client.find_nexus_mint_debits_since({"recipient"}, 0)

        self.assertFalse(lookup.complete)
        self.assertEqual(lookup.reason, "pagination_snapshot_unavailable")

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

    def test_reconciliation_rejects_fractional_sqlite_base_unit_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                state_db.mark_processed_sig(
                    "mint-sig", 100, 2_000_000, "mint-tx", 0.0,
                    "debit_confirmed", 77, amount_usdd_units=1_898_000,
                    nexus_destination="recipient", memo="nexus:recipient",
                )
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        "UPDATE processed_sigs SET amount_usdd_units = ? WHERE sig = ?",
                        (1_898_000.5, "mint-sig"),
                    )
                    conn.commit()
                finally:
                    conn.close()
                result = balance_reconciler.run_balance_reconciliation(waterline_ts=0)

        self.assertFalse(result["healthy"])
        self.assertTrue(any("non-integer base-unit evidence" in reason
                            for reason in result["incomplete_reasons"]))

    @patch.object(main.alerts, "critical")
    def test_reconciliation_failure_latches_exposure_until_an_explicitly_healthy_result(self, critical):
        """An incomplete Nexus mint history must block new Solana/Nexus exposure until green."""
        paused = main.update_reconciliation_exposure_pause(False, {
            "healthy": False,
            "checked_addresses": 0,
            "discrepancies": [],
            "incomplete_reasons": ["Nexus history incomplete"],
            "account_errors": [],
        })
        self.assertTrue(paused)

        resumed = main.update_reconciliation_exposure_pause(paused, {
            "healthy": True,
            "checked_addresses": 1,
            "discrepancies": [],
            "incomplete_reasons": [],
            "account_errors": [],
        })
        self.assertFalse(resumed)

        critical.assert_called_once_with(
            "balance_reconciliation_incomplete",
            "double-mint reconciliation is not healthy; no green result is valid",
            checked_addresses=0,
            incomplete_reasons=["Nexus history incomplete"],
            account_errors=[],
        )

    def test_balance_reconciliation_becomes_due_by_elapsed_interval_not_wall_clock_modulo(self):
        """A missed exact clock second must not leave exposure paused indefinitely."""
        self.assertTrue(main.is_balance_reconciliation_due(601, 1))
        self.assertFalse(main.is_balance_reconciliation_due(600, 1))

    @patch.object(main.alerts, "critical")
    def test_reconciliation_exception_latches_exposure(self, critical):
        """A Nexus reconciliation exception is not permission to keep accepting deposits."""
        paused = main.update_reconciliation_exposure_pause(
            False, error=RuntimeError("Nexus node timeout")
        )

        self.assertTrue(paused)
        critical.assert_called_once_with(
            "balance_reconciliation_incomplete",
            "double-mint reconciliation failed; new exposure remains paused until an explicitly healthy result",
            checked_addresses=0,
            incomplete_reasons=["balance reconciliation failed: Nexus node timeout"],
            account_errors=[],
        )

    def test_unhealthy_startup_reconciliation_runs_solana_poller_in_exposure_pause_mode(self):
        """An unhealthy Nexus read-back must block new Solana deposits, not merely alert."""
        unhealthy = {
            "healthy": False, "checked_addresses": 0, "discrepancies": [],
            "incomplete_reasons": ["Nexus history incomplete"], "account_errors": [],
        }

        def run_one_poller(func, label, _budget):
            func()
            if label == "solana":
                main._stop_event.set()

        recovery = {
            "reference_seeded": False, "interrupted_nexus_transfers_held": 0,
            "added_nexus_processed": 0, "added_refunded_sigs": 0,
            "found_nexus_memos": 0, "found_refund_memos": 0,
        }
        with (
            patch.object(main, "validate_production_controls", return_value=True),
            patch.object(main.state_db, "init_db"),
            patch.object(main, "acquire_singleton_lock", return_value=True),
            patch.object(nexus_client, "validate_session_config", return_value=(True, "ok")),
            patch.object(nexus_client, "validate_heartbeat_asset", return_value=(True, "ok")),
            patch.object(solana_client, "get_token_account_balance", return_value=10_000_000),
            patch.object(nexus_client, "get_circulating_nexus_supply", return_value=10),
            patch.object(startup_recovery, "perform_startup_recovery", return_value=recovery),
            patch.object(balance_reconciler, "run_balance_reconciliation", return_value=unhealthy),
            patch.object(fees, "maintain_backing_and_bounds", return_value=False),
            patch.object(main.time, "time", return_value=601),
            patch.object(main, "_run_with_watchdog", side_effect=run_one_poller),
            patch.object(main, "poll_solana_deposits") as poll_solana,
            patch.object(main.alerts, "critical"),
        ):
            main.run()

        poll_solana.assert_called_once_with(paused=True)

    @patch.object(main.alerts, "critical")
    def test_startup_reconciliation_alerts_when_evidence_is_unhealthy(self, critical):
        """Startup must not report zero checked recipients as a green reconciliation."""
        result = {
            "healthy": False,
            "checked_addresses": 0,
            "discrepancies": [],
            "incomplete_reasons": ["no completed mint recipients were checked"],
            "account_errors": [],
        }

        main.report_startup_balance_reconciliation(result)

        critical.assert_called_once_with(
            "balance_reconciliation_incomplete",
            "double-mint reconciliation is not healthy; no green result is valid",
            checked_addresses=0,
            incomplete_reasons=["no completed mint recipients were checked"],
            account_errors=[],
        )

    @patch.object(main.alerts, "critical")
    def test_startup_reconciliation_alerts_when_no_result_is_returned(self, critical):
        """An invalid reconciliation result must be an explicit safety event."""
        main.report_startup_balance_reconciliation(None)

        critical.assert_called_once_with(
            "balance_reconciliation_incomplete",
            "double-mint reconciliation returned no result; no green result is valid",
            checked_addresses=0,
            incomplete_reasons=["balance reconciliation returned no result"],
            account_errors=[],
        )

    def test_nexus_transfer_intent_rejects_non_integer_base_units(self):
        """A durable Nexus debit intent must never truncate an inexact money value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                for invalid_units in (True, 1.9, Decimal("1"), "1000000"):
                    with self.subTest(invalid_units=repr(invalid_units)):
                        with self.assertRaisesRegex(ValueError, "exact positive integer"):
                            state_db.create_nexus_transfer_intent(
                                kind="refund",
                                source_txid=f"credit-invalid-{repr(invalid_units)}",
                                from_address="TREASURY",
                                to_address="sender",
                                amount_usdd_units=cast(int, invalid_units),
                            )

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

    def test_nexus_transfer_intent_requires_audited_preparation_before_authorization(self):
        """A direct API caller cannot skip the operator's durable preparation decision."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-missing-preparation",
                    from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                )
                with self.assertRaisesRegex(ValueError, "audited preparation"):
                    state_db.authorize_nexus_transfer_intent(
                        intent["id"], actor="alice", rationale="reviewed",
                        expected_reference=intent["reference"],
                    )

    @patch.object(nexus_client, "_run", return_value=(0, '{"txid":"must-not-run"}', ""))
    def test_nexus_transfer_intent_requires_audited_execution_request_before_debit(self, run):
        """A direct API caller cannot consume authorization without a named execution request."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-missing-execution-request",
                    from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="alice", rationale="reviewed source credit",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="alice", rationale="approved transfer",
                    expected_reference=intent["reference"],
                )

                outcome = nexus_client.execute_nexus_transfer_intent(intent["id"])
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.status, "authorized")
        self.assertEqual(stored["status"], "authorized")
        run.assert_not_called()

    def test_nexus_transfer_intent_rejects_second_disposition_for_same_credit(self):
        """One source credit must never authorize both a refund and quarantine debit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                refund = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-single-disposition",
                    from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                )
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    state_db.create_nexus_transfer_intent(
                        kind="quarantine", source_txid="credit-single-disposition",
                        from_address="TREASURY", to_address="QUARANTINE",
                        amount_usdd_units=1_000_000,
                    )
                intents = state_db.get_nexus_transfer_intents_by_status(("prepared",))

        self.assertEqual(intents, [refund])

    def test_transfer_intent_migration_refuses_preexisting_duplicate_source(self):
        """An unsafe pre-upgrade ledger is held for manual evidence-based resolution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("""CREATE TABLE nexus_transfer_intents (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_txid TEXT NOT NULL,
                    from_address TEXT NOT NULL, to_address TEXT NOT NULL,
                    amount_usdd_units INTEGER NOT NULL, reference TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, remote_txid TEXT, created_timestamp INTEGER NOT NULL,
                    last_attempt_timestamp INTEGER, resolved_timestamp INTEGER,
                    UNIQUE(kind, source_txid)
                )""")
                for kind, intent_id, reference in (
                    ("refund", "old-refund", "bridge-xfer:old-refund"),
                    ("quarantine", "old-quarantine", "bridge-xfer:old-quarantine"),
                ):
                    conn.execute(
                        """INSERT INTO nexus_transfer_intents
                           VALUES (?, ?, 'credit-duplicate', 'TREASURY', 'destination', 100,
                                   ?, 'prepared', NULL, 1, NULL, NULL)""",
                        (intent_id, kind, reference),
                    )
                conn.commit()
            finally:
                conn.close()
            with patch.object(state_db, "DB_PATH", db_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe duplicate Nexus transfer intents"):
                    state_db.init_db()


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
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
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

    def test_submitted_nexus_transfer_intent_refuses_remote_txid_replacement(self):
        """A persisted Nexus txid is immutable through final resolution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-immutable-remote-txid",
                    from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
                )
                state_db.claim_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(
                    intent["id"], status="submitted", remote_txid="returned-txid"
                )

                with self.assertRaisesRegex(ValueError, "remote txid.*immutable"):
                    state_db.update_nexus_transfer_intent(
                        intent["id"], status="completed", remote_txid="replacement-txid", resolved=True
                    )
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertEqual(stored["status"], "submitted")
        self.assertEqual(stored["remote_txid"], "returned-txid")

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
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
                )
                state_db.claim_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(
                    intent["id"], status="submitted", remote_txid="chain-tx"
                )
                state_db.update_nexus_transfer_intent(
                    intent["id"], status="completed", remote_txid="chain-tx",
                    contract_id=0, resolved=True
                )

                with self.assertRaises(ValueError):
                    state_db.update_nexus_transfer_intent(
                        intent["id"], status="outcome_unknown"
                    )
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["remote_txid"], "chain-tx")
        self.assertIsNotNone(stored["resolved_timestamp"])

    @patch.object(nexus_client, "find_nexus_transfer_debits_by_references")
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
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
                )
                result = nexus_client.execute_nexus_transfer_intent(intent["id"])
                again = nexus_client.execute_nexus_transfer_intent(intent["id"])
                find_by_reference.return_value = nexus_client.BatchLookup({}, False, "timeout")
                unresolved = nexus_client.resolve_nexus_transfer_intents()
                find_by_reference.return_value = nexus_client.BatchLookup(
                    {intent["reference"]: [nexus_client.TransferDebitEvidence(
                        remote_txid="chain-txid", contract_id=0, from_address="TREASURY",
                        to_address="QUARANTINE", amount_usdd_units=2_000_000,
                    )]},
                    True,
                )
                resolved = nexus_client.resolve_nexus_transfer_intents()
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertEqual(result.status, "outcome_unknown")
        self.assertFalse(again.executed)
        self.assertEqual(unresolved, 0)
        self.assertEqual(resolved, 1)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["remote_txid"], "chain-txid")
        self.assertEqual(stored["contract_id"], 0)
        self.assertEqual(run.call_count, 1)

    def test_transfer_resolution_holds_single_candidate_from_incomplete_lookup(self):
        """One observed DEBIT cannot prove global uniqueness from a bounded scan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                nexus_client, "find_nexus_transfer_debits_by_references"
            ) as lookup:
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-incomplete-lookup",
                    from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
                )
                state_db.claim_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(intent["id"], status="outcome_unknown")
                lookup.return_value = nexus_client.BatchLookup({intent["reference"]: [
                    nexus_client.TransferDebitEvidence(
                        remote_txid="observed-only-tx", contract_id=0,
                        from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                    )
                ]}, False, "pagination_truncated")

                self.assertEqual(nexus_client.resolve_nexus_transfer_intents(), 0)
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertEqual(stored["status"], "outcome_unknown")
        self.assertIsNone(stored["remote_txid"])

    def test_transfer_debit_lookup_parses_nested_nexus_endpoints(self):
        """Current LLL-TAO contract filters return endpoint objects, not flat strings."""
        response = json.dumps([{
            "txid": "nested-endpoints-tx",
            "contracts": [{
                "id": 7, "OP": "DEBIT", "reference": "bridge-xfer:nested",
                "from": {"address": "TREASURY-REGISTER"},
                "to": {"address": "RECIPIENT-REGISTER"},
                "amount": "1.000000",
            }],
        }])
        with patch.object(nexus_client, "_run", return_value=(0, response, "")):
            lookup = nexus_client.find_nexus_transfer_debits_by_references(
                ["bridge-xfer:nested"]
            )

        evidence = lookup.values["bridge-xfer:nested"][0]
        self.assertEqual(evidence.from_address, "TREASURY-REGISTER")
        self.assertEqual(evidence.to_address, "RECIPIENT-REGISTER")
        self.assertEqual(evidence.contract_id, 7)

    def test_transfer_resolution_holds_two_exact_contracts_in_one_transaction(self):
        """A pair of exact contracts sharing a txid must never complete one transfer intent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-ambiguous-contracts",
                    from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
                )
                state_db.claim_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(intent["id"], status="outcome_unknown")
                response = [{
                    "txid": "ambiguous-transfer",
                    "contracts": [
                        {"id": 0, "OP": "DEBIT", "reference": intent["reference"],
                         "from": "TREASURY", "to": "sender", "amount": "1.0"},
                        {"id": 1, "OP": "DEBIT", "reference": intent["reference"],
                         "from": "TREASURY", "to": "sender", "amount": "1.0"},
                    ],
                }]
                with patch.object(nexus_client, "_run", return_value=(0, json.dumps(response), "")):
                    resolved = nexus_client.resolve_nexus_transfer_intents()
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        assert stored is not None
        self.assertEqual(resolved, 0)
        self.assertEqual(stored["status"], "outcome_unknown")
        self.assertIsNone(stored["remote_txid"])

    @patch("builtins.print")
    @patch.object(nexus_client, "_run", return_value=(1, "", "Nexus API request failed"))
    def test_ambiguous_nexus_transfer_client_never_uses_console_prose(self, _run, print_mock):
        """A durable Nexus transfer hold must be emitted to structured logging, not print()."""
        intent = {
            "id": "intent-structured-log",
            "from_address": "TREASURY",
            "to_address": "QUARANTINE",
            "amount_usdd_units": 2_000_000,
            "reference": "bridge-xfer:intent-structured-log",
        }
        with patch.object(nexus_client.config, "NEXUS_PIN", "test-pin"), patch.object(
            nexus_client.state_db, "claim_nexus_transfer_intent", return_value=intent
        ), patch.object(nexus_client.state_db, "update_nexus_transfer_intent"), patch.object(
            nexus_client, "_log"
        ) as log:
            result = nexus_client.execute_nexus_transfer_intent(intent["id"])

        self.assertEqual(result.status, "outcome_unknown")
        print_mock.assert_not_called()
        log.assert_called_once_with(
            "NEXUS_TRANSFER_OUTCOME_UNKNOWN",
            level=logging.WARNING,
            intent_id="intent-structured-log",
            reference="bridge-xfer:intent-structured-log",
            reason="cli_error",
            error="Nexus API request failed",
        )

    @patch.object(nexus_client, "_run", return_value=(1, "", "Nexus API request failed"))
    def test_nexus_transfer_outcome_is_persisted_when_structured_logging_fails(self, _run):
        """An observability failure must never make an ambiguous Nexus debit retryable."""
        intent = {
            "id": "intent-log-failure",
            "from_address": "TREASURY",
            "to_address": "QUARANTINE",
            "amount_usdd_units": 2_000_000,
            "reference": "bridge-xfer:intent-log-failure",
        }
        with patch.object(nexus_client.config, "NEXUS_PIN", "test-pin"), patch.object(
            nexus_client.state_db, "claim_nexus_transfer_intent", return_value=intent
        ), patch.object(
            nexus_client.state_db, "update_nexus_transfer_intent"
        ) as update, patch.object(
            nexus_client.structured_logging, "emit", side_effect=RuntimeError("logger failed")
        ):
            result = nexus_client.execute_nexus_transfer_intent(intent["id"])

        self.assertEqual(result.status, "outcome_unknown")
        update.assert_called_once_with(intent["id"], status="outcome_unknown")

    def test_restart_marks_interrupted_nexus_transfer_as_outcome_unknown_without_reexecution(self):
        """A crash after claiming an intent leaves an explicit hold, never a second debit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-crash-after-claim",
                    from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
                )
                self.assertIsNotNone(state_db.claim_nexus_transfer_intent(intent["id"]))

                recovered = state_db.recover_interrupted_nexus_transfer_intents()
                stored = state_db.get_nexus_transfer_intent(intent["id"])
                repeated = nexus_client.execute_nexus_transfer_intent(intent["id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "outcome_unknown")
        self.assertFalse(repeated.executed)
        self.assertEqual(repeated.status, "outcome_unknown")

    @patch.object(startup_recovery.nexus_client, "get_last_reference", return_value=99)
    @patch.object(startup_recovery, "_fallback_recent_scan", return_value={"fallback_mode": True})
    @patch.object(startup_recovery.nexus_client, "get_heartbeat_asset", return_value=None)
    @patch.object(state_db, "recover_interrupted_nexus_transfer_intents", return_value=1)
    def test_startup_recovery_holds_interrupted_nexus_transfers_before_scanning(
        self, recover, _heartbeat, _fallback, _reference
    ):
        stats = startup_recovery.perform_startup_recovery()

        recover.assert_called_once_with()
        self.assertEqual(stats["interrupted_nexus_transfers_held"], 1)

    def test_transfer_resolution_rejects_reference_match_with_wrong_debit_terms(self):
        """A public reference alone must not release a held credit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path):
                state_db.init_db()
                intent = state_db.create_nexus_transfer_intent(
                    kind="refund", source_txid="credit-wrong-terms", from_address="TREASURY",
                    to_address="sender", amount_usdd_units=1_000_000,
                )
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="test-operator", rationale="test preparation",
                )
                state_db.authorize_nexus_transfer_intent(
                    intent["id"], actor="test-operator", rationale="test authorization",
                    expected_reference=intent["reference"],
                )
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="test-operator", rationale="test execution request",
                )
                state_db.claim_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(intent["id"], status="outcome_unknown")
                response = [{
                    "txid": "unrelated-debit",
                    "contracts": [{
                        "OP": "DEBIT",
                        "reference": intent["reference"],
                        "from": "UNRELATED_SOURCE",
                        "to": "UNRELATED_DESTINATION",
                        "amount": "999.0",
                    }],
                }]
                with patch.object(nexus_client, "_run", return_value=(0, json.dumps(response), "")) as run:
                    resolved = nexus_client.resolve_nexus_transfer_intents()
                stored = state_db.get_nexus_transfer_intent(intent["id"])

        self.assertEqual(resolved, 0)
        self.assertEqual(stored["status"], "outcome_unknown")
        self.assertIsNone(stored["remote_txid"])
        self.assertEqual(run.call_count, 1)

    @patch.object(nexus_client, "_run", return_value=(0, '{"txid":"must-not-run"}', ""))
    def test_legacy_direct_account_debit_is_fail_closed(self, run):
        """Only the durable, authorized intent workflow may issue a Nexus account debit."""
        result = nexus_client.debit_account_with_txid(
            "TREASURY", "sender", 1_000_000, "legacy-reference"
        )

        self.assertEqual(result, (False, None))
        run.assert_not_called()

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
                state_db.record_nexus_transfer_preparation(
                    intent["id"], actor="alice", rationale="mapping absent; refund prepared",
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
                state_db.record_nexus_transfer_execution_request(
                    intent["id"], actor="alice", rationale="refund execution requested",
                )
                executed = nexus_client.execute_nexus_transfer_intent(intent["id"])
                state_db.update_nexus_transfer_intent(
                    intent["id"], status="completed", remote_txid="refund-tx",
                    contract_id=0, resolved=True
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
            "prepared_refund", "authorized_execution", "execution_requested", "finalized_refund",
        ])
        self.assertTrue(all(event["actor"] == "alice" for event in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
