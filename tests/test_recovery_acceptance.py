"""Collected acceptance coverage for conservative recovery across real SQLite artifacts.

The fixtures exercise persisted databases, copied WAL state, startup recovery, and the
runtime confirmation/backing/dashboard surfaces.  External transports remain mocked.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest
from solders.signature import Signature

from src import config, dashboard, fees, nexus_client, solana_client, startup_recovery, state_db


SOURCE_SIG = "legacy-source-signature"
PAYOUT_SIG = "legacy-payout-signature"


def _create_pre_provenance_terminal(path, kind: str) -> None:
    """Reproduce the disposition tables immediately before provenance columns existed."""
    details = state_db._SOLANA_SIG_DISPOSITION[kind]
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"""CREATE TABLE {details['table']} (
                sig TEXT PRIMARY KEY,
                timestamp INTEGER,
                from_address TEXT,
                destination_address TEXT,
                amount_usdc_units INTEGER,
                memo TEXT,
                payout_memo TEXT,
                {details['signature_column']} TEXT,
                {details['units_column']} INTEGER,
                status TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE fee_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sig TEXT,
                txid TEXT,
                kind TEXT,
                amount_usdc_units INTEGER,
                amount_usdd_units INTEGER,
                contract_id INTEGER NOT NULL DEFAULT -1,
                timestamp INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE solana_payout_budget_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                obligation_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                event TEXT NOT NULL,
                amount_usdc_units INTEGER NOT NULL,
                signature TEXT,
                evidence TEXT,
                timestamp INTEGER NOT NULL,
                UNIQUE(obligation_id, event)
            )"""
        )
        conn.execute(
            f"""INSERT INTO {details['table']}
                (sig, timestamp, from_address, destination_address,
                 amount_usdc_units, memo, payout_memo,
                 {details['signature_column']}, {details['units_column']}, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                SOURCE_SIG,
                10,
                "source-token-account",
                "destination-token-account",
                10_000,
                "nexus:recipient",
                f"swapService:v1:{kind}:{SOURCE_SIG}",
                PAYOUT_SIG,
                1,
                details["terminal_status"],
            ),
        )
        conn.execute(
            """INSERT INTO fee_entries
                (sig, txid, kind, amount_usdc_units, amount_usdd_units,
                 contract_id, timestamp)
                VALUES (?, NULL, ?, ?, NULL, -1, ?)""",
            (SOURCE_SIG, f"{kind}_flat_fee", 9_999, 11),
        )
        obligation_id = f"{kind}:{SOURCE_SIG}"
        budget_kind = f"solana_{kind}"
        conn.executemany(
            """INSERT INTO solana_payout_budget_events
               (obligation_id, kind, event, amount_usdc_units, signature,
                evidence, timestamp)
               VALUES (?, ?, ?, 1, ?, NULL, ?)""",
            [
                (obligation_id, budget_kind, "reserved", None, 11),
                (obligation_id, budget_kind, "submitted", PAYOUT_SIG, 11),
                (obligation_id, budget_kind, "confirmed", PAYOUT_SIG, 11),
            ],
        )


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
def test_in_place_upgrade_holds_old_recovery_terminal_even_outside_scan_range(
    tmp_path, kind
):
    """A legacy terminal cannot remain hidden/fee-green until a bounded scan finds it."""
    db_path = tmp_path / "legacy.db"
    _create_pre_provenance_terminal(db_path, kind)

    with patch.object(state_db, "DB_PATH", str(db_path)), patch.object(
        state_db.time, "time", return_value=200_000
    ):
        state_db.init_db()
        details = state_db._SOLANA_SIG_DISPOSITION[kind]
        with sqlite3.connect(db_path) as conn:
            terminal = conn.execute(
                f"SELECT 1 FROM {details['table']} WHERE sig = ?", (SOURCE_SIG,)
            ).fetchone()
            fee = conn.execute(
                "SELECT 1 FROM fee_entries WHERE sig = ?", (SOURCE_SIG,)
            ).fetchone()
            held = conn.execute(
                """SELECT amount_usdc_units, status FROM unprocessed_sigs
                   WHERE sig = ?""",
                (SOURCE_SIG,),
            ).fetchone()
        liability = state_db.get_unresolved_solana_liability_units()
        issues = dashboard.api_issues()["issues"]

    assert terminal is None
    assert fee is None
    assert held == (10_000, details["evidence_held_status"])
    assert liability == 10_000
    assert any(issue["id"] == SOURCE_SIG for issue in issues)


