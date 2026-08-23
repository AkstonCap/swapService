"""Read-only operator dashboard for swapService.

Design constraints, and why:

* **Read-only, always.** It opens the state database with SQLite's `mode=ro` URI, so it
  physically cannot mutate swap state or hold a write lock against the service loop.
  There is deliberately no "retry", "refund" or "release" button: a web endpoint that can
  move funds is a far larger attack surface than one that can only look. Manual
  intervention stays on the CLI, where it is authenticated by shell access.
* **No new dependencies.** Built on `http.server` from the standard library. A custodial
  bridge should not grow a web-framework supply chain for a monitoring page.
* **No chain credentials.** Balances and ratios come from the `metrics_snapshot` row the
  service writes each cycle. The dashboard never talks to Solana or Nexus, so it needs no
  vault keypair, no Nexus PIN and no RPC key.
* **Everything is escaped.** Deposit memos and counterparty addresses are attacker-
  controlled. They are rendered into the browser of the person who authorises fund
  recovery, which makes stored XSS here a privileged-context bug. Values reach the page
  only as JSON (never interpolated into HTML) and are inserted with `textContent`.
* **Localhost by default.** Binds 127.0.0.1 unless explicitly overridden, and requires a
  token if bound anywhere else.
"""
from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import state_db

try:
    from . import config as _cfg
    USDC_DECIMALS = int(_cfg.SOLANA_TOKEN_DECIMALS)
    USDD_DECIMALS = int(_cfg.NEXUS_TOKEN_DECIMALS)
    SOL_SYM = str(_cfg.SOLANA_TOKEN_SYMBOL)
    NXS_SYM = str(_cfg.NEXUS_TOKEN_NAME)
except Exception:
    # Dashboard must still start if config cannot load (e.g. no chain env present).
    USDC_DECIMALS = int(os.getenv("SOLANA_TOKEN_DECIMALS", os.getenv("USDC_DECIMALS", "6")))
    USDD_DECIMALS = int(os.getenv("NEXUS_TOKEN_DECIMALS", os.getenv("USDD_DECIMALS", "6")))
    SOL_SYM = os.getenv("SOLANA_TOKEN_SYMBOL", "USDC")
    NXS_SYM = os.getenv("NEXUS_TOKEN_NAME", "USDD")

# Statuses that mean "a human should look at this".
SIG_ISSUE_STATUSES = (
    "debit unverified", "debit in flight", "to be refunded", "to be quarantined",
    "quarantine failed", "refund pending",
)
TXID_ISSUE_STATUSES = (
    "quarantined", "refund pending", "collecting refund", "trade balance to be checked",
)


def _ro_conn() -> sqlite3.Connection:
    """Open the state DB strictly read-only, so the dashboard can never corrupt state."""
    path = os.path.abspath(state_db.DB_PATH)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _rows(sql: str, params=()) -> list[dict]:
    conn = _ro_conn()
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _scalar(sql: str, params=(), default=0):
    conn = _ro_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default
    finally:
        conn.close()


def _units(v, decimals: int) -> float | None:
    if v is None:
        return None
    try:
        return int(v) / (10 ** decimals)
    except Exception:
        return None


# --------------------------------------------------------------------------- API


