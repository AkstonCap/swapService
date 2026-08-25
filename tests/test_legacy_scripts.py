"""Enforce the formerly executable test scripts through pytest.

Each legacy check deliberately changes process-global import, environment and database
state. Running one check per subprocess is the isolation boundary: test collection and
later checks cannot inherit those mutations. The scripts stay runnable directly for
operator diagnostics, while ``python -m pytest -q`` is now a complete suite command.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGACY_SCRIPTS = (
    "legacy_smoke.py",
    "legacy_session.py",
    "legacy_token_pair.py",
    "legacy_frozen_names.py",
    "legacy_dashboard.py",
)


@pytest.fixture
def run_isolated_script():
    """Run one legacy check in a fresh interpreter and return its completed result."""

    def run(script_name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "tests" / script_name)],
            cwd=ROOT,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    return run


@pytest.mark.parametrize("script_name", LEGACY_SCRIPTS)
def test_legacy_script_passes_in_an_isolated_interpreter(run_isolated_script, script_name):
    result = run_isolated_script(script_name)

    assert result.returncode == 0, (
        f"{script_name} failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
