#!/usr/bin/env python3
"""Quarantine Viewer - Display quarantined bridge transactions for manual handling.

Usage:
    python quarantine_viewer.py           # Show all quarantined entries
    python quarantine_viewer.py --solana  # Show only the Solana-side quarantine
    python quarantine_viewer.py --nexus   # Show only the Nexus-side quarantine
    python quarantine_viewer.py --export  # Export to CSV files
"""

import sqlite3
import os
import sys
import argparse
import re
from datetime import datetime
from decimal import Decimal

# Default database path (can be overridden via STATE_DB_PATH env var)
DB_PATH = os.getenv("STATE_DB_PATH", "swap_service.db")

# Token labels and decimals for the configured pair. Read straight from the environment
# so this tool still runs against a database when the service package cannot be imported
# (a bare checkout, or a machine without the Solana/Nexus dependencies installed).
SOL_SYM = os.getenv("SOLANA_TOKEN_SYMBOL", "USDC")
NXS_SYM = os.getenv("NEXUS_TOKEN_NAME", "USDD")
SOL_DECIMALS = int(os.getenv("SOLANA_TOKEN_DECIMALS", os.getenv("USDC_DECIMALS", "6")))
NXS_DECIMALS = int(os.getenv("NEXUS_TOKEN_DECIMALS", os.getenv("USDD_DECIMALS", "6")))

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


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(s) -> str:
    """Strip control/ANSI characters from untrusted text.

    `memo` and `from_address` are copied verbatim from depositor-supplied transaction
    data. Printed raw to a TTY, an embedded ESC/CR sequence can erase or forge rows in
    the very table an operator uses to authorise manual fund recovery.
    """
    if s is None:
        return ""
    return _CONTROL_CHARS.sub("", str(s))


