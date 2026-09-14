import hashlib
import os
import sqlite3
import time
from typing import List, Optional, Tuple

from . import receipt_contract

DB_PATH = os.getenv("STATE_DB_PATH", "swap_service.db")


# --------------------------------------------------------------------------- #
# Frozen on-disk key strings.
#
# The bridge is token-pair agnostic, and its code identifiers say "solana" and
# "nexus" rather than naming the USDC/USDD pair it was first written for. These
# constants are the exception: their NAMES are generic, but their VALUES are the
# original strings, because they are rows in the state database rather than code.
#
#   * `attempts.action_key`  gates the retry budget. A renamed key looks like a
#     fresh action, so a deposit that had already burned its attempts would get a
#     full new budget and be retried past the point where it should have stopped.
#   * `reservations.kind`    is the cross-worker mutual-exclusion guard. A renamed
#     kind cannot see a reservation written by the previous build, so a process
#     that crashed mid-debit could be debited a second time after an upgrade.
#
# Both failure modes move real funds, and they are triggered by exactly the case a
# rename is most likely to hit: an upgrade over a database with work in flight.
# Renaming these would need a migration that rewrites the existing rows; a naming
# improvement does not justify that risk, so the values stay put.
#
# Purely descriptive strings carry no such property and WERE renamed: `payouts.kind`
# and `fee_entries.kind` are labels nothing branches on, so pre-upgrade rows simply
# keep their old label in the dashboard's history.
# --------------------------------------------------------------------------- #

DEBIT_RESERVATION_KIND = "usdc_to_usdd_debit"     # reservations.kind


def debit_attempt_key(sig: str) -> str:
    """Retry-budget key for debiting the Nexus-side token against a Solana deposit."""
    return f"usdd_debit:{sig}"


def refund_attempt_key(sig: str) -> str:
    """Retry-budget key for refunding a Solana deposit to its sender."""
    return f"usdc_refund:{sig}"


def quarantine_send_attempt_key(sig: str) -> str:
    """Retry-budget key for the on-chain move of a Solana deposit into quarantine."""
    return f"usdc_quarantine_send:{sig}"


def quarantine_attempt_key(sig: str) -> str:
    """Retry-budget key for marking a Solana deposit quarantined."""
    return f"usdc_quarantine:{sig}"


def payout_attempt_key(txid: str) -> str:
    """Retry-budget key for paying a Nexus credit out on Solana."""
    return f"usdc_send:{txid}"


def nexus_refund_attempt_key(txid: str) -> str:
    """Retry-budget key for refunding a Nexus credit to its sender."""
    return f"usdd_refund:{txid}"


def nexus_refund_unresolved_attempt_key(txid: str) -> str:
    """Retry-budget key for a Nexus refund whose outcome could not be determined."""
    return f"usdd_refund_unresolved:{txid}"


def nexus_refund_pending_attempt_key(txid: str) -> str:
    """Retry-budget key for a Nexus refund awaiting confirmation."""
    return f"usdd_refund_pending:{txid}"


def nexus_collect_refund_attempt_key(txid: str) -> str:
    """Retry-budget key for collecting funds back before a Nexus refund."""
    return f"usdd_collect_refund:{txid}"

def init_db():
    """Initialize DB tables if not exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # WAL is persistent per database file and improves concurrent read/write safety.
    cursor.execute("PRAGMA journal_mode=WAL")

    # Core tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_sigs (
            sig TEXT PRIMARY KEY,
            timestamp INTEGER,
            amount_usdc_units INTEGER,
            txid TEXT,
            amount_usdd REAL,
            amount_usdd_units INTEGER,
            nexus_destination TEXT,
            memo TEXT,
            status TEXT,
            reference INTEGER,
            contract_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unprocessed_sigs (
            sig TEXT PRIMARY KEY,
            timestamp INTEGER,
            memo TEXT,
            from_address TEXT,
            amount_usdc_units INTEGER,
            amount_usdd_units INTEGER,
            status TEXT,
            txid TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quarantined_sigs (
            sig TEXT PRIMARY KEY,
            timestamp INTEGER,
            from_address TEXT,
            destination_address TEXT,
            amount_usdc_units INTEGER,
            memo TEXT,
            payout_memo TEXT,
            quarantine_sig TEXT,
            quarantined_units INTEGER,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS refunded_sigs (
            sig TEXT PRIMARY KEY,
            timestamp INTEGER,
            from_address TEXT,
            destination_address TEXT,
            amount_usdc_units INTEGER,
            memo TEXT,
            payout_memo TEXT,
            refund_sig TEXT,
            refunded_units INTEGER,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unprocessed_txids (
            txid TEXT NOT NULL,
            contract_id INTEGER NOT NULL DEFAULT -1,
            timestamp INTEGER,
            amount_usdd REAL,
            from_address TEXT,
            to_address TEXT,
            owner_from_address TEXT,
            confirmations_credit INTEGER,
            status TEXT,
            receival_account TEXT,
            sig TEXT,
            amount_usdd_units INTEGER,
            hold_reason TEXT,
            payout_solana_units INTEGER,
            payout_fee_nexus_units INTEGER,
            PRIMARY KEY (txid, contract_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_txids (
            txid TEXT NOT NULL,
            contract_id INTEGER NOT NULL DEFAULT -1,
            timestamp INTEGER,
            amount_usdd REAL,
            amount_usdd_units INTEGER,
            from_address TEXT,
            to_address TEXT,
            owner TEXT,
            sig TEXT,
            status TEXT,
            payout_solana_units INTEGER,
            payout_fee_nexus_units INTEGER,
            payout_receival_account TEXT,
            PRIMARY KEY (txid, contract_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS refunded_txids (
            txid TEXT NOT NULL,
            contract_id INTEGER NOT NULL DEFAULT -1,
            timestamp INTEGER,
            amount_usdd REAL,
            from_address TEXT,
            to_address TEXT,
            owner_from_address TEXT,
            confirmations_credit INTEGER,
            status TEXT,
            sig TEXT,
            PRIMARY KEY (txid, contract_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quarantined_txids (
            txid TEXT NOT NULL,
            contract_id INTEGER NOT NULL DEFAULT -1,
            timestamp INTEGER,
            amount_usdd REAL,
            from_address TEXT,
            to_address TEXT,
            owner TEXT,
            sig TEXT,
            status TEXT,
            PRIMARY KEY (txid, contract_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            nickname TEXT PRIMARY KEY,
            chain TEXT,
            ticker TEXT,
            name TEXT,
            address TEXT,
            balance REAL,
            timestamp INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS heartbeat (
            name TEXT PRIMARY KEY,
            last_beat INTEGER,
            wline_sol INTEGER,
            wline_nxs INTEGER
        )
    """)
    
    # Reservations table for preventing duplicate processing (with TTL)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            PRIMARY KEY (kind, key)
        )
    """)
    
    # Attempts tracking for retry logic
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            action_key TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            last_timestamp INTEGER
        )
    """)

    # Every Nexus-side transfer must first have a durable local intent. A source
    # credit contract may authorize exactly one remote debit: allowing one "refund"
    # and one "quarantine" intent for the same source identity would permit two
    # dispositions of the same funds.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nexus_transfer_intents (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            source_txid TEXT NOT NULL,
            source_contract_id INTEGER NOT NULL DEFAULT -1,
            from_address TEXT NOT NULL,
            to_address TEXT NOT NULL,
            amount_usdd_units INTEGER NOT NULL,
            reference TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            remote_txid TEXT,
            contract_id INTEGER,
            created_timestamp INTEGER NOT NULL,
            last_attempt_timestamp INTEGER,
            resolved_timestamp INTEGER
        )
    """)
    # The ledger is append-only: authorization and final disposition are not inferred
    # from a mutable status value, but attributable to a named operator and rationale.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nexus_transfer_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            rationale TEXT NOT NULL,
            evidence TEXT,
            timestamp INTEGER NOT NULL,
            UNIQUE(intent_id, action)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_nexus_transfer_audit_events_intent ON nexus_transfer_audit_events(intent_id, timestamp)")
    
    # Counters table for atomic sequence generation (e.g., reference numbers)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS counters (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)
    
    # Waterline proposals (ephemeral, cleared after applying to heartbeat)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS waterline_proposals (
            chain TEXT PRIMARY KEY,
            proposed_timestamp INTEGER NOT NULL
        )
    """)

    # Durable cursor for a bounded, finalized Solana vault-history range.  The
    # public heartbeat retains a timestamp for compatibility, but a timestamp is
    # not a pagination cursor: multiple signatures may share it.  This state is
    # written in the same transaction as every admitted deposit page so a crash
    # cannot skip history by remembering `before` without its queue rows.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS solana_deposit_scan_cursor (
            vault_account TEXT PRIMARY KEY,
            mint TEXT NOT NULL,
            lower_timestamp INTEGER NOT NULL,
            before_signature TEXT,
            upper_timestamp INTEGER NOT NULL,
            started_timestamp INTEGER NOT NULL,
            network TEXT,
            commitment TEXT,
            query_identity TEXT,
            previous_timestamp INTEGER
        )
    """)
    cursor.execute("PRAGMA table_info(solana_deposit_scan_cursor)")
    _solana_scan_cursor_columns = {row[1] for row in cursor.fetchall()}
    for _column, _definition in (
        ("network", "TEXT"), ("commitment", "TEXT"),
        ("query_identity", "TEXT"), ("previous_timestamp", "INTEGER"),
    ):
        if _column not in _solana_scan_cursor_columns:
            cursor.execute(
                f"ALTER TABLE solana_deposit_scan_cursor ADD COLUMN {_column} {_definition}"
            )
    # Append-only evidence for each committed range page and completion.  It is
    # operational evidence, never a financial lifecycle replacement.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS solana_deposit_scan_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vault_account TEXT NOT NULL,
            mint TEXT NOT NULL,
            lower_timestamp INTEGER NOT NULL,
            upper_timestamp INTEGER NOT NULL,
            request_before_signature TEXT,
            next_before_signature TEXT,
            signature_count INTEGER NOT NULL,
            admitted_count INTEGER NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('page_committed', 'range_completed')),
            timestamp INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_solana_deposit_scan_events_vault ON solana_deposit_scan_events(vault_account, id)")

    # Helius continuation tokens are meaningful only for the exact immutable query
    # that produced them.  Keep this separate from the core-RPC `before` cursor so an
    # operational provider switch can never reinterpret one provider's cursor as the
    # other's.  Query identity includes network, vault, mint, commitment and both
    # timestamp boundaries; pages, deposits and holds are committed atomically below.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS helius_deposit_scan_cursor (
            vault_account TEXT PRIMARY KEY,
            network TEXT NOT NULL,
            mint TEXT NOT NULL,
            commitment TEXT NOT NULL,
            lower_timestamp INTEGER NOT NULL,
            upper_timestamp INTEGER NOT NULL,
            pagination_token TEXT,
            previous_timestamp INTEGER,
            query_identity TEXT NOT NULL UNIQUE,
            started_timestamp INTEGER NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS helius_deposit_scan_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_identity TEXT NOT NULL,
            network TEXT NOT NULL,
            vault_account TEXT NOT NULL,
            mint TEXT NOT NULL,
            commitment TEXT NOT NULL,
            lower_timestamp INTEGER NOT NULL,
            upper_timestamp INTEGER NOT NULL,
            request_pagination_token TEXT,
            next_pagination_token TEXT,
            signature_count INTEGER NOT NULL,
            admitted_count INTEGER NOT NULL,
            held_count INTEGER NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('page_committed', 'range_completed')),
            timestamp INTEGER NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_helius_deposit_scan_events_query "
        "ON helius_deposit_scan_events(query_identity, id)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS solana_deposit_scan_seen (
            query_identity TEXT NOT NULL,
            signature TEXT NOT NULL,
            block_timestamp INTEGER NOT NULL,
            PRIMARY KEY (query_identity, signature)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS solana_deposit_holds (
            signature TEXT PRIMARY KEY,
            block_timestamp INTEGER NOT NULL,
            memo TEXT,
            from_address TEXT,
            amount_units INTEGER,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            provider TEXT NOT NULL,
            query_identity TEXT NOT NULL,
            first_seen_timestamp INTEGER NOT NULL,
            updated_timestamp INTEGER NOT NULL,
            network TEXT,
            vault_account TEXT,
            mint TEXT,
            observed_commitment TEXT,
            finality_required INTEGER,
            replay_attempts INTEGER NOT NULL DEFAULT 0,
            last_replay_timestamp INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("PRAGMA table_info(solana_deposit_holds)")
    _solana_hold_columns = {row[1] for row in cursor.fetchall()}
    for _column, _definition in (
        ("network", "TEXT"), ("vault_account", "TEXT"), ("mint", "TEXT"),
        ("observed_commitment", "TEXT"), ("finality_required", "INTEGER"),
        ("replay_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_replay_timestamp", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if _column not in _solana_hold_columns:
            cursor.execute(
                f"ALTER TABLE solana_deposit_holds ADD COLUMN {_column} {_definition}"
            )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_solana_deposit_holds_reason_ts "
        "ON solana_deposit_holds(reason, block_timestamp)"
    )
    
    # Fee tracking journal
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fee_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sig TEXT,
            txid TEXT,
            kind TEXT NOT NULL,
            amount_usdc_units INTEGER,
            amount_usdd_units INTEGER,
            contract_id INTEGER NOT NULL DEFAULT -1,
            timestamp INTEGER NOT NULL
        )
    """)
    
    # Outbound payout ledger, for rolling exposure caps
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            amount_usdc_units INTEGER NOT NULL,
            reference TEXT,
            timestamp INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payouts_ts ON payouts(timestamp)")

    # Append-only budget events make every outbound-cap decision durable.  A reservation
    # is keyed by the underlying financial obligation, never by a helper call: retries,
    # restarts, and concurrent workers therefore all see the same capacity claim.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS solana_payout_budget_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            obligation_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('reserved', 'submitted', 'confirmed', 'released')),
            amount_usdc_units INTEGER NOT NULL,
            signature TEXT,
            evidence TEXT,
            timestamp INTEGER NOT NULL,
            UNIQUE(obligation_id, event)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_solana_payout_budget_events_obligation "
        "ON solana_payout_budget_events(obligation_id, event)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_solana_payout_budget_events_event_ts "
        "ON solana_payout_budget_events(event, timestamp)"
    )

    # Receipt publication spends operator NXS. Its frozen payload is committed with
    # payout finalization so a crash cannot lose the obligation.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS swap_receipts (
            source_signature TEXT PRIMARY KEY,
            receipt_name TEXT NOT NULL UNIQUE,
            expected_owner TEXT,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            asset_address TEXT,
            manual_review_error TEXT,
            created_timestamp INTEGER NOT NULL,
            updated_timestamp INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_swap_receipts_status ON swap_receipts(status, created_timestamp)")

    # An ambiguous create can still have spent NXS. Every reservation therefore stays
    # charged against the configured lifetime budget until separately reviewed accounting
    # can prove a different actual spend.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipt_nxs_budget_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_signature TEXT NOT NULL,
            receipt_name TEXT NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('reserved', 'create_reported', 'published')),
            expected_cost_nxs_units INTEGER NOT NULL,
            create_txid TEXT,
            asset_address TEXT,
            timestamp INTEGER NOT NULL,
            UNIQUE(source_signature, event)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_receipt_nxs_budget_events_source "
        "ON receipt_nxs_budget_events(source_signature, event)"
    )

    # Hot-path indexes. Every poll filters these tables by status and orders by
    # timestamp; without an index each is a full scan + sort. Measured at 20k rows:
    # ~2.6-3.1x faster status queries, and get_unprocessed_sigs() drops from a 34ms
    # full scan. Cheap to maintain at this write volume.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usigs_status_ts ON unprocessed_sigs(status, timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usigs_ts        ON unprocessed_sigs(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_utxids_status_ts ON unprocessed_txids(status, timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_utxids_ts        ON unprocessed_txids(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rsigs_status    ON refunded_sigs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_qsigs_status    ON quarantined_sigs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fee_ts          ON fee_entries(timestamp)")

    # Latest metrics snapshot, written by the service loop and read by the operator
    # dashboard. Keeps the dashboard a pure DB reader: it needs no RPC access, no Nexus
    # CLI and no vault credentials of its own.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS metrics_snapshot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            timestamp INTEGER NOT NULL,
            vault_usdc_units INTEGER,
            circulating_usdd_units INTEGER,
            ratio_bps INTEGER,
            paused INTEGER,
            payouts_24h_units INTEGER,
            fees_usdc_units INTEGER,
            fees_usdd_units INTEGER
        )
    """)

    # Fee summary (optional aggregated view)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fee_summary (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_collected_usdc INTEGER DEFAULT 0,
            total_collected_usdd INTEGER DEFAULT 0,
            last_updated INTEGER
        )
    """)

    # --- Lightweight migrations for pre-existing databases ---
    # Receipt obligations originally required provider ownership before insertion. Rebuild
    # that small outbox table so new exact payout evidence can wait durably for later owner
    # authentication without dropping already-bound obligations.
    _receipt_info = list(cursor.execute("PRAGMA table_info(swap_receipts)"))
    _receipt_owner = next((row for row in _receipt_info if row[1] == "expected_owner"), None)
    if _receipt_owner is not None and _receipt_owner[3]:
        cursor.execute("""
            CREATE TABLE swap_receipts_owner_binding_v2 (
                source_signature TEXT PRIMARY KEY,
                receipt_name TEXT NOT NULL UNIQUE,
                expected_owner TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                asset_address TEXT,
                manual_review_error TEXT,
                created_timestamp INTEGER NOT NULL,
                updated_timestamp INTEGER NOT NULL
            )
        """)
        cursor.execute("""
            INSERT INTO swap_receipts_owner_binding_v2
            SELECT source_signature, receipt_name, expected_owner, payload_json, status,
                   asset_address, NULL, created_timestamp, updated_timestamp
            FROM swap_receipts
        """)
        cursor.execute("DROP TABLE swap_receipts")
        cursor.execute("ALTER TABLE swap_receipts_owner_binding_v2 RENAME TO swap_receipts")
        cursor.execute(
            "CREATE INDEX idx_swap_receipts_status "
            "ON swap_receipts(status, created_timestamp)"
        )
    _receipt_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(swap_receipts)")
    }
    if "manual_review_error" not in _receipt_columns:
        cursor.execute("ALTER TABLE swap_receipts ADD COLUMN manual_review_error TEXT")

    # unprocessed_txids.sig persists the Solana send signature so Nexus->Solana
    # confirmation can use get_signature_statuses instead of scanning memos.
    cursor.execute("PRAGMA table_info(unprocessed_txids)")
    _utx_cols = {row[1] for row in cursor.fetchall()}
    if "sig" not in _utx_cols:
        cursor.execute("ALTER TABLE unprocessed_txids ADD COLUMN sig TEXT")
    # Exact base-unit amount. `amount_usdd` is REAL, so deriving a refund from it
    # round-trips through binary float (8.29 -> 8289999 base units, a 1-unit shortfall).
    if "amount_usdd_units" not in _utx_cols:
        cursor.execute("ALTER TABLE unprocessed_txids ADD COLUMN amount_usdd_units INTEGER")
    # A held credit must retain why automated processing stopped so the dashboard
    # shows actionable evidence instead of only an opaque lifecycle label.
    if "hold_reason" not in _utx_cols:
        cursor.execute("ALTER TABLE unprocessed_txids ADD COLUMN hold_reason TEXT")
    if "payout_solana_units" not in _utx_cols:
        cursor.execute("ALTER TABLE unprocessed_txids ADD COLUMN payout_solana_units INTEGER")
    if "payout_fee_nexus_units" not in _utx_cols:
        cursor.execute("ALTER TABLE unprocessed_txids ADD COLUMN payout_fee_nexus_units INTEGER")

    # A debit intent fixes both the exact Nexus output and its unique reference before
    # the debit is attempted. The output cannot be recomputed later under changed fee
    # configuration: that would turn an active first-time recipient into a false
    # remote surplus during reconciliation.
    cursor.execute("PRAGMA table_info(unprocessed_sigs)")
    _usig_cols = {row[1] for row in cursor.fetchall()}
    if "reference" not in _usig_cols:
        cursor.execute("ALTER TABLE unprocessed_sigs ADD COLUMN reference INTEGER")
    if "amount_usdd_units" not in _usig_cols:
        cursor.execute("ALTER TABLE unprocessed_sigs ADD COLUMN amount_usdd_units INTEGER")

    # A refund/quarantine proof must match the exact token-account recipient used for
    # the send.  ``from_address`` can be a wallet owner, whose ATA is resolved before
    # submission, so it is not itself sufficient transaction evidence.  Existing
    # rows deliberately remain NULL and cannot be auto-terminalized.
    for _table in ("refunded_sigs", "quarantined_sigs"):
        _columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({_table})")}
        if "destination_address" not in _columns:
            cursor.execute(f"ALTER TABLE {_table} ADD COLUMN destination_address TEXT")
        if "payout_memo" not in _columns:
            cursor.execute(f"ALTER TABLE {_table} ADD COLUMN payout_memo TEXT")

    # Transfer intents written before contract-level source admission only identify a
    # transaction. Preserve every immutable id/reference/remote result and mark the
    # missing source contract explicitly as legacy. The rebuilt table deliberately has
    # no txid-only UNIQUE constraint: current intents are unique by exact source below,
    # while multiple legacy rows must remain visible for manual chain disposition.
    cursor.execute("PRAGMA table_info(nexus_transfer_intents)")
    _transfer_cols = {row[1] for row in cursor.fetchall()}
    if "source_contract_id" not in _transfer_cols:
        cursor.execute("DROP INDEX IF EXISTS idx_nexus_transfer_intents_source")
        cursor.execute("""
            CREATE TABLE nexus_transfer_intents_source_identity_v3 (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                source_txid TEXT NOT NULL,
                source_contract_id INTEGER NOT NULL DEFAULT -1,
                from_address TEXT NOT NULL,
                to_address TEXT NOT NULL,
                amount_usdd_units INTEGER NOT NULL,
                reference TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                remote_txid TEXT,
                contract_id INTEGER,
                created_timestamp INTEGER NOT NULL,
                last_attempt_timestamp INTEGER,
                resolved_timestamp INTEGER
            )
        """)
        remote_contract = "contract_id" if "contract_id" in _transfer_cols else "NULL"
        cursor.execute(
            """INSERT INTO nexus_transfer_intents_source_identity_v3
               (id, kind, source_txid, source_contract_id, from_address, to_address,
                amount_usdd_units, reference, status, remote_txid, contract_id,
                created_timestamp, last_attempt_timestamp, resolved_timestamp)
               SELECT id, kind, source_txid, -1, from_address, to_address,
                      amount_usdd_units, reference, 'legacy_manual_hold', remote_txid, """
            + remote_contract
            + """, created_timestamp, last_attempt_timestamp, resolved_timestamp
               FROM nexus_transfer_intents"""
        )
        cursor.execute("DROP TABLE nexus_transfer_intents")
        cursor.execute(
            "ALTER TABLE nexus_transfer_intents_source_identity_v3 "
            "RENAME TO nexus_transfer_intents"
        )
    cursor.execute(
        """UPDATE nexus_transfer_intents SET status = 'legacy_manual_hold'
           WHERE source_contract_id = -1 AND status != 'legacy_manual_hold'"""
    )
    # Replace the old txid-only index even if a partially upgraded database already
    # acquired the new column. Startup is serialized and this remains in one migration
    # transaction, so there is no interval in which a worker can insert without it.
    cursor.execute("DROP INDEX IF EXISTS idx_nexus_transfer_intents_source")
    try:
        cursor.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_nexus_transfer_intents_source
               ON nexus_transfer_intents(source_txid, source_contract_id)
               WHERE source_contract_id >= 0"""
        )
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise RuntimeError(
            "unsafe duplicate Nexus transfer intents share an exact source identity; "
            "resolve them manually before starting the service"
        ) from exc
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_nexus_transfer_intents_status "
        "ON nexus_transfer_intents(status, created_timestamp)"
    )

    cursor.execute("PRAGMA table_info(fee_entries)")
    _fee_cols = {row[1] for row in cursor.fetchall()}
    if "contract_id" not in _fee_cols:
        cursor.execute(
            "ALTER TABLE fee_entries ADD COLUMN contract_id INTEGER NOT NULL DEFAULT -1"
        )
    try:
        cursor.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_fee_entries_nexus_source
               ON fee_entries(txid, contract_id)
               WHERE txid IS NOT NULL AND contract_id >= 0
                     AND amount_usdd_units IS NOT NULL"""
        )
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise RuntimeError(
            "conflicting Nexus fee evidence shares an exact source identity; "
            "resolve it manually before starting the service"
        ) from exc

    # Completed Solana->Nexus mints must retain all evidence required for later
    # reconciliation.  The queue row is deliberately removed after confirmation, so
    # recovering its memo or destination by joining back to unprocessed_sigs makes a
    # missing-evidence reconciliation look healthy.
    cursor.execute("PRAGMA table_info(processed_sigs)")
    _psig_cols = {row[1] for row in cursor.fetchall()}
    for _column, _definition in (
        ("amount_usdd_units", "INTEGER"),
        ("nexus_destination", "TEXT"),
        ("memo", "TEXT"),
        ("contract_id", "INTEGER"),
    ):
        if _column not in _psig_cols:
            cursor.execute(f"ALTER TABLE processed_sigs ADD COLUMN {_column} {_definition}")

    # The legacy REAL token amount cannot safely participate in reconciliation.
    # Current processed Nexus credits record their source amount in base units; older
    # rows remain explicitly incomplete rather than being silently rounded.
    cursor.execute("PRAGMA table_info(processed_txids)")
    _ptx_cols = {row[1] for row in cursor.fetchall()}
    if "amount_usdd_units" not in _ptx_cols:
        cursor.execute("ALTER TABLE processed_txids ADD COLUMN amount_usdd_units INTEGER")
    if "payout_solana_units" not in _ptx_cols:
        cursor.execute("ALTER TABLE processed_txids ADD COLUMN payout_solana_units INTEGER")
    if "payout_fee_nexus_units" not in _ptx_cols:
        cursor.execute("ALTER TABLE processed_txids ADD COLUMN payout_fee_nexus_units INTEGER")
    if "payout_receival_account" not in _ptx_cols:
        cursor.execute("ALTER TABLE processed_txids ADD COLUMN payout_receival_account TEXT")

    # SQLite cannot add a composite primary key in place.  Legacy rows have no
    # authoritative contract id, so preserve them under the explicit -1 sentinel
    # rather than inventing a chain identity.  New custody evidence always uses a
    # non-negative contract id, and cannot collide with those legacy rows.
    def _migrate_credit_identity(table: str, columns: tuple[str, ...], ddl: str) -> None:
        existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        if "contract_id" in existing:
            return
        legacy_columns = tuple(column for column in columns if column != "contract_id")
        temp = f"{table}_contract_identity_v2"
        cursor.execute(f"CREATE TABLE {temp} ({ddl})")
        cursor.execute(
            f"INSERT INTO {temp} ({', '.join(columns)}) "
            f"SELECT {legacy_columns[0]}, -1, {', '.join(legacy_columns[1:])} FROM {table}"
        )
        cursor.execute(f"DROP TABLE {table}")
        cursor.execute(f"ALTER TABLE {temp} RENAME TO {table}")

    _migrate_credit_identity(
        "unprocessed_txids",
        ("txid", "contract_id", "timestamp", "amount_usdd", "from_address", "to_address",
         "owner_from_address", "confirmations_credit", "status", "receival_account", "sig",
         "amount_usdd_units", "hold_reason", "payout_solana_units", "payout_fee_nexus_units"),
        "txid TEXT NOT NULL, contract_id INTEGER NOT NULL DEFAULT -1, timestamp INTEGER, "
        "amount_usdd REAL, from_address TEXT, to_address TEXT, owner_from_address TEXT, "
        "confirmations_credit INTEGER, status TEXT, receival_account TEXT, sig TEXT, "
        "amount_usdd_units INTEGER, hold_reason TEXT, payout_solana_units INTEGER, "
        "payout_fee_nexus_units INTEGER, PRIMARY KEY (txid, contract_id)",
    )
    _migrate_credit_identity(
        "processed_txids",
        ("txid", "contract_id", "timestamp", "amount_usdd", "amount_usdd_units",
         "from_address", "to_address", "owner", "sig", "status", "payout_solana_units",
         "payout_fee_nexus_units", "payout_receival_account"),
        "txid TEXT NOT NULL, contract_id INTEGER NOT NULL DEFAULT -1, timestamp INTEGER, "
        "amount_usdd REAL, amount_usdd_units INTEGER, from_address TEXT, to_address TEXT, "
        "owner TEXT, sig TEXT, status TEXT, payout_solana_units INTEGER, "
        "payout_fee_nexus_units INTEGER, payout_receival_account TEXT, "
        "PRIMARY KEY (txid, contract_id)",
    )
    _migrate_credit_identity(
        "refunded_txids",
        ("txid", "contract_id", "timestamp", "amount_usdd", "from_address", "to_address",
         "owner_from_address", "confirmations_credit", "status", "sig"),
        "txid TEXT NOT NULL, contract_id INTEGER NOT NULL DEFAULT -1, timestamp INTEGER, "
        "amount_usdd REAL, from_address TEXT, to_address TEXT, owner_from_address TEXT, "
        "confirmations_credit INTEGER, status TEXT, sig TEXT, PRIMARY KEY (txid, contract_id)",
    )
    _migrate_credit_identity(
        "quarantined_txids",
        ("txid", "contract_id", "timestamp", "amount_usdd", "from_address", "to_address",
         "owner", "sig", "status"),
        "txid TEXT NOT NULL, contract_id INTEGER NOT NULL DEFAULT -1, timestamp INTEGER, "
        "amount_usdd REAL, from_address TEXT, to_address TEXT, owner TEXT, sig TEXT, "
        "status TEXT, PRIMARY KEY (txid, contract_id)",
    )

    conn.commit()
    conn.close()


