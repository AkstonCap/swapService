#!/usr/bin/env python3
"""Quarantine Viewer - Display quarantined USDC and USDD transactions for manual handling.

Usage:
    python quarantine_viewer.py           # Show all quarantined entries
    python quarantine_viewer.py --usdc    # Show only USDC (Solana) quarantined
    python quarantine_viewer.py --usdd    # Show only USDD (Nexus) quarantined
    python quarantine_viewer.py --export  # Export to CSV files
"""

import sqlite3
import os
import sys
import argparse
from datetime import datetime
from decimal import Decimal

# Default database path (can be overridden via STATE_DB_PATH env var)
DB_PATH = os.getenv("STATE_DB_PATH", "swap_service.db")

# Terminal colors
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'

def color(text: str, c: str) -> str:
    """Apply color to text if terminal supports it."""
    if sys.stdout.isatty():
        return f"{c}{text}{Colors.ENDC}"
    return text


def format_timestamp(ts: int | None) -> str:
    """Convert Unix timestamp to readable format."""
    if not ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def format_amount(units: int | None, decimals: int = 6, ticker: str = "") -> str:
    """Format base units to human-readable token amount."""
    if units is None:
        return "N/A"
    try:
        amount = Decimal(units) / (Decimal(10) ** decimals)
        formatted = f"{amount:.{decimals}f}".rstrip('0').rstrip('.')
        return f"{formatted} {ticker}".strip()
    except Exception:
        return str(units)


def truncate(s: str | None, max_len: int = 20) -> str:
    """Truncate string with ellipsis."""
    if not s:
        return "N/A"
    s = str(s)
    if len(s) <= max_len:
        return s
    return s[:max_len-3] + "..."


def print_table(headers: list[str], rows: list[list[str]], title: str = ""):
    """Print a formatted ASCII table."""
    if not rows:
        print(color(f"\n  No {title.lower()} found.\n", Colors.DIM))
        return
    
    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    
    # Build separator and format strings
    separator = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header_fmt = "|" + "|".join(f" {{:<{w}}} " for w in col_widths) + "|"
    row_fmt = header_fmt
    
    # Print title
    if title:
        total_width = sum(col_widths) + len(col_widths) * 3 + 1
        print()
        print(color(f" {title} ".center(total_width, "="), Colors.BOLD + Colors.CYAN))
    
    # Print table
    print(separator)
    print(color(header_fmt.format(*headers), Colors.BOLD + Colors.HEADER))
    print(separator)
    for row in rows:
        print(row_fmt.format(*[str(c) for c in row]))
    print(separator)
    print(f"  Total: {len(rows)} entries\n")


def get_quarantined_usdc() -> list[tuple]:
    """Fetch quarantined USDC deposits (USDC→USDD direction failures)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, timestamp, from_address, amount_usdc_units, memo, 
               quarantine_sig, quarantined_units, status
        FROM quarantined_sigs
        ORDER BY timestamp DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_quarantined_usdd() -> list[tuple]:
    """Fetch quarantined USDD credits (USDD→USDC direction failures)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT txid, timestamp, amount_usdd, from_address, to_address,
               owner, sig, status
        FROM quarantined_txids
        ORDER BY timestamp DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_failed_refunds_usdc() -> list[tuple]:
    """Fetch USDC refunds that are stuck or failed."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, timestamp, from_address, amount_usdc_units, memo, status
        FROM unprocessed_sigs
        WHERE status LIKE '%refund%' OR status LIKE '%quarantine%'
        ORDER BY timestamp DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_failed_refunds_usdd() -> list[tuple]:
    """Fetch USDD refunds that are stuck or failed."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT txid, timestamp, amount_usdd, from_address, status
        FROM unprocessed_txids
        WHERE status LIKE '%refund%' OR status LIKE '%quarantine%'
        ORDER BY timestamp DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def display_usdc_quarantine():
    """Display quarantined USDC transactions."""
    # Quarantined (finalized)
    rows = get_quarantined_usdc()
    table_rows = []
    for sig, ts, from_addr, amount, memo, qsig, qunits, status in rows:
        table_rows.append([
            truncate(sig, 16),
            format_timestamp(ts),
            truncate(from_addr, 16),
            format_amount(amount, 6, "USDC"),
            truncate(memo, 25),
            truncate(status, 20),
        ])
    
    print_table(
        ["Deposit Sig", "Timestamp", "From Address", "Amount", "Memo", "Status"],
        table_rows,
        "Quarantined USDC Deposits (USDC→USDD Failures)"
    )
    
    # Pending refunds/quarantine
    pending = get_failed_refunds_usdc()
    if pending:
        pending_rows = []
        for sig, ts, from_addr, amount, memo, status in pending:
            pending_rows.append([
                truncate(sig, 16),
                format_timestamp(ts),
                truncate(from_addr, 16),
                format_amount(amount, 6, "USDC"),
                truncate(memo, 25),
                truncate(status, 20),
            ])
        
        print_table(
            ["Deposit Sig", "Timestamp", "From Address", "Amount", "Memo", "Status"],
            pending_rows,
            "Pending USDC Refunds/Quarantine (In Progress)"
        )


