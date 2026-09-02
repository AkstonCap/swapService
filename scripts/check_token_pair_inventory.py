#!/usr/bin/env python3
"""Keep Batch 7's USDC/USDD literal inventory complete and reviewable.

The inventory deliberately covers active runtime, helper and operator/public-document
surfaces.  Tests, vendor material and historical review evidence are excluded: fixture
values and historical findings are not current pair-selection semantics.  Each active
line containing a legacy token literal must have an explicit marker in
``docs/TOKEN_PAIR_LITERAL_INVENTORY.md`` classifying its migration treatment.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "TOKEN_PAIR_LITERAL_INVENTORY.md"
# Delimit on alphanumerics, rather than regex ``\b``: an underscore is a Python
# word character, but `VAULT_USDC_ACCOUNT` and `NEXUS_USDD_TREASURY_ACCOUNT`
# are precisely the legacy identifiers Batch 7 must inventory.
TOKEN_LITERAL = re.compile(r"(?<![A-Za-z0-9])(?:USDC|USDD)(?![A-Za-z0-9])")
MARKER = re.compile(
    r"<!--\s*token-pair-inventory:\s*(?P<path>[^:\s]+):"
    r"(?P<lines>\d+(?:,\d+)*)\s*-->"
)
CLASSIFICATIONS = (
    "Runtime semantics",
    "Migration alias",
    "Frozen compatibility state",
    "Display metadata",
    "Public pair-specific example",
    "Planned/schema example",
    "Runtime helper defaults",
    "v1 compatibility",
)
HISTORICAL_DOC_PREFIXES = (
    "docs/DEVELOPMENT_REVIEW_",
    "docs/POST_CHANGE_REVIEW_",
)
HISTORICAL_DOCS = {
    "docs/AUDIT_FINDINGS.md",
    "docs/RISK_ASSESSMENT.md",
}
EXCLUDED_PATHS = {
    "docs/TOKEN_PAIR_LITERAL_INVENTORY.md",
    "scripts/check_token_pair_inventory.py",
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, text=True, capture_output=True, check=True
    )
    return result.stdout.splitlines()


def is_active_surface(path: str) -> bool:
    """Select all maintained implementation and active guidance surfaces.

    Tests, vendored Nexus API documentation and dated review/audit evidence have
    explicit exclusions because their literals are fixtures or historical evidence,
    not current pair-selection semantics. All other tracked Python, Markdown and
    `.env.example` surfaces are discovered automatically, including new helpers and
    `.github` developer guidance.
    """
    if path in EXCLUDED_PATHS or path.startswith(("tests/", "Nexus API docs/")):
        return False
    if path in HISTORICAL_DOCS or path.startswith(HISTORICAL_DOC_PREFIXES):
        return False
    return path == ".env.example" or path.endswith((".py", ".md"))


def index_text(relative: str) -> str:
    """Read the candidate commit, never unrelated unstaged worktree edits."""
    result = subprocess.run(
        ["git", "show", f":{relative}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot read staged {relative}: {result.stderr.strip()}")
    return result.stdout


def active_literal_lines() -> set[tuple[str, int]]:
    found: set[tuple[str, int]] = set()
    for relative in tracked_files():
        if not is_active_surface(relative):
            continue
        for line_number, line in enumerate(index_text(relative).splitlines(), start=1):
            if TOKEN_LITERAL.search(line):
                found.add((relative, line_number))
    return found


def inventory_entries() -> tuple[set[tuple[str, int]], list[str]]:
    if not INVENTORY.is_file():
        return set(), [f"Missing {INVENTORY.relative_to(ROOT)}"]
    entries: set[tuple[str, int]] = set()
    invalid: list[str] = []
    text = index_text(INVENTORY.relative_to(ROOT).as_posix())
    for match in MARKER.finditer(text):
        relative = match.group("path")
        entries.update((relative, int(line)) for line in match.group("lines").split(","))
        following = text[match.end():].lstrip().splitlines()
        row = following[0] if following else ""
        if not row.startswith("|") or not any(label in row for label in CLASSIFICATIONS):
            invalid.append(f"{relative}:{match.group('lines')} has no classified table row")
    return entries, invalid


def print_listing(entries: set[tuple[str, int]]) -> None:
    grouped: dict[str, list[int]] = {}
    for path, line in sorted(entries):
        grouped.setdefault(path, []).append(line)
    for path, lines in grouped.items():
        print(f"<!-- token-pair-inventory: {path}:{','.join(map(str, lines))} -->")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print current marker lines")
    args = parser.parse_args()

    active = active_literal_lines()
    if args.list:
        print_listing(active)
        return 0

    documented, invalid_markers = inventory_entries()
    missing = sorted(active - documented)
    stale = sorted(documented - active)
    if missing or stale or invalid_markers:
        print("Token-pair literal inventory is not current.", file=sys.stderr)
        if missing:
            print("Missing active entries:", file=sys.stderr)
            for path, line in missing:
                print(f"  {path}:{line}", file=sys.stderr)
        if stale:
            print("Stale inventory entries:", file=sys.stderr)
            for path, line in stale:
                print(f"  {path}:{line}", file=sys.stderr)
        if invalid_markers:
            print("Unclassified inventory markers:", file=sys.stderr)
            for marker in invalid_markers:
                print(f"  {marker}", file=sys.stderr)
        print(
            f"Update {INVENTORY.relative_to(ROOT)}; use --list to generate markers.",
            file=sys.stderr,
        )
        return 1

    print(f"Token-pair literal inventory is current ({len(active)} active lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
