from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("SOLANA_RPC_URL", "http://127.0.0.1:8899")
os.environ.setdefault("VAULT_KEYPAIR", "/tmp/nonexistent-keypair.json")
os.environ.setdefault("VAULT_USDC_ACCOUNT", "11111111111111111111111111111111")
os.environ.setdefault("USDC_MINT", "11111111111111111111111111111111")
os.environ.setdefault("SOL_MINT", "11111111111111111111111111111111")
os.environ.setdefault("NEXUS_PIN", "1234")
os.environ.setdefault("NEXUS_USDD_TREASURY_ACCOUNT", "TREASURY")
os.environ.setdefault("NEXUS_TOKEN_REGISTER_ADDRESS", "TOKEN-REGISTER")
os.environ.setdefault("SOL_MAIN_ACCOUNT", "11111111111111111111111111111111")
os.environ.setdefault("NEXUS_CLI_PATH", "/bin/false")

from src import (  # noqa: E402
    balance_reconciler,
    config,
    main,
    nexus_client,
    solana_client,
    startup_recovery,
    state_db,
)
from src.nexus_memo import (  # noqa: E402
    NexusPayoutEvidence,
    NexusPayoutMemo,
    parse_nexus_payout_memo,
)


NEXUS_TXID = "ab" * 64


class NexusPayoutMemoTests(unittest.TestCase):
    def test_composite_payout_memo_round_trips_exact_source_identity(self):
        memo = parse_nexus_payout_memo(f"nexus_txid:{NEXUS_TXID}:7")

        self.assertEqual(memo, NexusPayoutMemo(txid=NEXUS_TXID, contract_id=7))
        self.assertFalse(memo.is_legacy)

    def test_legacy_payout_memo_remains_explicitly_unresolved(self):
        memo = parse_nexus_payout_memo(f"nexus_txid:{NEXUS_TXID}")

        self.assertEqual(memo, NexusPayoutMemo(txid=NEXUS_TXID, contract_id=None))
        self.assertTrue(memo.is_legacy)

    def test_malformed_or_noncanonical_payout_identity_is_rejected(self):
        malformed = (
            "nexus_txid:synthetic:1",
            f"nexus_txid:{NEXUS_TXID.upper()}:1",
            f"nexus_txid:{NEXUS_TXID}:-1",
            f"nexus_txid:{NEXUS_TXID}:01",
            f"nexus_txid:{NEXUS_TXID}:4294967296",
            f"nexus_txid:{NEXUS_TXID}:1:extra",
            f"prefix nexus_txid:{NEXUS_TXID}:1",
            f"nexus_txid:{NEXUS_TXID}:1 trailing",
        )

        for value in malformed:
            with self.subTest(value=value):
                self.assertIsNone(parse_nexus_payout_memo(value))


class _MemoRpcClient:
    def get_signatures_for_address(self, *args, **kwargs):
        raise AssertionError("patched through _rpc_call")

    def get_transaction(self, *args, **kwargs):
        raise AssertionError("patched through _rpc_call")


def _memo_transaction(memo: str, *, amount: int = 3_000_000) -> dict:
    return {
        "transaction": {"message": {
            "accountKeys": [{
                "pubkey": str(config.SOL_MAIN_ACCOUNT),
                "signer": True,
                "writable": True,
            }],
            "instructions": [
            {
                "program": "spl-token",
                "parsed": {
                    "type": "transferChecked",
                    "info": {
                        "source": str(config.VAULT_USDC_ACCOUNT),
                        "destination": "recipient-token-account",
                        "mint": str(config.USDC_MINT),
                        "tokenAmount": {"amount": str(amount)},
                    },
                },
            },
            {
                "programId": "Memo111111111111111111111111111111111111111",
                "data": memo,
            },
        ]}},
        "meta": {"err": None, "logMessages": []},
    }


