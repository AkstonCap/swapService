import json
import sqlite3
from types import SimpleNamespace

import pytest

import nexus_transfer_operator as operator
from src import state_db


HOLD = "refund held for operator review"
READY = "ready for processing"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(path))
    state_db.init_db()
    return path


def add_credit(*, txid="source", contract_id=0, units=1_000_000, status=HOLD,
               receival_account=None):
    state_db.add_unprocessed_txid(
        txid=txid,
        contract_id=contract_id,
        timestamp=100,
        amount_usdd=units / 1_000_000,
        from_address=f"sender-{contract_id}",
        to_address="TREASURY",
        owner_from_address=f"owner-{contract_id}",
        confirmations_credit=2,
        status=status,
        receival_account=receival_account,
        amount_usdd_units=units,
        hold_reason="manual review" if status == HOLD else None,
    )


def complete_intent(intent, remote_txid, remote_contract_id):
    state_db.record_nexus_transfer_preparation(
        intent["id"], actor="alice", rationale="reviewed exact source"
    )
    state_db.authorize_nexus_transfer_intent(
        intent["id"], actor="alice", rationale="approved exact source",
        expected_reference=intent["reference"],
    )
    state_db.record_nexus_transfer_execution_request(
        intent["id"], actor="alice", rationale="execute exact source"
    )
    assert state_db.claim_nexus_transfer_intent(intent["id"])
    state_db.update_nexus_transfer_intent(
        intent["id"], status="completed", remote_txid=remote_txid,
        contract_id=remote_contract_id, resolved=True,
    )


def test_fresh_schema_and_reinit_keep_composite_identity_and_payout_terms(db):
    with sqlite3.connect(db) as conn:
        intent_columns = {row[1] for row in conn.execute("PRAGMA table_info(nexus_transfer_intents)")}
        pending_columns = {row[1] for row in conn.execute("PRAGMA table_info(unprocessed_txids)")}
        processed_columns = {row[1] for row in conn.execute("PRAGMA table_info(processed_txids)")}
        fee_columns = {row[1] for row in conn.execute("PRAGMA table_info(fee_entries)")}
    assert "source_contract_id" in intent_columns
    assert {"payout_solana_units", "payout_fee_nexus_units"} <= pending_columns
    assert {
        "payout_solana_units", "payout_fee_nexus_units", "payout_receival_account"
    } <= processed_columns
    assert "contract_id" in fee_columns

    add_credit(txid="same", contract_id=0)
    add_credit(txid="same", contract_id=1)
    first = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="same", source_contract_id=0,
        from_address="TREASURY", to_address="sender-0", amount_usdd_units=1_000_000,
    )
    second = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="same", source_contract_id=1,
        from_address="TREASURY", to_address="sender-1", amount_usdd_units=1_000_000,
    )
    assert first["id"] != second["id"]
    assert first["reference"] != second["reference"]

    state_db.init_db()
    assert state_db.get_nexus_transfer_intent(first["id"])["source_contract_id"] == 0
    assert state_db.get_nexus_transfer_intent(second["id"])["source_contract_id"] == 1


