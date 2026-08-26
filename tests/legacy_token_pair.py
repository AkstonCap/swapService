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
nc.debit_nexus_token_with_txid("someNexusAccount", 12345678, 99)
joined = " ".join(seen["cmd"])
check("debit uses from=BTCn", "from=BTCn" in joined, joined[:110])
check("debit never says USDD", "USDD" not in joined, joined[:110])

print("\n[4] Minimums scale with the configured decimals")
check("8dp minimum formatted correctly", "." in rec["min_to_nexus"] or rec["min_to_nexus"].isdigit(),
      rec["min_to_nexus"])

print("\n[5] Backing math is correct when the two sides have DIFFERENT decimals")
# The original pair was 6dp on both sides, so much of the backing math subtracted a Nexus
# base-unit figure straight from a Solana one. That is off by 10**(difference) for any
# other pair: a fully-backed vault can look 100x over-collateralised, and the surplus
# logic mints against the phantom difference. Every cross-side comparison must convert.
cfg8, _ = load({**BASE, "SOLANA_VAULT_ACCOUNT": "V", "SOLANA_TOKEN_MINT": "M",
                "SOLANA_TOKEN_DECIMALS": "8", "NEXUS_TOKEN_DECIMALS": "6",
                "NEXUS_TOKEN_NAME": "BTCn", "NEXUS_USDD_TREASURY_ACCOUNT": "T"})

# 1.5 tokens, expressed on each side
solana_1_5 = 150_000_000    # 8dp
nexus_1_5 = 1_500_000       # 6dp
check("nexus->solana converts 1.5 exactly", cfg8.nexus_units_to_solana(nexus_1_5) == solana_1_5,
      str(cfg8.nexus_units_to_solana(nexus_1_5)))
check("solana->nexus converts 1.5 exactly", cfg8.solana_units_to_nexus(solana_1_5) == nexus_1_5,
      str(cfg8.solana_units_to_nexus(solana_1_5)))
check("exactly-backed vault has ratio 1, not 100",
      cfg8.nexus_units_to_solana(nexus_1_5) == solana_1_5)
check("no phantom surplus when exactly backed",
      max(0, solana_1_5 - cfg8.nexus_units_to_solana(nexus_1_5)) == 0)

# Rounding direction: a liability must never round down, or a deficit can read as solvent.
cfg6, _ = load({**BASE, "SOLANA_VAULT_ACCOUNT": "V", "SOLANA_TOKEN_MINT": "M",
                "SOLANA_TOKEN_DECIMALS": "6", "NEXUS_TOKEN_DECIMALS": "8",
                "NEXUS_TOKEN_NAME": "BTCn", "NEXUS_USDD_TREASURY_ACCOUNT": "T"})
# 1.00000001 on an 8dp Nexus side does not fit in 6dp; it must round UP to 1.000001
check("liability rounds up, never down", cfg6.nexus_units_to_solana(100_000_001) == 1_000_001,
      str(cfg6.nexus_units_to_solana(100_000_001)))
check("under-backed vault still reads as under-backed",
      1_000_000 < cfg6.nexus_units_to_solana(100_000_001))
# Minting must round DOWN so it can never exceed what the surplus backs.
check("mint rounds down, never up", cfg6.solana_units_to_nexus(1_000_001) == 100_000_100,
      str(cfg6.solana_units_to_nexus(1_000_001)))

# Equal decimals must stay a no-op, so the common case is untouched.
cfgEq, _ = load({**BASE, "SOLANA_VAULT_ACCOUNT": "V", "SOLANA_TOKEN_MINT": "M",
                 "SOLANA_TOKEN_DECIMALS": "6", "NEXUS_TOKEN_DECIMALS": "6",
                 "NEXUS_TOKEN_NAME": "USDD", "NEXUS_USDD_TREASURY_ACCOUNT": "T"})