def display_usdd_quarantine():
    """Display quarantined USDD transactions."""
    # Quarantined (finalized)
    rows = get_quarantined_usdd()
    table_rows = []
    for txid, ts, amount, from_addr, to_addr, owner, sig, status in rows:
        table_rows.append([
            truncate(txid, 16),
            format_timestamp(ts),
            truncate(from_addr, 16),
            format_amount(int(float(amount or 0) * 1_000_000), 6, "USDD"),
            truncate(owner, 16),
            truncate(status, 20),
        ])
    
    print_table(
        ["Nexus TxID", "Timestamp", "From Address", "Amount", "Owner", "Status"],
        table_rows,
        "Quarantined USDD Credits (USDD→USDC Failures)"
    )
    
    # Pending refunds/quarantine
    pending = get_failed_refunds_usdd()
    if pending:
        pending_rows = []
        for txid, ts, amount, from_addr, status in pending:
            pending_rows.append([
                truncate(txid, 16),
                format_timestamp(ts),
                truncate(from_addr, 16),
                format_amount(int(float(amount or 0) * 1_000_000), 6, "USDD"),
                truncate(status, 25),
            ])
        
        print_table(
            ["Nexus TxID", "Timestamp", "From Address", "Amount", "Status"],
            pending_rows,
            "Pending USDD Refunds/Quarantine (In Progress)"
        )