@contextmanager
def _isolated_current_state(tmp_path, *, now=1_000):
    db_path = tmp_path / "current.db"
    with patch.object(state_db, "DB_PATH", str(db_path)), patch.object(
        state_db.time, "time", return_value=now
    ):
        state_db.init_db()
        yield db_path


def _prepare_current_disposition(kind: str, *, source_sig: str, signature: str) -> str:
    details = state_db._SOLANA_SIG_DISPOSITION[kind]
    payout_memo = solana_client._solana_sig_disposition_memo(kind, source_sig)
    state_db.add_unprocessed_sig(
        source_sig, 800, "nexus:recipient", "source-token-account", 110,
        details["ready_statuses"][0], None,
    )
    assert state_db.prepare_solana_sig_disposition(
        source_sig=source_sig,
        kind=kind,
        timestamp=800,
        from_address="source-token-account",
        destination_address="destination-token-account",
        amount_usdc_units=110,
        memo="nexus:recipient",
        payout_memo=payout_memo,
        payout_units=100,
        cap_units=1_000,
    )
    return payout_memo


def _corrupt_intent_evidence(evidence: str, corruption: str) -> str:
    if corruption == "duplicate_key":
        return '{"version":false,' + evidence[1:]
    if corruption == "nested_duplicate_key":
        value = json.loads(evidence)
        marker = f'"service_terms_version":{value["service_terms_version"]}'
        return evidence.replace(
            marker, '"service_terms_version":{"value":1,"value":2}', 1
        )
    if corruption == "nonfinite_overflow":
        return evidence.replace('"source_timestamp":800', '"source_timestamp":1e999', 1)
    if corruption == "nonfinite_constant":
        return evidence.replace('"source_timestamp":800', '"source_timestamp":NaN', 1)
    if corruption == "malformed_wire_value":
        return evidence.replace('"source_timestamp":800', '"source_timestamp":[800]', 1)

    value = json.loads(evidence)
    if corruption == "source_signature_whitespace":
        value["source_signature"] = f' {value["source_signature"]} '
    else:
        field = {
            "version_bool": "version",
            "version_float": "version",
            "source_timestamp_bool": "source_timestamp",
            "source_timestamp_float": "source_timestamp",
            "source_amount_bool": "source_amount_solana_units",
            "source_amount_float": "source_amount_solana_units",
            "payout_amount_bool": "payout_amount_solana_units",
            "payout_amount_float": "payout_amount_solana_units",
            "fee_bool": "fee_solana_units",
            "fee_float": "fee_solana_units",
            "terms_bool": "service_terms_version",
            "terms_float": "service_terms_version",
            "refund_fee_bool": "refund_solana_fee_units",
            "refund_fee_float": "refund_solana_fee_units",
        }[corruption]
        value[field] = True if corruption.endswith("_bool") else float(value[field])
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate_key",
        "nested_duplicate_key",
        "version_bool",
        "version_float",
        "source_timestamp_bool",
        "source_timestamp_float",
        "source_amount_bool",
        "source_amount_float",
        "payout_amount_bool",
        "payout_amount_float",
        "fee_bool",
        "fee_float",
        "terms_bool",
        "terms_float",
        "refund_fee_bool",
        "refund_fee_float",
        "nonfinite_overflow",
        "nonfinite_constant",
        "malformed_wire_value",
        "source_signature_whitespace",
    ],
)
def test_restart_migration_holds_terminal_with_nonexact_intent_evidence(
    tmp_path, corruption
):
    source_sig = "terminal-corruption"
    signature = str(Signature.from_bytes(bytes([39]) * 64))
    with _isolated_current_state(tmp_path) as db_path:
        _prepare_current_disposition("refund", source_sig=source_sig, signature=signature)
        assert state_db.record_solana_sig_disposition_submission(
            source_sig=source_sig, kind="refund", payout_signature=signature
        )
        assert state_db.confirm_solana_sig_disposition(
            source_sig=source_sig, kind="refund", payout_signature=signature
        )
        with sqlite3.connect(db_path) as conn:
            evidence = conn.execute(
                "SELECT intent_evidence FROM refunded_sigs WHERE sig = ?", (source_sig,)
            ).fetchone()[0]
            cap_before = conn.execute(
                """SELECT event, amount_usdc_units, signature, evidence, timestamp
                   FROM solana_payout_budget_events WHERE obligation_id = ? ORDER BY id""",
                (f"refund:{source_sig}",),
            ).fetchall()
            conn.execute(
                "UPDATE refunded_sigs SET intent_evidence = ? WHERE sig = ?",
                (_corrupt_intent_evidence(evidence, corruption), source_sig),
            )

        state_db.init_db()

        with sqlite3.connect(db_path) as conn:
            terminal = conn.execute(
                "SELECT 1 FROM refunded_sigs WHERE sig = ?", (source_sig,)
            ).fetchone()
            source = conn.execute(
                """SELECT timestamp, memo, from_address, amount_usdc_units, status,
                          txid, reference, amount_usdd_units
                   FROM unprocessed_sigs WHERE sig = ?""",
                (source_sig,),
            ).fetchone()
            fee = conn.execute(
                "SELECT 1 FROM fee_entries WHERE sig = ?", (source_sig,)
            ).fetchone()
            cap_after = conn.execute(
                """SELECT event, amount_usdc_units, signature, evidence, timestamp
                   FROM solana_payout_budget_events WHERE obligation_id = ? ORDER BY id""",
                (f"refund:{source_sig}",),
            ).fetchall()
            migration = conn.execute(
                """SELECT payout_signature, payout_units, reversed_fee_units
                   FROM solana_disposition_provenance_migrations
                   WHERE kind = 'refund' AND source_signature = ?""",
                (source_sig,),
            ).fetchone()

    assert terminal is None
    assert source == (
        800, "nexus:recipient", "source-token-account", 110,
        "refund evidence held", None, None, None,
    )
    assert fee is None
    assert cap_after == cap_before
    assert migration == (signature, 100, 10)


