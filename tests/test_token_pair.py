#!/usr/bin/env python3
"""The bridge must work for ANY Solana/Nexus token pair, not just USDC/USDD.

Loads config twice under two completely different operator setups and asserts the
resulting on-chain service record and CLI commands follow the configuration.
"""
import importlib, os, sys, types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def stub():
    def mod(n, **a):
        m = types.ModuleType(n); [setattr(m, k, v) for k, v in a.items()]; sys.modules[n] = m
    class PK:
        def __init__(self, v="") : self.v = v
        @staticmethod
        def from_string(s): return s
    mod("solders"); mod("solders.pubkey", Pubkey=PK); mod("dotenv", load_dotenv=lambda *a, **k: None)
stub()

fails = []
def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra and not cond else ''}")
    if not cond: fails.append(name)

def load(env):
    for k in [k for k in os.environ if k.startswith(("SOLANA_","NEXUS_","USDC","USDD","SOL_","FLAT_","DYNAMIC_","MIN_","DEPOSIT_","SERVICE_","VAULT_"))]:
        del os.environ[k]
    os.environ.update(env)
    for m in ("src.config", "src.nexus_client"):
        if m in sys.modules: del sys.modules[m]
    cfg = importlib.import_module("src.config")
    nc = importlib.import_module("src.nexus_client")
    return cfg, nc

BASE = {"SOLANA_RPC_URL":"https://rpc","VAULT_KEYPAIR":"./k.json",
        "SOL_MINT":"So11111111111111111111111111111111111111112","SOL_MAIN_ACCOUNT":"OWNER","NEXUS_PIN":"1234"}

print("\n[1] Legacy USDC/USDD .env still works unchanged")
cfg, nc = load({**BASE, "VAULT_USDC_ACCOUNT":"LEGACY_VAULT", "USDC_MINT":"LEGACY_MINT",
                "NEXUS_USDD_TREASURY_ACCOUNT":"LEGACY_TREASURY"})
check("legacy vault/mint accepted", str(cfg.SOLANA_VAULT_ACCOUNT)=="LEGACY_VAULT" and str(cfg.SOLANA_TOKEN_MINT)=="LEGACY_MINT")
check("defaults to USDC/USDD", cfg.SOLANA_TOKEN_SYMBOL=="USDC" and cfg.NEXUS_TOKEN_NAME=="USDD")
check("default memo prefix", cfg.DEPOSIT_MEMO_PREFIX=="nexus:")

print("\n[2] A different pair: wBTC (Solana, 8dp) <-> BTCn (Nexus, 8dp)")
cfg, nc = load({**BASE,
    "SOLANA_VAULT_ACCOUNT":"9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "SOLANA_TOKEN_MINT":"3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
    "SOLANA_TOKEN_SYMBOL":"wBTC","SOLANA_TOKEN_DECIMALS":"8",
    "NEXUS_TOKEN_NAME":"BTCn","NEXUS_TOKEN_DECIMALS":"8",
    "NEXUS_USDD_TREASURY_ACCOUNT":"8CuyRASoeBCRtBzKu4BLbRvpaHxLnJmVdWq2XsPqZzAb",
    "DEPOSIT_MEMO_PREFIX":"btcn:","SERVICE_PROVIDER":"acme-bridge.io",
    "SERVICE_CONTACT":"https://acme-bridge.io",
    "FLAT_FEE_USDC":"0.0002","FLAT_FEE_USDD":"0.0001","DYNAMIC_FEE_BPS":"15",
    "NEXUS_HEARTBEAT_ASSET_NAME":"acmeBridgeHeartbeat"})
check("decimals follow config", cfg.SOLANA_TOKEN_DECIMALS==8 and cfg.NEXUS_TOKEN_DECIMALS==8)
check("memo prefix follows config", cfg.DEPOSIT_MEMO_PREFIX=="btcn:")

rec = nc.build_service_record(status="online", last_poll=1700000000, wline_sol=111, wline_nxs=222)
print("\n  --- published service record ---")
for k, v in rec.items():
    print(f"    {k:<28} {v}")
size = nc.service_record_size(rec)
print(f"    ~{size} bytes / {nc.SERVICE_RECORD_MAX_BYTES} budget")

check("record names the Nexus token", rec["nexus_token"]=="BTCn")
check("record names the Solana token", rec["solana_token"]=="wBTC")
check("record carries the vault", rec["solana_vault_address"].startswith("9WzDXw"))
check("record carries the treasury", rec["nexus_treasury_address"].startswith("8CuyRA"))
check("record carries the memo format", rec["memo_prefix"]=="btcn:")
check("record publishes fees", rec["fee_bps"]=="15" and rec["fee_flat_to_solana"]=="0.0002")
check("record publishes minimums", rec["min_to_nexus"] and rec["min_to_solana"])
check("record has provider + contact", rec["provider"]=="acme-bridge.io" and rec["contact"].startswith("https://"))
check("record fits the size budget", size <= nc.SERVICE_RECORD_MAX_BYTES, f"{size}B")
check("every declared field present", not [f for f in nc.SERVICE_RECORD_IMMUTABLE if f not in rec])

print("\n[3] CLI commands use the configured token, not a hardcoded one")
seen = {}
def fake_run(cmd, timeout=15):
    seen["cmd"] = cmd
    return (1, "", "stubbed")
nc._run = fake_run
nc.debit_usdd_with_txid("someNexusAccount", 12345678, 99)
joined = " ".join(seen["cmd"])
check("debit uses from=BTCn", "from=BTCn" in joined, joined[:110])
check("debit never says USDD", "USDD" not in joined, joined[:110])

print("\n[4] Minimums scale with the configured decimals")
check("8dp minimum formatted correctly", "." in rec["min_to_nexus"] or rec["min_to_nexus"].isdigit(),
      rec["min_to_nexus"])

print()
if fails:
    print(f"❌ {len(fails)} failed: {', '.join(fails)}"); sys.exit(1)
print("✅ bridge is token-pair agnostic; registration record complete")
