# filepath: c:\Users\Arne\Documents\GitHub\swapService\src\state_db.py
import sqlite3
import os
from typing import List, Optional, Tuple

DB_PATH = os.getenv("STATE_DB_PATH", "swap_service.db")

def init_db():
    """Initialize DB tables if not exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
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
            status TEXT
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
            status TEXT
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

    Table schema only has: sig, timestamp, from_address, amount_usdc_units, memo, status.
    Extra legacy fields (quarantine_sig, quarantined_units) are ignored.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO quarantined_sigs (sig, timestamp, from_address, amount_usdc_units, memo, quarantine_sig, quarantined_units, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (sig, timestamp, from_address, amount_usdc_units, memo, quarantine_sig, quarantined_units, status))
    conn.commit()
    conn.close()

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

def mark_processed_txid(txid: str, sig: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO processed_txids (txid, sig)
        VALUES (?, ?)
    """, (txid, sig))
    conn.commit()
    conn.close()




## Refunded txids

def mark_refunded_txid(txid: str, sig: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO refunded_txids (txid, sig)
        VALUES (?, ?)
    """, (txid, sig))
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

# Add similar functions for other state (e.g., nexus txids, fees)