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
            reason TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unprocessed_sigs (
            sig TEXT PRIMARY KEY,
            timestamp INTEGER,
            memo TEXT,
            from_address TEXT,
            amount_usdc REAL
        )
    """)
    # Add other tables as needed (e.g., for nexus txids, fees)
    conn.commit()
    conn.close()

def is_processed_sig(sig: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed_sigs WHERE sig = ?", (sig,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def is_unprocessed_sig(sig: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM unprocessed_sigs WHERE sig = ?", (sig,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_unprocessed_sig(sig: str, timestamp: int, memo: str, from_address: str, amount_usdc: float):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO unprocessed_sigs (sig, timestamp, memo, from_address, amount_usdc)
        VALUES (?, ?, ?, ?, ?)
    """, (sig, timestamp, memo, from_address, amount_usdc))
    conn.commit()
    conn.close()

def get_unprocessed_sigs() -> List[Tuple[str, int, str, str, float]]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT sig, timestamp, memo, from_address, amount_usdc FROM unprocessed_sigs ORDER BY timestamp ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_processed_sig(sig: str, timestamp: int, reason: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO processed_sigs (sig, timestamp, reason)
        VALUES (?, ?, ?)
    """, (sig, timestamp, reason))
    conn.commit()
    conn.close()

def remove_unprocessed_sig(sig: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM unprocessed_sigs WHERE sig = ?", (sig,))
    conn.commit()
    conn.close()

# Add similar functions for other state (e.g., nexus txids, fees)