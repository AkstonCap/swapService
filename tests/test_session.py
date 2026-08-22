#!/usr/bin/env python3
"""Nexus multiuser/session handling.

The rule, from the bundled API docs: `register/*` never takes a session; finance/*,
assets/*, market/*, supply/* require `session=<id>` when the node runs multiuser=1, and
must NOT receive one in single-user mode ("For single-user API mode the session should
not be supplied").
"""
import os, sys, types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def mod(n, **a):
    m = types.ModuleType(n); [setattr(m, k, v) for k, v in a.items()]; sys.modules[n] = m
class PK:
    @staticmethod
    def from_string(s): return s
mod("solders"); mod("solders.pubkey", Pubkey=PK); mod("dotenv", load_dotenv=lambda *a, **k: None)
os.environ.update({"SOLANA_RPC_URL":"x","VAULT_KEYPAIR":"/k","VAULT_USDC_ACCOUNT":"V","USDC_MINT":"M",
 "SOL_MINT":"S","NEXUS_PIN":"PIN123","NEXUS_USDD_TREASURY_ACCOUNT":"T","SOL_MAIN_ACCOUNT":"O",
 "STATE_DB_PATH":"/tmp/sess.db"})
from src import config, nexus_client as nc  # noqa: E402

fails = []
def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra and not cond else ''}")
    if not cond: fails.append(name)

CLI = config.NEXUS_CLI
# Every command family the service actually issues, with the documented expectation.
CASES = [
    (True,  [CLI, "finance/debit/token", "from=USDD", "to=x", "amount=1", "pin=PIN123"]),
    (True,  [CLI, "finance/debit/account", "from=a", "to=b", "amount=1"]),
    (True,  [CLI, "finance/get/token/currentsupply", "name=USDD"]),
    (True,  [CLI, "finance/get/account", "name=default"]),
    (True,  [CLI, "finance/transactions/token/txid,confirmations", "name=USDD"]),
    (True,  [CLI, "assets/update/asset", "name=hb", "format=basic"]),
    (True,  [CLI, "assets/get/asset", "name=hb"]),
    (True,  [CLI, "market/list/bid", "market=USDD/NXS"]),
    (True,  [CLI, "market/execute/order", "txid=abc"]),
    (False, [CLI, "register/get/finance:account", "address=abc"]),
    (False, [CLI, "register/list/assets:asset/owner,txid_toService", "results.owner=x"]),
    (False, [CLI, "register/transactions/finance:token/txid,timestamp", "name=USDD"]),
]

print("\n[1] Endpoint classification matches the API docs")
for expected, cmd in CASES:
    got = nc.needs_session(cmd)
    check(f"{'session' if expected else 'no sess'}  {cmd[1][:46]}", got == expected, f"got {got}")

print("\n[2] Single-user mode: session must NOT be supplied")
config.NEXUS_MULTIUSER = False
config.NEXUS_SESSION = "SESSION-ABC"
for _, cmd in CASES:
    out = nc.apply_session(cmd)
    if any(str(a).startswith("session=") for a in out):
        check("no session added in single-user mode", False, cmd[1]); break
else:
    check("no session added in single-user mode", True)

print("\n[3] Multiuser mode: session added to session-scoped calls only")
config.NEXUS_MULTIUSER = True
for expected, cmd in CASES:
    out = nc.apply_session(cmd)
    has = any(str(a) == "session=SESSION-ABC" for a in out)
    check(f"{'added' if expected else 'omitted'}  {cmd[1][:44]}", has == expected, f"got {has}")

print("\n[4] Edge cases")
already = [CLI, "finance/debit/token", "session=EXPLICIT", "to=x"]
check("existing session not duplicated",
      sum(1 for a in nc.apply_session(already) if str(a).startswith("session=")) == 1)
config.NEXUS_SESSION = ""
check("multiuser with empty session adds nothing",
      not any(str(a).startswith("session=") for a in nc.apply_session(CASES[0][1])))

print("\n[5] Startup validation")
config.NEXUS_MULTIUSER, config.NEXUS_SESSION = True, ""
ok, msg = nc.validate_session_config()
check("multiuser + no session -> refuses", ok is False, msg)
config.NEXUS_SESSION = "SESSION-ABC"
ok, msg = nc.validate_session_config()
check("multiuser + session -> ok", ok is True, msg)
check("session id not echoed in full", "SESSION-ABC" not in msg, msg)
config.NEXUS_MULTIUSER = False
ok, msg = nc.validate_session_config()
check("single-user + stray session -> warns but starts", ok is True and "NOT" in msg, msg)

print("\n[6] Secrets redacted from logs")
config.NEXUS_MULTIUSER, config.NEXUS_SESSION = True, "SESSION-ABC"
red = nc.redact("error: pin=PIN123 session=SESSION-ABC rejected")
check("PIN redacted", "PIN123" not in red, red)
check("session redacted", "SESSION-ABC" not in red, red)

print()
if fails:
    print(f"❌ {len(fails)} failed: {', '.join(fails)}"); sys.exit(1)
print("✅ session handling correct in both modes")