check("equal decimals is an identity", cfgEq.nexus_units_to_solana(12_345_678) == 12_345_678
      and cfgEq.solana_units_to_nexus(12_345_678) == 12_345_678)

print("\n[6] Fees, thresholds and public terms retain their own token scales")
# Every threshold must be held in the base units of the token it governs.  In
# particular, a 0.5 Solana-side flat fee is 50_000_000 units at 8dp but
# 500_000 units at 6dp; it must never be re-used as a Nexus-side threshold.
# The zero-decimal pair uses whole-token fees because fractional fees cannot be
# represented by that configured pair.
for label, sol_decimals, nexus_decimals, fee_to_nexus, fee_to_solana, expected in (
    ("6/6", 6, 6, "0.1", "0.5", (200_000, 1_000_000, 50_000, "0.2", "1", 9_890_000, 9_490_000)),
    ("8/6", 8, 6, "0.1", "0.5", (20_000_000, 1_000_000, 50_000, "0.2", "1", 9_890_000, 949_000_000)),
    ("6/8", 6, 8, "0.1", "0.5", (200_000, 100_000_000, 5_000_000, "0.2", "1", 989_000_000, 9_490_000)),
    ("9/6", 9, 6, "0.1", "0.5", (200_000_000, 1_000_000, 50_000, "0.2", "1", 9_890_000, 9_490_000_000)),
    ("0/0", 0, 0, "2", "5", (4, 10, 1, "4", "10", 8, 5)),
):
    cfg_case, nc_case = load({
        **BASE, "SOLANA_VAULT_ACCOUNT": "V", "SOLANA_TOKEN_MINT": "M",
        "SOLANA_TOKEN_DECIMALS": str(sol_decimals),
        "NEXUS_TOKEN_DECIMALS": str(nexus_decimals),
        "NEXUS_TOKEN_NAME": "PAIR", "NEXUS_USDD_TREASURY_ACCOUNT": "T",
        "FLAT_FEE_USDD": fee_to_nexus, "FLAT_FEE_USDC": fee_to_solana,
    })
    (expected_deposit, expected_credit, expected_dust, expected_to_nexus,
     expected_to_solana, expected_nexus_output, expected_solana_output) = expected
    check(f"{label}: enforced Solana deposit minimum", cfg_case.MIN_DEPOSIT_SOLANA_UNITS == expected_deposit,
          str(cfg_case.MIN_DEPOSIT_SOLANA_UNITS))
    check(f"{label}: enforced Nexus credit minimum", cfg_case.MIN_CREDIT_NEXUS_UNITS == expected_credit,
          str(cfg_case.MIN_CREDIT_NEXUS_UNITS))
    check(f"{label}: enforced Nexus dust floor", cfg_case.DUST_CREDIT_NEXUS_UNITS == expected_dust,
          str(cfg_case.DUST_CREDIT_NEXUS_UNITS))
    terms = nc_case.build_service_record(last_poll=1)
    check(f"{label}: published min_to_nexus", terms["min_to_nexus"] == expected_to_nexus,
          terms["min_to_nexus"])
    check(f"{label}: published min_to_solana", terms["min_to_solana"] == expected_to_solana,
          terms["min_to_solana"])
    ten_solana_units = 10 * (10 ** sol_decimals)
    ten_nexus_units = 10 * (10 ** nexus_decimals)
    check(f"{label}: exact Solana-to-Nexus output",
          nc_case.get_nexus_send_amount_units(ten_solana_units) == expected_nexus_output,
          str(nc_case.get_nexus_send_amount_units(ten_solana_units)))
    check(f"{label}: exact Nexus-to-Solana output",
          nc_case.get_solana_send_amount_units(ten_nexus_units) == expected_solana_output,
          str(nc_case.get_solana_send_amount_units(ten_nexus_units)))

print()
if fails:
    print(f"❌ {len(fails)} failed: {', '.join(fails)}"); sys.exit(1)
print("✅ bridge is token-pair agnostic; registration record complete; backing math and money terms scale-correct")
