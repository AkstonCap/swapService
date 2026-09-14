from __future__ import annotations

import base64
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
from src import swap_receipts  # noqa: E402
from src.nexus_memo import (  # noqa: E402
    NexusPayoutEvidence,
    NexusPayoutMemo,
    parse_nexus_payout_memo,
)


NEXUS_TXID = "ab" * 64
SOLANA_PAYOUT_SIGNATURE = "1" * 64
SOLANA_DEPOSIT_SIGNATURE = str(solana_client.Signature.from_bytes(bytes([2]) * 64))
SOLANA_REFUND_SOURCE_SIGNATURE = str(solana_client.Signature.from_bytes(bytes([3]) * 64))
SOLANA_QUARANTINE_SOURCE_SIGNATURE = str(solana_client.Signature.from_bytes(bytes([4]) * 64))
SOLANA_REFUND_PAYOUT_SIGNATURE = str(solana_client.Signature.from_bytes(bytes([5]) * 64))
SOLANA_QUARANTINE_PAYOUT_SIGNATURE = str(solana_client.Signature.from_bytes(bytes([6]) * 64))


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


def _memo_transaction(
    memo: str, *, amount: int = 3_000_000,
    destination: str = "recipient-token-account",
) -> dict:
    return {
        "transaction": {"signatures": [SOLANA_PAYOUT_SIGNATURE], "message": {
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
                        "destination": destination,
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


def _source_deposit_transaction(
    *, source_signature: str = SOLANA_DEPOSIT_SIGNATURE,
    source_token_account: str = "recipient-token-account",
    amount: int = 3_100_000,
    memo: str = "nexus:recipient",
    timestamp: int = 150,
) -> dict:
    return {
        "blockTime": timestamp,
        "transaction": {"signatures": [source_signature], "message": {
            "accountKeys": [
                {"pubkey": str(config.VAULT_USDC_ACCOUNT), "signer": False, "writable": True},
                {"pubkey": source_token_account, "signer": True, "writable": True},
            ],
            "instructions": [
                {
                    "program": "spl-token",
                    "parsed": {
                        "type": "transferChecked",
                        "info": {
                            "source": source_token_account,
                            "destination": str(config.VAULT_USDC_ACCOUNT),
                            "mint": str(config.USDC_MINT),
                            "tokenAmount": {"amount": str(amount)},
                        },
                    },
                },
                {"program": "spl-memo", "parsed": memo},
            ],
        }},
        "meta": {
            "err": None,
            "preTokenBalances": [{
                "accountIndex": 0, "mint": str(config.USDC_MINT),
                "uiTokenAmount": {"amount": "100"},
            }],
            "postTokenBalances": [{
                "accountIndex": 0, "mint": str(config.USDC_MINT),
                "uiTokenAmount": {"amount": str(amount + 100)},
            }],
            "innerInstructions": [],
            "logMessages": [],
        },
    }


class SolanaMemoScannerTests(unittest.TestCase):
    def test_recent_scanner_returns_composite_payout_identity(self):
        memo = f"nexus_txid:{NEXUS_TXID}:7"
        with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
            solana_client,
            "_rpc_call",
            side_effect=[[{"signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 200, "confirmationStatus": "finalized", "err": None}], _memo_transaction(memo)],
        ):
            result = solana_client.scan_recent_memos(search_limit=10)

        self.assertEqual(
            result["nexus_payouts"],
            {(NEXUS_TXID, 7): NexusPayoutEvidence(
                txid=NEXUS_TXID,
                contract_id=7,
                solana_signature=SOLANA_PAYOUT_SIGNATURE,
                to_token_account="recipient-token-account",
                amount_solana_units=3_000_000,
            )},
        )

    def test_waterline_scanner_returns_composite_payout_identity_with_complete_evidence(self):
        memo = f"nexus_txid:{NEXUS_TXID}:7"
        with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
            solana_client,
            "_rpc_call",
            side_effect=[[{"signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 200, "confirmationStatus": "finalized", "err": None}], _memo_transaction(memo)],
        ):
            result = solana_client.scan_memos_since_timestamp(100)

        self.assertTrue(result["complete"])
        self.assertIsNone(result["reason"])
        self.assertEqual(
            result["nexus_payouts"],
            {(NEXUS_TXID, 7): NexusPayoutEvidence(
                txid=NEXUS_TXID,
                contract_id=7,
                solana_signature=SOLANA_PAYOUT_SIGNATURE,
                to_token_account="recipient-token-account",
                amount_solana_units=3_000_000,
            )},
        )

    def test_waterline_scanner_reconstructs_current_disposition_source_and_output(self):
        """Current emitters bind the outbound spend to the exact incoming deposit."""
        for kind in ("refund", "quarantine"):
            with self.subTest(kind=kind):
                memo = solana_client._solana_sig_disposition_memo(
                    kind, SOLANA_DEPOSIT_SIGNATURE
                )
                with patch.object(
                    config, "USDC_QUARANTINE_ACCOUNT", "recipient-token-account"
                ), patch.object(
                    solana_client, "_get_client", return_value=_MemoRpcClient()
                ), patch.object(solana_client, "_rpc_call", side_effect=[[
                    {
                        "signature": SOLANA_PAYOUT_SIGNATURE,
                        "blockTime": 200,
                        "confirmationStatus": "finalized",
                        "err": None,
                    }
                ], _memo_transaction(memo), _source_deposit_transaction()]):
                    scan = solana_client.scan_memos_since_timestamp(100)

                self.assertTrue(scan["complete"], scan)
                self.assertEqual(
                    scan["solana_dispositions"],
                    {
                        (kind, SOLANA_DEPOSIT_SIGNATURE): {
                            "kind": kind,
                            "source_signature": SOLANA_DEPOSIT_SIGNATURE,
                            "solana_signature": SOLANA_PAYOUT_SIGNATURE,
                            "destination_token_account": "recipient-token-account",
                            "amount_solana_units": 3_000_000,
                            "timestamp": 200,
                            "source_timestamp": 150,
                            "source_token_account": "recipient-token-account",
                            "source_amount_solana_units": 3_100_000,
                            "source_memo": "nexus:recipient",
                        }
                    },
                )
                with patch.object(solana_client, "scan_memos_since_timestamp", return_value=scan):
                    recovered = startup_recovery._rebuild_solana_from_waterline(100)

                self.assertTrue(recovered["recovery_complete"], recovered)
                self.assertEqual(
                    recovered["_solana_dispositions"], scan["solana_dispositions"]
                )

    def test_source_deposit_parser_failure_is_logged_with_sanitized_reason(self):
        evidence = {
            "kind": "refund",
            "source_signature": SOLANA_DEPOSIT_SIGNATURE,
            "solana_signature": SOLANA_PAYOUT_SIGNATURE,
            "destination_token_account": "recipient-token-account",
            "amount_solana_units": 100,
            "timestamp": 200,
        }
        events = []
        with patch.dict(os.environ, {"NEXUS_PIN": "recovery-secret-value"}), patch.object(
            solana_client, "_rpc_call", return_value={"blockTime": 150}
        ), patch.object(
            solana_client, "_extract_core_deposit_evidence",
            side_effect=RuntimeError("parser rejected recovery-secret-value evidence"),
        ), patch.object(
            solana_client, "_log", side_effect=lambda event, **fields: events.append((event, fields))
        ):
            completed = solana_client._complete_solana_disposition_source_evidence(
                _MemoRpcClient(), evidence
            )

        self.assertIsNone(completed)
        self.assertEqual(events[0][0], "solana_disposition_source_evidence_rejected")
        self.assertIn("***", events[0][1]["reason"])
        self.assertNotIn("recovery-secret-value", events[0][1]["reason"])

    def test_waterline_scanner_never_certifies_unclassified_vault_spend(self):
        """Unrecognized wire encoding may hold recovery, never erase cap spend."""
        memo = solana_client._solana_sig_disposition_memo("refund", SOLANA_DEPOSIT_SIGNATURE)
        variants = (
            base64.b64encode(memo.encode()).decode(),
            f"swapService:v2:refund:{SOLANA_DEPOSIT_SIGNATURE}",
            "unrecognized-outbound-memo",
            None,
        )
        for wire_memo in variants:
            with self.subTest(wire_memo=wire_memo):
                transaction = _memo_transaction(memo)
                instructions = transaction["transaction"]["message"]["instructions"]
                if wire_memo is None:
                    instructions.pop()
                else:
                    instructions[-1]["data"] = wire_memo
                with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
                    solana_client, "_rpc_call", side_effect=[[
                        {"signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 200,
                         "confirmationStatus": "finalized", "err": None}
                    ], transaction],
                ):
                    scan = solana_client.scan_memos_since_timestamp(100)
                self.assertFalse(scan["complete"], scan)
                self.assertEqual(scan["reason"], "unclassified_solana_vault_spend")
                self.assertEqual(scan["nexus_payouts"], {})
                self.assertEqual(scan["solana_dispositions"], {})
                with patch.object(solana_client, "scan_memos_since_timestamp", return_value=scan):
                    recovered = startup_recovery._rebuild_solana_from_waterline(100)
                self.assertFalse(recovered["recovery_complete"])

    def test_waterline_scanner_holds_opaque_and_nested_vault_spend(self):
        for variant in ("opaque", "inner", "extra_debit", "multiple_identities"):
            with self.subTest(variant=variant):
                transaction = _memo_transaction(f"nexus_txid:{NEXUS_TXID}:7")
                instructions = transaction["transaction"]["message"]["instructions"]
                if variant == "opaque":
                    instructions[0] = {
                        "programId": str(solana_client.TOKEN_PROGRAM_ID),
                        "accounts": [str(config.VAULT_USDC_ACCOUNT)], "data": "unparsed",
                    }
                    instructions.pop()
                elif variant == "inner":
                    transaction["meta"]["innerInstructions"] = [
                        {"index": 0, "instructions": [instructions.pop(0)]}
                    ]
                    instructions.clear()
                elif variant == "extra_debit":
                    instructions.append({
                        "program": "spl-token", "parsed": {"type": "transfer", "info": {
                            "source": str(config.VAULT_USDC_ACCOUNT),
                            "destination": "another-recipient", "amount": "1000",
                        }},
                    })
                else:
                    instructions.append({"program": "spl-memo", "parsed": f"nexus_txid:{NEXUS_TXID}:8"})
                with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
                    solana_client, "_rpc_call", side_effect=[[
                        {"signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 200,
                         "confirmationStatus": "finalized", "err": None}
                    ], transaction],
                ):
                    scan = solana_client.scan_memos_since_timestamp(100)
                self.assertFalse(scan["complete"], scan)
                self.assertEqual(scan["nexus_payouts"], {})
                self.assertEqual(scan["solana_dispositions"], {})

    def test_waterline_scanner_rejects_incomplete_spend_schema(self):
        for variant in ("missing_source", "missing_info", "missing_error", "invalid_inner"):
            with self.subTest(variant=variant):
                transaction = _memo_transaction("unused")
                instructions = transaction["transaction"]["message"]["instructions"]
                instructions.pop()
                if variant == "missing_source":
                    del instructions[0]["parsed"]["info"]["source"]
                elif variant == "missing_info":
                    instructions[0]["parsed"] = {}
                elif variant == "missing_error":
                    instructions.clear()
                    del transaction["meta"]["err"]
                else:
                    instructions.clear()
                    transaction["meta"]["innerInstructions"] = {}
                with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
                    solana_client, "_rpc_call", side_effect=[[
                        {"signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 200,
                         "confirmationStatus": "finalized", "err": None}
                    ], transaction],
                ):
                    scan = solana_client.scan_memos_since_timestamp(100)
                self.assertFalse(scan["complete"], scan)
                self.assertEqual(scan["nexus_payouts"], {})

    def test_waterline_scanner_keeps_incoming_transfers_and_empty_history_available(self):
        transaction = _memo_transaction("unused")
        instructions = transaction["transaction"]["message"]["instructions"]
        instructions.pop()
        instructions[0]["parsed"]["info"].update({
            "source": "external-source", "destination": str(config.VAULT_USDC_ACCOUNT),
        })
        for responses in ([[]], [[{"signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 200,
                                    "confirmationStatus": "finalized", "err": None}], transaction]):
            with self.subTest(responses=len(responses)):
                with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
                    solana_client, "_rpc_call", side_effect=responses,
                ):
                    scan = solana_client.scan_memos_since_timestamp(100)
                self.assertTrue(scan["complete"], scan)
                self.assertEqual(scan["solana_dispositions"], {})

    def test_current_disposition_requires_exact_source_amount_recipient_and_chronology(self):
        cases = (
            ("refund", "wrong-refund-recipient", "recipient-token-account", 3_100_000, 150),
            ("refund", "recipient-token-account", "recipient-token-account", 2_900_000, 150),
            ("refund", "recipient-token-account", "recipient-token-account", 3_100_000, 201),
            ("quarantine", "wrong-quarantine-recipient", "quarantine-token-account", 3_100_000, 150),
        )
        for kind, destination, quarantine_account, source_amount, source_timestamp in cases:
            with self.subTest(kind=kind, destination=destination, source_amount=source_amount):
                memo = solana_client._solana_sig_disposition_memo(
                    kind, SOLANA_DEPOSIT_SIGNATURE
                )
                with patch.object(
                    config, "USDC_QUARANTINE_ACCOUNT", quarantine_account
                ), patch.object(
                    solana_client, "_get_client", return_value=_MemoRpcClient()
                ), patch.object(solana_client, "_rpc_call", side_effect=[[
                    {
                        "signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 200,
                        "confirmationStatus": "finalized", "err": None,
                    }
                ], _memo_transaction(memo, destination=destination),
                    _source_deposit_transaction(
                        amount=source_amount, timestamp=source_timestamp
                    )]):
                    scan = solana_client.scan_memos_since_timestamp(100)

                self.assertFalse(scan["complete"], scan)
                self.assertEqual(
                    scan["reason"], "missing_solana_disposition_source_evidence"
                )
                self.assertEqual(scan["solana_dispositions"], {})

    def test_waterline_scanner_rejects_nonmonotonic_page_before_cutoff(self):
        """An old entry cannot hide newer recovery evidence later in the same page."""
        with patch.object(
            solana_client, "_get_client", return_value=_MemoRpcClient()
        ), patch.object(solana_client, "_rpc_call", return_value=[
            {
                "signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 90,
                "confirmationStatus": "finalized", "err": None,
            },
            {
                "signature": SOLANA_DEPOSIT_SIGNATURE, "blockTime": 200,
                "confirmationStatus": "finalized", "err": None,
            },
        ]) as rpc:
            scan = solana_client.scan_memos_since_timestamp(100)

        self.assertFalse(scan["complete"], scan)
        self.assertEqual(scan["reason"], "nonmonotonic_signature_page")
        self.assertEqual(rpc.call_count, 1)

    def test_waterline_scanner_rejects_current_disposition_without_exact_transfer(self):
        memo = f"swapService:v1:refund:{SOLANA_DEPOSIT_SIGNATURE}"
        transaction = _memo_transaction(memo, amount=0)
        with patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()), patch.object(
            solana_client,
            "_rpc_call",
            side_effect=[[
                {
                    "signature": SOLANA_PAYOUT_SIGNATURE,
                    "blockTime": 200,
                    "confirmationStatus": "finalized",
                    "err": None,
                }
            ], transaction],
        ):
            scan = solana_client.scan_memos_since_timestamp(100)

        self.assertFalse(scan["complete"])
        self.assertEqual(scan["reason"], "missing_solana_disposition_transfer_evidence")
        self.assertEqual(scan["solana_dispositions"], {})


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
    def test_state_reconstructs_wipeout_and_backup_dispositions_atomically_idempotently(self):
        """Chain evidence restores terminal source state even when its reservation is absent."""
        for kind in ("refund", "quarantine"):
            for backup in (False, True):
                with self.subTest(kind=kind, backup=backup), tempfile.TemporaryDirectory() as tmpdir:
                    db_path = os.path.join(tmpdir, "state.db")
                    payout_memo = solana_client._solana_sig_disposition_memo(
                        kind, SOLANA_DEPOSIT_SIGNATURE
                    )
                    with patch.object(state_db, "DB_PATH", db_path), patch.object(
                        state_db.time, "time", return_value=1_000
                    ):
                        state_db.init_db()
                        if backup:
                            state_db.add_unprocessed_sig(
                                SOLANA_DEPOSIT_SIGNATURE, 800, "nexus:recipient",
                                "recipient-token-account", 3_100_000,
                                "refund submission held" if kind == "refund"
                                else "quarantine submission held", None,
                            )
                            details = state_db._SOLANA_SIG_DISPOSITION[kind]
                            with sqlite3.connect(db_path) as conn:
                                conn.execute(
                                    f"""INSERT INTO {details['table']}
                                       (sig, timestamp, from_address, destination_address,
                                        amount_usdc_units, memo, payout_memo,
                                        {details['signature_column']}, {details['units_column']}, status)
                                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitting')""",
                                    (
                                        SOLANA_DEPOSIT_SIGNATURE, 800,
                                        "recipient-token-account", "recipient-token-account",
                                        3_100_000, "nexus:recipient", payout_memo,
                                        None, 3_000_000,
                                    ),
                                )
                        kwargs = {
                            "kind": kind,
                            "source_signature": SOLANA_DEPOSIT_SIGNATURE,
                            "source_timestamp": 800,
                            "source_token_account": "recipient-token-account",
                            "source_amount_solana_units": 3_100_000,
                            "source_memo": "nexus:recipient",
                            "payout_signature": SOLANA_PAYOUT_SIGNATURE,
                            "destination_token_account": "recipient-token-account",
                            "payout_amount_solana_units": 3_000_000,
                            "chain_timestamp": 900,
                            "payout_memo": payout_memo,
                        }
                        self.assertTrue(
                            state_db.reconstruct_confirmed_solana_sig_disposition(**kwargs)
                        )
                        self.assertTrue(
                            state_db.reconstruct_confirmed_solana_sig_disposition(**kwargs)
                        )
                        self.assertEqual(state_db.payout_budget_used(86400), 3_000_000)
                        details = state_db._SOLANA_SIG_DISPOSITION[kind]
                        with sqlite3.connect(db_path) as conn:
                            terminal = conn.execute(
                                f"""SELECT sig, timestamp, from_address, destination_address,
                                           amount_usdc_units, memo, payout_memo,
                                           {details['signature_column']}, {details['units_column']}, status
                                    FROM {details['table']}"""
                            ).fetchall()
                            events = conn.execute(
                                """SELECT event, kind, amount_usdc_units, signature, timestamp
                                   FROM solana_payout_budget_events ORDER BY id"""
                            ).fetchall()
                            fees = conn.execute(
                                """SELECT kind, amount_usdc_units, timestamp FROM fee_entries"""
                            ).fetchall()
                            pending = conn.execute(
                                "SELECT 1 FROM unprocessed_sigs"
                            ).fetchall()
                            opposing = conn.execute(
                                f"SELECT 1 FROM {'quarantined_sigs' if kind == 'refund' else 'refunded_sigs'}"
                            ).fetchall()

                    self.assertEqual(terminal, [(
                        SOLANA_DEPOSIT_SIGNATURE, 800, "recipient-token-account",
                        "recipient-token-account", 3_100_000, "nexus:recipient",
                        payout_memo, SOLANA_PAYOUT_SIGNATURE, 3_000_000,
                        f"{kind}_confirmed",
                    )])
                    self.assertEqual(events, [
                        ("reserved", f"solana_{kind}", 3_000_000, None, 900),
                        ("submitted", f"solana_{kind}", 3_000_000,
                         SOLANA_PAYOUT_SIGNATURE, 900),
                        ("confirmed", f"solana_{kind}", 3_000_000,
                         SOLANA_PAYOUT_SIGNATURE, 900),
                    ])
                    self.assertEqual(fees, [(f"{kind}_flat_fee", 100_000, 900)])
                    self.assertEqual(pending, [])
                    self.assertEqual(opposing, [])

    def test_disposition_reconstruction_refuses_active_nexus_mint_source_and_preserves_cap(self):
        """A confirmed refund cannot erase an unresolved Nexus mint obligation."""
        conflicts = (
            ("status", "debited, awaiting confirmation"),
            ("txid", "existing-nexus-mint-txid"),
            ("reference", 77),
            ("amount_usdd_units", 100),
        )
        for field, value in conflicts:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "state.db")
                with patch.object(state_db, "DB_PATH", db_path), patch.object(
                    state_db.time, "time", return_value=1_000
                ):
                    state_db.init_db()
                    state_db.add_unprocessed_sig(
                        SOLANA_DEPOSIT_SIGNATURE, 800, "nexus:recipient",
                        "recipient-token-account", 110, "refund submission held", None,
                    )
                    self.assertTrue(state_db.reserve_solana_payout_budget(
                        obligation_id=f"refund:{SOLANA_DEPOSIT_SIGNATURE}",
                        kind="solana_refund", amount_usdc_units=100, cap_units=1_000,
                    ))
                    with sqlite3.connect(db_path) as conn:
                        conn.execute(
                            f"UPDATE unprocessed_sigs SET {field}=? WHERE sig=?",
                            (value, SOLANA_DEPOSIT_SIGNATURE),
                        )
                    restored = state_db.reconstruct_confirmed_solana_sig_disposition(
                        kind="refund", source_signature=SOLANA_DEPOSIT_SIGNATURE,
                        source_timestamp=800, source_token_account="recipient-token-account",
                        source_amount_solana_units=110, source_memo="nexus:recipient",
                        payout_signature=SOLANA_PAYOUT_SIGNATURE,
                        destination_token_account="recipient-token-account",
                        payout_amount_solana_units=100, chain_timestamp=900,
                        payout_memo=solana_client._solana_sig_disposition_memo(
                            "refund", SOLANA_DEPOSIT_SIGNATURE
                        ),
                    )
                    with sqlite3.connect(db_path) as conn:
                        pending = conn.execute(
                            """SELECT status, txid, reference, amount_usdd_units
                               FROM unprocessed_sigs WHERE sig=?""",
                            (SOLANA_DEPOSIT_SIGNATURE,),
                        ).fetchone()
                        terminal_count = conn.execute(
                            "SELECT COUNT(*) FROM refunded_sigs"
                        ).fetchone()[0]
                    cap = state_db.payout_budget_used(86400)

            self.assertFalse(restored)
            self.assertIsNotNone(pending)
            self.assertEqual(terminal_count, 0)
            self.assertEqual(cap, 100)

    def test_disposition_reconstruction_requires_known_source_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                state_db.time, "time", return_value=1_000
            ):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    SOLANA_DEPOSIT_SIGNATURE, 800, "nexus:recipient",
                    "recipient-token-account", 110, "operator custom hold", None,
                )
                restored = state_db.reconstruct_confirmed_solana_sig_disposition(
                    kind="refund", source_signature=SOLANA_DEPOSIT_SIGNATURE,
                    source_timestamp=800, source_token_account="recipient-token-account",
                    source_amount_solana_units=110, source_memo="nexus:recipient",
                    payout_signature=SOLANA_PAYOUT_SIGNATURE,
                    destination_token_account="recipient-token-account",
                    payout_amount_solana_units=100, chain_timestamp=900,
                    payout_memo=solana_client._solana_sig_disposition_memo(
                        "refund", SOLANA_DEPOSIT_SIGNATURE
                    ),
                )
                self.assertTrue(state_db.is_unprocessed_sig(SOLANA_DEPOSIT_SIGNATURE))
                self.assertEqual(state_db.payout_budget_used(86400), 0)

        self.assertFalse(restored)

    def test_missing_source_memo_is_canonical_across_wipeout_and_backup_dispositions(self):
        for kind in ("refund", "quarantine"):
            for backup in (False, True):
                with self.subTest(kind=kind, backup=backup), tempfile.TemporaryDirectory() as tmpdir:
                    db_path = os.path.join(tmpdir, "state.db")
                    details = state_db._SOLANA_SIG_DISPOSITION[kind]
                    payout_memo = solana_client._solana_sig_disposition_memo(
                        kind, SOLANA_DEPOSIT_SIGNATURE
                    )
                    with patch.object(state_db, "DB_PATH", db_path), patch.object(
                        state_db.time, "time", return_value=1_000
                    ):
                        state_db.init_db()
                        if backup:
                            state_db.add_unprocessed_sig(
                                SOLANA_DEPOSIT_SIGNATURE, 800, "",
                                "recipient-token-account", 110,
                                details["held_status"], None,
                            )
                            with sqlite3.connect(db_path) as conn:
                                conn.execute(
                                    f"""INSERT INTO {details['table']}
                                       (sig, timestamp, from_address, destination_address,
                                        amount_usdc_units, memo, payout_memo,
                                        {details['signature_column']}, {details['units_column']}, status)
                                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'submitting')""",
                                    (
                                        SOLANA_DEPOSIT_SIGNATURE, 800,
                                        "recipient-token-account", "recipient-token-account",
                                        110, "", payout_memo, 100,
                                    ),
                                )
                        restored = state_db.reconstruct_confirmed_solana_sig_disposition(
                            kind=kind, source_signature=SOLANA_DEPOSIT_SIGNATURE,
                            source_timestamp=800,
                            source_token_account="recipient-token-account",
                            source_amount_solana_units=110, source_memo=None,
                            payout_signature=SOLANA_PAYOUT_SIGNATURE,
                            destination_token_account="recipient-token-account",
                            payout_amount_solana_units=100, chain_timestamp=900,
                            payout_memo=payout_memo,
                        )
                        with sqlite3.connect(db_path) as conn:
                            saved = conn.execute(
                                f"SELECT memo, status FROM {details['table']} WHERE sig=?",
                                (SOLANA_DEPOSIT_SIGNATURE,),
                            ).fetchone()

                    self.assertTrue(restored)
                    self.assertEqual(saved, ("", details["terminal_status"]))

    def test_missing_source_memo_is_canonical_during_normal_disposition_preparation(self):
        for kind in ("refund", "quarantine"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "state.db")
                details = state_db._SOLANA_SIG_DISPOSITION[kind]
                payout_memo = solana_client._solana_sig_disposition_memo(
                    kind, SOLANA_DEPOSIT_SIGNATURE
                )
                with patch.object(state_db, "DB_PATH", db_path), patch.object(
                    state_db.time, "time", return_value=1_000
                ):
                    state_db.init_db()
                    state_db.add_unprocessed_sig(
                        SOLANA_DEPOSIT_SIGNATURE, 800, "", "recipient-token-account",
                        110, details["ready_statuses"][0], None,
                    )
                    prepared = state_db.prepare_solana_sig_disposition(
                        source_sig=SOLANA_DEPOSIT_SIGNATURE, kind=kind, timestamp=800,
                        from_address="recipient-token-account",
                        destination_address="recipient-token-account",
                        amount_usdc_units=110, memo=None, payout_memo=payout_memo,
                        payout_units=100, cap_units=1_000,
                    )
                    with sqlite3.connect(db_path) as conn:
                        saved = conn.execute(
                            f"SELECT memo, status FROM {details['table']} WHERE sig=?",
                            (SOLANA_DEPOSIT_SIGNATURE,),
                        ).fetchone()

                self.assertTrue(prepared)
                self.assertEqual(saved, ("", "submitting"))

    def test_reconstructed_cap_uses_inclusive_rolling_block_time_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                state_db.time, "time", return_value=100_000
            ):
                state_db.init_db()
                self.assertTrue(state_db.reconstruct_confirmed_solana_payout_budget(
                    obligation_id="nexus:at-boundary:0", signature="boundary-signature",
                    amount_usdc_units=100, chain_timestamp=13_600,
                ))
                self.assertTrue(state_db.reconstruct_confirmed_solana_payout_budget(
                    obligation_id="nexus:before-boundary:0", signature="older-signature",
                    amount_usdc_units=200, chain_timestamp=13_599,
                ))
                used = state_db.payout_budget_used(86400)

        self.assertEqual(used, 100)

    def test_backup_terminal_disposition_reanchors_cap_and_fee_to_block_time(self):
        """Local confirmation clocks cannot shift recovered rolling-window accounting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "state.db")
            payout_memo = solana_client._solana_sig_disposition_memo(
                "refund", SOLANA_DEPOSIT_SIGNATURE
            )
            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                state_db.time, "time", return_value=950
            ):
                state_db.init_db()
                state_db.add_unprocessed_sig(
                    SOLANA_DEPOSIT_SIGNATURE, 800, "nexus:recipient",
                    "recipient-token-account", 110, "to be refunded", None,
                )
                self.assertTrue(state_db.prepare_solana_sig_disposition(
                    source_sig=SOLANA_DEPOSIT_SIGNATURE, kind="refund", timestamp=800,
                    from_address="recipient-token-account",
                    destination_address="recipient-token-account",
                    amount_usdc_units=110, memo="nexus:recipient",
                    payout_memo=payout_memo, payout_units=100, cap_units=1_000,
                ))
                self.assertTrue(state_db.record_solana_sig_disposition_submission(
                    source_sig=SOLANA_DEPOSIT_SIGNATURE, kind="refund",
                    payout_signature=SOLANA_PAYOUT_SIGNATURE,
                ))
                self.assertTrue(state_db.confirm_solana_sig_disposition(
                    source_sig=SOLANA_DEPOSIT_SIGNATURE, kind="refund",
                    payout_signature=SOLANA_PAYOUT_SIGNATURE,
                ))

            with patch.object(state_db, "DB_PATH", db_path), patch.object(
                state_db.time, "time", return_value=1_000
            ):
                restored = state_db.reconstruct_confirmed_solana_sig_disposition(
                    kind="refund", source_signature=SOLANA_DEPOSIT_SIGNATURE,
                    source_timestamp=800, source_token_account="recipient-token-account",
                    source_amount_solana_units=110, source_memo="nexus:recipient",
                    payout_signature=SOLANA_PAYOUT_SIGNATURE,
                    destination_token_account="recipient-token-account",
                    payout_amount_solana_units=100, chain_timestamp=900,
                    payout_memo=payout_memo,
                )
                with sqlite3.connect(db_path) as conn:
                    confirmed_timestamp = conn.execute(
                        """SELECT timestamp FROM solana_payout_budget_events
                           WHERE event = 'confirmed'"""
                    ).fetchone()[0]
                    fee_timestamp = conn.execute(
                        "SELECT timestamp FROM fee_entries"
                    ).fetchone()[0]

        self.assertTrue(restored)
        self.assertEqual(confirmed_timestamp, 900)
        self.assertEqual(fee_timestamp, 900)

    def test_disposition_before_newer_heartbeat_rebuilds_terminal_state_and_budget(self):
        """The rolling-window scan recovers spends older than the latest safe waterline."""
        for kind in ("refund", "quarantine"):
            for existing_budget in (0, 12345):
                with self.subTest(kind=kind, existing_budget=existing_budget), tempfile.TemporaryDirectory() as tmpdir:
                    transaction = _memo_transaction(
                        solana_client._solana_sig_disposition_memo(kind, SOLANA_DEPOSIT_SIGNATURE)
                    )
                    source_transaction = _source_deposit_transaction(
                        timestamp=97000
                    )
                    entry = {"signature": SOLANA_PAYOUT_SIGNATURE, "blockTime": 98000,
                             "confirmationStatus": "finalized", "err": None}
                    heartbeat = {
                        "address": "heartbeat-address", "last_poll_timestamp": "100000",
                        "last_safe_timestamp_nexus": "99000", "last_safe_timestamp_solana": "99000",
                    }
                    db_path = os.path.join(tmpdir, "state.db")
                    with (
                        patch.object(state_db, "DB_PATH", db_path),
                        patch.object(config, "USDC_QUARANTINE_ACCOUNT", "recipient-token-account"),
                        patch.object(startup_recovery.time, "time", return_value=100000),
                        patch.object(state_db.time, "time", return_value=100000),
                        patch.object(nexus_client, "get_heartbeat_asset", return_value=heartbeat),
                        patch.object(
                            nexus_client, "fetch_deposits_since",
                            return_value=nexus_client.DepositScan([], True),
                        ) as nexus_scan,
                        patch.object(nexus_client, "get_last_reference", return_value=7) as reference,
                        patch.object(solana_client, "_get_client", return_value=_MemoRpcClient()),
                        patch.object(solana_client, "_rpc_call", side_effect=[
                            [entry], [entry], transaction, source_transaction,
                        ]) as rpc,
                    ):
                        state_db.init_db()
                        if existing_budget:
                            self.assertTrue(state_db.reserve_solana_payout_budget(
                                obligation_id="existing-reservation", kind="nexus_payout",
                                amount_usdc_units=existing_budget, cap_units=existing_budget,
                            ))
                        result = startup_recovery.perform_startup_recovery()
                        self.assertTrue(result["recovery_complete"], result)
                        self.assertFalse(result["recovery_incomplete"], result)
                        self.assertEqual(
                            state_db.payout_budget_used(86400), existing_budget + 3_000_000
                        )
                        self.assertEqual(rpc.call_count, 4)
                        nexus_scan.assert_called_once_with("TREASURY", 99000)
                        reference.assert_called_once_with()
                        with sqlite3.connect(db_path) as conn:
                            self.assertEqual(
                                conn.execute(
                                    f"SELECT status FROM {'refunded_sigs' if kind == 'refund' else 'quarantined_sigs'}"
                                ).fetchall(),
                                [(f"{kind}_confirmed",)],
                            )
                            self.assertEqual(conn.execute(
                                "SELECT COUNT(*) FROM unprocessed_sigs"
                            ).fetchone()[0], 0)

    def test_incomplete_solana_enumeration_writes_no_recovery_markers(self):
        scan = {
            "complete": False,
            "reason": "pagination_truncated",
            "nexus_payouts": {(NEXUS_TXID, 7): SOLANA_PAYOUT_SIGNATURE},
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
            "legacy_nexus_txids": {NEXUS_TXID: SOLANA_PAYOUT_SIGNATURE},
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
        payout_units = nexus_client.get_solana_send_amount_units(4_000_000)
        memo_scan = {
            "complete": True,
            "reason": None,
            "nexus_payouts": {(NEXUS_TXID, 1): NexusPayoutEvidence(
                txid=NEXUS_TXID,
                contract_id=1,
                solana_signature=SOLANA_PAYOUT_SIGNATURE,
                to_token_account="recipient-token-account",
                amount_solana_units=payout_units,
            )},
            # The finalized signature page's blockTime is the authoritative chain
            # timestamp for the cap event reconstructed after a database wipeout.
            "nexus_payout_timestamps": {(NEXUS_TXID, 1): 1_000},
            "legacy_nexus_txids": {},
            "malformed_nexus_memos": [],
            "refund_sigs": {},
            "quarantined_sigs": {},
            "solana_dispositions": {
                ("refund", SOLANA_REFUND_SOURCE_SIGNATURE): {
                    "kind": "refund",
                    "source_signature": SOLANA_REFUND_SOURCE_SIGNATURE,
                    "solana_signature": SOLANA_REFUND_PAYOUT_SIGNATURE,
                    "destination_token_account": "recipient-token-account",
                    "amount_solana_units": 100,
                    "timestamp": 1_000,
                    "source_timestamp": 950,
                    "source_token_account": "recipient-token-account",
                    "source_amount_solana_units": 110,
                    "source_memo": "nexus:refund-recipient",
                },
                ("quarantine", SOLANA_QUARANTINE_SOURCE_SIGNATURE): {
                    "kind": "quarantine",
                    "source_signature": SOLANA_QUARANTINE_SOURCE_SIGNATURE,
                    "solana_signature": SOLANA_QUARANTINE_PAYOUT_SIGNATURE,
                    "destination_token_account": "quarantine-token-account",
                    "amount_solana_units": 200,
                    "timestamp": 1_000,
                    "source_timestamp": 950,
                    "source_token_account": "quarantine-source-token-account",
                    "source_amount_solana_units": 210,
                    "source_memo": "malformed-deposit-memo",
                },
            },
        }
        empty_heartbeat_scan = {
            **memo_scan,
            "nexus_payouts": {},
            "nexus_payout_timestamps": {},
            "solana_dispositions": {},
        }
        heartbeat = {
            "address": "heartbeat-address",
            "last_poll_timestamp": "2000",
            "last_safe_timestamp_nexus": "900",
            "last_safe_timestamp_solana": "2000",
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
                patch.object(config, "USDC_QUARANTINE_ACCOUNT", "quarantine-token-account"),
                patch.object(startup_recovery.time, "time", return_value=1_001),
                patch.object(state_db, "recover_interrupted_nexus_transfer_intents", return_value=0),
                patch.object(nexus_client, "get_heartbeat_asset", return_value=heartbeat),
                patch.object(
                    solana_client, "scan_memos_since_timestamp",
                    side_effect=[empty_heartbeat_scan, memo_scan] * 2,
                ),
                patch.object(nexus_client, "fetch_deposits_since", return_value=nexus_client.DepositScan([tx], True)),
                patch.object(nexus_client, "get_account_info", return_value={"owner": "owner"}),
                patch.object(nexus_client, "get_last_reference", return_value=99),
                patch.object(state_db.time, "time", return_value=1_001),
            ):
                state_db.init_db()
                result = startup_recovery.perform_startup_recovery()
                second_result = startup_recovery.perform_startup_recovery()
                reconstructed_cap_used = state_db.payout_budget_used(86400)
                second_full_cap_reserved = state_db.reserve_solana_payout_budget(
                    obligation_id="nexus:next-credit:0",
                    kind="nexus_payout",
                    amount_usdc_units=payout_units,
                    cap_units=payout_units,
                )
                conn = sqlite3.connect(db_path)
                processed = conn.execute(
                    "SELECT txid, contract_id, amount_usdd_units, from_address, sig FROM processed_txids"
                ).fetchall()
                queued = conn.execute(
                    "SELECT txid, contract_id, amount_usdd_units, from_address FROM unprocessed_txids"
                ).fetchall()
                refund = conn.execute(
                    "SELECT sig, refund_sig, refunded_units, status FROM refunded_sigs"
                ).fetchall()
                quarantine = conn.execute(
                    """SELECT sig, quarantine_sig, quarantined_units, status
                       FROM quarantined_sigs"""
                ).fetchall()
                conn.close()

        self.assertTrue(result["recovery_complete"], result)
        self.assertTrue(second_result["recovery_complete"], second_result)
        self.assertEqual(reconstructed_cap_used, payout_units + 300)
        self.assertFalse(second_full_cap_reserved)
        self.assertEqual(
            processed,
            [(NEXUS_TXID, 1, 4_000_000, "sender-b", SOLANA_PAYOUT_SIGNATURE)],
        )
        self.assertEqual(queued, [(NEXUS_TXID, 0, 3_000_000, "sender-a")])
        self.assertEqual(refund, [(
            SOLANA_REFUND_SOURCE_SIGNATURE, SOLANA_REFUND_PAYOUT_SIGNATURE,
            100, "refund_confirmed",
        )])
        self.assertEqual(quarantine, [(
            SOLANA_QUARANTINE_SOURCE_SIGNATURE, SOLANA_QUARANTINE_PAYOUT_SIGNATURE,
            200, "quarantine_confirmed",
        )])


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

    def test_receipt_enabled_startup_requires_receipt_capable_provider_registration(self):
        recovery = {"recovery_complete": True, "recovery_incomplete": False}
        with (
            patch.object(main, "validate_production_controls", return_value=True),
            patch.object(main.state_db, "init_db"),
            patch.object(main, "acquire_singleton_lock", return_value=True),
            patch.object(startup_recovery, "perform_startup_recovery", return_value=recovery),
            patch.object(config, "NEXUS_SWAP_RECEIPTS_ENABLED", True),
            patch.object(
                swap_receipts,
                "receipt_provider_registration",
                return_value=(None, "configured provider registration is not readable"),
            ) as receipt_registration,
            patch.object(nexus_client, "validate_session_config") as session_check,
            patch.object(nexus_client, "validate_heartbeat_asset") as heartbeat_check,
            patch.object(main.alerts, "critical") as critical,
        ):
            result = main.run()

        self.assertFalse(result)
        receipt_registration.assert_called_once_with()
        session_check.assert_not_called()
        heartbeat_check.assert_not_called()
        critical.assert_called_once_with(
            "receipt_provider_registration_invalid",
            "receipt publication is enabled but its provider registration is not admissible",
            reason="configured provider registration is not readable",
        )


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