def test_legacy_intent_migration_preserves_evidence_and_holds_without_fabrication(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE nexus_transfer_intents (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_txid TEXT NOT NULL,
                from_address TEXT NOT NULL, to_address TEXT NOT NULL,
                amount_usdd_units INTEGER NOT NULL, reference TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, remote_txid TEXT, contract_id INTEGER,
                created_timestamp INTEGER NOT NULL, last_attempt_timestamp INTEGER,
                resolved_timestamp INTEGER, UNIQUE(kind, source_txid)
            );
            CREATE TABLE nexus_transfer_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT NOT NULL,
                action TEXT NOT NULL, actor TEXT NOT NULL, rationale TEXT NOT NULL,
                evidence TEXT, timestamp INTEGER NOT NULL, UNIQUE(intent_id, action)
            );
            INSERT INTO nexus_transfer_intents VALUES
                ('legacy-id', 'refund', 'shared-source', 'TREASURY', 'sender', 1000000,
                 'legacy-reference', 'completed', 'remote-evidence', 7, 10, 11, 12);
            INSERT INTO nexus_transfer_intents VALUES
                ('legacy-id-2', 'quarantine', 'shared-source', 'TREASURY', 'quarantine',
                 1000000, 'legacy-reference-2', 'submitted', 'remote-evidence-2', 8,
                 10, 11, NULL);
            INSERT INTO nexus_transfer_audit_events
                (intent_id, action, actor, rationale, evidence, timestamp)
                VALUES ('legacy-id', 'authorized_execution', 'old-operator',
                        'old-rationale', 'legacy-reference', 10);
        """)
    monkeypatch.setattr(state_db, "DB_PATH", str(path))

    state_db.init_db()
    state_db.init_db()

    migrated = state_db.get_nexus_transfer_intent("legacy-id")
    assert migrated["source_contract_id"] == -1
    assert migrated["status"] == "legacy_manual_hold"
    assert migrated["remote_txid"] == "remote-evidence"
    assert migrated["contract_id"] == 7
    assert migrated["reference"] == "legacy-reference"
    migrated_sibling = state_db.get_nexus_transfer_intent("legacy-id-2")
    assert migrated_sibling["source_contract_id"] == -1
    assert migrated_sibling["status"] == "legacy_manual_hold"
    assert migrated_sibling["remote_txid"] == "remote-evidence-2"
    assert migrated_sibling["contract_id"] == 8
    assert migrated_sibling["reference"] == "legacy-reference-2"
    assert state_db.get_nexus_transfer_audit_events("legacy-id")[0]["actor"] == "old-operator"

    with pytest.raises(ValueError, match="legacy"):
        state_db.authorize_nexus_transfer_intent(
            "legacy-id", actor="alice", rationale="unsafe",
            expected_reference="legacy-reference",
        )
    with pytest.raises(ValueError, match="legacy"):
        state_db.record_nexus_transfer_execution_request(
            "legacy-id", actor="alice", rationale="unsafe"
        )
    assert state_db.claim_nexus_transfer_intent("legacy-id") is None
    assert not state_db.finalize_nexus_transfer_disposition(
        "legacy-id", actor="alice", rationale="unsafe",
        expected_remote_txid="remote-evidence",
    )
    with pytest.raises(ValueError, match="legacy"):
        state_db.create_nexus_transfer_intent(
            kind="refund", source_txid="shared-source", source_contract_id=0,
            from_address="TREASURY", to_address="sender", amount_usdd_units=1_000_000,
        )


def test_siblings_prepare_authorize_and_finalize_independently(db):
    add_credit(txid="siblings", contract_id=0)
    add_credit(txid="siblings", contract_id=1)
    intents = [
        state_db.create_nexus_transfer_intent(
            kind="refund", source_txid="siblings", source_contract_id=contract_id,
            from_address="TREASURY", to_address=f"sender-{contract_id}",
            amount_usdd_units=1_000_000,
        )
        for contract_id in (0, 1)
    ]
    complete_intent(intents[0], "remote-0", 40)
    complete_intent(intents[1], "remote-1", 41)

    assert state_db.finalize_nexus_transfer_disposition(
        intents[0]["id"], actor="alice", rationale="confirmed source zero",
        expected_remote_txid="remote-0",
    )
    assert state_db.is_unprocessed_txid("siblings", 1)
    assert not state_db.is_unprocessed_txid("siblings", 0)
    assert state_db.is_refunded_txid("siblings", 0)
    assert not state_db.is_refunded_txid("siblings", 1)

    assert state_db.finalize_nexus_transfer_disposition(
        intents[1]["id"], actor="alice", rationale="confirmed source one",
        expected_remote_txid="remote-1",
    )
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT txid, contract_id, sig FROM refunded_txids ORDER BY contract_id"
        ).fetchall()
    assert rows == [("siblings", 0, "remote-0"), ("siblings", 1, "remote-1")]
    evidence = json.loads(state_db.get_nexus_transfer_audit_events(intents[1]["id"])[0]["evidence"])
    assert evidence["source_txid"] == "siblings"
    assert evidence["source_contract_id"] == 1


def test_finalization_rolls_back_when_audit_slot_contains_conflicting_evidence(db):
    add_credit(txid="audit-conflict", contract_id=2)
    intent = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="audit-conflict", source_contract_id=2,
        from_address="TREASURY", to_address="sender-2", amount_usdd_units=1_000_000,
    )
    complete_intent(intent, "remote-audit", 8)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO nexus_transfer_audit_events
               (intent_id, action, actor, rationale, evidence, timestamp)
               VALUES (?, 'finalized_refund', 'mallory', 'conflict', 'tampered', 1)""",
            (intent["id"],),
        )
    with pytest.raises(ValueError, match="conflicting.*audit"):
        state_db.finalize_nexus_transfer_disposition(
            intent["id"], actor="alice", rationale="real finalization",
            expected_remote_txid="remote-audit",
        )
    assert state_db.is_unprocessed_txid("audit-conflict", 2)
    assert not state_db.is_refunded_txid("audit-conflict", 2)


