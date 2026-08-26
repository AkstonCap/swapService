"""Regression contract for the local and GitHub quality gates."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LINK_CHECKER = ROOT / "scripts" / "check_markdown_links.py"


def test_markdown_link_checker_accepts_tracked_documentation():
    """Local Markdown links must stay valid before CI can publish a green build."""
    result = subprocess.run(
        [sys.executable, str(LINK_CHECKER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_ci_workflow_runs_the_required_quality_gates():
    """The checked-in workflow must enforce the release-quality baseline."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    for expected in (
        "pull_request:",
        "push:",
        "actions/checkout@",
        "actions/setup-python@",
        "python -m pip install -r requirements.txt",
        "python -m pip check",
        "python -m compileall -q src",
        "python -m pytest -q",
        "python scripts/check_markdown_links.py",
        "git diff --check",
    ):
        assert expected in workflow, f"CI workflow is missing required gate: {expected}"