def api_summary() -> dict:
    snap = state_db.get_metrics_snapshot() or {}
    now = int(time.time())
    ratio_bps = snap.get("ratio_bps")

    hb = _rows("SELECT name, last_beat, wline_sol, wline_nxs FROM heartbeat LIMIT 1")
    hb = hb[0] if hb else {}

    counts = {
        "unprocessed_sigs": _scalar("SELECT COUNT(*) FROM unprocessed_sigs"),
        "unprocessed_txids": _scalar("SELECT COUNT(*) FROM unprocessed_txids"),
        "processed_sigs": _scalar("SELECT COUNT(*) FROM processed_sigs"),
        "processed_txids": _scalar("SELECT COUNT(*) FROM processed_txids"),
        "refunded_sigs": _scalar("SELECT COUNT(*) FROM refunded_sigs"),
        "refunded_txids": _scalar("SELECT COUNT(*) FROM refunded_txids"),
        "quarantined_sigs": _scalar("SELECT COUNT(*) FROM quarantined_sigs"),
        "quarantined_txids": _scalar("SELECT COUNT(*) FROM quarantined_txids"),
    }

    cap = 0
    try:
        from . import config
        cap = int(getattr(config, "DAILY_PAYOUT_CAP_USDC_UNITS", 0) or 0)
    except Exception:
        cap = 0
    spent = snap.get("payouts_24h_units")
    if spent is None:
        spent = _scalar(
            "SELECT COALESCE(SUM(amount_usdc_units),0) FROM payouts WHERE timestamp >= ?",
            (now - 86400,),
        )

    snap_age = (now - int(snap["timestamp"])) if snap.get("timestamp") else None

    return {
        "now": now,
        "snapshot_age_sec": snap_age,
        "snapshot_stale": snap_age is None or snap_age > 300,
        "paused": bool(snap.get("paused")),
        "vault_usdc": _units(snap.get("vault_usdc_units"), USDC_DECIMALS),
        "circulating_usdd": _units(snap.get("circulating_usdd_units"), USDD_DECIMALS),
        "ratio": (ratio_bps / 10000.0) if ratio_bps is not None else None,
        "ratio_bps": ratio_bps,
        "fees_usdc": _units(snap.get("fees_usdc_units"), USDC_DECIMALS),
        "fees_usdd": _units(snap.get("fees_usdd_units"), USDD_DECIMALS),
        "payout_cap_usdc": _units(cap, USDC_DECIMALS) if cap else None,
        "payout_24h_usdc": _units(spent, USDC_DECIMALS),
        "payout_cap_pct": (100.0 * spent / cap) if cap else None,
        "heartbeat": {
            "name": hb.get("name"),
            "last_beat": hb.get("last_beat"),
            "age_sec": (now - int(hb["last_beat"])) if hb.get("last_beat") else None,
            "waterline_solana": hb.get("wline_sol"),
            "waterline_nexus": hb.get("wline_nxs"),
        },
        "counts": counts,
    }


def api_issues() -> dict:
    """Anything an operator should act on, newest first, with a plain-language reason."""
    now = int(time.time())
    issues: list[dict] = []

    marks = ",".join("?" for _ in SIG_ISSUE_STATUSES)
    for r in _rows(
        f"""SELECT sig, timestamp, status, amount_usdc_units, from_address, memo, reference
            FROM unprocessed_sigs WHERE status IN ({marks})
            ORDER BY timestamp ASC LIMIT 200""", SIG_ISSUE_STATUSES):
        issues.append({
            "kind": f"{SOL_SYM}→{NXS_SYM}",
            "id": r["sig"],
            "status": r["status"],
            "age_sec": (now - int(r["timestamp"])) if r.get("timestamp") else None,
            "amount": _units(r.get("amount_usdc_units"), USDC_DECIMALS),
            "unit": SOL_SYM,
            "counterparty": r.get("from_address"),
            "detail": r.get("memo"),
            "reference": r.get("reference"),
        })

    marks = ",".join("?" for _ in TXID_ISSUE_STATUSES)
    for r in _rows(
        f"""SELECT txid, timestamp, status, amount_usdd, amount_usdd_units, from_address, receival_account
            FROM unprocessed_txids WHERE status IN ({marks})
            ORDER BY timestamp ASC LIMIT 200""", TXID_ISSUE_STATUSES):
        amt = _units(r.get("amount_usdd_units"), USDD_DECIMALS)
        issues.append({
            "kind": f"{NXS_SYM}→{SOL_SYM}",
            "id": r["txid"],
            "status": r["status"],
            "age_sec": (now - int(r["timestamp"])) if r.get("timestamp") else None,
            "amount": amt if amt is not None else r.get("amount_usdd"),
            "unit": NXS_SYM,
            "counterparty": r.get("from_address"),
            "detail": r.get("receival_account"),
            "reference": None,
        })

    # Quarantined USDD that was NOT actually moved still counts toward backing.
    for r in _rows("""SELECT txid, timestamp, status, amount_usdd, from_address
                      FROM quarantined_txids WHERE status LIKE '%NOT moved%'
                      ORDER BY timestamp DESC LIMIT 100"""):
        issues.append({
            "kind": f"{NXS_SYM} quarantine",
            "id": r["txid"],
            "status": r["status"],
            "age_sec": (now - int(r["timestamp"])) if r.get("timestamp") else None,
            "amount": r.get("amount_usdd"),
            "unit": NXS_SYM,
            "counterparty": r.get("from_address"),
            "detail": "still in treasury — overstates the backing ratio",
            "reference": None,
        })

    # Actions that have burned their retry budget.
    max_attempts = 3
    try:
        from . import config
        max_attempts = int(getattr(config, "MAX_ACTION_ATTEMPTS", 3))
    except Exception:
        pass
    exhausted = _rows(
        "SELECT action_key, count, last_timestamp FROM attempts WHERE count >= ? "
        "ORDER BY last_timestamp DESC LIMIT 100", (max_attempts,))

    return {
        "now": now,
        "issues": issues,
        "exhausted_attempts": exhausted,
        "counts": {"issues": len(issues), "exhausted": len(exhausted)},
    }