def test_finalization_never_replaces_conflicting_terminal_evidence(db):
    add_credit(txid="terminal-conflict", contract_id=4)
    intent = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="terminal-conflict", source_contract_id=4,
        from_address="TREASURY", to_address="sender-4", amount_usdd_units=1_000_000,
    )
    complete_intent(intent, "real-remote", 3)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO refunded_txids
               (txid, contract_id, timestamp, amount_usdd, from_address, to_address,
                owner_from_address, confirmations_credit, status, sig)
               VALUES ('terminal-conflict', 4, 1, 99.0, 'attacker', 'wrong', 'wrong',
                       0, 'conflicting', 'conflicting-remote')"""
        )
    assert not state_db.finalize_nexus_transfer_disposition(
        intent["id"], actor="alice", rationale="must refuse conflict",
        expected_remote_txid="real-remote",
    )
    assert state_db.is_unprocessed_txid("terminal-conflict", 4)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT amount_usdd, sig FROM refunded_txids "
            "WHERE txid='terminal-conflict' AND contract_id=4"
        ).fetchone() == (99.0, "conflicting-remote")


def test_operator_finalization_rechecks_no_competing_payout_evidence(db):
    add_credit(txid="late-payout", contract_id=6)
    intent = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="late-payout", source_contract_id=6,
        from_address="TREASURY", to_address="sender-6", amount_usdd_units=1_000_000,
    )
    complete_intent(intent, "refund-remote", 9)
    state_db.update_unprocessed_txid(
        txid="late-payout", contract_id=6, sig="competing-solana-payout"
    )
    assert not state_db.finalize_nexus_transfer_disposition(
        intent["id"], actor="alice", rationale="must hold competing evidence",
        expected_remote_txid="refund-remote",
    )
    assert state_db.is_unprocessed_txid("late-payout", 6)
    assert not state_db.is_refunded_txid("late-payout", 6)


def test_operator_prepare_requires_and_uses_contract_id(db, monkeypatch):
    add_credit(txid="operator-source", contract_id=3)
    monkeypatch.setattr(
        operator, "_nexus_modules",
        lambda: (SimpleNamespace(NEXUS_USDD_TREASURY_ACCOUNT="TREASURY"), None),
    )
    assert operator.main([
        "--db", str(db), "prepare", "--kind", "refund", "--txid", "operator-source",
        "--contract-id", "3", "--operator", "alice", "--reason", "reviewed",
    ]) == 0
    intents = state_db.get_nexus_transfer_intents_by_status(("prepared",))
    assert len(intents) == 1
    assert intents[0]["source_contract_id"] == 3

    with pytest.raises(SystemExit):
        operator.main([
            "--db", str(db), "prepare", "--kind", "refund", "--txid", "operator-source",
            "--operator", "alice", "--reason", "reviewed",
        ])


def test_prepare_nexus_payout_claims_immutable_terms_once(db):
    add_credit(
        txid="payout", contract_id=4, units=2_000_000, status=READY,
        receival_account="SOLANA-DEST",
    )
    terms = dict(
        txid="payout", contract_id=4, receival_account="SOLANA-DEST",
        amount_usdd_units=2_000_000, payout_solana_units=1_800_000,
        payout_fee_nexus_units=200_000,
    )
    assert state_db.prepare_nexus_payout(**terms)
    assert not state_db.prepare_nexus_payout(**terms)
    assert not state_db.prepare_nexus_payout(
        **{**terms, "payout_solana_units": 1_700_000,
           "payout_fee_nexus_units": 300_000}
    )
    row = state_db.get_unprocessed_txids_as_dicts()[0]
    assert row["comment"] == "sending"
    assert row["payout_solana_units"] == 1_800_000
    assert row["payout_fee_nexus_units"] == 200_000


def test_payout_claim_and_transfer_disposition_are_mutually_exclusive(db):
    add_credit(txid="exclusive-intent", contract_id=1, status=HOLD)
    intent = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="exclusive-intent", source_contract_id=1,
        from_address="TREASURY", to_address="sender-1", amount_usdd_units=1_000_000,
    )
    state_db.update_unprocessed_txid(
        txid="exclusive-intent", contract_id=1, status=READY,
        receival_account="SOLANA-DEST",
    )
    assert not state_db.prepare_nexus_payout(
        txid="exclusive-intent", contract_id=1, receival_account="SOLANA-DEST",
        amount_usdd_units=1_000_000, payout_solana_units=900_000,
        payout_fee_nexus_units=100_000,
    )
    assert state_db.get_nexus_transfer_intent(intent["id"])["status"] == "prepared"

    add_credit(txid="exclusive-payout", contract_id=2, status=READY,
               receival_account="SOLANA-DEST")
    assert state_db.prepare_nexus_payout(
        txid="exclusive-payout", contract_id=2, receival_account="SOLANA-DEST",
        amount_usdd_units=1_000_000, payout_solana_units=900_000,
        payout_fee_nexus_units=100_000,
    )
    with pytest.raises(ValueError, match="held source"):
        state_db.create_nexus_transfer_intent(
            kind="refund", source_txid="exclusive-payout", source_contract_id=2,
            from_address="TREASURY", to_address="sender-2",
            amount_usdd_units=1_000_000,
        )


def test_authorization_rechecks_exact_held_source_under_transaction(db):
    add_credit(txid="changed-before-auth", contract_id=3)
    intent = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="changed-before-auth", source_contract_id=3,
        from_address="TREASURY", to_address="sender-3", amount_usdd_units=1_000_000,
    )
    state_db.record_nexus_transfer_preparation(
        intent["id"], actor="alice", rationale="initial review"
    )
    state_db.update_unprocessed_txid(
        txid="changed-before-auth", contract_id=3, status="sending", sig="payout-sig"
    )
    with pytest.raises(ValueError, match="exact held source"):
        state_db.authorize_nexus_transfer_intent(
            intent["id"], actor="alice", rationale="stale approval",
            expected_reference=intent["reference"],
        )
    assert state_db.get_nexus_transfer_intent(intent["id"])["status"] == "prepared"


def test_operator_claim_rechecks_source_after_authorization(db):
    add_credit(txid="changed-before-claim", contract_id=4)
    intent = state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="changed-before-claim", source_contract_id=4,
        from_address="TREASURY", to_address="sender-4", amount_usdd_units=1_000_000,
    )
    state_db.record_nexus_transfer_preparation(
        intent["id"], actor="alice", rationale="reviewed"
    )
    state_db.authorize_nexus_transfer_intent(
        intent["id"], actor="alice", rationale="approved",
        expected_reference=intent["reference"],
    )
    state_db.record_nexus_transfer_execution_request(
        intent["id"], actor="alice", rationale="requested"
    )
    state_db.update_unprocessed_txid(
        txid="changed-before-claim", contract_id=4, status="sending", sig="payout-sig"
    )
    assert state_db.claim_nexus_transfer_intent(intent["id"]) is None
    assert state_db.get_nexus_transfer_intent(intent["id"])["status"] == "authorized"


@pytest.mark.parametrize(
    "override",
    [
        {"contract_id": -1},
        {"amount_usdd_units": True},
        {"amount_usdd_units": 0},
        {"payout_solana_units": 0},
        {"payout_solana_units": 1.5},
        {"payout_fee_nexus_units": -1},
        {"payout_fee_nexus_units": 2_000_001},
        {"receival_account": ""},
    ],
)
def test_prepare_nexus_payout_rejects_invalid_terms_without_claiming(db, override):
    add_credit(
        txid="invalid-payout", contract_id=4, units=2_000_000, status=READY,
        receival_account="SOLANA-DEST",
    )
    terms = dict(
        txid="invalid-payout", contract_id=4, receival_account="SOLANA-DEST",
        amount_usdd_units=2_000_000, payout_solana_units=1_800_000,
        payout_fee_nexus_units=200_000,
    )
    with pytest.raises(ValueError):
        state_db.prepare_nexus_payout(**{**terms, **override})
    row = state_db.get_unprocessed_txids_as_dicts()[0]
    assert row["comment"] == READY
    assert row["payout_solana_units"] is None
    assert row["payout_fee_nexus_units"] is None


def test_prepare_nexus_payout_database_failure_rolls_back_claim(db):
    add_credit(
        txid="rollback-payout", contract_id=2, units=2_000_000, status=READY,
        receival_account="SOLANA-DEST",
    )
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TRIGGER reject_payout_claim BEFORE UPDATE ON unprocessed_txids
            WHEN NEW.status = 'sending'
            BEGIN SELECT RAISE(ABORT, 'injected payout failure'); END
        """)
    with pytest.raises(sqlite3.DatabaseError, match="injected payout failure"):
        state_db.prepare_nexus_payout(
            txid="rollback-payout", contract_id=2, receival_account="SOLANA-DEST",
            amount_usdd_units=2_000_000, payout_solana_units=1_800_000,
            payout_fee_nexus_units=200_000,
        )
    row = state_db.get_unprocessed_txids_as_dicts()[0]
    assert row["comment"] == READY
    assert row["payout_solana_units"] is None


