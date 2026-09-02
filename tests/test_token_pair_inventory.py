"""Regression gate for Batch 7's token-pair literal inventory."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_token_pair_inventory.py"


def _checker_module():
    spec = importlib.util.spec_from_file_location("token_pair_inventory_checker", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_matches_legacy_identifiers_not_only_standalone_words():
    """Legacy configuration names such as VAULT_USDC_ACCOUNT need classification too."""
    checker = _checker_module()

    for value in ("VAULT_USDC_ACCOUNT", "NEXUS_USDD_TREASURY_ACCOUNT", "USDC", "USDD"):
        assert checker.TOKEN_LITERAL.search(value), value


def test_inventory_discovers_active_developer_and_operator_surfaces():
    """A new active helper or developer guide cannot bypass the migration inventory."""
    checker = _checker_module()

    assert checker.is_active_surface("nexus_transfer_operator.py")
    assert checker.is_active_surface(".github/copilot-instructions.md")


def test_active_token_pair_literals_have_a_current_classification_inventory():
    """New USDC/USDD semantics cannot bypass the planned migration inventory."""
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
