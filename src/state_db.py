import hashlib
import os
import sqlite3
import time
from typing import List, Optional, Tuple

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
            reference INTEGER
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
            amount_usdc_units INTEGER,
            memo TEXT,
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
            amount_usdc_units INTEGER,
            memo TEXT,
            refund_sig TEXT,
            refunded_units INTEGER,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unprocessed_txids (
            txid TEXT PRIMARY KEY,
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
            hold_reason TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_txids (
            txid TEXT PRIMARY KEY,
            timestamp INTEGER,
            amount_usdd REAL,
            amount_usdd_units INTEGER,
            from_address TEXT,
            to_address TEXT,
            owner TEXT,
            sig TEXT,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS refunded_txids (
            txid TEXT PRIMARY KEY,
            timestamp INTEGER,
            amount_usdd REAL,
            from_address TEXT,
            to_address TEXT,
            owner_from_address TEXT,
            confirmations_credit INTEGER,
            status TEXT,
            sig TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quarantined_txids (
            txid TEXT PRIMARY KEY,
            timestamp INTEGER,
            amount_usdd REAL,
            from_address TEXT,
            to_address TEXT,
            owner TEXT,
            sig TEXT,
            status TEXT
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
    # credit may authorize exactly one remote debit: allowing one "refund" and one
    # "quarantine" intent for the same source would permit two dispositions of the
    # same funds. The source-only unique index below also migrates existing ledgers.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nexus_transfer_intents (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            source_txid TEXT NOT NULL,
            from_address TEXT NOT NULL,
            to_address TEXT NOT NULL,
            amount_usdd_units INTEGER NOT NULL,
            reference TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            remote_txid TEXT,
            created_timestamp INTEGER NOT NULL,
            last_attempt_timestamp INTEGER,
            resolved_timestamp INTEGER,
            UNIQUE(kind, source_txid)
        )
    """)
    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_nexus_transfer_intents_source "
            "ON nexus_transfer_intents(source_txid)"
        )
    except sqlite3.IntegrityError as exc:
        # Do not guess which pre-upgrade duplicate intent is safe. Refusing startup
        # preserves both records for manual chain-evidence resolution.
        conn.close()
        raise RuntimeError(
            "unsafe duplicate Nexus transfer intents share a source_txid; "
            "resolve them manually before starting the service"
        ) from exc
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_nexus_transfer_intents_status ON nexus_transfer_intents(status, created_timestamp)")
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
    
    # Fee tracking journal
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fee_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sig TEXT,
            txid TEXT,
            kind TEXT NOT NULL,
            amount_usdc_units INTEGER,
            amount_usdd_units INTEGER,
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

    conn.commit()
    conn.close()


# Nexus transfer intents -------------------------------------------------------
#
# A Nexus CLI timeout/non-zero result is not proof the node did not accept a
# debit.  These rows capture all debit inputs before invocation and make the
# persisted reference the only identifier used for post-crash resolution.
_NEXUS_TRANSFER_COLUMNS = (
    "id", "kind", "source_txid", "from_address", "to_address",
    "amount_usdd_units", "reference", "status", "remote_txid",
    "created_timestamp", "last_attempt_timestamp", "resolved_timestamp",
)
_NEXUS_TRANSFER_AUDIT_COLUMNS = (
    "id", "intent_id", "action", "actor", "rationale", "evidence", "timestamp",
)


def _nexus_transfer_intent_id(source_txid: str) -> str:
    """Stable identity for the one permissible Nexus transfer per source credit."""
    return "nexus-transfer-" + hashlib.sha256(source_txid.encode("utf-8")).hexdigest()[:32]


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


def _record_nexus_transfer_audit_event(
    conn, *, intent_id: str, action: str, actor: str, rationale: str, evidence: str | None = None
) -> None:
    """Append one immutable operator event. Repeating an action is idempotent."""
    conn.execute(
        """INSERT OR IGNORE INTO nexus_transfer_audit_events
           (intent_id, action, actor, rationale, evidence, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (intent_id, action, actor, rationale, evidence, int(time.time())),
    )


def create_nexus_transfer_intent(
    *,
    kind: str,
    source_txid: str,
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
    units = int(amount_usdd_units)
    if not kind or not source_txid or not from_address or not to_address or units <= 0:
        raise ValueError("Nexus transfer intent requires kind, source, addresses and positive units")

    intent_id = _nexus_transfer_intent_id(source_txid)
    reference = _nexus_transfer_reference(intent_id)
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT " + ", ".join(_NEXUS_TRANSFER_COLUMNS) +
            " FROM nexus_transfer_intents WHERE source_txid = ?",
            (source_txid,),
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
        conn.execute(
            """INSERT INTO nexus_transfer_intents
               (id, kind, source_txid, from_address, to_address, amount_usdd_units,
                reference, status, created_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?)""",
            (intent_id, kind, source_txid, from_address, to_address, units, reference, now),
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
        requested = conn.execute(
            """SELECT 1 FROM nexus_transfer_audit_events
               WHERE intent_id = ? AND action = 'execution_requested' AND evidence =
               (SELECT reference FROM nexus_transfer_intents WHERE id = ?)""",
            (intent_id, intent_id),
        ).fetchone()
        if requested is None:
            conn.commit()
            return None
        updated = conn.execute(
            """UPDATE nexus_transfer_intents
               SET status = 'executing', last_attempt_timestamp = ?
               WHERE id = ? AND status = 'authorized'""",
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
        recovered = conn.execute(
            """UPDATE nexus_transfer_intents
               SET status = 'outcome_unknown'
               WHERE status = 'executing'"""
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
        if expected_reference != str(intent["reference"]):
            raise ValueError("Nexus transfer reference confirmation does not match")
        if intent["status"] != "prepared":
            raise ValueError("only a prepared Nexus transfer intent may be authorized")
        preparation = conn.execute(
            """SELECT 1 FROM nexus_transfer_audit_events
               WHERE intent_id = ? AND action = ? AND evidence = ?""",
            (intent_id, f"prepared_{intent['kind']}", str(intent["reference"])),
        ).fetchone()
        if preparation is None:
            raise ValueError("Nexus transfer requires an audited preparation before authorization")
        conn.execute("UPDATE nexus_transfer_intents SET status = 'authorized' WHERE id = ?", (intent_id,))
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action="authorized_execution", actor=actor,
            rationale=rationale, evidence=expected_reference,
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
        if intent is None or intent["status"] != "prepared":
            raise ValueError("only a prepared Nexus transfer intent may be attributed")
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action=f"prepared_{intent['kind']}", actor=actor,
            rationale=rationale, evidence=str(intent["reference"]),
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
        if intent is None or intent["status"] != "authorized":
            raise ValueError("only an authorized Nexus transfer intent may be executed")
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action="execution_requested", actor=actor,
            rationale=rationale, evidence=str(intent["reference"]),
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
    """Move a held source row only after its completed transfer has exact chain evidence."""
    actor = _require_operator_text(actor, "operator")
    rationale = _require_operator_text(rationale, "disposition rationale")
    expected_remote_txid = _require_operator_text(expected_remote_txid, "remote txid confirmation")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        intent = get_nexus_transfer_intent(intent_id, conn=conn)
        if (intent is None or intent["status"] != "completed" or
                str(intent.get("remote_txid") or "") != expected_remote_txid):
            conn.commit()
            return False
        source = conn.execute(
            """SELECT txid, timestamp, amount_usdd, from_address, to_address,
                      owner_from_address, confirmations_credit, status, amount_usdd_units
               FROM unprocessed_txids WHERE txid = ?""",
            (intent["source_txid"],),
        ).fetchone()
        if source is None:
            conn.commit()
            return False
        txid, timestamp, amount, sender, treasury, owner, confirmations, source_status, units = source
        if (source_status != "refund held for operator review" or
                int(units or -1) != int(intent["amount_usdd_units"]) or
                str(treasury or "") != str(intent["from_address"])):
            conn.commit()
            return False
        if intent["kind"] == "refund":
            if str(sender or "") != str(intent["to_address"]):
                conn.commit()
                return False
            conn.execute(
                """INSERT OR REPLACE INTO refunded_txids
                   (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address,
                    confirmations_credit, status, sig)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (txid, timestamp, amount, sender, treasury, owner, confirmations,
                 "refund_confirmed_by_operator", expected_remote_txid),
            )
            action = "finalized_refund"
        elif intent["kind"] == "quarantine":
            conn.execute(
                """INSERT OR REPLACE INTO quarantined_txids
                   (txid, timestamp, amount_usdd, from_address, to_address, owner, sig, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (txid, timestamp, amount, sender, treasury, owner, expected_remote_txid,
                 "quarantine_confirmed_by_operator"),
            )
            action = "finalized_quarantine"
        else:
            conn.commit()
            return False
        conn.execute("DELETE FROM unprocessed_txids WHERE txid = ?", (txid,))
        _record_nexus_transfer_audit_event(
            conn, intent_id=intent_id, action=action, actor=actor, rationale=rationale,
            evidence=expected_remote_txid,
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
        if status in {"submitted", "completed"} and not next_remote_txid:
            raise ValueError("submitted or completed Nexus transfer intent requires a remote txid")
        if resolved and status != "completed":
            raise ValueError("only a completed Nexus transfer intent may be marked resolved")
        now = int(time.time())
        conn.execute(
            """UPDATE nexus_transfer_intents
               SET status = ?, remote_txid = COALESCE(?, remote_txid),
                   resolved_timestamp = CASE WHEN ? THEN ? ELSE resolved_timestamp END
               WHERE id = ?""",
            (status, supplied_remote_txid or None, 1 if resolved else 0, now, intent_id),
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
            "ORDER BY created_timestamp ASC LIMIT ?",
            tuple(statuses) + (int(limit),),
        ).fetchall()
        return [intent for row in rows
                if (intent := _nexus_transfer_intent_dict(row)) is not None]
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
    """Gross Solana units tied to deposits whose lifecycle is not terminal.

    Every row in ``unprocessed_sigs`` is still owed a Nexus credit, refund, or
    quarantine handling. Gross subtraction is conservative: it can defer fee
    realization, but cannot classify user funds as spendable surplus.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usdc_units), 0) FROM unprocessed_sigs"
        ).fetchone()
        return max(0, int((row or (0,))[0] or 0))
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
         nexus_destination, memo, status, reference)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sig, timestamp, amount_usdc_units, txid, amount_usdd, amount_usdd_units,
         nexus_destination, memo, status, reference),
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

def is_unprocessed_txid(txid: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM unprocessed_txids WHERE txid = ?", (txid,))
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
):
    """Insert/update processed txid. Status optional (e.g. 'credited', 'skipped')."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO processed_txids
        (txid, timestamp, amount_usdd, amount_usdd_units, from_address, to_address, owner, sig, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (txid, timestamp, amount_usdd, amount_usdd_units, from_address, to_address, owner, sig, status),
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
            txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, sig
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, sig),
    )
    conn.commit()
    conn.close()

def is_refunded_txid(txid: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM refunded_txids WHERE txid = ?", (txid,))
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
                   "FROM quarantined_txids WHERE txid = ?", (txid,))
    prev = cursor.fetchone() or (None, None, None, None, None, None)
    cursor.execute(
        """
        INSERT OR REPLACE INTO quarantined_txids
        (txid, timestamp, amount_usdd, from_address, to_address, owner, sig, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txid,
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

def is_quarantined_txid(txid: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM quarantined_txids WHERE txid = ?", (txid,))
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
        (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account, sig, amount_usdd_units, hold_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account, sig, amount_usdd_units, hold_reason))
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
        SELECT txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account, sig, amount_usdd_units, hold_reason
        FROM unprocessed_txids
        ORDER BY timestamp ASC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def update_unprocessed_txid(
    txid: str,
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
    
    values.append(txid)
    sql = f"UPDATE unprocessed_txids SET {', '.join(fields)} WHERE txid = ?"
    cursor.execute(sql, tuple(values))
    conn.commit()
    conn.close()


def remove_unprocessed_txid(txid: str):
    """Remove an unprocessed txid."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM unprocessed_txids WHERE txid = ?", (txid,))
    conn.commit()
    conn.close()


def is_processed_txid(txid: str) -> bool:
    """Check if txid has been processed."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed_txids WHERE txid = ?", (txid,))
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
            "ts": t[1],
            "amount_usdd": t[2],
            "from": t[3],  # from_address
            "to": t[4],  # to_address
            "owner": t[5],  # owner_from_address
            "confirmations": t[6],  # confirmations_credit
            "comment": t[7],  # status
            "receival_account": t[8] if len(t) > 8 else None,
            "sig": t[9] if len(t) > 9 else None,
            "amount_usdd_units": t[10] if len(t) > 10 else None,
            "hold_reason": t[11] if len(t) > 11 else None,
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