@pytest.mark.parametrize("kind", ["refund", "quarantine"])
@pytest.mark.parametrize("lifecycle", ["prepared", "awaiting", "confirmed"])
def test_current_provenance_retains_frozen_authorization_across_restart_and_config_change(
    tmp_path, kind, lifecycle
):
    signature = str(Signature.from_bytes(bytes([31 if kind == "refund" else 32]) * 64))
    pair = replace(
        config.SWAP_PAIR,
        fees=replace(config.SWAP_PAIR.fees, refund_solana_units=10),
    )
    with patch.object(config, "SWAP_PAIR", pair), patch.object(
        config, "SERVICE_TERMS_VERSION", 7
    ), _isolated_current_state(tmp_path) as db_path:
        _prepare_current_disposition(kind, source_sig=f"current-{kind}-{lifecycle}", signature=signature)
        source_sig = f"current-{kind}-{lifecycle}"
        if lifecycle in {"awaiting", "confirmed"}:
            assert state_db.record_solana_sig_disposition_submission(
                source_sig=source_sig, kind=kind, payout_signature=signature
            )
        if lifecycle == "confirmed":
            assert state_db.confirm_solana_sig_disposition(
                source_sig=source_sig, kind=kind, payout_signature=signature
            )
        details = state_db._SOLANA_SIG_DISPOSITION[kind]
        with sqlite3.connect(db_path) as conn:
            before = conn.execute(
                f"""SELECT sig, timestamp, from_address, destination_address,
                           amount_usdc_units, memo, payout_memo,
                           {details['signature_column']}, {details['units_column']},
                           status, intent_provenance, intent_evidence
                    FROM {details['table']} WHERE sig = ?""",
                (source_sig,),
            ).fetchone()

        changed_pair = replace(
            config.SWAP_PAIR,
            fees=replace(config.SWAP_PAIR.fees, refund_solana_units=77),
        )
        with patch.object(config, "SWAP_PAIR", changed_pair), patch.object(
            config, "SERVICE_TERMS_VERSION", 99
        ), patch.object(config, "MIN_DEPOSIT_SOLANA_UNITS", 1_000_000), patch.object(
            config, "USDC_QUARANTINE_ACCOUNT", "changed-quarantine"
        ):
            state_db.init_db()

        with sqlite3.connect(db_path) as conn:
            after = conn.execute(
                f"""SELECT sig, timestamp, from_address, destination_address,
                           amount_usdc_units, memo, payout_memo,
                           {details['signature_column']}, {details['units_column']},
                           status, intent_provenance, intent_evidence
                    FROM {details['table']} WHERE sig = ?""",
                (source_sig,),
            ).fetchone()
            fees_found = conn.execute(
                "SELECT amount_usdc_units FROM fee_entries WHERE sig = ?",
                (source_sig,),
            ).fetchall()

    assert before == after
    assert json.loads(after[-1])["service_terms_version"] == 7
    assert json.loads(after[-1])["refund_solana_fee_units"] == 10
    assert after[8] == 100
    assert fees_found == ([(10,)] if lifecycle == "confirmed" else [])