def finalize_credit_args(**overrides):
    values = dict(
        txid="credit", contract_id=5, timestamp=100, amount_usdd=2.0,
        amount_usdd_units=2_000_000, from_address="sender-5", to_address="TREASURY",
        owner="owner-5", sig="solana-payout", status="processed",
        fee_kind="swap_nexus_to_solana", fee_nexus_units=200_000,
    )
    values.update(overrides)
    return values


def freeze_payout(*, txid="credit", contract_id=5, source_units=2_000_000,
                  payout_units=1_800_000, fee_units=200_000,
                  destination="SOLANA-DEST"):
    assert state_db.prepare_nexus_payout(
        txid=txid, contract_id=contract_id, receival_account=destination,
        amount_usdd_units=source_units, payout_solana_units=payout_units,
        payout_fee_nexus_units=fee_units,
    )
    state_db.update_unprocessed_txid(
        txid=txid, contract_id=contract_id,
        status="sig created, awaiting confirmations", sig="solana-payout",
    )


def test_finalize_nexus_credit_atomically_records_fee_terminal_and_exact_delete(db):
    add_credit(txid="credit", contract_id=5, units=2_000_000,
               status="sig created, awaiting confirmations", receival_account="SOLANA-DEST")
    add_credit(txid="credit", contract_id=6, units=3_000_000,
               status="sig created, awaiting confirmations", receival_account="OTHER")
    state_db.update_unprocessed_txid(txid="credit", contract_id=5, status=READY)
    freeze_payout()

    args = finalize_credit_args()
    assert state_db.finalize_nexus_credit(**args)
    assert state_db.finalize_nexus_credit(**args)
    assert not state_db.is_unprocessed_txid("credit", 5)
    assert state_db.is_unprocessed_txid("credit", 6)
    assert state_db.is_processed_txid("credit", 5)
    with sqlite3.connect(db) as conn:
        fees = conn.execute(
            "SELECT txid, contract_id, kind, amount_usdd_units FROM fee_entries"
        ).fetchall()
        terminals = conn.execute(
            "SELECT txid, contract_id, sig, amount_usdd_units FROM processed_txids"
        ).fetchall()
    assert fees == [("credit", 5, "swap_nexus_to_solana", 200_000)]
    assert terminals == [("credit", 5, "solana-payout", 2_000_000)]
    terminal = state_db.get_processed_nexus_credit("credit", 5)
    assert terminal["payout_solana_units"] == 1_800_000
    assert terminal["payout_fee_nexus_units"] == 200_000
    assert terminal["payout_receival_account"] == "SOLANA-DEST"