# Nexus transfer intents -------------------------------------------------------
#
# A Nexus CLI timeout/non-zero result is not proof the node did not accept a
# debit.  These rows capture all debit inputs before invocation and make the
# persisted reference the only identifier used for post-crash resolution.
_NEXUS_TRANSFER_COLUMNS = (
    "id", "kind", "source_txid", "source_contract_id", "from_address", "to_address",
    "amount_usdd_units", "reference", "status", "remote_txid", "contract_id",
    "created_timestamp", "last_attempt_timestamp", "resolved_timestamp",
)
_NEXUS_TRANSFER_AUDIT_COLUMNS = (
    "id", "intent_id", "action", "actor", "rationale", "evidence", "timestamp",
)


def _nexus_transfer_intent_id(source_txid: str, source_contract_id: int) -> str:
    """Stable identity for the one permissible Nexus transfer per source credit."""
    source = f"{source_txid}:{source_contract_id}"
    return "nexus-transfer-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def _nexus_transfer_reference(intent_id: str) -> str:
    # Reference fields are visible on-chain and may be length-limited. The durable
    # database row holds the expanded context; this compact value is unique/stable.
    return "bridge-xfer:" + intent_id.rsplit("-", 1)[-1]


def _nexus_transfer_intent_dict(row) -> dict | None:
    return dict(zip(_NEXUS_TRANSFER_COLUMNS, row)) if row else None


def _nexus_transfer_audit_dict(row) -> dict | None:
    return dict(zip(_NEXUS_TRANSFER_AUDIT_COLUMNS, row)) if row else None


def _require_operator_text(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Nexus transfer {field} is required")
    if len(text) > 500:
        raise ValueError(f"Nexus transfer {field} is too long")
    return text


def _nexus_transfer_audit_evidence(intent: dict, **extra: object) -> str:
    evidence = {
        "reference": str(intent["reference"]),
        "source_contract_id": int(intent["source_contract_id"]),
        "source_txid": str(intent["source_txid"]),
    }
    evidence.update(extra)
    return __import__("json").dumps(evidence, sort_keys=True, separators=(",", ":"))


def _audit_evidence_matches_intent(evidence: str | None, intent: dict) -> bool:
    try:
        decoded = __import__("json").loads(str(evidence or ""))
        return (
            decoded.get("reference") == str(intent["reference"])
            and decoded.get("source_txid") == str(intent["source_txid"])
            and type(decoded.get("source_contract_id")) is int
            and decoded["source_contract_id"] == int(intent["source_contract_id"])
        )
    except (TypeError, ValueError, AttributeError):
        return False


def _has_nexus_fee_evidence(conn, txid: str, contract_id: int) -> bool:
    """Treat legacy txid-only Nexus fees as a hold for every possible sibling."""
    return conn.execute(
        """SELECT 1 FROM fee_entries
           WHERE txid = ? AND amount_usdd_units IS NOT NULL
                 AND (contract_id = -1 OR contract_id = ?)
           LIMIT 1""",
        (txid, contract_id),
    ).fetchone() is not None


def _nexus_transfer_has_exact_held_source(conn, intent: dict) -> bool:
    """Verify the exact unspent source authorization inside the caller's transaction."""
    source_contract_id = intent.get("source_contract_id")
    if type(source_contract_id) is not int or source_contract_id < 0:
        return False
    if _has_nexus_fee_evidence(conn, str(intent["source_txid"]), source_contract_id):
        return False
    source = conn.execute(
        """SELECT amount_usdd_units, from_address, to_address, status, sig,
                  payout_solana_units, payout_fee_nexus_units
           FROM unprocessed_txids WHERE txid = ? AND contract_id = ?""",
        (intent["source_txid"], source_contract_id),
    ).fetchone()
    if source is None:
        return False
    units, sender, treasury, status, payout_sig, payout_units, payout_fee = source
    if (type(units) is not int
            or units != int(intent["amount_usdd_units"])
            or str(treasury or "") != str(intent["from_address"])
            or status != "refund held for operator review"
            or bool(payout_sig)
            or payout_units is not None
            or payout_fee is not None):
        return False
    if intent["kind"] == "refund" and str(sender or "") != str(intent["to_address"]):
        return False
    for table in ("processed_txids", "refunded_txids", "quarantined_txids"):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE txid = ? AND contract_id = ?",
            (intent["source_txid"], source_contract_id),
        ).fetchone():
            return False
    return True


def _record_nexus_transfer_audit_event(
    conn, *, intent_id: str, action: str, actor: str, rationale: str, evidence: str | None = None
) -> None:
    """Append immutable operator evidence; only an exact replay is idempotent."""
    existing = conn.execute(
        """SELECT actor, rationale, evidence FROM nexus_transfer_audit_events
           WHERE intent_id = ? AND action = ?""",
        (intent_id, action),
    ).fetchone()
    expected = (actor, rationale, evidence)
    if existing is not None:
        if existing != expected:
            raise ValueError("conflicting Nexus transfer audit evidence already exists")
        return
    conn.execute(
        """INSERT INTO nexus_transfer_audit_events
           (intent_id, action, actor, rationale, evidence, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (intent_id, action, actor, rationale, evidence, int(time.time())),
    )


def create_nexus_transfer_intent(
    *,
    kind: str,
    source_txid: str,
    source_contract_id: int,
    from_address: str,
    to_address: str,
    amount_usdd_units: int,
) -> dict:
    """Persist a deterministic Nexus transfer intent before any CLI invocation.

    Repeating the exact request returns the existing row. Reusing the same source
    with different transfer inputs is rejected: it would be a distinct remote debit
    without a distinct source authorization.
    """
    kind = str(kind or "").strip()
    source_txid = str(source_txid or "").strip()
    from_address = str(from_address or "").strip()
    to_address = str(to_address or "").strip()
    # This is the durable boundary before a Nexus account debit.  Integer base
    # units must already have been calculated upstream; coercing floats, Decimal
    # values, booleans or strings here could silently authorize a different debit.
    if type(amount_usdd_units) is not int or amount_usdd_units <= 0:
        raise ValueError("Nexus transfer intent requires exact positive integer units")
    units = amount_usdd_units
    if type(source_contract_id) is not int or source_contract_id < 0:
        raise ValueError("Nexus transfer intent requires a nonnegative source contract id")
    if not kind or not source_txid or not from_address or not to_address:
        raise ValueError("Nexus transfer intent requires kind, source and addresses")

    intent_id = _nexus_transfer_intent_id(source_txid, source_contract_id)
    reference = _nexus_transfer_reference(intent_id)
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            """SELECT 1 FROM nexus_transfer_intents
               WHERE source_txid = ? AND source_contract_id = -1 LIMIT 1""",
            (source_txid,),
        ).fetchone():
            raise ValueError(
                "legacy Nexus transfer intent blocks fresh debit for this source txid"
            )
        row = conn.execute(
            "SELECT " + ", ".join(_NEXUS_TRANSFER_COLUMNS) +
            " FROM nexus_transfer_intents WHERE source_txid = ? AND source_contract_id = ?",
            (source_txid, source_contract_id),
        ).fetchone()
        if row:
            existing = _nexus_transfer_intent_dict(row)
            if existing is None:  # pragma: no cover - row is truthy above
                raise RuntimeError("could not decode existing Nexus transfer intent")
            expected = (kind, from_address, to_address, units)
            observed = (existing["kind"], existing["from_address"], existing["to_address"],
                        int(existing["amount_usdd_units"]))
            if observed != expected:
                raise ValueError("existing Nexus transfer intent conflicts with requested inputs")
            conn.commit()
            return existing
        candidate = {
            "kind": kind,
            "source_txid": source_txid,
            "source_contract_id": source_contract_id,
            "from_address": from_address,
            "to_address": to_address,
            "amount_usdd_units": units,
        }
        if not _nexus_transfer_has_exact_held_source(conn, candidate):
            raise ValueError("Nexus transfer intent requires an exact held source credit")
        conn.execute(
            """INSERT INTO nexus_transfer_intents
               (id, kind, source_txid, source_contract_id, from_address, to_address, amount_usdd_units,
                reference, status, created_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)""",
            (intent_id, kind, source_txid, source_contract_id, from_address, to_address,
             units, reference, now),
        )
        conn.commit()
        created = get_nexus_transfer_intent(intent_id, conn=conn)
        if created is None:  # pragma: no cover - same transaction inserted the row
            raise RuntimeError("could not read newly created Nexus transfer intent")
        return created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_nexus_transfer_intent(intent_id: str, *, conn=None) -> dict | None:
    """Return the complete durable transfer record, if it exists."""
    owns_connection = conn is None
    conn = conn or sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT " + ", ".join(_NEXUS_TRANSFER_COLUMNS) +
            " FROM nexus_transfer_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        return _nexus_transfer_intent_dict(row)
    finally:
        if owns_connection:
            conn.close()


def claim_nexus_transfer_intent(intent_id: str) -> dict | None:
    """Atomically claim one authorized intent for its sole allowed remote invocation."""
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        intent = get_nexus_transfer_intent(intent_id, conn=conn)
        if intent is None or int(intent["source_contract_id"]) < 0:
            conn.commit()
            return None
        if not _nexus_transfer_has_exact_held_source(conn, intent):
            conn.commit()
            return None
        requested = conn.execute(
            """SELECT evidence FROM nexus_transfer_audit_events
               WHERE intent_id = ? AND action = 'execution_requested'""",
            (intent_id,),
        ).fetchone()
        if requested is None or not _audit_evidence_matches_intent(requested[0], intent):
            conn.commit()
            return None
        updated = conn.execute(
            """UPDATE nexus_transfer_intents
               SET status = 'executing', last_attempt_timestamp = ?
               WHERE id = ? AND status = 'authorized' AND source_contract_id >= 0""",
            (now, intent_id),
        ).rowcount
        if not updated:
            conn.commit()
            return None
        row = get_nexus_transfer_intent(intent_id, conn=conn)
        if row is None:  # pragma: no cover - UPDATE succeeded for an existing row
            raise RuntimeError("could not read claimed Nexus transfer intent")
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_interrupted_nexus_transfer_intents() -> int:
    """Turn pre-completion execution claims into explicit restart holds.

    This is called only during startup, after the singleton lock is held. An ``executing``
    row proves that the sole permitted CLI attempt may already have reached the node, but
    the process died before recording a parsed result. It must therefore become
    ``outcome_unknown`` and require positive chain-reference resolution, never another debit.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE nexus_transfer_intents SET status = 'legacy_manual_hold'
               WHERE source_contract_id = -1 AND status != 'legacy_manual_hold'"""
        )
        recovered = conn.execute(
            """UPDATE nexus_transfer_intents
               SET status = 'outcome_unknown'
               WHERE status = 'executing' AND source_contract_id >= 0"""
        ).rowcount
        conn.commit()
        return int(recovered)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def authorize_nexus_transfer_intent(
    intent_id: str, *, actor: str, rationale: str, expected_reference: str
) -> dict:
    """Authorize exactly one prepared transfer after an operator confirms its reference."""
    actor = _require_operator_text(actor, "operator")
    rationale = _require_operator_text(rationale, "authorization rationale")
    expected_reference = _require_operator_text(expected_reference, "reference confirmation")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        intent = get_nexus_transfer_intent(intent_id, conn=conn)
        if intent is None:
            raise ValueError("Nexus transfer intent does not exist")
        if int(intent["source_contract_id"]) < 0:
            raise ValueError("legacy Nexus transfer intent cannot be authorized")
        if expected_reference != str(intent["reference"]):
            raise ValueError("Nexus transfer reference confirmation does not match")
        if intent["status"] != "prepared":
            raise ValueError("only a prepared Nexus transfer intent may be authorized")
        if not _nexus_transfer_has_exact_held_source(conn, intent):
            raise ValueError("Nexus transfer authorization requires exact held source evidence")
        preparation = conn.execute(
            """SELECT evidence FROM nexus_transfer_audit_events
               WHERE intent_id = ? AND action = ?""",
            (intent_id, f"prepared_{intent['kind']}"),
        ).fetchone()
        if preparation is None or not _audit_evidence_matches_intent(preparation[0], intent):
            raise ValueError("Nexus transfer requires an audited preparation before authorization")
        conn.execute("UPDATE nexus_transfer_intents SET status = 'authorized' WHERE id = ?", (intent_id,))
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action="authorized_execution", actor=actor,
            rationale=rationale, evidence=_nexus_transfer_audit_evidence(intent),
        )
        conn.commit()
        authorized = get_nexus_transfer_intent(intent_id, conn=conn)
        if authorized is None:  # pragma: no cover - same transaction updated the row
            raise RuntimeError("could not read authorized Nexus transfer intent")
        return authorized
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_nexus_transfer_preparation(
    intent_id: str, *, actor: str, rationale: str
) -> None:
    """Attribute the operator decision that prepared a transfer from a held credit."""
    actor = _require_operator_text(actor, "operator")
    rationale = _require_operator_text(rationale, "preparation rationale")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        intent = get_nexus_transfer_intent(intent_id, conn=conn)
        if intent is not None and int(intent["source_contract_id"]) < 0:
            raise ValueError("legacy Nexus transfer intent cannot be prepared")
        if intent is None or intent["status"] != "prepared":
            raise ValueError("only a prepared Nexus transfer intent may be attributed")
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action=f"prepared_{intent['kind']}", actor=actor,
            rationale=rationale, evidence=_nexus_transfer_audit_evidence(intent),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_nexus_transfer_execution_request(
    intent_id: str, *, actor: str, rationale: str
) -> None:
    """Durably attribute the manual command that will consume an authorization."""
    actor = _require_operator_text(actor, "operator")
    rationale = _require_operator_text(rationale, "execution rationale")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        intent = get_nexus_transfer_intent(intent_id, conn=conn)
        if intent is not None and int(intent["source_contract_id"]) < 0:
            raise ValueError("legacy Nexus transfer intent cannot be executed")
        if intent is None or intent["status"] != "authorized":
            raise ValueError("only an authorized Nexus transfer intent may be executed")
        if not _nexus_transfer_has_exact_held_source(conn, intent):
            raise ValueError("Nexus transfer execution requires exact held source evidence")
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action="execution_requested", actor=actor,
            rationale=rationale, evidence=_nexus_transfer_audit_evidence(intent),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_nexus_transfer_audit_events(intent_id: str) -> list[dict]:
    """Return immutable authorization and disposition events in append order."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT " + ", ".join(_NEXUS_TRANSFER_AUDIT_COLUMNS) +
            " FROM nexus_transfer_audit_events WHERE intent_id = ? ORDER BY id ASC",
            (intent_id,),
        ).fetchall()
        return [event for row in rows if (event := _nexus_transfer_audit_dict(row)) is not None]
    finally:
        conn.close()