@pytest.mark.parametrize(
    "corruption",
    [
        "absent_provenance",
        "malformed_evidence",
        "recovery_only",
        "source",
        "destination",
        "output",
        "signature",
        "terms_bool",
        "duplicate_key",
        "version_bool",
        "source_timestamp_float",
    ],
)
def test_live_confirmation_fails_closed_on_incomplete_or_conflicting_provenance(
    tmp_path, corruption
):
    source_sig = f"corrupt-{corruption}"
    signature = str(Signature.from_bytes(bytes([41]) * 64))
    with _isolated_current_state(tmp_path) as db_path:
        memo = _prepare_current_disposition(
            "refund", source_sig=source_sig, signature=signature
        )
        assert state_db.record_solana_sig_disposition_submission(
            source_sig=source_sig, kind="refund", payout_signature=signature
        )
        with sqlite3.connect(db_path) as conn:
            if corruption == "absent_provenance":
                conn.execute(
                    "UPDATE refunded_sigs SET intent_provenance = NULL WHERE sig = ?",
                    (source_sig,),
                )
            elif corruption == "malformed_evidence":
                conn.execute(
                    "UPDATE refunded_sigs SET intent_evidence = '{' WHERE sig = ?",
                    (source_sig,),
                )
            elif corruption == "recovery_only":
                conn.execute(
                    "UPDATE refunded_sigs SET intent_provenance = 'recovery_only' WHERE sig = ?",
                    (source_sig,),
                )
            elif corruption == "source":
                conn.execute(
                    "UPDATE refunded_sigs SET from_address = 'conflicting-source' WHERE sig = ?",
                    (source_sig,),
                )
            elif corruption == "destination":
                conn.execute(
                    """UPDATE refunded_sigs SET destination_address = 'conflicting-destination'
                       WHERE sig = ?""",
                    (source_sig,),
                )
            elif corruption == "output":
                conn.execute(
                    "UPDATE refunded_sigs SET refunded_units = 99 WHERE sig = ?",
                    (source_sig,),
                )
            elif corruption == "signature":
                conn.execute(
                    "UPDATE refunded_sigs SET refund_sig = 'conflicting-signature' WHERE sig = ?",
                    (source_sig,),
                )
            else:
                evidence = conn.execute(
                    "SELECT intent_evidence FROM refunded_sigs WHERE sig = ?", (source_sig,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE refunded_sigs SET intent_evidence = ? WHERE sig = ?",
                    (_corrupt_intent_evidence(evidence, corruption), source_sig),
                )
        with sqlite3.connect(db_path) as conn:
            disposition_before = conn.execute(
                "SELECT * FROM refunded_sigs WHERE sig = ?", (source_sig,)
            ).fetchone()
            source_before = conn.execute(
                "SELECT * FROM unprocessed_sigs WHERE sig = ?", (source_sig,)
            ).fetchone()
            cap_before = conn.execute(
                """SELECT event, amount_usdc_units, signature, evidence, timestamp
                   FROM solana_payout_budget_events WHERE obligation_id = ? ORDER BY id""",
                (f"refund:{source_sig}",),
            ).fetchall()
            fee_before = conn.execute(
                "SELECT * FROM fee_entries WHERE sig = ?", (source_sig,)
            ).fetchall()

        with patch.object(solana_client, "_get_client", return_value=_RpcClient()), patch.object(
            solana_client, "_rpc_call",
            return_value=_exact_disposition_transaction(signature, memo),
        ):
            processed = solana_client.check_sig_confirmations(1, 2.0)

        with sqlite3.connect(db_path) as conn:
            disposition_after = conn.execute(
                "SELECT * FROM refunded_sigs WHERE sig = ?", (source_sig,)
            ).fetchone()
            source_after = conn.execute(
                "SELECT * FROM unprocessed_sigs WHERE sig = ?", (source_sig,)
            ).fetchone()
            cap_after = conn.execute(
                """SELECT event, amount_usdc_units, signature, evidence, timestamp
                   FROM solana_payout_budget_events WHERE obligation_id = ? ORDER BY id""",
                (f"refund:{source_sig}",),
            ).fetchall()
            fee_after = conn.execute(
                "SELECT * FROM fee_entries WHERE sig = ?", (source_sig,)
            ).fetchall()

    assert processed == 0
    assert disposition_after == disposition_before
    assert disposition_after[9] == "awaiting confirmation"
    assert source_after == source_before
    assert source_after[6] == "refund sent, awaiting confirmation"
    assert cap_after == cap_before
    assert {row[0] for row in cap_after} == {"reserved", "submitted"}
    assert fee_after == fee_before == []


class _RpcClient:
    def get_transaction(self, *args, **kwargs):
        raise AssertionError("transport is patched")


def _exact_disposition_transaction(signature: str, memo: str) -> dict:
    return {
        "transaction": {
            "signatures": [signature],
            "message": {
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
                                "destination": "destination-token-account",
                                "mint": str(config.USDC_MINT),
                                "tokenAmount": {"amount": "100"},
                            },
                        },
                    },
                    {"program": "spl-memo", "parsed": memo},
                ],
            },
        },
        "meta": {"err": None, "logMessages": []},
    }