_TX_SOURCES = {
    "pending_usdc": ("""SELECT sig AS id, timestamp, status, amount_usdc_units AS amount,
                               from_address AS counterparty, memo AS detail
                        FROM unprocessed_sigs ORDER BY timestamp DESC LIMIT ?""", USDC_DECIMALS, SOL_SYM),
    "pending_usdd": ("""SELECT txid AS id, timestamp, status, amount_usdd_units AS amount,
                               from_address AS counterparty, receival_account AS detail
                        FROM unprocessed_txids ORDER BY timestamp DESC LIMIT ?""", USDD_DECIMALS, NXS_SYM),
    "completed_usdc": ("""SELECT sig AS id, timestamp, status, amount_usdc_units AS amount,
                                 txid AS counterparty, CAST(reference AS TEXT) AS detail
                          FROM processed_sigs ORDER BY timestamp DESC LIMIT ?""", USDC_DECIMALS, SOL_SYM),
    "completed_usdd": ("""SELECT txid AS id, timestamp, status, NULL AS amount,
                                 from_address AS counterparty, sig AS detail
                          FROM processed_txids ORDER BY timestamp DESC LIMIT ?""", USDD_DECIMALS, NXS_SYM),
    "refunded_usdc": ("""SELECT sig AS id, timestamp, status, refunded_units AS amount,
                                from_address AS counterparty, refund_sig AS detail
                         FROM refunded_sigs ORDER BY timestamp DESC LIMIT ?""", USDC_DECIMALS, SOL_SYM),
    "refunded_usdd": ("""SELECT txid AS id, timestamp, status, NULL AS amount,
                                from_address AS counterparty, sig AS detail
                         FROM refunded_txids ORDER BY timestamp DESC LIMIT ?""", USDD_DECIMALS, NXS_SYM),
    "quarantined_usdc": ("""SELECT sig AS id, timestamp, status, quarantined_units AS amount,
                                   from_address AS counterparty, quarantine_sig AS detail
                            FROM quarantined_sigs ORDER BY timestamp DESC LIMIT ?""", USDC_DECIMALS, SOL_SYM),
    "quarantined_usdd": ("""SELECT txid AS id, timestamp, status, NULL AS amount,
                                   from_address AS counterparty, sig AS detail
                            FROM quarantined_txids ORDER BY timestamp DESC LIMIT ?""", USDD_DECIMALS, NXS_SYM),
    "fees": ("""SELECT CAST(id AS TEXT) AS id, timestamp, kind AS status,
                       COALESCE(amount_usdc_units, amount_usdd_units) AS amount,
                       sig AS counterparty, txid AS detail
                FROM fee_entries ORDER BY timestamp DESC LIMIT ?""", USDC_DECIMALS, "units"),
    "payouts": ("""SELECT CAST(id AS TEXT) AS id, timestamp, kind AS status,
                          amount_usdc_units AS amount, reference AS counterparty,
                          NULL AS detail
                   FROM payouts ORDER BY timestamp DESC LIMIT ?""", USDC_DECIMALS, SOL_SYM),
}