class SolanaMemoScannerTests(unittest.TestCase):
    def test_recent_scanner_returns_composite_payout_identity(self):
        memo = f"nexus_txid:{NEXUS_TXID}:7"
        with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
            solana_client,
            "_rpc_call",
            side_effect=[[{"signature": "solana-payout-sig", "blockTime": 200, "confirmationStatus": "finalized", "err": None}], _memo_transaction(memo)],
        ):
            result = solana_client.scan_recent_memos(search_limit=10)

        self.assertEqual(
            result["nexus_payouts"],
            {(NEXUS_TXID, 7): NexusPayoutEvidence(
                txid=NEXUS_TXID,
                contract_id=7,
                solana_signature="solana-payout-sig",
                to_token_account="recipient-token-account",
                amount_solana_units=3_000_000,
            )},
        )

    def test_waterline_scanner_returns_composite_payout_identity_with_complete_evidence(self):
        memo = f"nexus_txid:{NEXUS_TXID}:7"
        with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
            solana_client,
            "_rpc_call",
            side_effect=[[{"signature": "solana-payout-sig", "blockTime": 200, "confirmationStatus": "finalized", "err": None}], _memo_transaction(memo)],
        ):
            result = solana_client.scan_memos_since_timestamp(100)

        self.assertTrue(result["complete"])
        self.assertIsNone(result["reason"])
        self.assertEqual(
            result["nexus_payouts"],
            {(NEXUS_TXID, 7): NexusPayoutEvidence(
                txid=NEXUS_TXID,
                contract_id=7,
                solana_signature="solana-payout-sig",
                to_token_account="recipient-token-account",
                amount_solana_units=3_000_000,
            )},
        )


class SolanaMemoLookupTests(unittest.TestCase):
    def test_memo_lookup_never_matches_contract_id_by_log_substring(self):
        requested = f"nexus_txid:{NEXUS_TXID}:1"
        tx = _memo_transaction(requested)
        tx["transaction"]["message"]["instructions"] = []
        tx["meta"]["logMessages"] = [f"{requested}0"]
        entry = {
            "signature": "wrong-sibling",
            "blockTime": 200,
            "confirmationStatus": "finalized",
            "err": None,
        }
        with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
            solana_client, "_rpc_call", side_effect=[[entry], tx]
        ):
            found = solana_client.find_signature_with_memo(requested)

        self.assertIsNone(found)

    def test_memo_lookup_rejects_failed_transaction_even_with_exact_instruction(self):
        requested = f"nexus_txid:{NEXUS_TXID}:1"
        tx = _memo_transaction(requested)
        tx["meta"]["err"] = {"InstructionError": [0, "failed"]}
        entry = {
            "signature": "failed-signature",
            "blockTime": 200,
            "confirmationStatus": "finalized",
            "err": None,
        }
        with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
            solana_client, "_rpc_call", side_effect=[[entry], tx]
        ):
            found = solana_client.find_signature_with_memo(requested)

        self.assertIsNone(found)


