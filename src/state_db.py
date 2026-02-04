import sqlite3
import os
from typing import List, Optional, Tuple
from typing import List, Optional, Tuple

DB_PATH = os.getenv("STATE_DB_PATH", "swap_service.db")

def init_db():
    """Initialize DB tables if not exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Core tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_sigs (
            sig TEXT PRIMARY KEY,
            timestamp INTEGER,
            amount_usdc_units INTEGER,
            txid TEXT,
            amount_usdd REAL,
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
            receival_account TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_txids (
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
    
    # Fee summary (optional aggregated view)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fee_summary (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_collected_usdc INTEGER DEFAULT 0,
            total_collected_usdd INTEGER DEFAULT 0,
            last_updated INTEGER
        )
    """)
    
    conn.commit()
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
        INSERT OR REPLACE INTO processed_sigs (sig, timestamp, amount_usdc_units, txid, amount_usdd, status, reference)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (sig, timestamp, amount_usdc_units, txid, amount_usdd, status, reference),
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
def is_quarantined_sig(sig: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM quarantined_sigs WHERE sig = ?", (sig,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


## Unprocessed txids USDD -> USDC

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
):
    """Insert/update processed txid. Status optional (e.g. 'credited', 'skipped')."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO processed_txids (txid, timestamp, amount_usdd, from_address, to_address, owner, sig, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (txid, timestamp, amount_usdd, from_address, to_address, owner, sig, status),
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

def mark_quarantined_txid(txid: str, sig: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO quarantined_txids (txid, sig)
        VALUES (?, ?)
    """, (txid, sig))
    conn.commit()
    conn.close()

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

def should_attempt(action_key: str, max_attempts: int = 3) -> bool:
    """Check if action should be attempted based on attempt count.
    
    Args:
        action_key: Unique identifier for the action
        max_attempts: Maximum allowed attempts (default 3)
    
    Returns:
        True if attempt count < max_attempts, False otherwise.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT count FROM attempts WHERE action_key = ?
    """, (action_key,))
    row = cursor.fetchone()
    conn.close()
    
    if row is None:
        return True  # No attempts yet
    return row[0] < max_attempts


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
        sig: Solana signature (for USDC->USDD fees)
        txid: Nexus txid (for USDD->USDC fees)
        kind: Type of fee ('flat', 'dynamic', 'swap', etc.)
        amount_usdc_units: Fee amount in USDC base units
        amount_usdd_units: Fee amount in USDD base units
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
        (total_usdc_units, total_usdd_units) tuple
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            COALESCE(SUM(amount_usdc_units), 0) as total_usdc,
            COALESCE(SUM(amount_usdd_units), 0) as total_usdd
        FROM fee_entries
    """)
    row = cursor.fetchone()
    conn.close()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def update_fee_summary():
    """Update aggregated fee summary (call periodically)."""
    import time
    now = int(time.time())
    total_usdc, total_usdd = get_total_fees_collected()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO fee_summary (id, total_collected_usdc, total_collected_usdd, last_updated)
        VALUES (1, ?, ?, ?)
    """, (total_usdc, total_usdd, now))
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
    cursor = conn.cursor()
    
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
) -> None:
    """Add or update an unprocessed txid."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO unprocessed_txids
        (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account))
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
        SELECT txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account
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

def save_last_vault_balance(balance: int):
    """Save last known vault balance for delta calculation."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO accounts (nickname, chain, ticker, name, address, balance, timestamp)
        VALUES ('vault_last_balance', 'solana', 'USDC', 'Last Vault Balance', '', ?, ?)
    """, (float(balance), int(__import__('time').time())))
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
            "receival_account": t[8] if len(t) > 8 else None
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


def write_unprocessed_txids(txids: list[dict]) -> None:
    """Replace all unprocessed txids with provided list (compatible with old write_jsonl)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM unprocessed_txids")
    
    for row in txids:
        cursor.execute("""
            INSERT OR REPLACE INTO unprocessed_txids 
            (txid, timestamp, amount_usdd, from_address, to_address, owner_from_address, confirmations_credit, status, receival_account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row.get("txid"),
            row.get("ts"),
            row.get("amount_usdd"),
            row.get("from"),
            row.get("to"),
            row.get("owner"),
            row.get("confirmations"),
            row.get("comment"),
            row.get("receival_account")
        ))
    
    conn.commit()
    conn.close()


def add_processed_txid_from_dict(row: dict) -> None:
    """Add processed txid from dict (compatible with old append_jsonl)."""
    mark_processed_txid(
        txid=row.get("txid"),
        timestamp=row.get("ts"),
        amount_usdd=row.get("amount_usdd"),
        from_address=row.get("from"),
        to_address=row.get("to"),
        owner=row.get("owner"),
        sig=row.get("sig"),
        status=row.get("comment")
    )


def add_unprocessed_txid_from_dict(row: dict) -> None:
    """Add unprocessed txid from dict (compatible with old append_jsonl)."""
    add_unprocessed_txid(
        txid=row.get("txid"),
        timestamp=row.get("ts"),
        amount_usdd=row.get("amount_usdd"),
        from_address=row.get("from"),
        to_address=row.get("to"),
        owner_from_address=row.get("owner"),
        confirmations_credit=row.get("confirmations"),
        status=row.get("comment")
    )


# Add similar functions for other state (e.g., nexus txids, fees)