def api_transactions(source: str, limit: int = 100) -> dict:
    spec = _TX_SOURCES.get(source)
    if not spec:
        return {"error": "unknown source", "sources": sorted(_TX_SOURCES)}
    sql, decimals, unit = spec
    limit = max(1, min(500, int(limit)))
    out = []
    for r in _rows(sql, (limit,)):
        out.append({
            "id": r.get("id"),
            "timestamp": r.get("timestamp"),
            "status": r.get("status"),
            "amount": _units(r.get("amount"), decimals),
            "unit": unit,
            "counterparty": r.get("counterparty"),
            "detail": r.get("detail"),
        })
    return {"source": source, "unit": unit, "rows": out, "count": len(out)}


# --------------------------------------------------------------------------- HTTP


def _auth_token() -> str | None:
    return os.getenv("DASHBOARD_TOKEN") or None


class _Handler(BaseHTTPRequestHandler):
    server_version = "swapServiceDashboard"

    def log_message(self, fmt, *args):  # keep the service log readable
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page renders attacker-controlled text; lock the browser down hard.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def _authorised(self) -> bool:
        token = _auth_token()
        if not token:
            return True
        supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not supplied:
            supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return secrets.compare_digest(supplied, token)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if not self._authorised():
            return self._json(401, {"error": "unauthorized"})

        try:
            if route == "/":
                return self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            if route == "/api/summary":
                return self._json(200, api_summary())
            if route == "/api/issues":
                return self._json(200, api_issues())
            if route == "/api/transactions":
                q = parse_qs(parsed.query)
                return self._json(200, api_transactions(
                    q.get("source", ["pending_usdc"])[0], int(q.get("limit", ["100"])[0])))
            if route == "/api/health":
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": type(e).__name__, "message": str(e)})

    # Read-only by construction: no mutating verbs are implemented.
    def do_POST(self):
        self._json(405, {"error": "this dashboard is read-only"})

    do_PUT = do_DELETE = do_PATCH = do_POST


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(port or os.getenv("DASHBOARD_PORT", "8787"))
    token = _auth_token()

    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit(
            f"Refusing to bind {host} without DASHBOARD_TOKEN set.\n"
            "This dashboard exposes counterparty addresses, amounts and operational state.\n"
            "Either bind 127.0.0.1 (default) and reach it over an SSH tunnel, or set\n"
            "DASHBOARD_TOKEN and put it behind TLS."
        )

    print(f"Operator dashboard: http://{host}:{port}  (read-only, DB={state_db.DB_PATH})")
    if not token:
        print("  no DASHBOARD_TOKEN set — localhost only")
    ThreadingHTTPServer((host, port), _Handler).serve_forever()


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>swapService — operator</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0e1116;--panel:#161b22;--line:#272e38;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff}
@media(prefers-color-scheme:light){:root{--bg:#f6f8fa;--panel:#fff;--line:#d0d7de;
--fg:#1f2328;--dim:#656d76;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--accent:#0969da}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:baseline;gap:12px;padding:16px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.muted{color:var(--dim);font-size:12px}
.wrap{padding:20px;max-width:1400px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.card .v{font-size:22px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
.card .s{font-size:12px;color:var(--dim);margin-top:2px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.bar{height:4px;background:var(--line);border-radius:2px;margin-top:8px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent)}
nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:6px 11px;cursor:pointer;font-size:13px}
button[aria-selected=true]{background:var(--accent);border-color:var(--accent);color:#fff}
table{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
font-size:12.5px;vertical-align:top}
th{color:var(--dim);font-weight:600;text-transform:uppercase;font-size:10.5px;letter-spacing:.05em}
tr:last-child td{border-bottom:none}
td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
word-break:break-all;max-width:260px}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
border:1px solid var(--line);white-space:nowrap}
.banner{padding:10px 14px;border-radius:8px;margin-bottom:14px;border:1px solid}
.banner.bad{background:rgba(248,81,73,.1);border-color:var(--bad)}
.banner.warn{background:rgba(210,153,34,.1);border-color:var(--warn)}
.empty{padding:22px;text-align:center;color:var(--dim)}
</style></head><body>
<header><h1>swapService</h1><span class="muted">operator dashboard · read-only</span>
<span class="muted" id="upd" style="margin-left:auto"></span></header>
<div class="wrap">
  <div id="banners"></div>
  <div class="cards" id="cards"></div>
  <nav id="tabs"></nav>
  <div id="panel"></div>
</div>
<script>
// Values are inserted with textContent only. Memos and addresses are supplied by
// depositors, so nothing from the API is ever parsed as HTML.
const F=(n,d=6)=>n==null?"—":Number(n).toLocaleString(undefined,{maximumFractionDigits:d});
const AGE=s=>s==null?"—":s<60?s+"s":s<3600?Math.floor(s/60)+"m":s<86400?Math.floor(s/3600)+"h":Math.floor(s/86400)+"d";
const TS=t=>t?new Date(t*1000).toISOString().replace("T"," ").slice(0,19):"—";
const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e;};
const TABS=[["issues","Issues"],["pending_usdc","Pending USDC→USDD"],["pending_usdd","Pending USDD→USDC"],
 ["completed_usdc","Completed USDC→USDD"],["completed_usdd","Completed USDD→USDC"],
 ["refunded_usdc","Refunds USDC"],["refunded_usdd","Refunds USDD"],
 ["quarantined_usdc","Quarantine USDC"],["quarantined_usdd","Quarantine USDD"],
 ["payouts","Payouts 24h"],["fees","Fees"]];
let tab="issues";
const qs=new URLSearchParams(location.search), tok=qs.get("token");
const api=p=>fetch(p+(tok?(p.includes("?")?"&":"?")+"token="+encodeURIComponent(tok):""))
  .then(r=>{if(!r.ok)throw new Error("HTTP "+r.status);return r.json()});

function card(k,v,s,cls){const c=el("div","card");c.append(el("div","k",k));
  const val=el("div","v "+(cls||""),v);c.append(val);if(s)c.append(el("div","s",s));return c;}

function renderSummary(d){
  const b=document.getElementById("banners");b.textContent="";
  if(d.paused){const x=el("div","banner bad");
    x.append(el("strong",null,"PAUSED — backing deficit. "));
    x.append(document.createTextNode("New swaps are stopped; refunds and quarantine continue."));b.append(x);}
  if(d.snapshot_stale){const x=el("div","banner warn");
    x.append(el("strong",null,"Metrics are stale. "));
    x.append(document.createTextNode("Last snapshot "+AGE(d.snapshot_age_sec)+" ago — the service may be down or wedged."));b.append(x);}
  if(d.ratio!=null&&d.ratio<1){const x=el("div","banner bad");
    x.append(el("strong",null,"Under-collateralised. "));
    x.append(document.createTextNode("Vault USDC is below circulating USDD."));b.append(x);}

  const c=document.getElementById("cards");c.textContent="";
  const rc=d.ratio==null?"":d.ratio>=1?"ok":d.ratio>=0.99?"warn":"bad";
  c.append(card("Backing ratio", d.ratio==null?"—":d.ratio.toFixed(4),
    d.ratio_bps!=null?d.ratio_bps+" bps":"vault ÷ circulating", rc));
  c.append(card("Vault USDC", F(d.vault_usdc,2), "on Solana"));
  c.append(card("Circulating USDD", F(d.circulating_usdd,2), "on Nexus"));
  c.append(card("Open items",
    (d.counts.unprocessed_sigs+d.counts.unprocessed_txids),
    d.counts.unprocessed_sigs+" USDC · "+d.counts.unprocessed_txids+" USDD"));
  c.append(card("Quarantined",
    (d.counts.quarantined_sigs+d.counts.quarantined_txids),
    "needs manual review",
    (d.counts.quarantined_sigs+d.counts.quarantined_txids)>0?"warn":""));
  c.append(card("Fees collected", F(d.fees_usdc,2)+" / "+F(d.fees_usdd,2), "USDC / USDD"));
  const hb=card("Heartbeat", AGE(d.heartbeat.age_sec),
    d.heartbeat.name||"not configured",
    d.heartbeat.age_sec==null?"bad":d.heartbeat.age_sec>600?"warn":"ok");
  c.append(hb);
  if(d.payout_cap_usdc!=null){
    const pc=card("24h payouts", F(d.payout_24h_usdc,2),
      "cap "+F(d.payout_cap_usdc,2)+" USDC", d.payout_cap_pct>=90?"bad":d.payout_cap_pct>=70?"warn":"");
    const bar=el("div","bar");const i=el("i");i.style.width=Math.min(100,d.payout_cap_pct||0)+"%";
    bar.append(i);pc.append(bar);c.append(pc);
  } else {
    c.append(card("24h payouts", F(d.payout_24h_usdc,2), "no cap configured"));
  }
  document.getElementById("upd").textContent="updated "+new Date().toLocaleTimeString();
}

function table(cols,rows,build){
  if(!rows.length){const e=el("div","empty","Nothing here.");return e;}
  const t=el("table"),th=el("tr");cols.forEach(c=>th.append(el("th",null,c)));
  t.append(el("thead")).append(th);const tb=el("tbody");
  rows.forEach(r=>tb.append(build(r)));t.append(tb);return t;
}

function renderIssues(d){
  const p=document.getElementById("panel");p.textContent="";
  const rows=d.issues;
  p.append(table(["Age","Direction","Status","Amount","Counterparty","Detail","ID"],rows,r=>{
    const tr=el("tr");
    tr.append(el("td",null,AGE(r.age_sec)));
    tr.append(el("td",null,r.kind));
    const st=el("td");st.append(el("span","pill",r.status||"—"));tr.append(st);
    tr.append(el("td","num",r.amount==null?"—":F(r.amount,6)+" "+r.unit));
    tr.append(el("td","mono",r.counterparty||"—"));
    tr.append(el("td","mono",r.detail||"—"));
    tr.append(el("td","mono",r.id||"—"));
    return tr;}));
  if(d.exhausted_attempts.length){
    p.append(el("h3",null,"Retry budget exhausted"));
    p.append(table(["Action","Attempts","Last"],d.exhausted_attempts,a=>{
      const tr=el("tr");
      tr.append(el("td","mono",a.action_key));
      tr.append(el("td","num",a.count));
      tr.append(el("td",null,TS(a.last_timestamp)));
      return tr;}));
  }
}

function renderTx(d){
  const p=document.getElementById("panel");p.textContent="";
  p.append(table(["Time","Status","Amount","Counterparty","Detail","ID"],d.rows,r=>{
    const tr=el("tr");
    tr.append(el("td",null,TS(r.timestamp)));
    const st=el("td");st.append(el("span","pill",r.status||"—"));tr.append(st);
    tr.append(el("td","num",r.amount==null?"—":F(r.amount,6)+" "+r.unit));
    tr.append(el("td","mono",r.counterparty||"—"));
    tr.append(el("td","mono",r.detail||"—"));
    tr.append(el("td","mono",r.id||"—"));
    return tr;}));
}

function tabs(){const n=document.getElementById("tabs");n.textContent="";
  TABS.forEach(([k,label])=>{const b=el("button",null,label);
    b.setAttribute("aria-selected",k===tab);
    b.onclick=()=>{tab=k;tabs();load();};n.append(b);});}

function load(){
  api("/api/summary").then(renderSummary).catch(e=>{
    document.getElementById("upd").textContent="error: "+e.message;});
  if(tab==="issues") api("/api/issues").then(renderIssues).catch(()=>{});
  else api("/api/transactions?source="+tab+"&limit=200").then(renderTx).catch(()=>{});
}
tabs();load();setInterval(load,15000);
</script></body></html>
"""