def test_actual_live_confirmation_uses_frozen_output_after_terms_change(tmp_path):
    signature = str(Signature.from_bytes(bytes([51]) * 64))
    old_pair = replace(
        config.SWAP_PAIR,
        fees=replace(config.SWAP_PAIR.fees, refund_solana_units=10),
    )
    with patch.object(config, "SWAP_PAIR", old_pair), patch.object(
        config, "SERVICE_TERMS_VERSION", 3
    ), _isolated_current_state(tmp_path) as db_path:
        memo = _prepare_current_disposition(
            "refund", source_sig="live-confirmation", signature=signature
        )
        assert state_db.record_solana_sig_disposition_submission(
            source_sig="live-confirmation", kind="refund", payout_signature=signature
        )
        changed_pair = replace(
            config.SWAP_PAIR,
            fees=replace(config.SWAP_PAIR.fees, refund_solana_units=99),
        )
        with patch.object(config, "SWAP_PAIR", changed_pair), patch.object(
            config, "SERVICE_TERMS_VERSION", 100
        ), patch.object(solana_client, "_get_client", return_value=_RpcClient()), patch.object(
            solana_client, "_rpc_call", return_value=_exact_disposition_transaction(signature, memo)
        ):
            assert solana_client.check_sig_confirmations(1, 2.0) == 1

        with sqlite3.connect(db_path) as conn:
            terminal = conn.execute(
                "SELECT refunded_units, status FROM refunded_sigs WHERE sig = 'live-confirmation'"
            ).fetchone()
            fee = conn.execute(
                "SELECT amount_usdc_units FROM fee_entries WHERE sig = 'live-confirmation'"
            ).fetchone()

    assert terminal == (100, "refund_confirmed")
    assert fee == (10,)


@pytest.mark.parametrize("value", [True, 1.0])
@pytest.mark.parametrize(
    "field",
    ["amount_solana_units", "timestamp", "source_timestamp", "source_amount_solana_units"],
)
def test_startup_recovery_rejects_bool_and_float_wire_integers(value, field):
    evidence = {
        "kind": "refund",
        "source_signature": "source",
        "solana_signature": "payout",
        "destination_token_account": "source-token",
        "amount_solana_units": 100,
        "timestamp": 200,
        "source_timestamp": 100,
        "source_token_account": "source-token",
        "source_amount_solana_units": 110,
        "source_memo": None,
    }
    evidence[field] = value
    scan = {
        "complete": True,
        "reason": None,
        "legacy_nexus_txids": {},
        "malformed_nexus_memos": [],
        "nexus_payouts": {},
        "nexus_payout_timestamps": {},
        "refund_sigs": {},
        "quarantined_sigs": {},
        "solana_dispositions": {("refund", "source"): evidence},
    }
    with patch.object(solana_client, "scan_memos_since_timestamp", return_value=scan):
        result = startup_recovery._rebuild_solana_from_waterline(1)

    assert result == {
        "recovery_complete": False,
        "error": "solana_memo_scan_incomplete:invalid_disposition_evidence",
    }


def _empty_memo_scan() -> dict:
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