class StartupReconstructionTests(unittest.TestCase):
    def test_incomplete_solana_enumeration_writes_no_recovery_markers(self):
        scan = {
            "complete": False,
            "reason": "pagination_truncated",
            "nexus_payouts": {(NEXUS_TXID, 7): "solana-payout-sig"},
            "legacy_nexus_txids": {},
            "malformed_nexus_memos": [],
            "refund_sigs": {},
            "quarantined_sigs": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                solana_client, "scan_memos_since_timestamp", return_value=scan
            ):
                state_db.init_db()
                result = startup_recovery._rebuild_solana_from_waterline(100)
                conn = sqlite3.connect(db_path)
                processed = conn.execute("SELECT * FROM processed_txids").fetchall()
                conn.close()

        self.assertFalse(result["recovery_complete"])
        self.assertEqual(result["error"], "solana_memo_scan_incomplete:pagination_truncated")
        self.assertEqual(processed, [])

    def test_legacy_txid_only_payout_memo_stays_unresolved_without_synthetic_marker(self):
        scan = {
            "complete": True,
            "reason": None,
            "nexus_payouts": {},
            "legacy_nexus_txids": {NEXUS_TXID: "solana-payout-sig"},
            "malformed_nexus_memos": [],
            "refund_sigs": {},
            "quarantined_sigs": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                solana_client, "scan_memos_since_timestamp", return_value=scan
            ):
                state_db.init_db()
                result = startup_recovery._rebuild_solana_from_waterline(100)
                conn = sqlite3.connect(db_path)
                processed = conn.execute("SELECT * FROM processed_txids").fetchall()
                conn.close()

        self.assertFalse(result["recovery_complete"])
        self.assertEqual(result["error"], "legacy_nexus_payout_identity_unresolved")
        self.assertEqual(processed, [])

    def test_wipeout_roundtrip_archives_paid_sibling_and_queues_only_unpaid_sibling(self):
        tx = {
            "txid": NEXUS_TXID,
            "timestamp": 1_000,
            "confirmations": 2,
            "contracts": [
                {"id": 0, "OP": "CREDIT", "from": "sender-a", "to": "TREASURY", "amount": "3"},
                {"id": 1, "OP": "CREDIT", "from": "sender-b", "to": "TREASURY", "amount": "4"},
            ],
        }
        memo_scan = {
            "complete": True,
            "reason": None,
            "nexus_payouts": {(NEXUS_TXID, 1): NexusPayoutEvidence(
                txid=NEXUS_TXID,
                contract_id=1,
                solana_signature="solana-payout-sig",
                to_token_account="recipient-token-account",
                amount_solana_units=nexus_client.get_solana_send_amount_units(4_000_000),
            )},
            "legacy_nexus_txids": {},
            "malformed_nexus_memos": [],
            "refund_sigs": {},
            "quarantined_sigs": {},
        }
        heartbeat = {
            "address": "heartbeat-address",
            "last_poll_timestamp": "2000",
            "last_safe_timestamp_nexus": "900",
            "last_safe_timestamp_solana": "900",
        }
        pair = replace(
            config.SWAP_PAIR,
            nexus=replace(config.SWAP_PAIR.nexus, treasury_account="TREASURY"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with (
                patch.object(state_db, "DB_PATH", db_path),
                patch.object(config, "SWAP_PAIR", pair),
                patch.object(state_db, "recover_interrupted_nexus_transfer_intents", return_value=0),
                patch.object(nexus_client, "get_heartbeat_asset", return_value=heartbeat),
                patch.object(solana_client, "scan_memos_since_timestamp", return_value=memo_scan),
                patch.object(nexus_client, "fetch_deposits_since", return_value=nexus_client.DepositScan([tx], True)),
                patch.object(nexus_client, "get_account_info", return_value={"owner": "owner"}),
                patch.object(nexus_client, "get_last_reference", return_value=99),
            ):
                state_db.init_db()
                result = startup_recovery.perform_startup_recovery()
                second_result = startup_recovery.perform_startup_recovery()
                conn = sqlite3.connect(db_path)
                processed = conn.execute(
                    "SELECT txid, contract_id, amount_usdd_units, from_address, sig FROM processed_txids"
                ).fetchall()
                queued = conn.execute(
                    "SELECT txid, contract_id, amount_usdd_units, from_address FROM unprocessed_txids"
                ).fetchall()
                conn.close()

        self.assertTrue(result["recovery_complete"], result)
        self.assertTrue(second_result["recovery_complete"], second_result)
        self.assertEqual(
            processed,
            [(NEXUS_TXID, 1, 4_000_000, "sender-b", "solana-payout-sig")],
        )
        self.assertEqual(queued, [(NEXUS_TXID, 0, 3_000_000, "sender-a")])


class _AlreadyStoppedEvent:
    def is_set(self):
        return True

    def set(self):
        pass


class MainStartupRecoveryGateTests(unittest.TestCase):
    def test_recovery_error_aborts_before_heartbeat_metrics_reconciliation_or_pollers(self):
        recovery = {
            "recovery_complete": False,
            "recovery_incomplete": True,
            "error": "solana_memo_scan_incomplete:pagination_truncated",
        }
        with (
            patch.object(main, "validate_production_controls", return_value=True),
            patch.object(main.state_db, "init_db"),
            patch.object(main, "acquire_singleton_lock", return_value=True),
            patch.object(startup_recovery, "perform_startup_recovery", return_value=recovery),
            patch.object(nexus_client, "validate_session_config") as session_check,
            patch.object(nexus_client, "validate_heartbeat_asset") as heartbeat_check,
            patch.object(solana_client, "get_token_account_balance") as balance_read,
            patch.object(balance_reconciler, "run_balance_reconciliation") as reconcile,
            patch.object(main, "_run_with_watchdog") as poller,
            patch.object(main.alerts, "critical") as critical,
            patch.object(main.threading, "Event", return_value=_AlreadyStoppedEvent()),
        ):
            result = main.run()

        self.assertFalse(result)
        session_check.assert_not_called()
        heartbeat_check.assert_not_called()
        balance_read.assert_not_called()
        reconcile.assert_not_called()
        poller.assert_not_called()
        critical.assert_called_once()

    def test_recovery_exception_aborts_before_any_external_startup_check(self):
        with (
            patch.object(main, "validate_production_controls", return_value=True),
            patch.object(main.state_db, "init_db"),
            patch.object(main, "acquire_singleton_lock", return_value=True),
            patch.object(startup_recovery, "perform_startup_recovery", side_effect=RuntimeError("boom")),
            patch.object(nexus_client, "validate_heartbeat_asset") as heartbeat_check,
            patch.object(main, "_run_with_watchdog") as poller,
            patch.object(main.alerts, "critical") as critical,
        ):
            result = main.run()

        self.assertFalse(result)
        heartbeat_check.assert_not_called()
        poller.assert_not_called()
        critical.assert_called_once()

    def test_complete_recovery_allows_clean_startup_without_creating_exposure(self):
        recovery = {
            "recovery_complete": True,
            "recovery_incomplete": False,
            "reference_seeded": 9,
            "interrupted_nexus_transfers_held": 0,
        }
        healthy = {
            "healthy": True,
            "checked_addresses": 1,
            "discrepancies": [],
            "incomplete_reasons": [],
            "account_errors": [],
        }
        with (
            patch.object(main, "validate_production_controls", return_value=True),
            patch.object(main.state_db, "init_db"),
            patch.object(main, "acquire_singleton_lock", return_value=True),
            patch.object(startup_recovery, "perform_startup_recovery", return_value=recovery) as recover,
            patch.object(nexus_client, "validate_session_config", return_value=(True, "ok")),
            patch.object(nexus_client, "validate_heartbeat_asset", return_value=(True, "ok")),
            patch.object(solana_client, "get_token_account_balance", return_value=10_000_000),
            patch.object(nexus_client, "get_circulating_nexus_supply", return_value=10),
            patch.object(balance_reconciler, "run_balance_reconciliation", return_value=healthy),
            patch.object(main.threading, "Event", return_value=_AlreadyStoppedEvent()),
            patch.object(main, "_run_with_watchdog") as poller,
            patch.object(nexus_client, "update_heartbeat_asset") as heartbeat_update,
        ):
            result = main.run()

        self.assertTrue(result)
        recover.assert_called_once_with()
        poller.assert_not_called()
        heartbeat_update.assert_not_called()


class NexusLegacyWrapperTests(unittest.TestCase):
    def test_legacy_refund_wrapper_without_contract_identity_is_refused_before_intent_write(self):
        with patch.object(nexus_client.state_db, "create_nexus_transfer_intent") as create:
            result = nexus_client.refund_nexus_token(
                "sender", 1_000_000, f"missing mapping txid: {NEXUS_TXID}"
            )

        self.assertFalse(result)
        create.assert_not_called()


class NexusPaginationTests(unittest.TestCase):
    def test_mutable_offset_multi_page_scan_never_claims_snapshot_completeness(self):
        newest_page = [
            {
                "txid": f"{value:0128x}",
                "timestamp": value,
                "confirmations": 2,
                "contracts": [],
            }
            for value in range(200, 100, -1)
        ]
        terminal_page = [{
            "txid": f"{999:0128x}",
            "timestamp": 99,
            "confirmations": 2,
            "contracts": [],
        }]
        with patch.object(
            nexus_client,
            "_run",
            side_effect=[
                (0, json.dumps(newest_page), ""),
                (0, json.dumps(terminal_page), ""),
            ],
        ):
            result = nexus_client.fetch_deposits_since("TREASURY", 100, max_pages=2)

        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "pagination_snapshot_unavailable")
        self.assertEqual(result.deposits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