def test_finalize_positive_payout_requires_frozen_source_terms(db):
    assert not state_db.finalize_nexus_credit(**finalize_credit_args(
        txid="missing-source", contract_id=5,
    ))
    add_credit(txid="credit", contract_id=5, units=2_000_000, status=READY,
               receival_account="SOLANA-DEST")
    assert not state_db.finalize_nexus_credit(**finalize_credit_args())
    assert state_db.is_unprocessed_txid("credit", 5)


def test_finalize_positive_payout_rejects_fee_conflicting_with_frozen_terms(db):
    add_credit(txid="credit", contract_id=5, units=2_000_000, status=READY,
               receival_account="SOLANA-DEST")
    freeze_payout()
    assert not state_db.finalize_nexus_credit(**finalize_credit_args(
        fee_nexus_units=300_000,
    ))
    assert state_db.is_unprocessed_txid("credit", 5)
    assert not state_db.is_processed_txid("credit", 5)


def test_finalize_positive_payout_rejects_conflicting_pending_signature(db):
    add_credit(txid="credit", contract_id=5, units=2_000_000, status=READY,
               receival_account="SOLANA-DEST")
    freeze_payout()
    assert not state_db.finalize_nexus_credit(**finalize_credit_args(
        sig="different-payout-signature",
    ))
    assert state_db.is_unprocessed_txid("credit", 5)
    assert not state_db.is_processed_txid("credit", 5)


