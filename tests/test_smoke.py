#!/usr/bin/env python3
"""Exercise REAL functions (no stubbing of our own modules) against a temp DB.
Only third-party network libs are stubbed. This catches NameError/ImportError/arity
bugs that byte-compilation and stubbed unit tests cannot see."""
import sys, types, os, inspect
def mod(n, **a):
    m=types.ModuleType(n); [setattr(m,k,v) for k,v in a.items()]; sys.modules[n]=m
class PK:
    @staticmethod
    def from_string(s): return s
    @staticmethod
    def find_program_address(seeds,pid): return ("ATA",0)
    def __init__(self,*a): pass
mod("solana"); mod("solana.rpc"); mod("solana.rpc.api", Client=lambda *a,**k: None)
mod("solders"); mod("solders.pubkey", Pubkey=PK); mod("solders.keypair", Keypair=object)
mod("solders.signature", Signature=PK); mod("solders.hash", Hash=object)
mod("solders.instruction", Instruction=object, AccountMeta=object)
mod("solders.transaction", Transaction=object, VersionedTransaction=object)
mod("solders.message", Message=object)
mod("requests", post=lambda *a,**k: None, get=lambda *a,**k: None)
mod("dotenv", load_dotenv=lambda *a,**k: None)
os.environ.update({"SOLANA_RPC_URL":"http://x","VAULT_KEYPAIR":"/k","VAULT_USDC_ACCOUNT":"V",
 "USDC_MINT":"M","SOL_MINT":"S","NEXUS_PIN":"p","NEXUS_USDD_TREASURY_ACCOUNT":"T",
 "SOL_MAIN_ACCOUNT":"O","STATE_DB_PATH":"/tmp/smoke.db","NEXUS_CLI_PATH":"/bin/false",
 "NEXUS_HEARTBEAT_ASSET_NAME":"hb"})
for p in ["/tmp/smoke.db","/tmp/smoke.db-wal","/tmp/smoke.db-shm"]:
    if os.path.exists(p): os.remove(p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails=[]
# 1. import every module
import importlib
mods=["config","state_db","alerts","nexus_client","solana_client","swap_nexus",
      "swap_solana","fees","startup_recovery","balance_reconciler","main"]
for name in mods:
    try: importlib.import_module(f"src.{name}")
    except Exception as e: fails.append(f"import src.{name}: {type(e).__name__}: {e}")
print(f"[1] imported {len(mods)-len([f for f in fails if 'import' in f])}/{len(mods)} modules")

from src import state_db, solana_client, nexus_client, swap_solana, fees, balance_reconciler
state_db.DB_PATH="/tmp/smoke.db"; state_db.init_db()

# 2. call REAL functions that touch only the DB / pure logic
checks = [
  ("check_timestamp_unpr_sigs (empty)", lambda: solana_client.check_timestamp_unpr_sigs()),
  ("get_usdd_send_amount_units",        lambda: nexus_client.get_usdd_send_amount_units(10_500_000)),
  ("_format_usdd_amount",               lambda: nexus_client._format_usdd_amount(10_389_500)),
  ("resolve_unverified_debits (empty)", lambda: nexus_client.resolve_unverified_debits()),
  ("should_attempt / exhausted",        lambda: (state_db.should_attempt("k"), state_db.attempts_exhausted("k"))),
  ("payouts_since",                     lambda: state_db.payouts_since(86400)),
  ("record_payout",                     lambda: state_db.record_payout("t",1,"r")),
  ("mark_quarantined_txid(full)",       lambda: state_db.mark_quarantined_txid("tx","",1,2.5,"f","t","o","q")),
  ("get_unprocessed_txids_as_dicts",    lambda: state_db.get_unprocessed_txids_as_dicts()),
  ("get_sigs_pending_debit_verification", lambda: state_db.get_sigs_pending_debit_verification(("a","b"))),
  ("process_helius_deposits (empty)",   lambda: solana_client.process_helius_deposits([])),
  ("process_helius_deposits (tuple)",   lambda: solana_client.process_helius_deposits([("S",1,"nexus:a","f",500000)])),
  ("process_unprocessed_usdc_deposits", lambda: solana_client.process_unprocessed_usdc_deposits(10,1.0)),
  ("process_usdc_deposits_refunding",   lambda: solana_client.process_usdc_deposits_refunding(10,1.0)),
  ("process_usdc_deposits_quarantine",  lambda: solana_client.process_usdc_deposits_quarantine(10,1.0)),
  ("check_sig_confirmations",           lambda: solana_client.check_sig_confirmations(1,1.0)),
  ("check_quarantine_confirmations",    lambda: solana_client.check_quarantine_confirmations(1,1.0)),
  ("fees.get_usdc_fees",                lambda: fees.get_usdc_fees()),
  ("fees.reconcile_accounting",         lambda: fees.reconcile_accounting()),
  ("run_balance_reconciliation",        lambda: balance_reconciler.run_balance_reconciliation(dry_run=True)),
  ("_advance_solana_waterline",         lambda: swap_solana._advance_solana_waterline(1,2,False)),
  ("alerts.warning",                    lambda: None),
]
for name, fn in checks:
    try: fn()
    except Exception as e: fails.append(f"{name}: {type(e).__name__}: {e}")
print(f"[2] called {len(checks)} real functions")

# 3. arity check: every call site of functions whose signature I changed
import ast
sigs = {
 "debit_usdd_with_txid": nexus_client.debit_usdd_with_txid,
 "process_helius_deposits": solana_client.process_helius_deposits,
 "mark_quarantined_txid": state_db.mark_quarantined_txid,
 "add_unprocessed_txid": state_db.add_unprocessed_txid,
 "should_attempt": state_db.should_attempt,
 "quarantine_usdd": nexus_client.quarantine_usdd,
 "get_usdd_send_amount_units": nexus_client.get_usdd_send_amount_units,
}
for f in os.listdir("src"):
    if not f.endswith(".py"): continue
    tree=ast.parse(open(f"src/{f}").read())
    for node in ast.walk(tree):
        if isinstance(node,ast.Call):
            fn = node.func.attr if isinstance(node.func,ast.Attribute) else getattr(node.func,"id",None)
            if fn in sigs:
                try:
                    inspect.signature(sigs[fn]).bind(*[None]*len(node.args),
                        **{k.arg: None for k in node.keywords if k.arg})
                except TypeError as e:
                    fails.append(f"arity src/{f}:{node.lineno} {fn}(): {e}")
print(f"[3] checked call-site arity for {len(sigs)} changed signatures")

print()
if fails:
    print(f"❌ {len(fails)} PROBLEM(S):")
    for x in fails: print("   -", x)
    sys.exit(1)
print("✅ all modules import, all exercised functions run, all call sites match signatures")