def display_summary():
    """Display summary counts."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Counts
    cursor.execute("SELECT COUNT(*) FROM quarantined_sigs")
    usdc_quarantined = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM quarantined_txids")
    usdd_quarantined = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM unprocessed_sigs WHERE status LIKE '%refund%' OR status LIKE '%quarantine%'")
    usdc_pending = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM unprocessed_txids WHERE status LIKE '%refund%' OR status LIKE '%quarantine%'")
    usdd_pending = cursor.fetchone()[0]
    
    # Totals
    cursor.execute("SELECT COALESCE(SUM(amount_usdc_units), 0) FROM quarantined_sigs")
    usdc_total = cursor.fetchone()[0]
    
    cursor.execute("SELECT COALESCE(SUM(amount_usdd), 0) FROM quarantined_txids")
    usdd_total = cursor.fetchone()[0]
    
    conn.close()
    
    print()
    print(color(" QUARANTINE SUMMARY ".center(60, "="), Colors.BOLD + Colors.YELLOW))
    print()
    print(f"  {color('USDC→USDD Direction:', Colors.BOLD)}")
    print(f"    Quarantined:     {usdc_quarantined} entries ({format_amount(usdc_total, 6, 'USDC')})")
    print(f"    Pending:         {usdc_pending} entries")
    print()
    print(f"  {color('USDD→USDC Direction:', Colors.BOLD)}")
    print(f"    Quarantined:     {usdd_quarantined} entries ({format_amount(int(float(usdd_total) * 1_000_000), 6, 'USDD')})")
    print(f"    Pending:         {usdd_pending} entries")
    print()
    
    total = usdc_quarantined + usdd_quarantined + usdc_pending + usdd_pending
    if total == 0:
        print(color("  ✓ No quarantined or pending items requiring attention.\n", Colors.GREEN))
    else:
        print(color(f"  ⚠ {total} total items require manual review.\n", Colors.YELLOW))


def export_to_csv():
    """Export quarantined data to CSV files."""
    import csv
    
    # Export USDC quarantine
    usdc_rows = get_quarantined_usdc()
    if usdc_rows:
        with open("quarantine_usdc.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["sig", "timestamp", "from_address", "amount_usdc_units", "memo", "quarantine_sig", "quarantined_units", "status"])
            writer.writerows(usdc_rows)
        print(f"  Exported {len(usdc_rows)} USDC entries to quarantine_usdc.csv")
    
    # Export USDD quarantine
    usdd_rows = get_quarantined_usdd()
    if usdd_rows:
        with open("quarantine_usdd.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["txid", "timestamp", "amount_usdd", "from_address", "to_address", "owner", "sig", "status"])
            writer.writerows(usdd_rows)
        print(f"  Exported {len(usdd_rows)} USDD entries to quarantine_usdd.csv")
    
    # Export pending USDC
    usdc_pending = get_failed_refunds_usdc()
    if usdc_pending:
        with open("pending_usdc.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["sig", "timestamp", "from_address", "amount_usdc_units", "memo", "status"])
            writer.writerows(usdc_pending)
        print(f"  Exported {len(usdc_pending)} pending USDC entries to pending_usdc.csv")
    
    # Export pending USDD
    usdd_pending = get_failed_refunds_usdd()
    if usdd_pending:
        with open("pending_usdd.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["txid", "timestamp", "amount_usdd", "from_address", "status"])
            writer.writerows(usdd_pending)
        print(f"  Exported {len(usdd_pending)} pending USDD entries to pending_usdd.csv")
    
    print()


def main():
    parser = argparse.ArgumentParser(
        description="View quarantined USDC and USDD transactions for manual handling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python quarantine_viewer.py           # Show all quarantined entries
  python quarantine_viewer.py --usdc    # Show only USDC (Solana) quarantined
  python quarantine_viewer.py --usdd    # Show only USDD (Nexus) quarantined
  python quarantine_viewer.py --export  # Export to CSV files

Manual Handling:
  USDC deposits quarantined due to invalid memo or failed refunds should be
  manually reviewed and either:
    1. Refunded via Solana CLI to the from_address
    2. Marked as resolved in the database after investigation
  
  USDD credits quarantined due to missing asset mapping or invalid receival
  account should be:
    1. Refunded via Nexus CLI to the from_address
    2. Marked as resolved in the database after investigation
        """
    )
    parser.add_argument("--usdc", action="store_true", help="Show only USDC (USDC→USDD) quarantine")
    parser.add_argument("--usdd", action="store_true", help="Show only USDD (USDD→USDC) quarantine")
    parser.add_argument("--export", action="store_true", help="Export quarantine data to CSV files")
    parser.add_argument("--db", type=str, help="Path to database file (default: swap_service.db)")
    
    args = parser.parse_args()
    
    global DB_PATH
    if args.db:
        DB_PATH = args.db
    
    # Check database exists
    if not os.path.exists(DB_PATH):
        print(color(f"Error: Database not found at {DB_PATH}", Colors.RED))
        print("Set STATE_DB_PATH environment variable or use --db flag.")
        sys.exit(1)
    
    print(color("\n╔══════════════════════════════════════════════════════════╗", Colors.CYAN))
    print(color("║           QUARANTINE VIEWER - swapService                ║", Colors.CYAN + Colors.BOLD))
    print(color("╚══════════════════════════════════════════════════════════╝", Colors.CYAN))
    print(f"  Database: {DB_PATH}")
    
    if args.export:
        print()
        export_to_csv()
        return
    
    # Display summary first
    display_summary()
    
    # Display tables based on filters
    if args.usdc:
        display_usdc_quarantine()
    elif args.usdd:
        display_usdd_quarantine()
    else:
        display_usdc_quarantine()
        display_usdd_quarantine()


if __name__ == "__main__":
    main()