def test_finalize_nexus_credit_allows_exact_fee_only_and_no_fee_forms(db):
    add_credit(txid="fee-only", contract_id=0, units=100, status=READY)
    assert state_db.finalize_nexus_credit(**finalize_credit_args(
        txid="fee-only", contract_id=0, amount_usdd=0.0001, amount_usdd_units=100,
        from_address="sender-0", owner="owner-0", sig="",
        status="processed as fees", fee_kind="below_min_credit_nexus",
        fee_nexus_units=100,
    ))
    add_credit(txid="no-fee", contract_id=1, units=100, status=READY)
    state_db.update_unprocessed_txid(
        txid="no-fee", contract_id=1, receival_account="SOLANA-DEST"
    )
    freeze_payout(
        txid="no-fee", contract_id=1, source_units=100, payout_units=100,
        fee_units=0,
    )
    assert state_db.finalize_nexus_credit(**finalize_credit_args(
        txid="no-fee", contract_id=1, amount_usdd=0.0001, amount_usdd_units=100,
        from_address="sender-1", owner="owner-1", fee_kind=None,
        fee_nexus_units=0,
    ))


@pytest.mark.parametrize(
    "override",
    [
        {"contract_id": -1},
        {"amount_usdd_units": True},
        {"amount_usdd_units": 0},
        {"fee_nexus_units": -1},
        {"fee_nexus_units": 2_000_001},
        {"fee_kind": None, "fee_nexus_units": 1},
        {"fee_kind": "unexpected", "fee_nexus_units": 0},
        {"sig": "", "fee_nexus_units": 200_000},
        {"sig": "", "status": "processed", "fee_nexus_units": 2_000_000},
    ],
)
def test_finalize_nexus_credit_rejects_invalid_money_or_evidence(db, override):
    add_credit(txid="credit", contract_id=5, units=2_000_000, status=READY)
    with pytest.raises(ValueError):
        state_db.finalize_nexus_credit(**finalize_credit_args(**override))
    assert state_db.is_unprocessed_txid("credit", 5)
    assert not state_db.is_processed_txid("credit", 5)


def test_finalize_nexus_credit_conflicting_replay_preserves_first_evidence(db):
    add_credit(txid="credit", contract_id=5, units=2_000_000, status=READY)
    state_db.update_unprocessed_txid(
        txid="credit", contract_id=5, receival_account="SOLANA-DEST"
    )
    freeze_payout()
    assert state_db.finalize_nexus_credit(**finalize_credit_args())
    assert not state_db.finalize_nexus_credit(**finalize_credit_args(sig="different-sig"))
    assert not state_db.finalize_nexus_credit(**finalize_credit_args(fee_nexus_units=300_000))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT sig FROM processed_txids WHERE txid='credit' AND contract_id=5"
        ).fetchone() == ("solana-payout",)
        assert conn.execute(
            "SELECT amount_usdd_units FROM fee_entries WHERE txid='credit' AND contract_id=5"
        ).fetchone() == (200_000,)