def test_actual_startup_recovery_keeps_out_of_range_legacy_hold_visible_and_nonsendable(
    tmp_path
):
    db_path = tmp_path / "old-outside-range.db"
    _create_pre_provenance_terminal(db_path, "refund")
    heartbeat = {
        "last_safe_timestamp_nexus": "199000",
        "last_safe_timestamp_solana": "199000",
    }
    pair = replace(
        config.SWAP_PAIR,
        nexus=replace(config.SWAP_PAIR.nexus, treasury_account="TREASURY"),
    )
    with patch.object(state_db, "DB_PATH", str(db_path)), patch.object(
        state_db.time, "time", return_value=200_000
    ), patch.object(startup_recovery.time, "time", return_value=200_000), patch.object(
        config, "SWAP_PAIR", pair
    ), patch.object(nexus_client, "get_heartbeat_asset", return_value=heartbeat), patch.object(
        nexus_client, "fetch_deposits_since", return_value=nexus_client.DepositScan([], True)
    ), patch.object(nexus_client, "get_last_reference", return_value=9), patch.object(
        solana_client, "scan_memos_since_timestamp", return_value=_empty_memo_scan()
    ), patch.object(solana_client, "send_solana_token_to_account_with_sig") as send:
        state_db.init_db()
        first = startup_recovery.perform_startup_recovery()
        with sqlite3.connect(db_path) as conn:
            first_snapshot = sorted(conn.iterdump())
        second = startup_recovery.perform_startup_recovery()
        assert fees.available_backing_surplus_solana_units(10_000, 0) == 0
        assert solana_client.process_solana_deposits_refunding() == 0
        assert solana_client.process_solana_deposits_quarantine() == 0
        issue = next(item for item in dashboard.api_issues()["issues"] if item["id"] == SOURCE_SIG)
        with sqlite3.connect(db_path) as conn:
            second_snapshot = sorted(conn.iterdump())

    assert first["recovery_complete"] is True
    assert second["recovery_complete"] is True
    assert second_snapshot == first_snapshot
    assert issue["status"] == "refund evidence held"
    assert "do not terminalize or retry" in issue["operator_action"]
    assert any("refund evidence held" in statement for statement in second_snapshot)
    send.assert_not_called()


def test_actual_startup_recovery_never_greens_an_incomplete_rolling_window_scan(tmp_path):
    db_path = tmp_path / "incomplete-window.db"
    heartbeat = {
        "last_safe_timestamp_nexus": "199000",
        "last_safe_timestamp_solana": "199000",
    }
    incomplete = {
        **_empty_memo_scan(),
        "complete": False,
        "reason": "pagination_truncated",
    }
    pair = replace(
        config.SWAP_PAIR,
        nexus=replace(config.SWAP_PAIR.nexus, treasury_account="TREASURY"),
    )
    with patch.object(state_db, "DB_PATH", str(db_path)), patch.object(
        state_db.time, "time", return_value=200_000
    ), patch.object(startup_recovery.time, "time", return_value=200_000), patch.object(
        config, "SWAP_PAIR", pair
    ), patch.object(nexus_client, "get_heartbeat_asset", return_value=heartbeat), patch.object(
        solana_client, "scan_memos_since_timestamp",
        side_effect=[_empty_memo_scan(), incomplete],
    ), patch.object(nexus_client, "fetch_deposits_since") as nexus_scan, patch.object(
        nexus_client, "get_last_reference"
    ) as reference:
        state_db.init_db()
        # Retained custody history reaches the scan-completeness gate.
        state_db.add_unprocessed_sig(
            "retained-source", 100, "nexus:recipient", "sender", 110,
            "policy held, non-sendable", None,
        )
        result = startup_recovery.perform_startup_recovery()

    assert result["recovery_complete"] is False
    assert result["recovery_incomplete"] is True
    assert result["error"] == "solana_memo_scan_incomplete:pagination_truncated"
    nexus_scan.assert_not_called()
    reference.assert_not_called()