def csv_safe(value):
    """Neutralise spreadsheet formula injection (=, +, -, @ and tab/CR leaders)."""
    text = sanitize(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def format_token_amount(amount, ticker: str = "", decimals: int | None = None) -> str:
    """Format a token-unit amount exactly (no float scaling).

    `decimals` defaults to the Nexus-side token's precision; the quantum used to be
    hardcoded to six places, which silently truncated any pair with finer precision.
    """
    if amount is None:
        return "N/A"
    decs = NXS_DECIMALS if decimals is None else int(decimals)
    try:
        q = Decimal(str(amount)).quantize(Decimal(10) ** -decs)
        return f"{format(q, 'f').rstrip('0').rstrip('.') or '0'} {ticker}".strip()
    except Exception:
        return str(amount)


def truncate(s: str | None, max_len: int = 20) -> str:
    """Truncate string with ellipsis."""
    if not s:
        return "N/A"
    s = sanitize(s)
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
        print(row_fmt.format(*[sanitize(c) for c in row]))
    print(separator)
    print(f"  Total: {len(rows)} entries\n")


def get_quarantined_solana() -> list[tuple]:
    """Fetch quarantined Solana-side deposits (Solana→Nexus direction failures)."""
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


def get_quarantined_nexus() -> list[tuple]:
    """Fetch quarantined Nexus-side credits (Nexus→Solana direction failures)."""
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


def get_failed_refunds_solana() -> list[tuple]:
    """Fetch Solana-side refunds that are stuck or failed."""
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


def get_failed_refunds_nexus() -> list[tuple]:
    """Fetch Nexus-side refunds that are stuck or failed."""
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


def display_solana_quarantine():
    """Display quarantined Solana-side transactions."""
    # Quarantined (finalized)
    rows = get_quarantined_solana()
    table_rows = []
    for sig, ts, from_addr, amount, memo, qsig, qunits, status in rows:
        table_rows.append([
            truncate(sig, 16),
            format_timestamp(ts),
            truncate(from_addr, 16),
            format_amount(amount, SOL_DECIMALS, SOL_SYM),
            truncate(memo, 25),
            truncate(status, 20),
        ])
    
    print_table(
        ["Deposit Sig", "Timestamp", "From Address", "Amount", "Memo", "Status"],
        table_rows,
        f"Quarantined {SOL_SYM} Deposits ({SOL_SYM}→{NXS_SYM} Failures)"
    )
    
    # Pending refunds/quarantine
    pending = get_failed_refunds_solana()
    if pending:
        pending_rows = []
        for sig, ts, from_addr, amount, memo, status in pending:
            pending_rows.append([
                truncate(sig, 16),
                format_timestamp(ts),
                truncate(from_addr, 16),
                format_amount(amount, SOL_DECIMALS, SOL_SYM),
                truncate(memo, 25),
                truncate(status, 20),
            ])
        
        print_table(
            ["Deposit Sig", "Timestamp", "From Address", "Amount", "Memo", "Status"],
            pending_rows,
            f"Pending {SOL_SYM} Refunds/Quarantine (In Progress)"
        )


def display_nexus_quarantine():
    """Display quarantined Nexus-side transactions."""
    # Quarantined (finalized)
    rows = get_quarantined_nexus()
    table_rows = []
    for txid, ts, amount, from_addr, to_addr, owner, sig, status in rows:
        table_rows.append([
            truncate(txid, 16),
            format_timestamp(ts),
            truncate(from_addr, 16),
            format_token_amount(amount, NXS_SYM),
            truncate(owner, 16),
            truncate(status, 20),
        ])
    
    print_table(
        ["Nexus TxID", "Timestamp", "From Address", "Amount", "Owner", "Status"],
        table_rows,
        f"Quarantined {NXS_SYM} Credits ({NXS_SYM}→{SOL_SYM} Failures)"
    )
    
    # Pending refunds/quarantine
    pending = get_failed_refunds_nexus()
    if pending:
        pending_rows = []
        for txid, ts, amount, from_addr, status in pending:
            pending_rows.append([
                truncate(txid, 16),
                format_timestamp(ts),
                truncate(from_addr, 16),
                format_token_amount(amount, NXS_SYM),
                truncate(status, 25),
            ])
        
        print_table(
            ["Nexus TxID", "Timestamp", "From Address", "Amount", "Status"],
            pending_rows,
            f"Pending {NXS_SYM} Refunds/Quarantine (In Progress)"
        )


def display_summary():
    """Display summary counts."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Counts
    cursor.execute("SELECT COUNT(*) FROM quarantined_sigs")
    solana_quarantined = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM quarantined_txids")
    nexus_quarantined = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM unprocessed_sigs WHERE status LIKE '%refund%' OR status LIKE '%quarantine%'")
    solana_pending = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM unprocessed_txids WHERE status LIKE '%refund%' OR status LIKE '%quarantine%'")
    nexus_pending = cursor.fetchone()[0]
    
    # Totals
    cursor.execute("SELECT COALESCE(SUM(amount_usdc_units), 0) FROM quarantined_sigs")
    solana_total = cursor.fetchone()[0]
    
    cursor.execute("SELECT COALESCE(SUM(amount_usdd), 0) FROM quarantined_txids")
    nexus_total = cursor.fetchone()[0]
    
    conn.close()
    
    print()
    print(color(" QUARANTINE SUMMARY ".center(60, "="), Colors.BOLD + Colors.YELLOW))
    print()
    print(f"  {color(f'{SOL_SYM}→{NXS_SYM} Direction:', Colors.BOLD)}")
    print(f"    Quarantined:     {solana_quarantined} entries ({format_amount(solana_total, SOL_DECIMALS, SOL_SYM)})")
    print(f"    Pending:         {solana_pending} entries")
    print()
    print(f"  {color(f'{NXS_SYM}→{SOL_SYM} Direction:', Colors.BOLD)}")
    print(f"    Quarantined:     {nexus_quarantined} entries ({format_token_amount(nexus_total, NXS_SYM)})")
    print(f"    Pending:         {nexus_pending} entries")
    print()
    
    total = solana_quarantined + nexus_quarantined + solana_pending + nexus_pending
    if total == 0:
        print(color("  ✓ No quarantined or pending items requiring attention.\n", Colors.GREEN))
    else:
        print(color(f"  ⚠ {total} total items require manual review.\n", Colors.YELLOW))


def export_to_csv():
    """Export quarantined data to CSV files."""
    import csv
    
    # Export the Solana-side quarantine
    solana_rows = get_quarantined_solana()
    if solana_rows:
        with open("quarantine_solana_token.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["sig", "timestamp", "from_address", "amount_usdc_units", "memo", "quarantine_sig", "quarantined_units", "status"])
            writer.writerows([[csv_safe(c) for c in r] for r in solana_rows])
        print(f"  Exported {len(solana_rows)} {SOL_SYM} entries to quarantine_solana_token.csv")
    
    # Export the Nexus-side quarantine
    nexus_rows = get_quarantined_nexus()
    if nexus_rows:
        with open("quarantine_nexus_token.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["txid", "timestamp", "amount_usdd", "from_address", "to_address", "owner", "sig", "status"])
            writer.writerows([[csv_safe(c) for c in r] for r in nexus_rows])
        print(f"  Exported {len(nexus_rows)} {NXS_SYM} entries to quarantine_nexus_token.csv")
    
    # Export pending Solana-side refunds
    solana_pending = get_failed_refunds_solana()
    if solana_pending:
        with open("pending_solana_token.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["sig", "timestamp", "from_address", "amount_usdc_units", "memo", "status"])
            writer.writerows([[csv_safe(c) for c in r] for r in solana_pending])
        print(f"  Exported {len(solana_pending)} pending {SOL_SYM} entries to pending_solana_token.csv")
    
    # Export pending Nexus-side refunds
    nexus_pending = get_failed_refunds_nexus()
    if nexus_pending:
        with open("pending_nexus_token.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["txid", "timestamp", "amount_usdd", "from_address", "status"])
            writer.writerows([[csv_safe(c) for c in r] for r in nexus_pending])
        print(f"  Exported {len(nexus_pending)} pending {NXS_SYM} entries to pending_nexus_token.csv")
    
    print()


def main():
    parser = argparse.ArgumentParser(
        description=f"View quarantined {SOL_SYM} and {NXS_SYM} transactions for manual handling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  python quarantine_viewer.py           # Show all quarantined entries
  python quarantine_viewer.py --solana  # Show only {SOL_SYM} (Solana) quarantined
  python quarantine_viewer.py --nexus   # Show only {NXS_SYM} (Nexus) quarantined
  python quarantine_viewer.py --export  # Export to CSV files

Manual Handling:
  {SOL_SYM} deposits quarantined due to invalid memo or failed refunds should be
  manually reviewed and either:
    1. Refunded via Solana CLI to the from_address
    2. Marked as resolved in the database after investigation

  {NXS_SYM} credits quarantined due to missing asset mapping or invalid receival
  account should be:
    1. Refunded via Nexus CLI to the from_address
    2. Marked as resolved in the database after investigation
        """
    )
    # `--usdc`/`--usdd` remain as hidden aliases so existing operator scripts keep working.
    parser.add_argument("--solana", "--usdc", dest="solana", action="store_true",
                        help=f"Show only the Solana-side ({SOL_SYM}→{NXS_SYM}) quarantine")
    parser.add_argument("--nexus", "--usdd", dest="nexus", action="store_true",
                        help=f"Show only the Nexus-side ({NXS_SYM}→{SOL_SYM}) quarantine")
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
    
    # Display tables based on filters. Passing both flags together previously fell into
    # an elif and silently showed only one side, giving a partial view with no warning.
    show_solana = args.solana or not (args.solana or args.nexus)
    show_nexus = args.nexus or not (args.solana or args.nexus)
    if show_solana:
        display_solana_quarantine()
    if show_nexus:
        display_nexus_quarantine()


if __name__ == "__main__":
    main()