def test_finalize_nexus_credit_database_failure_rolls_back_fee_and_terminal(db):
    add_credit(txid="credit", contract_id=5, units=2_000_000, status=READY)
    state_db.update_unprocessed_txid(
        txid="credit", contract_id=5, receival_account="SOLANA-DEST"
    )
    freeze_payout()
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TRIGGER reject_credit_delete BEFORE DELETE ON unprocessed_txids
            WHEN OLD.txid = 'credit' AND OLD.contract_id = 5
            BEGIN SELECT RAISE(ABORT, 'injected finalization failure'); END
        """)
    with pytest.raises(sqlite3.DatabaseError, match="injected finalization failure"):
        state_db.finalize_nexus_credit(**finalize_credit_args())
    assert state_db.is_unprocessed_txid("credit", 5)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fee_entries").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM processed_txids").fetchone() == (0,)


def test_finalize_nexus_credit_supports_atomic_first_admission_without_pending_row(db):
    args = finalize_credit_args(
        txid="direct-fee", contract_id=9, amount_usdd=0.0001,
        amount_usdd_units=100, from_address="chain-sender", owner="chain-owner",
        sig="", status="processed as fees", fee_kind="fee_only_nexus_credit",
        fee_nexus_units=100,
    )
    assert state_db.finalize_nexus_credit(**args)
    assert state_db.finalize_nexus_credit(**args)
    assert state_db.is_processed_txid("direct-fee", 9)


def test_fee_or_payout_finalizer_cannot_supersede_transfer_disposition(db):
    add_credit(txid="finalizer-exclusive", contract_id=7)
    state_db.create_nexus_transfer_intent(
        kind="refund", source_txid="finalizer-exclusive", source_contract_id=7,
        from_address="TREASURY", to_address="sender-7", amount_usdd_units=1_000_000,
    )
    assert not state_db.finalize_nexus_credit(**finalize_credit_args(
        txid="finalizer-exclusive", contract_id=7, amount_usdd=1.0,
        amount_usdd_units=1_000_000, from_address="sender-7", owner="owner-7",
        sig="", status="processed as fees", fee_kind="fee_only_nexus_credit",
        fee_nexus_units=1_000_000,
    ))
    assert state_db.is_unprocessed_txid("finalizer-exclusive", 7)
    assert not state_db.is_processed_txid("finalizer-exclusive", 7)


def test_legacy_pending_and_fee_migration_preserves_rows_with_explicit_sentinels(tmp_path, monkeypatch):
    path = tmp_path / "legacy-ledger.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE unprocessed_txids (
                txid TEXT PRIMARY KEY, timestamp INTEGER, amount_usdd REAL,
                from_address TEXT, to_address TEXT, owner_from_address TEXT,
                confirmations_credit INTEGER, status TEXT, receival_account TEXT
            );
            INSERT INTO unprocessed_txids VALUES
                ('legacy-credit', 100, 1.0, 'sender', 'TREASURY', 'owner', 2,
                 'ready for processing', 'SOLANA-DEST');
            CREATE TABLE fee_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sig TEXT, txid TEXT,
                kind TEXT NOT NULL, amount_usdc_units INTEGER,
                amount_usdd_units INTEGER, timestamp INTEGER NOT NULL
            );
            INSERT INTO fee_entries
                (sig, txid, kind, amount_usdc_units, amount_usdd_units, timestamp)
                VALUES (NULL, 'legacy-credit', 'legacy-fee', NULL, 10, 100);
            CREATE TABLE processed_txids (
                txid TEXT PRIMARY KEY, timestamp INTEGER, amount_usdd REAL,
                from_address TEXT, to_address TEXT, owner TEXT, sig TEXT, status TEXT
            );
            INSERT INTO processed_txids VALUES
                ('legacy-terminal', 90, 0.5, 'old-sender', 'TREASURY', 'old-owner',
                 'old-payout', 'processed');
        """)
    monkeypatch.setattr(state_db, "DB_PATH", str(path))

    state_db.init_db()
    state_db.init_db()

    row = state_db.get_unprocessed_txids_as_dicts()[0]
    assert row["contract_id"] == -1
    assert row["payout_solana_units"] is None
    assert row["payout_fee_nexus_units"] is None
    with sqlite3.connect(path) as conn:
        fee = conn.execute(
            "SELECT txid, contract_id, kind, amount_usdd_units FROM fee_entries"
        ).fetchone()
    assert fee == ("legacy-credit", -1, "legacy-fee", 10)
    legacy_terminal = state_db.get_processed_nexus_credit("legacy-terminal", 0)
    assert legacy_terminal is None
    with sqlite3.connect(path) as conn:
        terminal = conn.execute(
            """SELECT contract_id, sig, payout_solana_units,
                      payout_fee_nexus_units, payout_receival_account
               FROM processed_txids WHERE txid = 'legacy-terminal'"""
        ).fetchone()
    assert terminal == (-1, "old-payout", None, None, None)