@pytest.mark.parametrize("restore_mode", ["online_backup", "copied_db_wal"])
def test_real_legacy_database_backup_restore_migrates_atomically_and_idempotently(
    tmp_path, restore_mode
):
    source = tmp_path / "source.db"
    restored = tmp_path / f"{restore_mode}.db"
    _create_pre_provenance_terminal(source, "refund")

    source_conn = sqlite3.connect(source)
    source_conn.execute("PRAGMA journal_mode=WAL")
    source_conn.execute("PRAGMA wal_autocheckpoint=0")
    # Rewrite every safety-relevant row while WAL mode is active.  The copied
    # restore therefore needs the WAL file, while SQLite online backup reads the
    # same committed snapshot through the engine.
    source_conn.execute("UPDATE refunded_sigs SET amount_usdc_units = 10001")
    source_conn.execute("UPDATE fee_entries SET amount_usdc_units = 10000")
    source_conn.execute("UPDATE solana_payout_budget_events SET timestamp = 12")
    source_conn.commit()
    if restore_mode == "online_backup":
        with sqlite3.connect(restored) as destination:
            source_conn.backup(destination)
    else:
        assert os.path.exists(f"{source}-wal")
        shutil.copy2(source, restored)
        shutil.copy2(f"{source}-wal", f"{restored}-wal")
        if os.path.exists(f"{source}-shm"):
            shutil.copy2(f"{source}-shm", f"{restored}-shm")
    source_conn.close()

    with patch.object(state_db, "DB_PATH", str(restored)), patch.object(
        state_db.time, "time", return_value=200_000
    ):
        state_db.init_db()
        with sqlite3.connect(restored) as conn:
            first_dump = list(conn.iterdump())
        state_db.init_db()
        with sqlite3.connect(restored) as conn:
            second_dump = list(conn.iterdump())
            hold = conn.execute(
                "SELECT amount_usdc_units, status FROM unprocessed_sigs WHERE sig = ?",
                (SOURCE_SIG,),
            ).fetchone()
            migration_count = conn.execute(
                "SELECT COUNT(*) FROM solana_disposition_provenance_migrations"
            ).fetchone()[0]
            cap_rows = conn.execute(
                """SELECT event, amount_usdc_units, signature
                   FROM solana_payout_budget_events ORDER BY id"""
            ).fetchall()

    assert sorted(first_dump) == sorted(second_dump)
    assert hold == (10_001, "refund evidence held")
    assert migration_count == 1
    assert cap_rows == [
        ("reserved", 1, None),
        ("submitted", 1, PAYOUT_SIG),
        ("confirmed", 1, PAYOUT_SIG),
    ]


