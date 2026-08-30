#!/usr/bin/env python3
"""Fail when a repository Markdown link targets a missing local path.

External links are intentionally out of scope: their availability is not deterministic in
CI. This check validates only relative file targets, which catches moved/renamed project
documents without requiring a third-party Markdown parser.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
INLINE_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
SKIPPED_SCHEMES = {"data", "mailto", "tel"}


def local_targets(markdown_file: Path):
    """Yield line number and normalized relative target for Markdown file links."""
    for line_number, line in enumerate(markdown_file.read_text(encoding="utf-8").splitlines(), 1):
        for match in INLINE_LINK.finditer(line):
            target = match.group(1).strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            if not target or target.startswith("#"):
                continue

            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.scheme in SKIPPED_SCHEMES:
                continue

            path = unquote(parsed.path)
            if path:
                yield line_number, path


def broken_links() -> list[str]:
    """Return all Markdown links whose local target is absent from the checkout."""
    failures: list[str] = []
    for markdown_file in sorted(ROOT.rglob("*.md")):
        if ".git" in markdown_file.parts:
            continue
        for line_number, target in local_targets(markdown_file):
            candidate = (markdown_file.parent / target).resolve()
            try:
                candidate.relative_to(ROOT)
            except ValueError:
                failures.append(
                    f"{markdown_file.relative_to(ROOT)}:{line_number}: "
                    f"link escapes repository: {target}"
                )
                continue
            if not candidate.exists():
                failures.append(
                    f"{markdown_file.relative_to(ROOT)}:{line_number}: "
                    f"missing local link target: {target}"
                )
    return failures


def main() -> int:
    failures = broken_links()
    if failures:
        print("Broken local Markdown links:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Local Markdown links: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