def test_fee_index_migration_fails_closed_on_conflicting_nonlegacy_evidence(db):
    with sqlite3.connect(db) as conn:
        conn.execute("DROP INDEX idx_fee_entries_nexus_source")
        for kind, units in (("first", 10), ("conflict", 11)):
            conn.execute(
                """INSERT INTO fee_entries
                   (sig, txid, kind, amount_usdc_units, amount_usdd_units,
                    contract_id, timestamp)
                   VALUES (NULL, 'duplicate-fee', ?, NULL, ?, 2, 100)""",
                (kind, units),
            )
    with pytest.raises(RuntimeError, match="conflicting Nexus fee evidence"):
        state_db.init_db()


def test_generic_solana_fee_api_keeps_legacy_shape_and_sentinel(db):
    state_db.add_fee_entry(
        sig="solana-sig", txid=None, kind="generic",
        amount_usdc_units=25, amount_usdd_units=None,
    )
    entry = state_db.get_fee_entries()[0]
    assert len(entry) == 7
    assert entry[1:6] == ("solana-sig", None, "generic", 25, None)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT contract_id FROM fee_entries WHERE sig='solana-sig'"
        ).fetchone() == (-1,)


def test_legacy_nexus_fee_evidence_holds_all_fresh_dispositions_for_txid(db):
    state_db.add_fee_entry(
        sig=None, txid="legacy-fee-source", kind="legacy-nexus-fee",
        amount_usdc_units=None, amount_usdd_units=100,
    )
    add_credit(txid="legacy-fee-source", contract_id=0, units=1_000_000)
    with pytest.raises(ValueError, match="held source"):
        state_db.create_nexus_transfer_intent(
            kind="refund", source_txid="legacy-fee-source", source_contract_id=0,
            from_address="TREASURY", to_address="sender-0", amount_usdd_units=1_000_000,
        )
    state_db.update_unprocessed_txid(
        txid="legacy-fee-source", contract_id=0, status=READY,
        receival_account="SOLANA-DEST",
    )
    assert not state_db.prepare_nexus_payout(
        txid="legacy-fee-source", contract_id=0, receival_account="SOLANA-DEST",
        amount_usdd_units=1_000_000, payout_solana_units=900_000,
        payout_fee_nexus_units=100_000,
    )
    assert not state_db.finalize_nexus_credit(**finalize_credit_args(
        txid="legacy-fee-source", contract_id=0, amount_usdd=1.0,
        amount_usdd_units=1_000_000, from_address="sender-0", owner="owner-0",
        sig="", status="processed as fees", fee_kind="fee_only_nexus_credit",
        fee_nexus_units=1_000_000,
    ))


def test_legacy_intents_are_never_returned_as_executable_or_recovered(db):
    with sqlite3.connect(db) as conn:
        conn.execute("""
            INSERT INTO nexus_transfer_intents
                (id, kind, source_txid, source_contract_id, from_address, to_address,
                 amount_usdd_units, reference, status, created_timestamp)
            VALUES ('legacy-active', 'refund', 'legacy-tx', -1, 'TREASURY', 'sender',
                    100, 'legacy-active-ref', 'executing', 1)
        """)
    assert state_db.get_nexus_transfer_intents_by_status(("executing",)) == []
    assert state_db.recover_interrupted_nexus_transfer_intents() == 0
    assert state_db.get_nexus_transfer_intent("legacy-active")["status"] == "legacy_manual_hold"