def test_prepare_failure_after_cap_write_rolls_back_and_retry_is_safe(tmp_path):
    with _isolated_current_state(tmp_path) as db_path:
        state_db.add_unprocessed_sig(
            "prepare-fault", 800, "memo", "source", 110, "to be refunded", None
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TRIGGER reject_refund_prepare BEFORE INSERT ON refunded_sigs
                   BEGIN SELECT RAISE(ABORT, 'injected prepare failure'); END"""
            )
        failed = state_db.prepare_solana_sig_disposition(
            source_sig="prepare-fault", kind="refund", timestamp=800,
            from_address="source", destination_address="destination",
            amount_usdc_units=110, memo="memo",
            payout_memo="swapService:v1:refund:prepare-fault",
            payout_units=100, cap_units=1_000,
        )
        assert failed.status is state_db.SolanaDispositionPrepareStatus.DB_FAILURE
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT 1 FROM solana_payout_budget_events WHERE obligation_id = 'refund:prepare-fault'"
            ).fetchone() is None
            assert conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = 'prepare-fault'"
            ).fetchone() == ("to be refunded",)
            conn.execute("DROP TRIGGER reject_refund_prepare")
        assert state_db.prepare_solana_sig_disposition(
            source_sig="prepare-fault", kind="refund", timestamp=800,
            from_address="source", destination_address="destination",
            amount_usdc_units=110, memo="memo",
            payout_memo="swapService:v1:refund:prepare-fault",
            payout_units=100, cap_units=1_000,
        )


@pytest.mark.parametrize("fault_target", ["fee", "source"])
def test_confirmation_failure_rolls_back_cap_fee_terminal_and_source_then_retries(
    tmp_path, fault_target
):
    signature = str(Signature.from_bytes(bytes([61]) * 64))
    source_sig = f"confirmation-{fault_target}"
    with _isolated_current_state(tmp_path) as db_path:
        _prepare_current_disposition("refund", source_sig=source_sig, signature=signature)
        assert state_db.record_solana_sig_disposition_submission(
            source_sig=source_sig, kind="refund", payout_signature=signature
        )
        with sqlite3.connect(db_path) as conn:
            if fault_target == "fee":
                conn.execute(
                    """CREATE TRIGGER reject_confirmation_fee BEFORE INSERT ON fee_entries
                       BEGIN SELECT RAISE(ABORT, 'injected fee failure'); END"""
                )
                trigger = "reject_confirmation_fee"
            else:
                conn.execute(
                    """CREATE TRIGGER reject_confirmation_source BEFORE DELETE ON unprocessed_sigs
                       BEGIN SELECT RAISE(ABORT, 'injected source failure'); END"""
                )
                trigger = "reject_confirmation_source"
        with pytest.raises(sqlite3.DatabaseError, match="injected .* failure"):
            state_db.confirm_solana_sig_disposition(
                source_sig=source_sig, kind="refund", payout_signature=signature
            )
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT status FROM refunded_sigs WHERE sig = ?", (source_sig,)
            ).fetchone() == ("awaiting confirmation",)
            assert conn.execute(
                "SELECT status FROM unprocessed_sigs WHERE sig = ?", (source_sig,)
            ).fetchone() == ("refund sent, awaiting confirmation",)
            assert conn.execute(
                "SELECT 1 FROM fee_entries WHERE sig = ?", (source_sig,)
            ).fetchone() is None
            assert conn.execute(
                """SELECT 1 FROM solana_payout_budget_events
                   WHERE obligation_id = ? AND event = 'confirmed'""",
                (f"refund:{source_sig}",),
            ).fetchone() is None
            conn.execute(f"DROP TRIGGER {trigger}")
        assert state_db.confirm_solana_sig_disposition(
            source_sig=source_sig, kind="refund", payout_signature=signature
        ) is True


def test_pre_provenance_ddl_migration_failure_is_atomic_and_retryable(tmp_path):
    """Failed legacy DDL leaves the committed fixture untouched and releases locks."""
    db_path = tmp_path / "pre-provenance-ddl-fault.db"
    _create_pre_provenance_terminal(db_path, "refund")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE refunded_sigs SET refunded_units = NULL WHERE sig = ?",
            (SOURCE_SIG,),
        )
        before = sorted(conn.iterdump())

    with patch.object(state_db, "DB_PATH", str(db_path)), patch.object(
        state_db.time, "time", return_value=1_000
    ):
        with pytest.raises(
            RuntimeError,
            match="unsafe refund terminal has unquantifiable legacy provenance",
        ):
            state_db.init_db()

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert sorted(conn.iterdump()) == before
            # This write proves the failed initializer released its transaction lock.
            conn.execute(
                "UPDATE refunded_sigs SET refunded_units = 1 WHERE sig = ?",
                (SOURCE_SIG,),
            )

        state_db.init_db()

        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT 1 FROM refunded_sigs WHERE sig = ?", (SOURCE_SIG,)
            ).fetchone() is None
            assert conn.execute(
                "SELECT amount_usdc_units, status FROM unprocessed_sigs WHERE sig = ?",
                (SOURCE_SIG,),
            ).fetchone() == (10_000, "refund evidence held")
            # A fresh immediate writer proves the successful retry also released its lock.
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()


def test_legacy_migration_failure_rolls_back_terminal_fee_and_hold_then_retries(tmp_path):
    db_path = tmp_path / "migration-fault.db"
    with patch.object(state_db, "DB_PATH", str(db_path)), patch.object(
        state_db.time, "time", return_value=1_000
    ):
        state_db.init_db()
        details = state_db._SOLANA_SIG_DISPOSITION["refund"]
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TRIGGER reject_migration_audit
                   BEFORE INSERT ON solana_disposition_provenance_migrations
                   BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"""
            )
            conn.execute(
                """INSERT INTO refunded_sigs
                   (sig, timestamp, from_address, destination_address,
                    amount_usdc_units, memo, payout_memo, refund_sig,
                    refunded_units, status, intent_provenance, intent_evidence)
                   VALUES (?, 800, 'source', 'destination', 110, 'memo', ?, ?, 100,
                           ?, 'legacy_unknown', NULL)""",
                (
                    SOURCE_SIG,
                    f"swapService:v1:refund:{SOURCE_SIG}",
                    PAYOUT_SIG,
                    details["terminal_status"],
                ),
            )
            conn.execute(
                """INSERT INTO fee_entries
                   (sig, txid, kind, amount_usdc_units, amount_usdd_units,
                    contract_id, timestamp)
                   VALUES (?, NULL, 'refund_flat_fee', 10, NULL, -1, 900)""",
                (SOURCE_SIG,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="injected migration failure"):
            state_db.init_db()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT status FROM refunded_sigs WHERE sig = ?", (SOURCE_SIG,)
            ).fetchone() == ("refund_confirmed",)
            assert conn.execute(
                "SELECT amount_usdc_units FROM fee_entries WHERE sig = ?", (SOURCE_SIG,)
            ).fetchone() == (10,)
            assert conn.execute(
                "SELECT 1 FROM unprocessed_sigs WHERE sig = ?", (SOURCE_SIG,)
            ).fetchone() is None
            conn.execute("DROP TRIGGER reject_migration_audit")
        state_db.init_db()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT 1 FROM refunded_sigs WHERE sig = ?", (SOURCE_SIG,)
            ).fetchone() is None
            assert conn.execute(
                "SELECT amount_usdc_units, status FROM unprocessed_sigs WHERE sig = ?",
                (SOURCE_SIG,),
            ).fetchone() == (110, "refund evidence held")