def finalize_nexus_transfer_disposition(
    intent_id: str, *, actor: str, rationale: str, expected_remote_txid: str
) -> bool:
    """Move only the exact held source after its completed transfer has chain evidence."""
    actor = _require_operator_text(actor, "operator")
    rationale = _require_operator_text(rationale, "disposition rationale")
    expected_remote_txid = _require_operator_text(expected_remote_txid, "remote txid confirmation")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        intent = get_nexus_transfer_intent(intent_id, conn=conn)
        if (intent is None or int(intent["source_contract_id"]) < 0
                or intent["status"] != "completed"
                or str(intent.get("remote_txid") or "") != expected_remote_txid
                or type(intent.get("contract_id")) is not int
                or int(intent["contract_id"]) < 0):
            conn.commit()
            return False
        if not _nexus_transfer_has_exact_held_source(conn, intent):
            conn.commit()
            return False
        source_contract_id = int(intent["source_contract_id"])
        source = conn.execute(
            """SELECT txid, contract_id, timestamp, amount_usdd, from_address, to_address,
                      owner_from_address, confirmations_credit, status, amount_usdd_units
               FROM unprocessed_txids WHERE txid = ? AND contract_id = ?""",
            (intent["source_txid"], source_contract_id),
        ).fetchone()
        if source is None:
            conn.commit()
            return False
        (txid, stored_contract_id, timestamp, amount, sender, treasury, owner,
         confirmations, source_status, units) = source
        if (stored_contract_id != source_contract_id
                or source_status != "refund held for operator review"
                or type(units) is not int
                or units != int(intent["amount_usdd_units"])
                or str(treasury or "") != str(intent["from_address"])):
            conn.commit()
            return False

        terminal_tables = ("refunded_txids", "quarantined_txids", "processed_txids")
        for table in terminal_tables:
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE txid = ? AND contract_id = ?",
                (txid, source_contract_id),
            ).fetchone():
                conn.commit()
                return False

        if intent["kind"] == "refund":
            if str(sender or "") != str(intent["to_address"]):
                conn.commit()
                return False
            conn.execute(
                """INSERT INTO refunded_txids
                   (txid, contract_id, timestamp, amount_usdd, from_address, to_address,
                    owner_from_address, confirmations_credit, status, sig)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (txid, source_contract_id, timestamp, amount, sender, treasury, owner,
                 confirmations, "refund_confirmed_by_operator", expected_remote_txid),
            )
            action = "finalized_refund"
        elif intent["kind"] == "quarantine":
            conn.execute(
                """INSERT INTO quarantined_txids
                   (txid, contract_id, timestamp, amount_usdd, from_address, to_address,
                    owner, sig, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (txid, source_contract_id, timestamp, amount, sender, treasury, owner,
                 expected_remote_txid, "quarantine_confirmed_by_operator"),
            )
            action = "finalized_quarantine"
        else:
            conn.commit()
            return False
        deleted = conn.execute(
            "DELETE FROM unprocessed_txids WHERE txid = ? AND contract_id = ?",
            (txid, source_contract_id),
        ).rowcount
        if deleted != 1:
            raise RuntimeError("exact held Nexus source changed during finalization")
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action=action, actor=actor, rationale=rationale,
            evidence=_nexus_transfer_audit_evidence(
                intent,
                remote_txid=expected_remote_txid,
                remote_contract_id=int(intent["contract_id"]),
                disposition=intent["kind"],
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_nexus_transfer_intent(
    intent_id: str,
    *,
    status: str,
    remote_txid: str | None = None,
    contract_id: int | None = None,
    resolved: bool = False,
) -> None:
    """Advance one execution intent without allowing ambiguous state regression.

    The row is a durable record of the sole permitted Nexus debit.  In particular,
    completed chain evidence must never be replaced by ``outcome_unknown`` by a
    delayed recovery path; that would hide proof needed for operator disposition.
    """
    transitions = {
        "executing": {"submitted", "outcome_unknown", "completed"},
        "submitted": {"completed"},
        "outcome_unknown": {"completed"},
    }
    status = str(status or "").strip()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        intent = get_nexus_transfer_intent(intent_id, conn=conn)
        if intent is None:
            raise ValueError("Nexus transfer intent does not exist")
        if int(intent["source_contract_id"]) < 0:
            raise ValueError("legacy Nexus transfer intent cannot be advanced")
        current_status = str(intent["status"])
        if status not in transitions.get(current_status, set()):
            raise ValueError(
                f"cannot transition Nexus transfer intent from {current_status!r} to {status!r}"
            )
        supplied_remote_txid = str(remote_txid or "").strip()
        persisted_remote_txid = str(intent.get("remote_txid") or "").strip()
        # Once the Nexus node returns a txid, it is part of the immutable identity of
        # the sole permitted debit.  A later resolver must prove the same txid; it
        # must not be able to replace it with another transaction that happens to
        # share a reference or is passed by an incorrect caller.
        if (persisted_remote_txid and supplied_remote_txid
                and supplied_remote_txid != persisted_remote_txid):
            raise ValueError("persisted Nexus remote txid is immutable")
        next_remote_txid = supplied_remote_txid or persisted_remote_txid
        if (contract_id is not None
                and (type(contract_id) is not int or contract_id < 0)):
            raise ValueError("Nexus transfer contract id must be a nonnegative integer")
        persisted_contract_id = intent.get("contract_id")
        if (persisted_contract_id is not None and contract_id is not None
                and int(contract_id) != int(persisted_contract_id)):
            raise ValueError("persisted Nexus contract id is immutable")
        next_contract_id = contract_id if contract_id is not None else persisted_contract_id
        if status in {"submitted", "completed"} and not next_remote_txid:
            raise ValueError("submitted or completed Nexus transfer intent requires a remote txid")
        if status == "completed" and next_contract_id is None:
            raise ValueError("completed Nexus transfer intent requires a contract id")
        if resolved and status != "completed":
            raise ValueError("only a completed Nexus transfer intent may be marked resolved")
        now = int(time.time())
        conn.execute(
            """UPDATE nexus_transfer_intents
               SET status = ?, remote_txid = COALESCE(?, remote_txid),
                   contract_id = COALESCE(?, contract_id),
                   resolved_timestamp = CASE WHEN ? THEN ? ELSE resolved_timestamp END
               WHERE id = ?""",
            (status, supplied_remote_txid or None, contract_id, 1 if resolved else 0, now, intent_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_nexus_transfer_intents_by_status(statuses: tuple[str, ...], limit: int = 200) -> list[dict]:
    """List nonterminal intents for positive on-chain reference resolution."""
    if not statuses:
        return []
    marks = ",".join("?" for _ in statuses)
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT " + ", ".join(_NEXUS_TRANSFER_COLUMNS) +
            f" FROM nexus_transfer_intents WHERE status IN ({marks}) "
            "AND source_contract_id >= 0 "
            "ORDER BY created_timestamp ASC LIMIT ?",
            tuple(statuses) + (int(limit),),
        ).fetchall()
        return [intent for row in rows
                if (intent := _nexus_transfer_intent_dict(row)) is not None]
    finally:
        conn.close()


## Durable Solana deposit scan cursor

_SOLANA_DEPOSIT_LIFECYCLE_TABLES = (
    "unprocessed_sigs", "processed_sigs", "refunded_sigs", "quarantined_sigs",
)


def _solana_deposit_lifecycle_locations(conn, signature: str) -> int:
    return sum(
        conn.execute(f"SELECT 1 FROM {table} WHERE sig = ?", (signature,)).fetchone() is not None
        for table in _SOLANA_DEPOSIT_LIFECYCLE_TABLES
    )


def _insert_solana_deposit(conn, deposit: tuple) -> bool:
    if not isinstance(deposit, tuple) or len(deposit) != 5:
        raise ValueError("invalid Solana deposit evidence")
    sig, timestamp, memo, from_address, amount_units = deposit
    if (not isinstance(sig, str) or not sig or type(timestamp) is not int or timestamp <= 0
            or not isinstance(memo, (str, type(None)))
            or not isinstance(from_address, (str, type(None)))
            or type(amount_units) is not int or amount_units <= 0):
        raise ValueError("invalid Solana deposit evidence")
    locations = _solana_deposit_lifecycle_locations(conn, sig)
    if locations > 1:
        raise ValueError("conflicting existing Solana deposit lifecycle evidence")
    if locations:
        return False
    held = conn.execute(
        """SELECT block_timestamp, memo, from_address, amount_units
             FROM solana_deposit_holds WHERE signature = ?""",
        (sig,),
    ).fetchone()
    if held is not None and held != (timestamp, memo, from_address, amount_units):
        raise ValueError("deposit conflicts with durable Solana hold evidence")
    conn.execute(
        """INSERT INTO unprocessed_sigs
           (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
           VALUES (?, ?, ?, ?, ?, 'ready for processing', NULL)""",
        (sig, timestamp, memo or "", from_address, amount_units),
    )
    if held is not None:
        conn.execute("DELETE FROM solana_deposit_holds WHERE signature = ?", (sig,))
    return True


def _insert_solana_deposit_hold(conn, hold: tuple) -> bool:
    if not isinstance(hold, tuple) or len(hold) not in {9, 14}:
        raise ValueError("invalid Solana deposit hold evidence")
    (signature, block_timestamp, memo, from_address, amount_units, reason,
     evidence_json, provider, query_identity) = hold[:9]
    if len(hold) == 14:
        network, vault_account, mint, observed_commitment, finality_required = hold[9:]
    else:
        network = vault_account = mint = observed_commitment = finality_required = None
    if (not isinstance(signature, str) or not signature
            or type(block_timestamp) is not int or block_timestamp <= 0
            or not isinstance(memo, (str, type(None)))
            or not isinstance(from_address, (str, type(None)))
            or (amount_units is not None and (type(amount_units) is not int or amount_units <= 0))
            or not isinstance(reason, str) or not reason
            or not isinstance(evidence_json, str) or not evidence_json
            or provider not in {"helius", "core"}
            or not isinstance(query_identity, str) or not query_identity
            or (network is not None and (not isinstance(network, str) or not network))
            or (vault_account is not None and (not isinstance(vault_account, str) or not vault_account))
            or (mint is not None and (not isinstance(mint, str) or not mint))
            or observed_commitment not in {None, "confirmed", "finalized"}
            or finality_required not in {None, 0, 1}):
        raise ValueError("invalid Solana deposit hold evidence")
    locations = _solana_deposit_lifecycle_locations(conn, signature)
    if locations > 1:
        raise ValueError("conflicting existing Solana deposit lifecycle evidence")
    if locations:
        return False
    existing = conn.execute(
        """SELECT block_timestamp, memo, from_address, amount_units, reason,
                  evidence_json, provider, network, vault_account, mint,
                  observed_commitment, finality_required
             FROM solana_deposit_holds WHERE signature = ?""",
        (signature,),
    ).fetchone()
    exact = (block_timestamp, memo, from_address, amount_units, reason,
             evidence_json, provider, network, vault_account, mint,
             observed_commitment, finality_required)
    now = int(time.time())
    if existing is None:
        conn.execute(
            """INSERT INTO solana_deposit_holds
               (signature, block_timestamp, memo, from_address, amount_units, reason,
                evidence_json, provider, query_identity, first_seen_timestamp,
                updated_timestamp, network, vault_account, mint, observed_commitment,
                finality_required)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signature, block_timestamp, memo, from_address, amount_units, reason,
             evidence_json, provider, query_identity, now, now, network, vault_account,
             mint, observed_commitment, finality_required),
        )
        return True
    if existing != exact:
        raise ValueError("conflicting Solana deposit hold evidence")
    conn.execute(
        "UPDATE solana_deposit_holds SET updated_timestamp = ? WHERE signature = ?",
        (now, signature),
    )
    return False


def get_helius_deposit_scan_cursor(vault_account: str) -> dict | None:
    """Return an incomplete Helius range, including its provider-issued token."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """SELECT vault_account, network, mint, commitment, lower_timestamp,
                      upper_timestamp, pagination_token, previous_timestamp,
                      query_identity, started_timestamp
                 FROM helius_deposit_scan_cursor WHERE vault_account = ?""",
            (vault_account,),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "vault_account", "network", "mint", "commitment", "lower_timestamp",
            "upper_timestamp", "pagination_token", "previous_timestamp",
            "query_identity", "started_timestamp",
        )
        return dict(zip(keys, row))
    finally:
        conn.close()


def commit_helius_deposit_scan_page(
    *, vault_account: str, network: str, mint: str, commitment: str,
    lower_timestamp: int, upper_timestamp: int, query_identity: str,
    request_pagination_token: str | None, next_pagination_token: str | None,
    previous_timestamp: int | None, page_last_timestamp: int | None,
    scanned_signatures: list[tuple[str, int]], deposits: list[tuple], holds: list[tuple],
    complete: bool,
) -> tuple[int, int]:
    """Atomically admit one bounded Helius page and advance only its exact token."""
    if (not all(isinstance(value, str) and value for value in
                (vault_account, network, mint, commitment, query_identity))
            or type(lower_timestamp) is not int or lower_timestamp < 0
            or type(upper_timestamp) is not int or upper_timestamp <= lower_timestamp
            or (request_pagination_token is not None
                and (not isinstance(request_pagination_token, str) or not request_pagination_token))
            or (next_pagination_token is not None
                and (not isinstance(next_pagination_token, str) or not next_pagination_token))
            or complete == (next_pagination_token is not None)):
        raise ValueError("invalid Helius deposit scan page")
    if previous_timestamp is not None and (
            type(previous_timestamp) is not int or previous_timestamp <= 0):
        raise ValueError("invalid Helius previous page timestamp")
    if page_last_timestamp is not None and (
            type(page_last_timestamp) is not int or page_last_timestamp <= 0):
        raise ValueError("invalid Helius page timestamp")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT network, mint, commitment, lower_timestamp, upper_timestamp,
                      pagination_token, previous_timestamp, query_identity
                 FROM helius_deposit_scan_cursor WHERE vault_account = ?""",
            (vault_account,),
        ).fetchone()
        identity = (network, mint, commitment, lower_timestamp, upper_timestamp,
                    request_pagination_token, previous_timestamp, query_identity)
        if existing is None:
            if request_pagination_token is not None or previous_timestamp is not None:
                raise ValueError("Helius cursor disappeared before page commit")
            conn.execute(
                """INSERT INTO helius_deposit_scan_cursor
                   (vault_account, network, mint, commitment, lower_timestamp,
                    upper_timestamp, pagination_token, previous_timestamp,
                    query_identity, started_timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                (vault_account, network, mint, commitment, lower_timestamp,
                 upper_timestamp, query_identity, int(time.time())),
            )
        elif existing != identity:
            raise ValueError("Helius scan cursor/query conflict")

        for signature, timestamp in scanned_signatures:
            if (not isinstance(signature, str) or not signature
                    or type(timestamp) is not int or timestamp <= 0):
                raise ValueError("invalid Helius scanned signature evidence")
            try:
                conn.execute(
                    """INSERT INTO solana_deposit_scan_seen
                       (query_identity, signature, block_timestamp) VALUES (?, ?, ?)""",
                    (query_identity, signature, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate Helius signature inside bounded query") from exc

        admitted = sum(_insert_solana_deposit(conn, deposit) for deposit in deposits)
        held = sum(_insert_solana_deposit_hold(conn, hold) for hold in holds)
        event = "range_completed" if complete else "page_committed"
        conn.execute(
            """INSERT INTO helius_deposit_scan_events
               (query_identity, network, vault_account, mint, commitment,
                lower_timestamp, upper_timestamp, request_pagination_token,
                next_pagination_token, signature_count, admitted_count, held_count,
                event, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (query_identity, network, vault_account, mint, commitment,
             lower_timestamp, upper_timestamp, request_pagination_token,
             next_pagination_token, len(scanned_signatures), admitted, held,
             event, int(time.time())),
        )
        if complete:
            conn.execute(
                "DELETE FROM solana_deposit_scan_seen WHERE query_identity = ?",
                (query_identity,),
            )
            conn.execute(
                "DELETE FROM helius_deposit_scan_cursor WHERE vault_account = ?",
                (vault_account,),
            )
        else:
            conn.execute(
                """UPDATE helius_deposit_scan_cursor
                      SET pagination_token = ?, previous_timestamp = ?
                    WHERE vault_account = ?""",
                (next_pagination_token, page_last_timestamp, vault_account),
            )
        conn.commit()
        return admitted, held
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_solana_deposit_holds(limit: int = 1000) -> list[dict]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("Solana deposit hold limit must be positive")
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """SELECT signature, block_timestamp, memo, from_address, amount_units,
                      reason, evidence_json, provider, query_identity,
                      first_seen_timestamp, updated_timestamp, network, vault_account,
                      mint, observed_commitment, finality_required, replay_attempts,
                      last_replay_timestamp
                 FROM solana_deposit_holds
                ORDER BY replay_attempts ASC,
                         CASE WHEN reason = 'awaiting_finalized' THEN 0 ELSE 1 END ASC,
                         last_replay_timestamp ASC, block_timestamp ASC, signature ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        keys = (
            "signature", "block_timestamp", "memo", "from_address", "amount_units",
            "reason", "evidence_json", "provider", "query_identity",
            "first_seen_timestamp", "updated_timestamp", "network", "vault_account",
            "mint", "observed_commitment", "finality_required", "replay_attempts",
            "last_replay_timestamp",
        )
        return [dict(zip(keys, row)) for row in rows]
    finally:
        conn.close()


def record_solana_deposit_hold_replay_attempt(signature: str) -> None:
    """Durably rotate attempted holds behind never-attempted rows."""
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    try:
        changed = conn.execute(
            """UPDATE solana_deposit_holds
                  SET replay_attempts = replay_attempts + 1,
                      last_replay_timestamp = ?, updated_timestamp = ?
                WHERE signature = ?""",
            (now, now, signature),
        ).rowcount
        if changed != 1:
            raise ValueError("Solana deposit hold disappeared during replay")
        conn.commit()
    finally:
        conn.close()


def get_completed_helius_query_provenance(query_identity: str) -> dict | None:
    """Recover legacy provenance only from a unique completed-query audit row."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """SELECT DISTINCT network, vault_account, mint, commitment
                 FROM helius_deposit_scan_events
                WHERE query_identity = ? AND event = 'range_completed'""",
            (query_identity,),
        ).fetchall()
        if len(rows) != 1:
            return None
        return dict(zip(("network", "vault_account", "mint", "observed_commitment"), rows[0]))
    finally:
        conn.close()


def get_earliest_solana_deposit_hold_timestamp() -> int | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT MIN(block_timestamp) FROM solana_deposit_holds").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def discard_preupgrade_solana_deposit_scan_cursor(
    vault_account: str, lower_timestamp: int,
) -> bool:
    """Discard only an identity-less legacy cursor so history is re-enumerated safely."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT lower_timestamp, network, commitment, query_identity
                 FROM solana_deposit_scan_cursor WHERE vault_account = ?""",
            (vault_account,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        if row[0] != lower_timestamp or any(value is not None for value in row[1:]):
            conn.rollback()
            return False
        deleted = conn.execute(
            """DELETE FROM solana_deposit_scan_cursor
                WHERE vault_account = ? AND lower_timestamp = ?
                  AND network IS NULL AND commitment IS NULL AND query_identity IS NULL""",
            (vault_account, lower_timestamp),
        ).rowcount
        conn.commit()
        return deleted == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def promote_solana_deposit_hold(
    signature: str, *, memo: str | None = None, from_address: str | None = None,
    amount_units: int | None = None,
) -> bool:
    """Atomically move one revalidated held candidate into the ordinary queue."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT block_timestamp, memo, from_address, amount_units
                 FROM solana_deposit_holds WHERE signature = ?""",
            (signature,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        deposit = (
            signature,
            row[0],
            row[1] if memo is None else memo,
            row[2] if from_address is None else from_address,
            row[3] if amount_units is None else amount_units,
        )
        conn.execute("DELETE FROM solana_deposit_holds WHERE signature = ?", (signature,))
        inserted = _insert_solana_deposit(conn, deposit)
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_solana_deposit_scan_cursor(vault_account: str) -> dict | None:
    """Return the incomplete exact-cursor range for one configured vault."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """SELECT vault_account, mint, lower_timestamp, before_signature,
                      upper_timestamp, started_timestamp, network, commitment, query_identity,
                      previous_timestamp
                 FROM solana_deposit_scan_cursor WHERE vault_account = ?""",
            (vault_account,),
        ).fetchone()
        if row is None:
            return None
        return {
            "vault_account": row[0], "mint": row[1], "lower_timestamp": row[2],
            "before_signature": row[3], "upper_timestamp": row[4],
            "started_timestamp": row[5], "network": row[6], "commitment": row[7],
            "query_identity": row[8], "previous_timestamp": row[9],
        }
    finally:
        conn.close()


def commit_solana_deposit_scan_page(
    *, vault_account: str, mint: str, network: str, commitment: str, query_identity: str,
    lower_timestamp: int, request_before_signature: str | None,
    next_before_signature: str | None, upper_timestamp: int,
    previous_timestamp: int | None, page_last_timestamp: int | None,
    scanned_signature_count: int,
    deposits: list[tuple[str, int, str | None, str | None, int]], complete: bool,
    holds: list[tuple] | None = None,
) -> int:
    """Atomically persist one validated history page and its exact resume cursor.

    The page's deposits enter the normal durable queue before the cursor can move.
    Existing lifecycle rows are retained rather than overwritten on range overlap.
    """
    if (not isinstance(vault_account, str) or not vault_account
            or not isinstance(mint, str) or not mint
            or not isinstance(network, str) or not network
            or not isinstance(commitment, str) or not commitment
            or not isinstance(query_identity, str) or not query_identity
            or type(lower_timestamp) is not int or lower_timestamp < 0
            or type(upper_timestamp) is not int or upper_timestamp <= 0
            or type(scanned_signature_count) is not int or scanned_signature_count < 0):
        raise ValueError("invalid Solana deposit scan page")
    if complete and next_before_signature is not None:
        raise ValueError("completed Solana scan cannot retain a pagination cursor")
    if not complete and (not isinstance(next_before_signature, str) or not next_before_signature):
        raise ValueError("incomplete Solana scan requires an exact cursor")
    if previous_timestamp is not None and (
            type(previous_timestamp) is not int or previous_timestamp <= 0):
        raise ValueError("invalid Solana previous page timestamp")
    if page_last_timestamp is not None and (
            type(page_last_timestamp) is not int or page_last_timestamp <= 0):
        raise ValueError("invalid Solana page timestamp")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT mint, lower_timestamp, before_signature, upper_timestamp,
                      network, commitment, query_identity, previous_timestamp
                 FROM solana_deposit_scan_cursor WHERE vault_account = ?""",
            (vault_account,),
        ).fetchone()
        if existing is None:
            if request_before_signature is not None:
                raise ValueError("Solana scan cursor disappeared before page commit")
            conn.execute(
                """INSERT INTO solana_deposit_scan_cursor
                   (vault_account, mint, lower_timestamp, before_signature, upper_timestamp,
                    started_timestamp, network, commitment, query_identity, previous_timestamp)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL)""",
                (vault_account, mint, lower_timestamp, upper_timestamp, int(time.time()),
                 network, commitment, query_identity),
            )
        else:
            if (existing[0] != mint or existing[1] != lower_timestamp
                    or existing[2] != request_before_signature
                    or existing[4] != network or existing[5] != commitment
                    or existing[6] != query_identity or existing[7] != previous_timestamp):
                raise ValueError("Solana scan cursor/configuration conflict")
            # A later page must not claim a newer range head.
            if existing[3] != upper_timestamp:
                raise ValueError("Solana scan upper timestamp conflict")

        admitted = sum(_insert_solana_deposit(conn, deposit) for deposit in deposits)
        for hold in holds or ():
            _insert_solana_deposit_hold(conn, hold)

        event = "range_completed" if complete else "page_committed"
        conn.execute(
            """INSERT INTO solana_deposit_scan_events
               (vault_account, mint, lower_timestamp, upper_timestamp, request_before_signature,
                next_before_signature, signature_count, admitted_count, event, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vault_account, mint, lower_timestamp, upper_timestamp, request_before_signature,
             next_before_signature, scanned_signature_count, admitted, event, int(time.time())),
        )
        if complete:
            conn.execute("DELETE FROM solana_deposit_scan_cursor WHERE vault_account = ?", (vault_account,))
        else:
            conn.execute(
                """UPDATE solana_deposit_scan_cursor
                      SET before_signature = ?, previous_timestamp = ?
                    WHERE vault_account = ?""",
                (next_before_signature, page_last_timestamp, vault_account),
            )
        conn.commit()
        return admitted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


## Unprocessed Signatures

def is_unprocessed_sig(sig: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM unprocessed_sigs WHERE sig = ?", (sig,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_unprocessed_sig(sig: str, timestamp: int, memo: str, from_address: str, amount_usdc_units: float, status: str | None = None, txid: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO unprocessed_sigs (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (sig, timestamp, memo, from_address, amount_usdc_units, status, txid))
    conn.commit()
    conn.close()

def get_unprocessed_sigs() -> List[Tuple[str, int, str, str, float, str | None, str | None]]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT sig, timestamp, memo, from_address, amount_usdc_units, status, txid FROM unprocessed_sigs ORDER BY timestamp ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_unresolved_solana_liability_units() -> int:
    """Gross quantified Solana units whose financial lifecycle is not terminal.

    Positive durable holds are liabilities just like queued deposits. An unquantified
    legacy hold disables surplus/backing authorization rather than contributing zero.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        # Hold promotion moves principal between tables atomically. All liability reads
        # must observe one snapshot rather than omit it between the two sums.
        conn.execute("BEGIN")
        unknown = conn.execute(
            "SELECT COUNT(*) FROM solana_deposit_holds WHERE amount_units IS NULL OR amount_units <= 0"
        ).fetchone()[0]
        if unknown:
            raise RuntimeError("unquantified Solana deposit hold makes liability unhealthy")
        overlap = conn.execute(
            """SELECT COUNT(*) FROM solana_deposit_holds h
               WHERE EXISTS (SELECT 1 FROM unprocessed_sigs u WHERE u.sig = h.signature)
                  OR EXISTS (SELECT 1 FROM processed_sigs p WHERE p.sig = h.signature)
                  OR EXISTS (SELECT 1 FROM refunded_sigs r WHERE r.sig = h.signature)
                  OR EXISTS (SELECT 1 FROM quarantined_sigs q WHERE q.sig = h.signature)"""
        ).fetchone()[0]
        if overlap:
            raise RuntimeError("Solana deposit hold overlaps lifecycle evidence")
        pending = conn.execute(
            "SELECT COALESCE(SUM(amount_usdc_units), 0) FROM unprocessed_sigs"
        ).fetchone()[0]
        held = conn.execute(
            "SELECT COALESCE(SUM(amount_units), 0) FROM solana_deposit_holds"
        ).fetchone()[0]
        return max(0, int(pending or 0) + int(held or 0))
    finally:
        conn.close()

def get_unprocessed_sig_status(sig: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM unprocessed_sigs WHERE sig = ?", (sig,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def filter_unprocessed_sigs(filters: dict) -> List[Tuple[str, int, str, str, float, str | None, str | None]]:
    """
    Fetch unprocessed sigs filtered by multiple attributes.
    
    Args:
        filters: Dict of filter criteria. Supported keys:
            - 'status': Exact match (str)
            - 'status_like': Partial match with LIKE (str, e.g., '%refund%')
            - 'amount_usdc_units_gt': Amount greater than (float)
            - 'amount_usdc_units_lt': Amount less than (float)
            - 'timestamp_gt': Timestamp greater than (int)
            - 'timestamp_lt': Timestamp less than (int)
            - 'memo_like': Memo partial match (str)
            - 'from_address': Exact from_address match (str)
            - 'txid': Exact txid match (str)
            - 'limit': Max rows to return (int, default 1000)
    
    Returns:
        List of tuples: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid) matching filters, ordered by timestamp ASC.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    where_clauses = []
    values = []
    
    # Build WHERE clauses dynamically
    for key, value in filters.items():
        if key == 'status' and value is not None:
            where_clauses.append("status = ?")
            values.append(value)
        elif key == 'status_in' and value:
            marks = ",".join("?" for _ in value)
            where_clauses.append(f"status IN ({marks})")
            values.extend(list(value))
        elif key == 'status_like' and value is not None:
            where_clauses.append("status LIKE ?")
            values.append(value)
        elif key == 'amount_usdc_units_gt' and value is not None:
            where_clauses.append("amount_usdc_units > ?")
            values.append(value)
        elif key == 'amount_usdc_units_lt' and value is not None:
            where_clauses.append("amount_usdc_units < ?")
            values.append(value)
        elif key == 'timestamp_gt' and value is not None:
            where_clauses.append("timestamp > ?")
            values.append(value)
        elif key == 'timestamp_lt' and value is not None:
            where_clauses.append("timestamp < ?")
            values.append(value)
        elif key == 'memo_like' and value is not None:
            where_clauses.append("memo LIKE ?")
            values.append(value)
        elif key == 'from_address' and value is not None:
            where_clauses.append("from_address = ?")
            values.append(value)
        elif key == 'txid' and value is not None:
            where_clauses.append("txid = ?")
            values.append(value)
    
    limit = filters.get('limit', 1000)  # Default limit to prevent large fetches
    sql = f"""
        SELECT sig, timestamp, memo, from_address, amount_usdc_units, status, txid 
        FROM unprocessed_sigs 
        {'WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''}
        ORDER BY timestamp ASC 
        LIMIT ?
    """
    values.append(limit)
    
    cursor.execute(sql, tuple(values))
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_unprocessed_sig(sig: str, timestamp: int | None = None, memo: str | None = None, from_address: str | None = None, amount_usdc_units: float | None = None, status: str | None = None, txid: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    fields = []
    values = []
    if timestamp is not None:
        fields.append("timestamp = ?")
        values.append(timestamp)
    if memo is not None:
        fields.append("memo = ?")
        values.append(memo)
    if from_address is not None:
        fields.append("from_address = ?")
        values.append(from_address)
    if amount_usdc_units is not None:
        fields.append("amount_usdc_units = ?")
        values.append(amount_usdc_units)
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if txid is not None:
        fields.append("txid = ?")
        values.append(txid)
    values.append(sig)
    sql = f"UPDATE unprocessed_sigs SET {', '.join(fields)} WHERE sig = ?"
    cursor.execute(sql, tuple(values))
    conn.commit()
    conn.close()

def update_unprocessed_sig_memo(sig: str, memo: str | None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE unprocessed_sigs SET memo = ? WHERE sig = ?
    """, (memo, sig))
    conn.commit()
    conn.close()

def update_unprocessed_sig_status(sig: str, status: str | None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE unprocessed_sigs SET status = ? WHERE sig = ?
    """, (status, sig))
    conn.commit()
    conn.close()

def update_unprocessed_sig_txid(sig: str, txid: str | None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE unprocessed_sigs SET txid = ? WHERE sig = ?
    """, (txid, sig))
    conn.commit()
    conn.close()

def set_unprocessed_sig_reference(sig: str, reference: int) -> None:
    """Persist one immutable Nexus debit reference before any remote action."""
    if isinstance(reference, bool) or not isinstance(reference, int):
        raise ValueError("Nexus debit reference must be an integer")
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            """UPDATE unprocessed_sigs SET reference = ?
               WHERE sig = ? AND (reference IS NULL OR reference = ?)""",
            (reference, sig, reference),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise ValueError(
                f"queued deposit {sig} is missing or already has a different Nexus reference"
            )
        conn.commit()
    finally:
        conn.close()


def set_unprocessed_sig_debit_intent(sig: str, reference: int, amount_usdd_units: int) -> None:
    """Atomically persist an intent and make it recoverable before a debit.

    The same transaction moves the deposit out of ``ready for processing``.  A crash
    after this commit must enter chain-evidence resolution rather than permit a fresh
    processing pass to allocate another reference or submit a second Nexus debit.
    """
    if isinstance(reference, bool) or not isinstance(reference, int):
        raise ValueError("Nexus debit reference must be an integer")
    if (isinstance(amount_usdd_units, bool) or not isinstance(amount_usdd_units, int)
            or amount_usdd_units <= 0):
        raise ValueError("Nexus debit amount must be positive integer base units")
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            """UPDATE unprocessed_sigs
           SET reference = ?, amount_usdd_units = ?,
               status = CASE WHEN status = 'ready for processing'
                             THEN 'debit in flight' ELSE status END
           WHERE sig = ?
             AND status IN ('ready for processing', 'debit in flight',
                            'debited, awaiting confirmation')
             AND (reference IS NULL OR reference = ?)
             AND (amount_usdd_units IS NULL OR amount_usdd_units = ?)""",
            (reference, amount_usdd_units, sig, reference, amount_usdd_units),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise ValueError(
                f"queued deposit {sig} is missing or already has a different Nexus debit intent"
            )
        conn.commit()
    finally:
        conn.close()


def get_unprocessed_sig_reference(sig: str) -> int | None:
    """Return the exact persisted debit reference for one queued deposit."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT reference FROM unprocessed_sigs WHERE sig = ?", (sig,)
        ).fetchone()
        if not row or row[0] is None:
            return None
        if isinstance(row[0], bool) or not isinstance(row[0], int):
            raise ValueError(f"queued deposit {sig} has a non-integer Nexus reference")
        return row[0]
    finally:
        conn.close()


def get_unprocessed_sig_nexus_amount(sig: str) -> int | None:
    """Return the exact output persisted before a Nexus debit was attempted."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT amount_usdd_units FROM unprocessed_sigs WHERE sig = ?", (sig,)
        ).fetchone()
        if not row or row[0] is None:
            return None
        if isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] <= 0:
            raise ValueError(f"queued deposit {sig} has invalid Nexus debit amount")
        return row[0]
    finally:
        conn.close()


def get_sigs_pending_debit_verification(statuses: tuple, limit: int = 500) -> List[Tuple]:
    """Rows whose debit outcome is unknown and must be resolved against the chain.

    Returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid, reference)
    """
    if not statuses:
        return []
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in statuses)
    cursor.execute(
        f"""
        SELECT sig, timestamp, memo, from_address, amount_usdc_units, status, txid, reference
        FROM unprocessed_sigs
        WHERE status IN ({placeholders})
        ORDER BY timestamp ASC
        LIMIT ?
        """,
        tuple(statuses) + (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def remove_unprocessed_sig(sig: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM unprocessed_sigs WHERE sig = ?", (sig,))
    conn.commit()
    conn.close()


_SWAP_RECEIPT_COLUMNS = (
    "source_signature", "receipt_name", "expected_owner", "payload_json", "status",
    "asset_address", "manual_review_error", "created_timestamp", "updated_timestamp",
)


def _canonical_receipt_payload(payload: dict) -> tuple[str, str]:
    import json
    canonical = receipt_contract.canonicalize_payload(payload)
    return canonical["source_signature"], json.dumps(
        canonical, sort_keys=True, separators=(",", ":")
    )


def enqueue_swap_receipt(
    payload: dict, receipt_name: str, *, conn=None,
) -> dict:
    """Persist one ownerless obligation; authenticated binding is a later transition."""
    source, encoded = _canonical_receipt_payload(payload)
    name = str(receipt_name or "").strip()
    if not name:
        raise ValueError("swap receipt deterministic name is required")
    owns = conn is None
    db = conn or sqlite3.connect(DB_PATH)
    now = int(time.time())
    try:
        existing = db.execute(
            "SELECT " + ", ".join(_SWAP_RECEIPT_COLUMNS)
            + " FROM swap_receipts WHERE source_signature = ?", (source,),
        ).fetchone()
        if existing is not None:
            row = dict(zip(_SWAP_RECEIPT_COLUMNS, existing))
            if row["receipt_name"] != name or row["payload_json"] != encoded:
                raise ValueError("existing swap receipt obligation conflicts with exact payout evidence")
            return row
        db.execute(
            """INSERT INTO swap_receipts
               (source_signature, receipt_name, expected_owner, payload_json, status,
                created_timestamp, updated_timestamp)
               VALUES (?, ?, NULL, ?, 'awaiting_owner', ?, ?)""",
            (source, name, encoded, now, now),
        )
        if owns:
            db.commit()
        row = get_swap_receipt(source, conn=db)
        if row is None:
            raise RuntimeError("could not read persisted swap receipt obligation")
        return row
    except Exception:
        if owns:
            db.rollback()
        raise
    finally:
        if owns:
            db.close()


def _manual_receipt_evidence_json(evidence: dict) -> tuple[str, str]:
    import json
    if not isinstance(evidence, dict) or set(evidence) != set(receipt_contract.EVIDENCE_FIELDS):
        raise ValueError("manual-review receipt evidence fields are incomplete")
    source = evidence.get("source_signature")
    if not isinstance(source, str) or not source:
        raise ValueError("manual-review receipt evidence requires a source signature")
    return source, json.dumps(evidence, sort_keys=True, separators=(",", ":"))


def _enqueue_manual_review_receipt(
    evidence: dict, receipt_name: str, error: str, *, conn,
) -> dict:
    source, encoded = _manual_receipt_evidence_json(evidence)
    name = str(receipt_name or "")
    if name != receipt_contract.receipt_name(source):
        raise ValueError("manual-review receipt does not match deterministic name")
    if not isinstance(error, str) or not error.strip():
        raise ValueError("manual-review receipt requires an explicit error")
    review_error = error.strip()[:1000]
    existing = conn.execute(
        "SELECT " + ", ".join(_SWAP_RECEIPT_COLUMNS)
        + " FROM swap_receipts WHERE source_signature = ?", (source,),
    ).fetchone()
    if existing is not None:
        row = dict(zip(_SWAP_RECEIPT_COLUMNS, existing))
        if (row["receipt_name"] != name or row["payload_json"] != encoded
                or row["status"] != "manual_review"
                or row["manual_review_error"] != review_error):
            raise ValueError("existing manual-review receipt conflicts with exact payout evidence")
        return row
    now = int(time.time())
    conn.execute(
        """INSERT INTO swap_receipts
           (source_signature, receipt_name, expected_owner, payload_json, status,
            asset_address, manual_review_error, created_timestamp, updated_timestamp)
           VALUES (?, ?, NULL, ?, 'manual_review', NULL, ?, ?, ?)""",
        (source, name, encoded, review_error, now, now),
    )
    row = get_swap_receipt(source, conn=conn)
    if row is None:
        raise RuntimeError("could not read persisted manual-review receipt obligation")
    return row


def mark_swap_receipt_manual_review(source_signature: str, error: str) -> bool:
    """Quarantine a malformed durable payload without releasing any NXS reservation."""
    source = _require_receipt_budget_text(source_signature, "source signature")
    if not isinstance(error, str) or not error.strip():
        raise ValueError("manual-review receipt requires an explicit error")
    review_error = error.strip()[:1000]
    with sqlite3.connect(DB_PATH) as conn:
        changed = conn.execute(
            """UPDATE swap_receipts
               SET status='manual_review', manual_review_error=?, updated_timestamp=?
               WHERE source_signature=?
                 AND status IN ('awaiting_owner','pending','creating','verifying')""",
            (review_error, int(time.time()), source),
        ).rowcount
        return changed == 1


def get_swap_receipt(source_signature: str, *, conn=None) -> dict | None:
    owns = conn is None
    db = conn or sqlite3.connect(DB_PATH)
    try:
        row = db.execute(
            "SELECT " + ", ".join(_SWAP_RECEIPT_COLUMNS)
            + " FROM swap_receipts WHERE source_signature = ?", (source_signature,),
        ).fetchone()
        return dict(zip(_SWAP_RECEIPT_COLUMNS, row)) if row is not None else None
    finally:
        if owns:
            db.close()


def list_swap_receipts_for_publication(limit: int = 100) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT " + ", ".join(_SWAP_RECEIPT_COLUMNS)
            + " FROM swap_receipts "
              "WHERE status IN ('awaiting_owner','pending','creating','verifying') "
              "ORDER BY created_timestamp ASC LIMIT ?", (int(limit),),
        ).fetchall()
    return [dict(zip(_SWAP_RECEIPT_COLUMNS, row)) for row in rows]


def bind_swap_receipt_owner(source_signature: str, expected_owner: str) -> bool:
    """Freeze the first authenticated provider owner; never replace it on mismatch."""
    source = _require_receipt_budget_text(source_signature, "source signature")
    owner = _require_receipt_budget_text(expected_owner, "provider owner")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT expected_owner, status FROM swap_receipts WHERE source_signature=?",
            (source,),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        if row[0] is not None:
            conn.commit()
            return row[0] == owner
        changed = conn.execute(
            """UPDATE swap_receipts
               SET expected_owner=?, status='pending', updated_timestamp=?
               WHERE source_signature=? AND expected_owner IS NULL
                     AND status='awaiting_owner'""",
            (owner, int(time.time()), source),
        ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _require_receipt_budget_text(value: str, field: str, max_length: int = 500) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length:
        raise ValueError(f"receipt NXS budget {field} is required and must be at most {max_length} characters")
    return text


def _require_receipt_budget_units(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"receipt NXS budget {field} must be an exact positive integer")
    return value


def claim_swap_receipt_with_nxs_budget(
    source_signature: str, *, expected_cost_nxs_units: int, budget_nxs_units: int,
) -> bool:
    """Atomically reserve bounded NXS spend and cross one receipt create boundary.

    A reservation is never released automatically: a failed, timed-out, or crashed
    create can still have spent NXS, and reusing that capacity would make the operator
    budget depend on an unprovable negative result.
    """
    source_signature = _require_receipt_budget_text(source_signature, "source signature")
    expected_cost_nxs_units = _require_receipt_budget_units(
        expected_cost_nxs_units, "expected cost"
    )
    budget_nxs_units = _require_receipt_budget_units(budget_nxs_units, "budget")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        receipt = conn.execute(
            "SELECT receipt_name, status FROM swap_receipts WHERE source_signature=?",
            (source_signature,),
        ).fetchone()
        if receipt is None or receipt[1] != "pending":
            conn.commit()
            return False
        if conn.execute(
            "SELECT 1 FROM receipt_nxs_budget_events "
            "WHERE source_signature=? AND event='reserved'", (source_signature,),
        ).fetchone() is not None:
            conn.commit()
            return False
        used = conn.execute(
            "SELECT COALESCE(SUM(expected_cost_nxs_units), 0) "
            "FROM receipt_nxs_budget_events WHERE event='reserved'"
        ).fetchone()[0]
        if int(used or 0) + expected_cost_nxs_units > budget_nxs_units:
            conn.commit()
            return False
        now = int(time.time())
        conn.execute(
            """INSERT INTO receipt_nxs_budget_events
               (source_signature, receipt_name, event, expected_cost_nxs_units, create_txid,
                asset_address, timestamp)
               VALUES (?, ?, 'reserved', ?, NULL, NULL, ?)""",
            (source_signature, receipt[0], expected_cost_nxs_units, now),
        )
        changed = conn.execute(
            """UPDATE swap_receipts SET status='creating', updated_timestamp=?
               WHERE source_signature=? AND status='pending'""",
            (now, source_signature),
        ).rowcount
        if changed != 1:
            raise RuntimeError("receipt changed while reserving its NXS create budget")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_swap_receipt_create_report(
    source_signature: str, *, create_txid: str | None = None, asset_address: str | None = None,
) -> bool:
    """Append parseable create response identity without authorizing another create."""
    source_signature = _require_receipt_budget_text(source_signature, "source signature")
    txid = str(create_txid or "").strip() or None
    address = str(asset_address or "").strip() or None
    if txid is None and address is None:
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        reserved = conn.execute(
            """SELECT receipt_name, expected_cost_nxs_units FROM receipt_nxs_budget_events
               WHERE source_signature=? AND event='reserved'""",
            (source_signature,),
        ).fetchone()
        if reserved is None:
            conn.commit()
            return False
        prior = conn.execute(
            """SELECT create_txid, asset_address FROM receipt_nxs_budget_events
               WHERE source_signature=? AND event='create_reported'""",
            (source_signature,),
        ).fetchone()
        if prior is not None:
            conn.commit()
            return prior == (txid, address)
        conn.execute(
            """INSERT INTO receipt_nxs_budget_events
               (source_signature, receipt_name, event, expected_cost_nxs_units, create_txid,
                asset_address, timestamp)
               VALUES (?, ?, 'create_reported', ?, ?, ?, ?)""",
            (source_signature, reserved[0], reserved[1], txid, address, int(time.time())),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_swap_receipt_publication(source_signature: str, status: str,
                                    asset_address: str | None = None) -> bool:
    if status not in {"verifying", "published"}:
        raise ValueError("invalid swap receipt publication status")
    address = str(asset_address or "").strip() or None
    if status == "published" and not address:
        raise ValueError("published swap receipt requires an asset address")
    with sqlite3.connect(DB_PATH) as conn:
        changed = conn.execute(
            """UPDATE swap_receipts SET status=?, asset_address=COALESCE(?, asset_address),
                      updated_timestamp=?
               WHERE source_signature=? AND status IN ('creating','verifying')""",
            (status, address, int(time.time()), source_signature),
        ).rowcount
        if changed == 1 and status == "published":
            reserved = conn.execute(
                """SELECT receipt_name, expected_cost_nxs_units FROM receipt_nxs_budget_events
                   WHERE source_signature=? AND event='reserved'""",
                (source_signature,),
            ).fetchone()
            if reserved is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO receipt_nxs_budget_events
                       (source_signature, receipt_name, event, expected_cost_nxs_units,
                        create_txid, asset_address, timestamp)
                       VALUES (?, ?, 'published', ?, NULL, ?, ?)""",
                    (source_signature, reserved[0], reserved[1], address, int(time.time())),
                )
        return changed == 1


def finalize_confirmed_solana_payout(
    *, sig: str, timestamp: int, amount_solana_units: int, output_txid: str,
    output_units: int, nexus_destination: str, memo: str, reference: int,
    output_contract_id: int, fee_solana_units: int,
    receipt_payload: dict | None = None, receipt_name: str | None = None,
    receipt_evidence: dict | None = None, receipt_error: str | None = None,
    nexus_decimals: int = 6,
) -> bool:
    """Atomically archive exact confirmed payout, fee and optional receipt obligation."""
    ints = (timestamp, amount_solana_units, output_units, reference, output_contract_id,
            fee_solana_units, nexus_decimals)
    if (any(type(value) is not int for value in ints) or output_units <= 0
            or output_contract_id < 0 or fee_solana_units < 0 or nexus_decimals < 0):
        raise ValueError("confirmed payout requires exact integer evidence")
    manual_review_requested = receipt_evidence is not None or receipt_error is not None
    if receipt_payload is not None and manual_review_requested:
        raise ValueError("receipt obligation cannot be both canonical and manual review")
    if manual_review_requested and (receipt_evidence is None or receipt_error is None):
        raise ValueError("manual-review receipt requires evidence and error")
    if receipt_payload is not None:
        canonical_receipt = receipt_contract.canonicalize_payload(receipt_payload)
        expected_receipt_evidence = {
            "source_signature": sig,
            "nexus_account": nexus_destination,
            "output_txid": output_txid,
            "output_contract_id": str(output_contract_id),
            "output_units": str(output_units),
            "reference": str(reference),
        }
        if any(
            canonical_receipt.get(key) != value
            for key, value in expected_receipt_evidence.items()
        ):
            raise ValueError("swap receipt does not match exact confirmed payout evidence")
    elif receipt_evidence is not None:
        _manual_receipt_evidence_json(receipt_evidence)
        exact_manual_financial_evidence = {
            "source_signature": sig,
            "nexus_account": nexus_destination,
            "output_txid": output_txid,
            "output_contract_id": output_contract_id,
            "output_units": output_units,
            "reference": reference,
        }
        if (any(
                receipt_evidence.get(key) != value
                for key, value in exact_manual_financial_evidence.items()
            ) or any(
                type(receipt_evidence.get(key)) is not int
                for key in ("output_contract_id", "output_units", "reference")
            ) or any(
                not isinstance(receipt_evidence.get(key), str)
                for key in ("solana_mint", "solana_vault", "nexus_token")
            )):
            raise ValueError("manual-review receipt does not match exact confirmed payout evidence")
    if receipt_payload is not None or receipt_evidence is not None:
        if str(receipt_name or "") != receipt_contract.receipt_name(sig):
            raise ValueError("swap receipt does not match deterministic name")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            """SELECT timestamp, amount_usdc_units, memo, status, txid, reference,
                      amount_usdd_units FROM unprocessed_sigs WHERE sig=?""", (sig,),
        ).fetchone()
        expected_source = (timestamp, amount_solana_units, memo,
                           "debited, awaiting confirmation", output_txid,
                           reference, output_units)
        terminal = conn.execute(
            """SELECT timestamp, amount_usdc_units, txid, amount_usdd_units,
                      nexus_destination, memo, status, reference, contract_id
               FROM processed_sigs WHERE sig=?""", (sig,),
        ).fetchone()
        expected_terminal = (timestamp, amount_solana_units, output_txid, output_units,
                             nexus_destination, memo, "debit_confirmed", reference,
                             output_contract_id)
        if source is None:
            if terminal != expected_terminal:
                conn.commit()
                return False
            if receipt_payload is not None or receipt_evidence is not None:
                # A terminal row created before receipts were enabled lacks a frozen
                # provider/pair snapshot. Never invent that historical context. Only an
                # already-durable obligation may be validated as an idempotent replay.
                if get_swap_receipt(sig, conn=conn) is None:
                    conn.commit()
                    return False
                if receipt_payload is not None:
                    enqueue_swap_receipt(
                        receipt_payload, receipt_name or "", conn=conn
                    )
                else:
                    assert receipt_evidence is not None
                    _enqueue_manual_review_receipt(
                        receipt_evidence, receipt_name or "", receipt_error or "", conn=conn
                    )
            conn.commit()
            return True
        if source != expected_source:
            conn.commit()
            return False
        if fee_solana_units:
            fee_rows = conn.execute(
                "SELECT amount_usdc_units FROM fee_entries "
                "WHERE sig=? AND kind='swap_solana_to_nexus'", (sig,),
            ).fetchall()
            if fee_rows and fee_rows != [(fee_solana_units,)]:
                conn.commit()
                return False
            if not fee_rows:
                conn.execute(
                    """INSERT INTO fee_entries
                       (sig, txid, kind, amount_usdc_units, amount_usdd_units, timestamp)
                       VALUES (?, ?, 'swap_solana_to_nexus', ?, NULL, ?)""",
                    (sig, output_txid, fee_solana_units, int(time.time())),
                )
        from decimal import Decimal
        amount_display = float(Decimal(output_units) / (Decimal(10) ** nexus_decimals))
        conn.execute(
            """INSERT INTO processed_sigs
               (sig, timestamp, amount_usdc_units, txid, amount_usdd, amount_usdd_units,
                nexus_destination, memo, status, reference, contract_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'debit_confirmed', ?, ?)""",
            (sig, timestamp, amount_solana_units, output_txid, amount_display, output_units,
             nexus_destination, memo, reference, output_contract_id),
        )
        if receipt_payload is not None:
            enqueue_swap_receipt(receipt_payload, receipt_name or "", conn=conn)
        elif receipt_evidence is not None:
            _enqueue_manual_review_receipt(
                receipt_evidence, receipt_name or "", receipt_error or "", conn=conn
            )
        if conn.execute("DELETE FROM unprocessed_sigs WHERE sig=?", (sig,)).rowcount != 1:
            raise RuntimeError("exact pending Solana deposit changed during finalization")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


## Processed Signatures

def mark_processed_sig(
    sig: str,
    timestamp: int,
    amount_usdc_units: int | None = None,
    txid: str | None = None,
    amount_usdd: float | None = None,
    status: str | None = None,
    reference: int | None = None,
    *,
    amount_usdd_units: int | None = None,
    nexus_destination: str | None = None,
    memo: str | None = None,
    contract_id: int | None = None,
):
    """Insert/update a processed signature record.

    Backward compatibility:
      Older call sites used: mark_processed_sig(sig, timestamp, "status text")
      In that case the third positional argument (amount_usdc_units) is actually a status string.
    """
    # Back-compat shim: if amount_usdc_units is actually a status string and no other
    # fields were supplied, treat it as status.
    if isinstance(amount_usdc_units, str) and status is None and txid is None and amount_usdd is None and reference is None:
        status = amount_usdc_units  # type: ignore
        amount_usdc_units = None

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO processed_sigs
        (sig, timestamp, amount_usdc_units, txid, amount_usdd, amount_usdd_units,
         nexus_destination, memo, status, reference, contract_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sig, timestamp, amount_usdc_units, txid, amount_usdd, amount_usdd_units,
         nexus_destination, memo, status, reference, contract_id),
    )
    conn.commit()
    conn.close()

def is_processed_sig(sig: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed_sigs WHERE sig = ?", (sig,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_latest_reference() -> int:
    """Fetch the latest used debit reference from processed_sigs (correct table holding reference)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT reference FROM processed_sigs WHERE reference IS NOT NULL ORDER BY reference DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


## Refunded Signatures

def mark_refunded_sig(sig: str, timestamp: int, from_address: str, amount_usdc_units: int, memo: str | None, refund_sig: str | None, refunded_units: int | None, status: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO refunded_sigs (sig, timestamp, from_address, amount_usdc_units, memo, refund_sig, refunded_units, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (sig, timestamp, from_address, amount_usdc_units, memo, refund_sig, refunded_units, status))
    conn.commit()
    conn.close()

def is_refunded_sig(sig: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM refunded_sigs WHERE sig = ?", (sig,))
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0

## Quarantined sigs
def mark_quarantined_sig(
    sig: str,
    timestamp: int,
    from_address: str,
    amount_usdc_units: int,
    memo: str | None,
    quarantine_sig: str | None = None,
    quarantined_units: int | None = None,
    status: str | None = None,
):
    """Insert/update quarantined signature.

    Schema includes quarantine_sig & quarantined_units so we persist them for later reconciliation / auditing.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO quarantined_sigs (sig, timestamp, from_address, amount_usdc_units, memo, quarantine_sig, quarantined_units, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sig, timestamp, from_address, amount_usdc_units, memo, quarantine_sig, quarantined_units, status),
    )
    conn.commit()
    conn.close()


def is_quarantined_sig(sig: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM quarantined_sigs WHERE sig = ?", (sig,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


## Unprocessed txids Nexus -> Solana

def mark_unprocessed_txid(
    txid: str,
    sig: str | None = None,  # legacy unused param (no 'sig' column in table)
    timestamp: int | None = None,
    amount_usdd: float | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    owner_from_address: str | None = None,
    confirmations_credit: int | None = None,
    status: str | None = None,
):
    """Insert/update an unprocessed Nexus txid.

    The historical signature parameter is ignored because the table has no 'sig' column.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO unprocessed_txids (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status),
    )
    conn.commit()
    conn.close()

def is_unprocessed_txid(txid: str, contract_id: int | None = None) -> bool:
    """Check whether any (or one exact) Nexus credit is still queued."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if contract_id is None:
        cursor.execute("SELECT 1 FROM unprocessed_txids WHERE txid = ?", (txid,))
    else:
        cursor.execute(
            "SELECT 1 FROM unprocessed_txids WHERE txid = ? AND contract_id = ?",
            (txid, contract_id),
        )
    result = cursor.fetchone()
    conn.close()
    return result is not None


## Processed txids
def mark_processed_txid(
    txid: str,
    timestamp: int,
    amount_usdd: float,
    from_address: str,
    to_address: str,
    owner: str,
    sig: str,
    status: str | None = None,
    *,
    amount_usdd_units: int | None = None,
    contract_id: int = -1,
):
    """Insert/update processed credit evidence by immutable Nexus contract identity."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO processed_txids
        (txid, contract_id, timestamp, amount_usdd, amount_usdd_units, from_address, to_address, owner, sig, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (txid, contract_id, timestamp, amount_usdd, amount_usdd_units, from_address, to_address, owner, sig, status),
    )
    conn.commit()
    conn.close()




## Refunded txids
def mark_refunded_txid(
    txid: str,
    sig: str | None = None,
    timestamp: int | None = None,
    amount_usdd: float | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    owner_from_address: str | None = None,
    confirmations_credit: int | None = None,
    status: str | None = None,
    *,
    contract_id: int = -1,
):
    """Insert/update refunded txid.

    Stores refund transfer signature in refunded_txids.sig (added via migration if missing).
    Unspecified fields remain NULL allowing partial population as info becomes available.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO refunded_txids (
            txid, contract_id, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, sig
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (txid, contract_id, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, sig),
    )
    conn.commit()
    conn.close()

def is_refunded_txid(txid: str, contract_id: int | None = None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if contract_id is None:
        cursor.execute("SELECT 1 FROM refunded_txids WHERE txid = ?", (txid,))
    else:
        cursor.execute(
            "SELECT 1 FROM refunded_txids WHERE txid = ? AND contract_id = ?",
            (txid, contract_id),
        )
    result = cursor.fetchone()
    conn.close()
    return result is not None


## Quarantined txids

def mark_quarantined_txid(
    txid: str,
    sig: str = "",
    timestamp: int | None = None,
    amount_usdd: float | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    *,
    contract_id: int = -1,
):
    """Record a quarantined Nexus txid.

    Previously this wrote only (txid, sig), leaving amount/from/to/owner permanently
    NULL - so quarantine_viewer summed `amount_usdd` and always reported zero
    quarantined, however much was actually stuck. Populate the full row.
    """
    import time as _time
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Preserve any previously-recorded detail if this is a re-mark with fewer fields.
    cursor.execute("SELECT timestamp, amount_usdd, from_address, to_address, owner, status "
                   "FROM quarantined_txids WHERE txid = ? AND contract_id = ?", (txid, contract_id))
    prev = cursor.fetchone() or (None, None, None, None, None, None)
    cursor.execute(
        """
        INSERT OR REPLACE INTO quarantined_txids
        (txid, contract_id, timestamp, amount_usdd, from_address, to_address, owner, sig, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txid,
            contract_id,
            timestamp if timestamp is not None else (prev[0] if prev[0] is not None else int(_time.time())),
            amount_usdd if amount_usdd is not None else prev[1],
            from_address if from_address is not None else prev[2],
            to_address if to_address is not None else prev[3],
            owner if owner is not None else prev[4],
            sig,
            status if status is not None else prev[5],
        ),
    )
    conn.commit()
    conn.close()


## Outbound payout ledger (rolling exposure caps)

def _require_solana_payout_budget_text(value: str, field: str, max_length: int = 500) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length:
        raise ValueError(f"Solana payout budget {field} is required and must be at most {max_length} characters")
    return text


def _require_solana_payout_budget_units(value: int, field: str, *, positive: bool = True) -> int:
    if type(value) is not int or (value <= 0 if positive else value < 0):
        comparator = "positive" if positive else "nonnegative"
        raise ValueError(f"Solana payout budget {field} must be an exact {comparator} integer")
    return value


def _payout_budget_usage_in_transaction(conn, cutoff: int) -> int:
    """Return capacity consumed by unsettled obligations plus recent final spend."""
    row = conn.execute(
        """SELECT COALESCE(SUM(amount_usdc_units), 0) FROM (
               SELECT reserved.amount_usdc_units
               FROM solana_payout_budget_events AS reserved
               WHERE reserved.event = 'reserved'
                 AND NOT EXISTS (
                     SELECT 1 FROM solana_payout_budget_events AS terminal
                     WHERE terminal.obligation_id = reserved.obligation_id
                       AND terminal.event IN ('confirmed', 'released')
                 )
               UNION ALL
               SELECT confirmed.amount_usdc_units
               FROM solana_payout_budget_events AS confirmed
               WHERE confirmed.event = 'confirmed' AND confirmed.timestamp >= ?
               UNION ALL
               SELECT payouts.amount_usdc_units
               FROM payouts
               WHERE payouts.timestamp >= ?
                 -- Primary sends retain this legacy compatibility row, but their
                 -- exact finalized signature is already represented above.
                 AND NOT EXISTS (
                     SELECT 1 FROM solana_payout_budget_events AS confirmed
                     WHERE confirmed.event = 'confirmed'
                       AND confirmed.signature = payouts.reference
                       AND confirmed.amount_usdc_units = payouts.amount_usdc_units
                 )
           )""",
        (cutoff, cutoff),
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


def _reserve_solana_payout_budget_in_transaction(
    conn, *, obligation_id: str, kind: str, amount_usdc_units: int,
    cap_units: int, window_sec: int, now: int,
) -> bool:
    """Reserve one exact obligation while the caller owns a SQLite write transaction."""
    existing = conn.execute(
        """SELECT amount_usdc_units FROM solana_payout_budget_events
           WHERE obligation_id = ? AND event = 'reserved'""",
        (obligation_id,),
    ).fetchone()
    if existing is not None:
        # A caller must not mistake an earlier claim for permission to submit again.
        # The durable source lifecycle owns retry/recovery; this reservation API only
        # authorizes the first claimant.
        return False
    if cap_units > 0 and _payout_budget_usage_in_transaction(conn, now - window_sec) + amount_usdc_units > cap_units:
        return False
    conn.execute(
        """INSERT INTO solana_payout_budget_events
           (obligation_id, kind, event, amount_usdc_units, signature, evidence, timestamp)
           VALUES (?, ?, 'reserved', ?, NULL, NULL, ?)""",
        (obligation_id, kind, amount_usdc_units, now),
    )
    return True


def reserve_solana_payout_budget(
    *, obligation_id: str, kind: str, amount_usdc_units: int, cap_units: int,
    window_sec: int = 86400,
) -> bool:
    """Atomically reserve rolling payout capacity for one durable obligation.

    Unsettled reservations consume capacity indefinitely; only a confirmed payout is
    allowed to age out of the rolling window.  This intentionally fails closed when a
    submitted transfer's outcome cannot yet be proven.
    """
    obligation_id = _require_solana_payout_budget_text(obligation_id, "obligation id")
    kind = _require_solana_payout_budget_text(kind, "kind", 100)
    amount_usdc_units = _require_solana_payout_budget_units(amount_usdc_units, "amount")
    cap_units = _require_solana_payout_budget_units(cap_units, "cap", positive=False)
    window_sec = _require_solana_payout_budget_units(window_sec, "window")
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        reserved = _reserve_solana_payout_budget_in_transaction(
            conn, obligation_id=obligation_id, kind=kind,
            amount_usdc_units=amount_usdc_units, cap_units=cap_units,
            window_sec=window_sec, now=now,
        )
        conn.commit()
        return reserved
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_solana_payout_submission(obligation_id: str, signature: str) -> bool:
    """Append the returned signature without making an ambiguous payout retryable."""
    obligation_id = _require_solana_payout_budget_text(obligation_id, "obligation id")
    signature = _require_solana_payout_budget_text(signature, "signature")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        reserved = conn.execute(
            """SELECT kind, amount_usdc_units FROM solana_payout_budget_events
               WHERE obligation_id = ? AND event = 'reserved'""",
            (obligation_id,),
        ).fetchone()
        if reserved is None or conn.execute(
            """SELECT 1 FROM solana_payout_budget_events
               WHERE obligation_id = ? AND event IN ('confirmed', 'released')""",
            (obligation_id,),
        ).fetchone() is not None:
            conn.commit()
            return False
        prior = conn.execute(
            """SELECT signature FROM solana_payout_budget_events
               WHERE obligation_id = ? AND event = 'submitted'""",
            (obligation_id,),
        ).fetchone()
        if prior is not None:
            conn.commit()
            return prior[0] == signature
        conn.execute(
            """INSERT INTO solana_payout_budget_events
               (obligation_id, kind, event, amount_usdc_units, signature, evidence, timestamp)
               VALUES (?, ?, 'submitted', ?, ?, NULL, ?)""",
            (obligation_id, reserved[0], reserved[1], signature, int(time.time())),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _settle_solana_payout_budget_in_transaction(
    conn, *, obligation_id: str, signature: str, amount_usdc_units: int,
) -> bool:
    reserved = conn.execute(
        """SELECT kind, amount_usdc_units FROM solana_payout_budget_events
           WHERE obligation_id = ? AND event = 'reserved'""",
        (obligation_id,),
    ).fetchone()
    if reserved is None or int(reserved[1]) != amount_usdc_units:
        return False
    submitted = conn.execute(
        """SELECT signature FROM solana_payout_budget_events
           WHERE obligation_id = ? AND event = 'submitted'""",
        (obligation_id,),
    ).fetchone()
    if submitted is not None and submitted[0] != signature:
        return False
    confirmed = conn.execute(
        """SELECT signature, amount_usdc_units FROM solana_payout_budget_events
           WHERE obligation_id = ? AND event = 'confirmed'""",
        (obligation_id,),
    ).fetchone()
    if confirmed is not None:
        return confirmed[0] == signature and int(confirmed[1]) == amount_usdc_units
    if conn.execute(
        """SELECT 1 FROM solana_payout_budget_events
           WHERE obligation_id = ? AND event = 'released'""",
        (obligation_id,),
    ).fetchone() is not None:
        return False
    conn.execute(
        """INSERT INTO solana_payout_budget_events
           (obligation_id, kind, event, amount_usdc_units, signature, evidence, timestamp)
           VALUES (?, ?, 'confirmed', ?, ?, NULL, ?)""",
        (obligation_id, reserved[0], amount_usdc_units, signature, int(time.time())),
    )
    return True


def settle_solana_payout_budget(obligation_id: str, signature: str, amount_usdc_units: int) -> bool:
    """Settle a reserved obligation only after exact finalized payout evidence."""
    obligation_id = _require_solana_payout_budget_text(obligation_id, "obligation id")
    signature = _require_solana_payout_budget_text(signature, "signature")
    amount_usdc_units = _require_solana_payout_budget_units(amount_usdc_units, "amount")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        settled = _settle_solana_payout_budget_in_transaction(
            conn, obligation_id=obligation_id, signature=signature,
            amount_usdc_units=amount_usdc_units,
        )
        conn.commit()
        return settled
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def payout_budget_used(seconds: int = 86400) -> int:
    """Capacity consumed by unsettled reservations plus finalized rolling-window spend."""
    seconds = _require_solana_payout_budget_units(seconds, "window")
    conn = sqlite3.connect(DB_PATH)
    try:
        return _payout_budget_usage_in_transaction(conn, int(time.time()) - seconds)
    finally:
        conn.close()


def _reconstruct_confirmed_solana_payout_budget_in_transaction(
    conn, *, obligation_id: str, kind: str, signature: str,
    amount_usdc_units: int, chain_timestamp: int,
) -> bool:
    """Restore authoritative confirmed spend while the caller owns the write lock."""
    expected = {
        "reserved": (kind, amount_usdc_units, None),
        "submitted": (kind, amount_usdc_units, signature),
        "confirmed": (kind, amount_usdc_units, signature),
    }
    conflicting_signature = conn.execute(
        """SELECT 1 FROM solana_payout_budget_events
           WHERE obligation_id != ? AND event IN ('submitted', 'confirmed')
             AND signature = ? LIMIT 1""",
        (obligation_id, signature),
    ).fetchone()
    if conflicting_signature is not None:
        return False
    rows = conn.execute(
        """SELECT event, kind, amount_usdc_units, signature, evidence, timestamp
           FROM solana_payout_budget_events
           WHERE obligation_id = ? ORDER BY id""",
        (obligation_id,),
    ).fetchall()
    observed: dict[str, tuple[str, int, str | None]] = {}
    for event, observed_kind, amount, event_signature, evidence, timestamp in rows:
        if (event not in expected or event in observed
                or (observed_kind, amount, event_signature) != expected[event]
                or evidence not in (None, "recovered_finalized_solana_payout")
                or type(timestamp) is not int or timestamp <= 0):
            return False
        observed[event] = (observed_kind, amount, event_signature)
    for event in ("reserved", "submitted", "confirmed"):
        if event in observed:
            continue
        event_kind, amount, event_signature = expected[event]
        conn.execute(
            """INSERT INTO solana_payout_budget_events
               (obligation_id, kind, event, amount_usdc_units, signature, evidence, timestamp)
               VALUES (?, ?, ?, ?, ?, 'recovered_finalized_solana_payout', ?)""",
            (obligation_id, event_kind, event, amount, event_signature, chain_timestamp),
        )
    # Only confirmed spend ages out of the cap. Replace a local confirmation-clock value
    # with the finalized block time; reserved/submitted timestamps remain intent history.
    conn.execute(
        """UPDATE solana_payout_budget_events
           SET timestamp = ?, evidence = 'recovered_finalized_solana_payout'
           WHERE obligation_id = ? AND event = 'confirmed'""",
        (chain_timestamp, obligation_id),
    )
    return True


def reconstruct_confirmed_solana_payout_budget(
    *, obligation_id: str, signature: str, amount_usdc_units: int, chain_timestamp: int,
    kind: str = "nexus_payout",
) -> bool:
    """Restore one exact finalized payout's rolling-cap evidence after database loss.

    The finalized block timestamp is authoritative for the rolling window. Existing
    exact partial ledgers are completed (including a missing reservation); contradictory
    amounts, kinds, identities, signatures or evidence remain an incomplete recovery.
    """
    obligation_id = _require_solana_payout_budget_text(obligation_id, "obligation id")
    kind = _require_solana_payout_budget_text(kind, "kind", 100)
    signature = _require_solana_payout_budget_text(signature, "signature")
    amount_usdc_units = _require_solana_payout_budget_units(amount_usdc_units, "amount")
    if type(chain_timestamp) is not int or chain_timestamp <= 0 or chain_timestamp > int(time.time()):
        raise ValueError("reconstructed Solana payout timestamp must be a non-future exact integer")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        restored = _reconstruct_confirmed_solana_payout_budget_in_transaction(
            conn, obligation_id=obligation_id, kind=kind, signature=signature,
            amount_usdc_units=amount_usdc_units, chain_timestamp=chain_timestamp,
        )
        if restored:
            conn.commit()
        else:
            conn.rollback()
        return restored
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_SOLANA_SIG_DISPOSITION = {
    "refund": {
        "table": "refunded_sigs",
        "signature_column": "refund_sig",
        "units_column": "refunded_units",
        "ready_statuses": ("to be refunded",),
        "held_status": "refund submission held",
        "awaiting_status": "refund sent, awaiting confirmation",
        "terminal_status": "refund_confirmed",
        "budget_kind": "solana_refund",
    },
    "quarantine": {
        "table": "quarantined_sigs",
        "signature_column": "quarantine_sig",
        "units_column": "quarantined_units",
        "ready_statuses": ("to be quarantined", "quarantine failed"),
        "held_status": "quarantine submission held",
        "awaiting_status": "quarantine sent, awaiting confirmation",
        "terminal_status": "quarantine_confirmed",
        "budget_kind": "solana_quarantine",
    },
}


def _solana_sig_disposition(source_sig: str, kind: str) -> tuple[str, dict]:
    source_sig = _require_solana_payout_budget_text(source_sig, "source signature")
    details = _SOLANA_SIG_DISPOSITION.get(kind)
    if details is None:
        raise ValueError("Solana disposition kind must be refund or quarantine")
    return source_sig, details


def _solana_sig_disposition_obligation_id(kind: str, source_sig: str) -> str:
    # The exact incoming Solana signature is immutable and already the primary key of
    # unprocessed_sigs.  A deterministic ID makes a restart see the same capacity claim.
    return f"{kind}:{source_sig}"


def _canonical_solana_source_memo(value: str | None) -> str:
    """Canonicalize only the optional deposit memo; identifiers remain byte-exact."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("reconstructed Solana source memo must be text or null")
    return value


def reconstruct_confirmed_solana_sig_disposition(
    *, kind: str, source_signature: str, source_timestamp: int,
    source_token_account: str, source_amount_solana_units: int,
    source_memo: str | None, payout_signature: str,
    destination_token_account: str, payout_amount_solana_units: int,
    chain_timestamp: int, payout_memo: str,
) -> bool:
    """Atomically restore one current refund/quarantine and its confirmed cap spend."""
    source_signature, details = _solana_sig_disposition(source_signature, kind)
    source_timestamp = _require_solana_payout_budget_units(source_timestamp, "source timestamp")
    source_token_account = _require_solana_payout_budget_text(
        source_token_account, "source token account"
    )
    source_amount_solana_units = _require_solana_payout_budget_units(
        source_amount_solana_units, "source amount"
    )
    payout_signature = _require_solana_payout_budget_text(payout_signature, "signature")
    destination_token_account = _require_solana_payout_budget_text(
        destination_token_account, "destination token account"
    )
    payout_amount_solana_units = _require_solana_payout_budget_units(
        payout_amount_solana_units, "amount"
    )
    payout_memo = _require_solana_payout_budget_text(payout_memo, "payout memo", 1024)
    source_memo = _canonical_solana_source_memo(source_memo)
    if (type(chain_timestamp) is not int or chain_timestamp <= 0
            or chain_timestamp > int(time.time()) or source_timestamp > chain_timestamp
            or payout_amount_solana_units > source_amount_solana_units):
        raise ValueError("reconstructed Solana disposition chronology or amount is invalid")

    obligation_id = _solana_sig_disposition_obligation_id(kind, source_signature)
    signature_column = details["signature_column"]
    units_column = details["units_column"]
    expected_terminal = (
        source_signature, source_timestamp, source_token_account,
        destination_token_account, source_amount_solana_units, source_memo,
        payout_memo, payout_signature, payout_amount_solana_units,
    )
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM processed_sigs WHERE sig = ?", (source_signature,)
        ).fetchone() is not None:
            conn.rollback()
            return False
        opposing_table = (
            "quarantined_sigs" if kind == "refund" else "refunded_sigs"
        )
        if conn.execute(
            f"SELECT 1 FROM {opposing_table} WHERE sig = ?", (source_signature,)
        ).fetchone() is not None:
            conn.rollback()
            return False

        pending = conn.execute(
            """SELECT timestamp, memo, from_address, amount_usdc_units, status, txid,
                      reference, amount_usdd_units
               FROM unprocessed_sigs WHERE sig = ?""",
            (source_signature,),
        ).fetchone()
        if pending is not None:
            pending_source = (
                pending[0], _canonical_solana_source_memo(pending[1]), pending[2], pending[3]
            )
            expected_pending_source = (
                source_timestamp, source_memo, source_token_account,
                source_amount_solana_units,
            )
            permitted_statuses = {
                *details["ready_statuses"], details["held_status"], details["awaiting_status"],
            }
            # Any frozen Nexus mint term is a separate active obligation. Positive
            # refund/quarantine chain evidence must not erase or release it.
            has_active_nexus_intent = any(value is not None for value in pending[5:8])
            if (pending_source != expected_pending_source
                    or pending[4] not in permitted_statuses
                    or has_active_nexus_intent):
                conn.rollback()
                return False

        terminal = conn.execute(
            f"""SELECT sig, timestamp, from_address, destination_address,
                       amount_usdc_units, memo, payout_memo,
                       {signature_column}, {units_column}, status
                FROM {details['table']} WHERE sig = ?""",
            (source_signature,),
        ).fetchone()
        if terminal is not None:
            terminal_immutable = (
                terminal[0], terminal[1], terminal[2], terminal[3], terminal[4],
                _canonical_solana_source_memo(terminal[5]), terminal[6], terminal[7],
                terminal[8],
            )
            immutable_match = (
                terminal_immutable[:7] == expected_terminal[:7]
                and terminal_immutable[8] == expected_terminal[8]
            )
            signature_match = (
                terminal[7] == payout_signature
                or (terminal[7] is None and terminal[9] == "submitting")
            )
            lifecycle_match = pending is None or (
                (terminal[9] == "submitting" and pending[4] == details["held_status"])
                or (
                    terminal[9] == "awaiting confirmation"
                    and pending[4] == details["awaiting_status"]
                )
            )
            if (not immutable_match or not signature_match or not lifecycle_match
                    or terminal[9] not in {
                        "submitting", "awaiting confirmation", details["terminal_status"],
                    }):
                conn.rollback()
                return False
        else:
            conn.execute(
                f"""INSERT INTO {details['table']}
                   (sig, timestamp, from_address, destination_address,
                    amount_usdc_units, memo, payout_memo,
                    {signature_column}, {units_column}, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*expected_terminal, details["terminal_status"]),
            )

        if not _reconstruct_confirmed_solana_payout_budget_in_transaction(
            conn, obligation_id=obligation_id, kind=details["budget_kind"],
            signature=payout_signature, amount_usdc_units=payout_amount_solana_units,
            chain_timestamp=chain_timestamp,
        ):
            conn.rollback()
            return False

        fee_units = source_amount_solana_units - payout_amount_solana_units
        fee_kind = f"{kind}_flat_fee"
        fees = conn.execute(
            """SELECT amount_usdc_units, timestamp FROM fee_entries
               WHERE sig = ? AND txid IS NULL AND kind = ?""",
            (source_signature, fee_kind),
        ).fetchall()
        if fees:
            if (fee_units <= 0 or len(fees) != 1 or fees[0][0] != fee_units
                    or type(fees[0][1]) is not int or fees[0][1] <= 0
                    or fees[0][1] > int(time.time())):
                conn.rollback()
                return False
            conn.execute(
                """UPDATE fee_entries SET timestamp = ?
                   WHERE sig = ? AND txid IS NULL AND kind = ?""",
                (chain_timestamp, source_signature, fee_kind),
            )
        elif fee_units:
            conn.execute(
                """INSERT INTO fee_entries
                   (sig, txid, kind, amount_usdc_units, amount_usdd_units,
                    contract_id, timestamp)
                   VALUES (?, NULL, ?, ?, NULL, -1, ?)""",
                (source_signature, fee_kind, fee_units, chain_timestamp),
            )

        conn.execute(
            f"""UPDATE {details['table']}
                SET {signature_column} = ?, status = ? WHERE sig = ?""",
            (payout_signature, details["terminal_status"], source_signature),
        )
        if pending is not None:
            deleted = conn.execute(
                """DELETE FROM unprocessed_sigs
                   WHERE sig = ? AND timestamp = ? AND COALESCE(memo, '') = ?
                     AND from_address = ? AND amount_usdc_units = ? AND status = ?
                     AND txid IS NULL AND reference IS NULL AND amount_usdd_units IS NULL""",
                (
                    source_signature, source_timestamp, source_memo, source_token_account,
                    source_amount_solana_units, pending[4],
                ),
            ).rowcount
            if deleted != 1:
                raise RuntimeError("exact Solana disposition source changed during reconstruction")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_solana_sig_disposition_evidence(
    *, source_sig: str, kind: str, payout_signature: str,
) -> tuple[str, int, str] | None:
    """Return frozen recipient, output and memo for one pending durable disposition.

    Rows written before recipient freezing intentionally return ``None``: their old
    status/signature record cannot prove which token account was paid.
    """
    source_sig, details = _solana_sig_disposition(source_sig, kind)
    payout_signature = _require_solana_payout_budget_text(payout_signature, "signature")
    conn = sqlite3.connect(DB_PATH)
    try:
        signature_column = details["signature_column"]
        units_column = details["units_column"]
        row = conn.execute(
            f"""SELECT {signature_column}, destination_address, {units_column}, payout_memo, status
               FROM {details['table']} WHERE sig = ?""", (source_sig,),
        ).fetchone()
        if row is None:
            return None
        recorded_signature, destination, payout_units, memo, status = row
        if (recorded_signature != payout_signature or not isinstance(destination, str)
                or not destination or type(payout_units) is not int or payout_units <= 0
                or not isinstance(memo, str) or status != "awaiting confirmation"):
            return None
        return destination, payout_units, memo
    finally:
        conn.close()


def prepare_solana_sig_disposition(
    *, source_sig: str, kind: str, timestamp: int, from_address: str,
    destination_address: str, amount_usdc_units: int, memo: str | None, payout_memo: str,
    payout_units: int, cap_units: int,
) -> bool:
    """Atomically freeze and cap-reserve one refund/quarantine before Solana RPC.

    Once this returns true, the source is deliberately held in a durable pre-submit
    state.  A process loss before a returned signature is an unknown outcome, not
    permission to resend or to release the cap reservation.
    """
    source_sig, details = _solana_sig_disposition(source_sig, kind)
    timestamp = _require_solana_payout_budget_units(timestamp, "source timestamp")
    from_address = _require_solana_payout_budget_text(from_address, "source address")
    destination_address = _require_solana_payout_budget_text(
        destination_address, "destination address"
    )
    payout_memo = _require_solana_payout_budget_text(payout_memo, "payout memo", 1024)
    amount_usdc_units = _require_solana_payout_budget_units(amount_usdc_units, "source amount")
    payout_units = _require_solana_payout_budget_units(payout_units, "amount")
    cap_units = _require_solana_payout_budget_units(cap_units, "cap", positive=False)
    memo = _canonical_solana_source_memo(memo)
    obligation_id = _solana_sig_disposition_obligation_id(kind, source_sig)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT timestamp, memo, from_address, amount_usdc_units, status
               FROM unprocessed_sigs WHERE sig = ?""", (source_sig,)
        ).fetchone()
        if (row is None or row[0] != timestamp
                or _canonical_solana_source_memo(row[1]) != memo
                or row[2] != from_address
                or type(row[3]) is not int or row[3] != amount_usdc_units
                or row[4] not in details["ready_statuses"]):
            conn.commit()
            return False
        # One incoming deposit can never authorize both a refund and a quarantine send.
        if any(conn.execute(f"SELECT 1 FROM {table} WHERE sig = ?", (source_sig,)).fetchone()
               for table in ("refunded_sigs", "quarantined_sigs")):
            conn.commit()
            return False
        if not _reserve_solana_payout_budget_in_transaction(
            conn, obligation_id=obligation_id, kind=details["budget_kind"],
            amount_usdc_units=payout_units, cap_units=cap_units, window_sec=86400,
            now=int(time.time()),
        ):
            conn.commit()
            return False
        signature_column = details["signature_column"]
        units_column = details["units_column"]
        conn.execute(
            f"""INSERT INTO {details['table']}
               (sig, timestamp, from_address, destination_address, amount_usdc_units, memo,
                payout_memo, {signature_column}, {units_column}, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'submitting')""",
            (source_sig, timestamp, from_address, destination_address, amount_usdc_units,
             memo, payout_memo, payout_units),
        )
        updated = conn.execute(
            """UPDATE unprocessed_sigs SET status = ?
               WHERE sig = ? AND status IN (""" + ", ".join("?" for _ in details["ready_statuses"]) + ")",
            (details["held_status"], source_sig, *details["ready_statuses"]),
        ).rowcount
        if updated != 1:
            raise RuntimeError("Solana disposition source changed during preparation")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_solana_sig_disposition_submission(
    *, source_sig: str, kind: str, payout_signature: str,
) -> bool:
    """Atomically append a returned Solana signature and advance its held source."""
    source_sig, details = _solana_sig_disposition(source_sig, kind)
    payout_signature = _require_solana_payout_budget_text(payout_signature, "signature")
    obligation_id = _solana_sig_disposition_obligation_id(kind, source_sig)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        reserved = conn.execute(
            """SELECT amount_usdc_units FROM solana_payout_budget_events
               WHERE obligation_id = ? AND event = 'reserved'""", (obligation_id,)
        ).fetchone()
        if reserved is None:
            conn.commit()
            return False
        signature_column = details["signature_column"]
        row = conn.execute(
            f"""SELECT {signature_column}, status FROM {details['table']} WHERE sig = ?""",
            (source_sig,),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        existing_signature, existing_status = row
        if existing_signature is not None or existing_status != "submitting":
            conn.commit()
            return existing_signature == payout_signature and existing_status == "awaiting confirmation"
        existing_submission = conn.execute(
            """SELECT signature FROM solana_payout_budget_events
               WHERE obligation_id = ? AND event = 'submitted'""", (obligation_id,)
        ).fetchone()
        if existing_submission is not None:
            conn.commit()
            return existing_submission[0] == payout_signature
        conn.execute(
            """INSERT INTO solana_payout_budget_events
               (obligation_id, kind, event, amount_usdc_units, signature, evidence, timestamp)
               VALUES (?, ?, 'submitted', ?, ?, NULL, ?)""",
            (obligation_id, details["budget_kind"], reserved[0], payout_signature, int(time.time())),
        )
        conn.execute(
            f"""UPDATE {details['table']} SET {signature_column} = ?, status = 'awaiting confirmation'
               WHERE sig = ? AND status = 'submitting' AND {signature_column} IS NULL""",
            (payout_signature, source_sig),
        )
        updated = conn.execute(
            "UPDATE unprocessed_sigs SET status = ? WHERE sig = ? AND status = ?",
            (details["awaiting_status"], source_sig, details["held_status"]),
        ).rowcount
        if updated != 1:
            raise RuntimeError("Solana disposition source changed during submission recording")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_solana_sig_disposition(
    *, source_sig: str, kind: str, payout_signature: str,
) -> bool | None:
    """Settle a new disposition reservation only with its exact confirmed signature.

    ``None`` means the row predates the durable disposition protocol and must use the
    legacy confirmation compatibility path.  ``False`` is a conflicting/incomplete
    durable row and must remain held.
    """
    source_sig, details = _solana_sig_disposition(source_sig, kind)
    payout_signature = _require_solana_payout_budget_text(payout_signature, "signature")
    obligation_id = _solana_sig_disposition_obligation_id(kind, source_sig)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        signature_column = details["signature_column"]
        units_column = details["units_column"]
        row = conn.execute(
            f"""SELECT {signature_column}, {units_column}, amount_usdc_units, status
               FROM {details['table']} WHERE sig = ?""", (source_sig,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        recorded_signature, payout_units, source_units, status = row
        has_reservation = conn.execute(
            """SELECT 1 FROM solana_payout_budget_events
               WHERE obligation_id = ? AND event = 'reserved'""", (obligation_id,)
        ).fetchone() is not None
        if not has_reservation:
            conn.commit()
            return None
        if (recorded_signature != payout_signature or type(payout_units) is not int
                or payout_units <= 0 or type(source_units) is not int
                or source_units < payout_units):
            conn.commit()
            return False
        if status == details["terminal_status"]:
            confirmed = conn.execute(
                """SELECT signature, amount_usdc_units FROM solana_payout_budget_events
                   WHERE obligation_id = ? AND event = 'confirmed'""", (obligation_id,)
            ).fetchone()
            conn.commit()
            return confirmed == (payout_signature, payout_units)
        if status != "awaiting confirmation":
            conn.commit()
            return False
        if not _settle_solana_payout_budget_in_transaction(
            conn, obligation_id=obligation_id, signature=payout_signature,
            amount_usdc_units=payout_units,
        ):
            conn.commit()
            return False
        fee_units = source_units - payout_units
        if fee_units:
            fee_kind = f"{kind}_flat_fee"
            existing_fee = conn.execute(
                """SELECT amount_usdc_units FROM fee_entries
                   WHERE sig = ? AND txid IS NULL AND kind = ?""",
                (source_sig, fee_kind),
            ).fetchall()
            if existing_fee and existing_fee != [(fee_units,)]:
                raise RuntimeError("Solana disposition fee evidence conflicts with frozen payout")
            if not existing_fee:
                conn.execute(
                    """INSERT INTO fee_entries
                       (sig, txid, kind, amount_usdc_units, amount_usdd_units, contract_id, timestamp)
                       VALUES (?, NULL, ?, ?, NULL, -1, ?)""",
                    (source_sig, fee_kind, fee_units, int(time.time())),
                )
        conn.execute(
            f"UPDATE {details['table']} SET status = ? WHERE sig = ?",
            (details["terminal_status"], source_sig),
        )
        deleted = conn.execute(
            "DELETE FROM unprocessed_sigs WHERE sig = ? AND status = ?",
            (source_sig, details["awaiting_status"]),
        ).rowcount
        if deleted != 1:
            raise RuntimeError("Solana disposition source changed during confirmation")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_payout(kind: str, amount_usdc_units: int, reference: str | None = None):
    """Log an outbound Solana-side payment for rolling-cap accounting."""
    import time as _time
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO payouts (kind, amount_usdc_units, reference, timestamp) VALUES (?, ?, ?, ?)",
        (kind, int(amount_usdc_units or 0), reference, int(_time.time())),
    )
    conn.commit()
    conn.close()


def payouts_since(seconds: int) -> int:
    """Total outbound Solana base units paid in the last `seconds`."""
    import time as _time
    cutoff = int(_time.time()) - int(seconds)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(SUM(amount_usdc_units), 0) FROM payouts WHERE timestamp >= ?", (cutoff,))
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row and row[0] else 0

def is_quarantined_txid(txid: str, contract_id: int | None = None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if contract_id is None:
        cursor.execute("SELECT 1 FROM quarantined_txids WHERE txid = ?", (txid,))
    else:
        cursor.execute(
            "SELECT 1 FROM quarantined_txids WHERE txid = ? AND contract_id = ?",
            (txid, contract_id),
        )
    result = cursor.fetchone()
    conn.close()
    return result is not None

## Accounts

def insert_account(nickname: str, chain: str, ticker: str, name: str, address: str, balance: float, timestamp: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO accounts (nickname, chain, ticker, name, address, balance, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (nickname, chain, ticker, name, address, balance, timestamp))
    conn.commit()
    conn.close()

def get_account(nickname: str) -> Optional[Tuple[str, str, str, str, str, float, int]]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts WHERE nickname = ?", (nickname,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_account_balance_timestamp(nickname: str, balance: float, timestamp: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE accounts
        SET balance = ?, timestamp = ?
        WHERE nickname = ?
    """, (balance, timestamp, nickname))
    conn.commit()
    conn.close()


## Heartbeat

def insert_heartbeat(name: str, last_beat: int, wline_sol: int, wline_nxs: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO heartbeat (name, last_beat, wline_sol, wline_nxs)
        VALUES (?, ?, ?, ?)
    """, (name, last_beat, wline_sol, wline_nxs))
    conn.commit()
    conn.close()

def get_heartbeat(name: str) -> Optional[Tuple[str, int, int, int]]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM heartbeat WHERE name = ?", (name,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_heartbeat(name: str, last_beat: int | None = None, wline_sol: int | None = None, wline_nxs: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE heartbeat
        SET last_beat = COALESCE(?, last_beat),
            wline_sol = COALESCE(?, wline_sol),
            wline_nxs = COALESCE(?, wline_nxs)
        WHERE name = ?
    """, (last_beat, wline_sol, wline_nxs, name))
    conn.commit()
    conn.close()


## Reservations (for preventing duplicate processing)

def reserve_action(kind: str, key: str, ttl_sec: int = 300) -> bool:
    """Reserve an action to prevent duplicate processing.
    
    Args:
        kind: Type of action (e.g., 'debit', 'credit', 'refund')
        key: Unique identifier (e.g., signature, txid)
        ttl_sec: Time-to-live in seconds (default 300s = 5min)
    
    Returns:
        True if reservation was successful (not already reserved or expired reservation),
        False if already reserved by another process.
    """
    import time
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # First clean up expired reservations
    cursor.execute("""
        DELETE FROM reservations 
        WHERE timestamp < ?
    """, (now - ttl_sec,))
    
    # Try to insert reservation
    try:
        cursor.execute("""
            INSERT INTO reservations (kind, key, timestamp)
            VALUES (?, ?, ?)
        """, (kind, key, now))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        # Already reserved
        conn.close()
        return False


def release_reservation(kind: str, key: str):
    """Release a reservation."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM reservations 
        WHERE kind = ? AND key = ?
    """, (kind, key))
    conn.commit()
    conn.close()


def is_reserved(kind: str, key: str, ttl_sec: int = 300) -> bool:
    """Check if an action is currently reserved."""
    import time
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1 FROM reservations 
        WHERE kind = ? AND key = ? AND timestamp >= ?
    """, (kind, key, now - ttl_sec))
    result = cursor.fetchone()
    conn.close()
    return result is not None


def cleanup_expired_reservations(ttl_sec: int = 300):
    """Remove expired reservations (call periodically)."""
    import time
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM reservations 
        WHERE timestamp < ?
    """, (now - ttl_sec,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


## Attempts tracking (for retry logic)

def _attempt_row(action_key: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT count, last_timestamp FROM attempts WHERE action_key = ?", (action_key,))
    row = cursor.fetchone()
    conn.close()
    return row


def attempts_exhausted(action_key: str, max_attempts: int | None = None) -> bool:
    """True when the action has used up its retry budget (terminal - quarantine/refund)."""
    if max_attempts is None:
        try:
            from . import config as _cfg
            max_attempts = int(getattr(_cfg, "MAX_ACTION_ATTEMPTS", 3))
        except Exception:
            max_attempts = 3
    row = _attempt_row(action_key)
    return bool(row and row[0] >= max_attempts)


def should_attempt(action_key: str, max_attempts: int | None = None,
                   cooldown_sec: int | None = None) -> bool:
    """True if the action may be attempted RIGHT NOW.

    Two distinct reasons return False - the retry budget is spent, or the cooldown has
    not elapsed. Callers must not treat them alike: use attempts_exhausted() for the
    terminal decision, otherwise simply retry on a later cycle.

    Previously this compared the counter only, so ACTION_RETRY_COOLDOWN_SEC - documented
    in README/SECURITY.md as the defence against fee-draining retry loops - did nothing,
    and retries fired every poll interval.
    """
    import time as _time
    try:
        from . import config as _cfg
        if max_attempts is None:
            max_attempts = int(getattr(_cfg, "MAX_ACTION_ATTEMPTS", 3))
        if cooldown_sec is None:
            cooldown_sec = int(getattr(_cfg, "ACTION_RETRY_COOLDOWN_SEC", 300))
    except Exception:
        max_attempts = max_attempts or 3
        cooldown_sec = cooldown_sec or 300

    row = _attempt_row(action_key)
    if row is None:
        return True  # never attempted
    count, last_ts = row[0], row[1] or 0
    if count >= max_attempts:
        return False
    if last_ts and (int(_time.time()) - int(last_ts)) < int(cooldown_sec):
        return False  # cooling down, not exhausted
    return True


def record_attempt(action_key: str):
    """Increment attempt counter for an action."""
    import time
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Try to increment existing record
    cursor.execute("""
        UPDATE attempts 
        SET count = count + 1, last_timestamp = ?
        WHERE action_key = ?
    """, (now, action_key))
    
    # If no rows updated, insert new record
    if cursor.rowcount == 0:
        cursor.execute("""
            INSERT INTO attempts (action_key, count, last_timestamp)
            VALUES (?, 1, ?)
        """, (action_key, now))
    
    conn.commit()
    conn.close()


def get_attempt_count(action_key: str) -> int:
    """Get current attempt count for an action."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT count FROM attempts WHERE action_key = ?
    """, (action_key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def get_attempt_last_timestamp(action_key: str) -> int:
    """Unix time of the most recent recorded attempt (0 if none)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT last_timestamp FROM attempts WHERE action_key = ?", (action_key,))
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row and row[0] else 0


def reset_attempts(action_key: str):
    """Reset attempt counter for an action."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM attempts WHERE action_key = ?
    """, (action_key,))
    conn.commit()
    conn.close()


## Waterline proposals (ephemeral, cleared after applying)

def propose_solana_waterline(ts: int):
    """Store proposed Solana waterline timestamp."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO waterline_proposals (chain, proposed_timestamp)
        VALUES ('solana', ?)
    """, (ts,))
    conn.commit()
    conn.close()


def propose_nexus_waterline(ts: int):
    """Store proposed Nexus waterline timestamp."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO waterline_proposals (chain, proposed_timestamp)
        VALUES ('nexus', ?)
    """, (ts,))
    conn.commit()
    conn.close()


def get_proposed_solana_waterline() -> int | None:
    """Get proposed Solana waterline."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT proposed_timestamp FROM waterline_proposals 
        WHERE chain = 'solana'
    """)
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_proposed_nexus_waterline() -> int | None:
    """Get proposed Nexus waterline."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT proposed_timestamp FROM waterline_proposals 
        WHERE chain = 'nexus'
    """)
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_and_clear_proposed_waterlines() -> tuple[int | None, int | None]:
    """Get proposed waterlines and clear them atomically.
    
    Returns:
        (solana_waterline, nexus_waterline) tuple
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get Solana waterline
    cursor.execute("""
        SELECT proposed_timestamp FROM waterline_proposals 
        WHERE chain = 'solana'
    """)
    sol_row = cursor.fetchone()
    sol_wl = sol_row[0] if sol_row else None
    
    # Get Nexus waterline
    cursor.execute("""
        SELECT proposed_timestamp FROM waterline_proposals 
        WHERE chain = 'nexus'
    """)
    nxs_row = cursor.fetchone()
    nxs_wl = nxs_row[0] if nxs_row else None
    
    # Clear both
    cursor.execute("DELETE FROM waterline_proposals")
    
    conn.commit()
    conn.close()
    return (sol_wl, nxs_wl)


def clear_waterline_proposals():
    """Clear all waterline proposals."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM waterline_proposals")
    conn.commit()
    conn.close()


def prepare_nexus_payout(
    *,
    txid: str,
    contract_id: int,
    receival_account: str,
    amount_usdd_units: int,
    payout_solana_units: int,
    payout_fee_nexus_units: int,
    payout_cap_solana_units: int | None = None,
    payout_cap_hold_status: str | None = None,
    payout_cap_hold_reason: str | None = None,
) -> bool:
    """Atomically freeze, cap-reserve, and claim one exact Nexus credit for RPC."""
    txid = str(txid or "").strip()
    receival_account = str(receival_account or "").strip()
    if not txid or not receival_account:
        raise ValueError("Nexus payout requires a txid and receival account")
    if type(contract_id) is not int or contract_id < 0:
        raise ValueError("Nexus payout contract id must be a nonnegative integer")
    if type(amount_usdd_units) is not int or amount_usdd_units <= 0:
        raise ValueError("Nexus payout source units must be an exact positive integer")
    if type(payout_solana_units) is not int or payout_solana_units <= 0:
        raise ValueError("Nexus payout units must be an exact positive integer")
    if (type(payout_fee_nexus_units) is not int
            or payout_fee_nexus_units < 0
            or payout_fee_nexus_units > amount_usdd_units):
        raise ValueError("Nexus payout fee must be exact, nonnegative, and no more than source")
    if payout_cap_solana_units is not None:
        _require_solana_payout_budget_units(
            payout_cap_solana_units, "cap", positive=False
        )
    if (payout_cap_hold_status is None) != (payout_cap_hold_reason is None):
        raise ValueError("Nexus payout cap hold status and reason must be provided together")
    if payout_cap_hold_status is not None:
        if payout_cap_hold_reason is None:
            raise ValueError("Nexus payout cap hold reason is required")
        payout_cap_hold_status = _require_solana_payout_budget_text(
            payout_cap_hold_status, "cap hold status", 200
        )
        payout_cap_hold_reason = _require_solana_payout_budget_text(
            payout_cap_hold_reason, "cap hold reason", 500
        )

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        if _has_nexus_fee_evidence(conn, txid, contract_id):
            conn.commit()
            return False
        if conn.execute(
            """SELECT 1 FROM nexus_transfer_intents
               WHERE source_txid = ? AND (source_contract_id = -1 OR source_contract_id = ?)
               LIMIT 1""",
            (txid, contract_id),
        ).fetchone():
            conn.commit()
            return False
        for table in ("processed_txids", "refunded_txids", "quarantined_txids"):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE txid = ? AND contract_id = ?",
                (txid, contract_id),
            ).fetchone():
                conn.commit()
                return False
        row = conn.execute(
            """SELECT status, receival_account, amount_usdd_units,
                      payout_solana_units, payout_fee_nexus_units
               FROM unprocessed_txids WHERE txid = ? AND contract_id = ?""",
            (txid, contract_id),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        status, stored_destination, stored_source_units, stored_payout, stored_fee = row
        if (status not in ("ready for processing", payout_cap_hold_status)
                or stored_destination != receival_account
                or type(stored_source_units) is not int
                or stored_source_units != amount_usdd_units
                or stored_payout is not None
                or stored_fee is not None):
            conn.commit()
            return False
        if payout_cap_solana_units is not None and not _reserve_solana_payout_budget_in_transaction(
            conn,
            obligation_id=f"nexus:{txid}:{contract_id}",
            kind="nexus_payout",
            amount_usdc_units=payout_solana_units,
            cap_units=payout_cap_solana_units,
            window_sec=86400,
            now=int(time.time()),
        ):
            if payout_cap_hold_status is not None:
                held = conn.execute(
                    """UPDATE unprocessed_txids SET status = ?, hold_reason = ?
                       WHERE txid = ? AND contract_id = ?
                         AND status IN ('ready for processing', ?)
                         AND payout_solana_units IS NULL AND payout_fee_nexus_units IS NULL""",
                    (payout_cap_hold_status, payout_cap_hold_reason, txid, contract_id,
                     payout_cap_hold_status),
                ).rowcount
                if held != 1:
                    raise RuntimeError("Nexus payout source changed during cap hold")
            conn.commit()
            return False
        updated = conn.execute(
            """UPDATE unprocessed_txids
               SET payout_solana_units = ?, payout_fee_nexus_units = ?, status = 'sending', hold_reason = NULL
               WHERE txid = ? AND contract_id = ? AND status IN ('ready for processing', ?)
                     AND payout_solana_units IS NULL AND payout_fee_nexus_units IS NULL""",
            (payout_solana_units, payout_fee_nexus_units, txid, contract_id, payout_cap_hold_status),
        ).rowcount
        conn.commit()
        return updated == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_nexus_credit(
    *,
    txid: str,
    contract_id: int,
    timestamp: int,
    amount_usdd: float,
    amount_usdd_units: int,
    from_address: str,
    to_address: str,
    owner: str,
    sig: str,
    status: str,
    fee_kind: str | None,
    fee_nexus_units: int,
) -> bool:
    """Atomically journal a source fee, archive the exact credit, and remove its queue row."""
    txid = str(txid or "").strip()
    from_address = str(from_address or "")
    to_address = str(to_address or "")
    owner = str(owner or "")
    if not txid or not from_address or not to_address:
        raise ValueError("Nexus credit terminal evidence requires source identity and addresses")
    if type(contract_id) is not int or contract_id < 0:
        raise ValueError("Nexus credit contract id must be a nonnegative integer")
    if type(amount_usdd_units) is not int or amount_usdd_units <= 0:
        raise ValueError("Nexus credit source units must be an exact positive integer")
    if (type(fee_nexus_units) is not int
            or fee_nexus_units < 0
            or fee_nexus_units > amount_usdd_units):
        raise ValueError("Nexus credit fee must be exact, nonnegative, and no more than source")
    if not isinstance(sig, str):
        raise ValueError("Nexus credit payout signature must be text")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("Nexus credit terminal status is required")
    if fee_nexus_units == 0:
        if fee_kind is not None:
            raise ValueError("zero Nexus fee requires fee_kind=None")
    elif not isinstance(fee_kind, str) or not fee_kind.strip():
        raise ValueError("positive Nexus fee requires a fee kind")
    if not sig:
        if fee_nexus_units != amount_usdd_units or "fee" not in status.lower():
            raise ValueError("positive-payout Nexus terminal evidence requires a signature")

    terminal_base = (
        txid, contract_id, timestamp, amount_usdd, amount_usdd_units,
        from_address, to_address, owner, sig, status,
    )
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            """SELECT 1 FROM fee_entries
               WHERE txid = ? AND contract_id = -1 AND amount_usdd_units IS NOT NULL
               LIMIT 1""",
            (txid,),
        ).fetchone():
            conn.commit()
            return False
        if conn.execute(
            """SELECT 1 FROM nexus_transfer_intents
               WHERE source_txid = ? AND (source_contract_id = -1 OR source_contract_id = ?)
               LIMIT 1""",
            (txid, contract_id),
        ).fetchone():
            conn.commit()
            return False
        existing_terminal = conn.execute(
            """SELECT txid, contract_id, timestamp, amount_usdd, amount_usdd_units,
                      from_address, to_address, owner, sig, status,
                      payout_solana_units, payout_fee_nexus_units,
                      payout_receival_account
               FROM processed_txids WHERE txid = ? AND contract_id = ?""",
            (txid, contract_id),
        ).fetchone()
        source = conn.execute(
            """SELECT timestamp, amount_usdd, amount_usdd_units, from_address,
                      to_address, owner_from_address, receival_account,
                      payout_solana_units, payout_fee_nexus_units, sig
               FROM unprocessed_txids WHERE txid = ? AND contract_id = ?""",
            (txid, contract_id),
        ).fetchone()
        expected_source = (
            timestamp, amount_usdd, amount_usdd_units, from_address, to_address, owner,
        )
        if source is not None and source[:6] != expected_source:
            conn.commit()
            return False

        payout_evidence: tuple[int | None, int | None, str | None]
        if sig:
            if source is not None:
                payout_evidence = (source[7], source[8], source[6])
            elif existing_terminal is not None:
                payout_evidence = existing_terminal[10:13]
            else:
                conn.commit()
                return False
            payout_units, frozen_fee_units, payout_destination = payout_evidence
            pending_sig = source[9] if source is not None else sig
            if (pending_sig != sig
                    or type(payout_units) is not int or payout_units <= 0
                    or type(frozen_fee_units) is not int or frozen_fee_units < 0
                    or frozen_fee_units > amount_usdd_units
                    or frozen_fee_units != fee_nexus_units
                    or not isinstance(payout_destination, str)
                    or not payout_destination):
                conn.commit()
                return False
        else:
            payout_evidence = (None, None, None)
        terminal = terminal_base + payout_evidence

        fee_rows = conn.execute(
            """SELECT kind, amount_usdd_units FROM fee_entries
               WHERE txid = ? AND contract_id = ? AND amount_usdd_units IS NOT NULL""",
            (txid, contract_id),
        ).fetchall()
        expected_fee = ((fee_kind, fee_nexus_units),) if fee_nexus_units > 0 else ()
        if tuple(fee_rows) != expected_fee and fee_rows:
            conn.commit()
            return False
        if existing_terminal is not None:
            exact = existing_terminal == terminal and tuple(fee_rows) == expected_fee
            conn.commit()
            return exact
        for table in ("refunded_txids", "quarantined_txids"):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE txid = ? AND contract_id = ?",
                (txid, contract_id),
            ).fetchone():
                conn.commit()
                return False
        if fee_nexus_units > 0 and not fee_rows:
            conn.execute(
                """INSERT INTO fee_entries
                   (sig, txid, kind, amount_usdc_units, amount_usdd_units, contract_id, timestamp)
                   VALUES (NULL, ?, ?, NULL, ?, ?, ?)""",
                (txid, fee_kind, fee_nexus_units, contract_id, int(time.time())),
            )
        if sig:
            obligation_id = f"nexus:{txid}:{contract_id}"
            budget_reserved = conn.execute(
                """SELECT 1 FROM solana_payout_budget_events
                   WHERE obligation_id = ? AND event = 'reserved'""",
                (obligation_id,),
            ).fetchone() is not None
            if budget_reserved and not _settle_solana_payout_budget_in_transaction(
                conn,
                obligation_id=obligation_id,
                signature=sig,
                amount_usdc_units=int(payout_evidence[0]),
            ):
                conn.commit()
                return False
        conn.execute(
            """INSERT INTO processed_txids
               (txid, contract_id, timestamp, amount_usdd, amount_usdd_units,
                from_address, to_address, owner, sig, status, payout_solana_units,
                payout_fee_nexus_units, payout_receival_account)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            terminal,
        )
        deleted = conn.execute(
            "DELETE FROM unprocessed_txids WHERE txid = ? AND contract_id = ?",
            (txid, contract_id),
        ).rowcount
        if source is not None and deleted != 1:
            raise RuntimeError("exact pending Nexus credit changed during finalization")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_processed_nexus_credit(txid: str, contract_id: int) -> dict | None:
    """Return complete terminal evidence for one exact nonlegacy Nexus source credit."""
    if type(contract_id) is not int or contract_id < 0:
        raise ValueError("Nexus credit contract id must be a nonnegative integer")
    columns = (
        "txid", "contract_id", "timestamp", "amount_usdd", "amount_usdd_units",
        "from_address", "to_address", "owner", "sig", "status",
        "payout_solana_units", "payout_fee_nexus_units", "payout_receival_account",
    )
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT " + ", ".join(columns)
            + " FROM processed_txids WHERE txid = ? AND contract_id = ?",
            (str(txid or ""), contract_id),
        ).fetchone()
        return dict(zip(columns, row)) if row is not None else None
    finally:
        conn.close()


## Fee tracking

def add_fee_entry(sig: str | None, txid: str | None, kind: str, amount_usdc_units: int | None = None, amount_usdd_units: int | None = None):
    """Add a fee entry to the journal.
    
    Args:
        sig: Solana signature (for Solana->Nexus fees)
        txid: Nexus txid (for Nexus->Solana fees)
        kind: Type of fee ('flat', 'dynamic', 'swap', etc.)
        amount_usdc_units: Fee amount in Solana base units
        amount_usdd_units: Fee amount in Nexus-side base units
    """
    import time
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO fee_entries (sig, txid, kind, amount_usdc_units, amount_usdd_units, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (sig, txid, kind, amount_usdc_units, amount_usdd_units, now))
    conn.commit()
    conn.close()


def get_fee_entries(limit: int = 1000, kind: str | None = None) -> List[Tuple]:
    """Get recent fee entries.
    
    Args:
        limit: Max number of entries to return
        kind: Optional filter by fee kind
    
    Returns:
        List of tuples: (id, sig, txid, kind, amount_usdc_units, amount_usdd_units, timestamp)
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if kind:
        cursor.execute("""
            SELECT id, sig, txid, kind, amount_usdc_units, amount_usdd_units, timestamp
            FROM fee_entries
            WHERE kind = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (kind, limit))
    else:
        cursor.execute("""
            SELECT id, sig, txid, kind, amount_usdc_units, amount_usdd_units, timestamp
            FROM fee_entries
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_total_fees_collected() -> Tuple[int, int]:
    """Get total fees collected.
    
    Returns:
        (total_solana_units, total_nexus_units) tuple
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            COALESCE(SUM(amount_usdc_units), 0) as total_solana,
            COALESCE(SUM(amount_usdd_units), 0) as total_nexus
        FROM fee_entries
    """)
    row = cursor.fetchone()
    conn.close()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def update_fee_summary():
    """Update aggregated fee summary (call periodically)."""
    import time
    now = int(time.time())
    total_solana, total_nexus = get_total_fees_collected()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO fee_summary (id, total_collected_usdc, total_collected_usdd, last_updated)
        VALUES (1, ?, ?, ?)
    """, (total_solana, total_nexus, now))  # column names frozen: see the header block
    conn.commit()
    conn.close()


## Helper: next reference number

def next_reference() -> int:
    """Get next unique reference number for Nexus debits.
    
    Uses atomic increment in counters table to ensure uniqueness even when
    multiple debits are processed in the same loop iteration.
    Falls back to MAX(reference) from processed_sigs on first use.
    
    Returns:
        Next reference number (1-based)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.isolation_level = None  # manual transaction control
    cursor = conn.cursor()
    cursor.execute("PRAGMA busy_timeout=5000")
    # Serialize reference generation across connections so two callers cannot read
    # the same value (which would duplicate/skip Nexus debit references).
    cursor.execute("BEGIN IMMEDIATE")

    # Try to increment existing counter atomically
    cursor.execute("""
        UPDATE counters SET value = value + 1 WHERE name = 'reference'
    """)
    
    if cursor.rowcount == 0:
        # Counter doesn't exist yet - initialize from processed_sigs or start at 1
        cursor.execute("""
            SELECT MAX(reference) FROM processed_sigs WHERE reference IS NOT NULL
        """)
        row = cursor.fetchone()
        current_max = row[0] if row and row[0] is not None else 0
        next_ref = current_max + 1
        
        # Insert initial counter value
        cursor.execute("""
            INSERT OR REPLACE INTO counters (name, value) VALUES ('reference', ?)
        """, (next_ref,))
        conn.commit()
        conn.close()
        return next_ref
    
    # Get the updated value
    cursor.execute("SELECT value FROM counters WHERE name = 'reference'")
    row = cursor.fetchone()
    next_ref = row[0] if row else 1
    
    conn.commit()
    conn.close()
    return next_ref


## Helper: finalize refund (mark as refunded and remove from unprocessed)

def finalize_refund(sig: str, reason: str = "refunded"):
    """Finalize a refund: move from unprocessed to refunded, update status.
    
    Args:
        sig: Signature to finalize refund for
        reason: Refund reason/status (default: 'refunded')
    """
    import time
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get unprocessed record
    cursor.execute("""
        SELECT timestamp, from_address, amount_usdc_units, memo 
        FROM unprocessed_sigs 
        WHERE sig = ?
    """, (sig,))
    row = cursor.fetchone()
    
    if row:
        ts, from_addr, amount, memo = row
        # Insert into refunded_sigs
        cursor.execute("""
            INSERT OR REPLACE INTO refunded_sigs 
            (sig, timestamp, from_address, amount_usdc_units, memo, refund_sig, refunded_units, status)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
        """, (sig, ts or now, from_addr, amount, memo, reason))
        
        # Remove from unprocessed
        cursor.execute("DELETE FROM unprocessed_sigs WHERE sig = ?", (sig,))
    
    conn.commit()
    conn.close()


def is_refunded(sig: str) -> bool:
    """Check if signature was refunded (convenience wrapper)."""
    return is_refunded_sig(sig)


## Get unprocessed txids

def add_unprocessed_txid(
    txid: str,
    contract_id: int = -1,
    timestamp: int | None = None,
    amount_usdd: float | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    owner_from_address: str | None = None,
    confirmations_credit: int | None = None,
    status: str | None = None,
    receival_account: str | None = None,
    sig: str | None = None,
    amount_usdd_units: int | None = None,
    hold_reason: str | None = None,
) -> None:
    """Add or update an unprocessed txid."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO unprocessed_txids
        (txid, contract_id, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account, sig, amount_usdd_units, hold_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (txid, contract_id, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account, sig, amount_usdd_units, hold_reason))
    conn.commit()
    conn.close()


def get_unprocessed_txids(limit: int = 1000) -> List[Tuple]:
    """Get unprocessed Nexus txids.
    
    Returns:
        List of tuples: (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account)
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT txid, contract_id, timestamp, amount_usdd, from_address, to_address,
               owner_from_address, confirmations_credit, status, receival_account, sig,
               amount_usdd_units, hold_reason, payout_solana_units, payout_fee_nexus_units
        FROM unprocessed_txids
        ORDER BY timestamp ASC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def update_unprocessed_txid(
    txid: str,
    contract_id: int = -1,
    timestamp: int | None = None,
    amount_usdd: float | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    owner_from_address: str | None = None,
    confirmations_credit: int | None = None,
    status: str | None = None,
    receival_account: str | None = None,
    sig: str | None = None,
    hold_reason: str | None = None,
):
    """Update specific fields of an unprocessed txid."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    fields = []
    values = []
    
    if timestamp is not None:
        fields.append("timestamp = ?")
        values.append(timestamp)
    if amount_usdd is not None:
        fields.append("amount_usdd = ?")
        values.append(amount_usdd)
    if from_address is not None:
        fields.append("from_address = ?")
        values.append(from_address)
    if to_address is not None:
        fields.append("to_address = ?")
        values.append(to_address)
    if owner_from_address is not None:
        fields.append("owner_from_address = ?")
        values.append(owner_from_address)
    if confirmations_credit is not None:
        fields.append("confirmations_credit = ?")
        values.append(confirmations_credit)
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if receival_account is not None:
        fields.append("receival_account = ?")
        values.append(receival_account)
    if sig is not None:
        fields.append("sig = ?")
        values.append(sig)
    if hold_reason is not None:
        fields.append("hold_reason = ?")
        values.append(hold_reason)

    if not fields:
        conn.close()
        return
    
    values.extend((txid, contract_id))
    sql = f"UPDATE unprocessed_txids SET {', '.join(fields)} WHERE txid = ? AND contract_id = ?"
    cursor.execute(sql, tuple(values))
    conn.commit()
    conn.close()


def remove_unprocessed_txid(txid: str, contract_id: int = -1):
    """Remove an unprocessed txid."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM unprocessed_txids WHERE txid = ? AND contract_id = ?", (txid, contract_id))
    conn.commit()
    conn.close()


def is_processed_txid(txid: str, contract_id: int | None = None) -> bool:
    """Check terminal evidence by exact identity, or any legacy txid when omitted."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if contract_id is None:
        cursor.execute("SELECT 1 FROM processed_txids WHERE txid = ?", (txid,))
    else:
        cursor.execute(
            "SELECT 1 FROM processed_txids WHERE txid = ? AND contract_id = ?",
            (txid, contract_id),
        )
    result = cursor.fetchone()
    conn.close()
    return result is not None


## Vault balance tracking (for Solana polling optimization)

def save_last_vault_balance(balance: int, ticker: str | None = None):
    """Save last known vault balance for delta calculation.

    `ticker` is a display label only. It defaults from the environment rather than from
    `config` so this module stays free of the chain-dependent import.
    """
    label = ticker or os.getenv("SOLANA_TOKEN_SYMBOL", "USDC")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO accounts (nickname, chain, ticker, name, address, balance, timestamp)
        VALUES ('vault_last_balance', 'solana', ?, 'Last Vault Balance', '', ?, ?)
    """, (label, float(balance), int(__import__('time').time())))
    conn.commit()
    conn.close()


def load_last_vault_balance() -> int:
    """Load last known vault balance."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT balance FROM accounts WHERE nickname = 'vault_last_balance'
    """)
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 0


## Dict-based accessors for easier migration from JSONL

def get_unprocessed_txids_as_dicts(limit: int = 1000) -> list[dict]:
    """Get unprocessed Nexus txids as list of dicts (compatible with old JSONL format).
    
    Returns:
        List of dicts with keys: txid, ts, amount_usdd, from, owner, confirmations, comment, receival_account
    """
    tuples = get_unprocessed_txids(limit)
    return [
        {
            "txid": t[0],
            "contract_id": t[1],
            "ts": t[2],
            "amount_usdd": t[3],
            "from": t[4],  # from_address
            "to": t[5],  # to_address
            "owner": t[6],  # owner_from_address
            "confirmations": t[7],  # confirmations_credit
            "comment": t[8],  # status
            "receival_account": t[9] if len(t) > 9 else None,
            "sig": t[10] if len(t) > 10 else None,
            "amount_usdd_units": t[11] if len(t) > 11 else None,
            "hold_reason": t[12] if len(t) > 12 else None,
            "payout_solana_units": t[13] if len(t) > 13 else None,
            "payout_fee_nexus_units": t[14] if len(t) > 14 else None,
        }
        for t in tuples
    ]


def get_processed_txids_as_dicts(limit: int = 1000) -> list[dict]:
    """Get processed Nexus txids as list of dicts (compatible with old JSONL format).
    
    Returns:
        List of dicts with keys: txid, ts, amount_usdd, from, owner, comment, sig
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT txid, timestamp, amount_usdd, from_address, to_address, owner, status, sig
        FROM processed_txids
        ORDER BY timestamp ASC
        LIMIT ?
    """, (limit,))
    tuples = cursor.fetchall()
    conn.close()
    
    return [
        {
            "txid": t[0],
            "ts": t[1],
            "amount_usdd": t[2],
            "from": t[3],  # from_address
            "to": t[4],  # to_address
            "owner": t[5],
            "comment": t[6],  # status
            "sig": t[7]
        }
        for t in tuples
    ]


# Removed: write_unprocessed_txids(), add_processed_txid_from_dict() and
# add_unprocessed_txid_from_dict() - leftovers from the JSONL-to-SQLite migration with no
# callers. Each rebuilt rows from a dict that had no `amount_usdd_units` and no `sig`, so
# calling one would drop the exact credited amount and, for write_unprocessed_txids(),
# DELETE the whole in-flight queue and lose the payout signature of every swap awaiting
# confirmation - which the confirmation pass needs to tell "already paid" from "never sent".
# Rows are written through add_unprocessed_txid()/update_unprocessed_txid(), which carry
# every column and update in place.

# Add similar functions for other state (e.g., nexus txids, fees)


## Metrics snapshot (operator dashboard)

def save_metrics_snapshot(vault_usdc_units: int | None, circulating_usdd_units: int | None,
                          paused: bool = False, payouts_24h_units: int | None = None,
                          fees_usdc_units: int | None = None, fees_usdd_units: int | None = None,
                          ratio_bps: int | None = None):
    """Persist the latest loop metrics for the dashboard to read.

    The two amount columns are on DIFFERENT scales - the vault in Solana base units, the
    circulating supply in Nexus base units - so dividing one by the other is only valid
    when the pair happens to share its decimals. This module has no access to config (by
    design), so the caller passes `ratio_bps` already computed on a single scale. The
    fallback below is kept only for callers that predate the argument, and is correct
    exactly when the decimals match.
    """
    import time as _time
    try:
        v = int(vault_usdc_units or 0)
        c = int(circulating_usdd_units or 0)
        if ratio_bps is None:
            ratio_bps = int((v * 10000) // c) if c > 0 else None
        else:
            ratio_bps = int(ratio_bps)
    except Exception:
        v, c, ratio_bps = 0, 0, None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO metrics_snapshot
        (id, timestamp, vault_usdc_units, circulating_usdd_units, ratio_bps, paused,
         payouts_24h_units, fees_usdc_units, fees_usdd_units)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (int(_time.time()), v, c, ratio_bps, 1 if paused else 0,
          payouts_24h_units, fees_usdc_units, fees_usdd_units))
    conn.commit()
    conn.close()


def get_metrics_snapshot() -> dict | None:
    """Latest metrics snapshot, or None if the service has not written one yet."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT timestamp, vault_usdc_units, circulating_usdd_units, ratio_bps, paused,
                   payouts_24h_units, fees_usdc_units, fees_usdd_units
            FROM metrics_snapshot WHERE id = 1
        """)
        row = cursor.fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    keys = ("timestamp", "vault_usdc_units", "circulating_usdd_units", "ratio_bps",
            "paused", "payouts_24h_units", "fees_usdc_units", "fees_usdd_units")
    return dict(zip(keys, row))
