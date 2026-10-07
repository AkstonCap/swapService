"""Recovery refusal must not look like an empty, healthy custody dashboard."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from unittest.mock import Mock

import pytest

from src import dashboard, nexus_client, startup_recovery, state_db


@pytest.fixture
def custody_db(monkeypatch, tmp_path):
    path = tmp_path / "custody.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(path))
    state_db.init_db()
    return path


def test_real_startup_latch_is_visible_and_invalidates_healthy_snapshot(custody_db, monkeypatch):
    monkeypatch.setattr(nexus_client, "get_heartbeat_asset", lambda: {
        "last_safe_timestamp_nexus": "100", "last_safe_timestamp_solana": "200",
    })
    scan = Mock(side_effect=AssertionError("must not scan"))
    monkeypatch.setattr(startup_recovery, "_rebuild_solana_from_waterline", scan)
    state_db.save_metrics_snapshot(
        vault_usdc_units=200, circulating_usdd_units=100, paused=False,
        payouts_24h_units=0, fees_usdc_units=0, fees_usdd_units=0,
    )
    assert startup_recovery.perform_startup_recovery()["recovery_complete"] is False
    scan.assert_not_called()

    summary = dashboard.api_summary()
    assert summary["recovery_admission"]["status"] == "held"
    assert summary["recovery_admission"]["liabilities_complete"] is False
    assert summary["recovery_admission"]["nexus_waterline"] == 100
    assert summary["recovery_admission"]["solana_waterline"] == 200
    assert summary["ratio"] is None
    assert summary["ratio_bps"] is None
    assert summary["payout_24h_solana"] is None
    assert summary["payout_cap_pct"] is None
    assert summary["fees_solana"] is None
    assert summary["fees_nexus"] is None
    assert summary["counts"]["unprocessed_sigs"] == 0  # Local count, not total obligations.

    issues = dashboard.api_issues()
    assert issues["counts"]["issues"] == 1
    issue = issues["issues"][0]
    assert issue["id"] == "custody-recovery-admission"
    assert issue["status"] == "empty_custody_database_recovery_held"
    assert issue["amount"] is None
    assert "unknown" in issue["detail"]
    assert "verified custody backup" in issue["operator_action"]
    assert "do not" in issue["operator_action"]

    with sqlite3.connect(custody_db) as conn:
        before = sorted(conn.iterdump())
    for _ in range(2):
        state_db.init_db()
        assert dashboard.api_summary()["recovery_admission"] == summary["recovery_admission"]
        assert dashboard.api_issues()["issues"] == issues["issues"]
    with sqlite3.connect(custody_db) as conn:
        assert sorted(conn.iterdump()) == before


@pytest.mark.parametrize("damage", ["missing_table", "malformed_waterline", "unexpected_reason"])
def test_unreadable_or_malformed_admission_is_unknown_not_clear(custody_db, damage):
    state_db.latch_empty_custody_recovery(nexus_waterline=100, solana_waterline=200)
    with sqlite3.connect(custody_db) as conn:
        if damage == "missing_table":
            conn.execute("DROP TABLE recovery_admission_holds")
        elif damage == "malformed_waterline":
            conn.execute("UPDATE recovery_admission_holds SET solana_waterline = 'secret-value'")
        else:
            conn.execute("UPDATE recovery_admission_holds SET reason = 'secret-value'")
    summary = dashboard.api_summary()
    assert summary["recovery_admission"]["status"] == "unknown"
    assert summary["recovery_admission"]["liabilities_complete"] is False
    assert summary["ratio"] is None
    assert summary["payout_24h_solana"] is None
    issues = dashboard.api_issues()
    assert issues["counts"]["issues"] == 1
    assert issues["issues"][0]["status"] == "custody_recovery_admission_unavailable"
    assert "secret-value" not in str(summary) + str(issues)


def test_admission_connection_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(dashboard, "_ro_conn", Mock(
        side_effect=sqlite3.OperationalError("secret-path-or-credential"),
    ))
    result = dashboard._recovery_admission_status()
    assert result["status"] == "unknown"
    assert "secret-path-or-credential" not in str(result)


def test_no_latch_is_not_a_recovery_completeness_claim(custody_db):
    state_db.save_metrics_snapshot(
        vault_usdc_units=200, circulating_usdd_units=100, paused=False,
    )
    summary = dashboard.api_summary()
    assert summary["recovery_admission"]["status"] == "unknown"
    assert summary["recovery_admission"]["liabilities_complete"] is False
    assert summary["ratio_bps"] is None
    assert dashboard.api_issues()["issues"][0]["id"] == "custody-recovery-admission"


@pytest.mark.parametrize("admission", ["held", "unknown", "not_held"])
def test_rendered_summary_does_not_present_missing_history_as_zero(custody_db, admission, running_custody):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required to execute the dashboard renderer")
    state_db.save_metrics_snapshot(
        vault_usdc_units=200, circulating_usdd_units=100, paused=True,
    )
    if admission == "held":
        state_db.latch_empty_custody_recovery(nexus_waterline=100, solana_waterline=200)
    elif admission == "unknown":
        with sqlite3.connect(custody_db) as conn:
            conn.execute("DROP TABLE recovery_admission_holds")
    else:
        running_custody()
    payload = {"summary": dashboard.api_summary(), "issues": dashboard.api_issues(), "admission": admission,
               "script": dashboard._PAGE.split("<script>", 1)[1].split("</script>", 1)[0]}
    # Execute the actual shipped renderer against a minimal text-only DOM. No browser,
    # network, package download or additional production dependency is needed in CI.
    script = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
const data = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Element {
  constructor() { this.children = []; this.text = ''; this.style = {}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
}
const roots = {};
const context = vm.createContext({
  document: {
    createElement: () => new Element(),
    createTextNode: text => { const e = new Element(); e.textContent = text; return e; },
    getElementById: id => roots[id] ||= new Element(),
  },
  fetch: () => new Promise(() => {}), setInterval: () => {},
  summary: data.summary, issues: data.issues,
});
vm.runInContext(data.script, context);
vm.runInContext('renderSummary(summary)', context);
vm.runInContext('renderIssues(issues)', context);
const text = roots.banners.textContent;
const cards = roots.cards.children;
const open = cards.find(c => c.textContent.includes('Open items'));
const backing = cards.find(c => c.textContent.includes('Backing ratio'));
if (data.admission === 'not_held') {
  assert.match(text, /backing deficit/);
  assert.doesNotMatch(text, /RECOVERY/);
  assert.equal(open.children[1].textContent, '0');
} else {
  assert.match(text, /RECOVERY/);
  assert.match(text, /unknown/);
  assert.doesNotMatch(text, /refunds and quarantine continue/);
  assert.equal(open.children[1].textContent, 'Unknown');
  assert.match(open.textContent, /local rows only/);
  assert.equal(backing.children[1].textContent, 'Unknown');
  assert.doesNotMatch(backing.children[1].className, /ok/);
  assert.match(roots.panel.textContent, /custody-recovery-admission/);
  assert.match(roots.panel.textContent, /unknown/);
}
'''
    result = subprocess.run([node, "-e", script], input=json.dumps(payload), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
