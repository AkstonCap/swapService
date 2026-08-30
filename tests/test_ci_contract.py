"""Regression contract for the local and GitHub quality gates."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LINK_CHECKER = ROOT / "scripts" / "check_markdown_links.py"
REQUIREMENTS = ROOT / "requirements.txt"


def test_security_remediation_pins_safe_http_and_env_dependencies():
    """The runtime lockfile must retain the reviewed advisory remediations."""
    requirements = REQUIREMENTS.read_text(encoding="utf-8")

    assert "python-dotenv==1.2.2" in requirements
    assert "requests==2.33.0" in requirements


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


def test_operator_docs_use_current_security_paths_and_fail_closed_nexus_guidance():
    """Operator instructions must not revive moved paths or unsafe Nexus refund/API settings."""
    config_guide = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    setup_guide = (ROOT / "SETUP.md").read_text(encoding="utf-8")

    assert "[docs/SECURITY.md](docs/SECURITY.md)" in config_guide
    for expected in (
        "[docs/SECURITY.md](docs/SECURITY.md)",
        "[docs/SWAP_INITIATOR_STATE_MACHINES.md](docs/SWAP_INITIATOR_STATE_MACHINES.md)",
        "[docs/STATE_MACHINES.md](docs/STATE_MACHINES.md)",
        "[docs/AUDIT_FINDINGS.md](docs/AUDIT_FINDINGS.md)",
        "`apiauth=1`",
        "Missing mapping -> hold for operator review (no automatic Nexus refund).",
    ):
        assert expected in setup_guide
    assert "nexus.conf must have apiauth=0" not in setup_guide
    assert "Missing mapping -> pending until timeout -> refund." not in setup_guide


def test_ci_workflow_runs_the_required_quality_gates():
    """The checked-in workflow must enforce the release-quality baseline."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    for expected in (
        "pull_request:",
        "push:",
        "actions/checkout@",
        "fetch-depth: 0",
        "actions/setup-python@",
        "python -m pip install -r requirements.txt",
        "python -m pip check",
        "python -m compileall -q src",
        "python -m pytest -q",
        "python scripts/check_markdown_links.py",
        "git diff --check",
    ):
        assert expected in workflow, f"CI workflow is missing required gate: {expected}"
