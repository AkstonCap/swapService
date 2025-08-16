import os
from dotenv import load_dotenv
from solders.pubkey import Pubkey as PublicKey

load_dotenv()

REQUIRED_ENV = [
    "SOLANA_RPC_URL",
    "VAULT_KEYPAIR",
    "VAULT_USDC_ACCOUNT",
    "USDC_MINT",
    "NEXUS_PIN",
    "NEXUS_USDD_TREASURY_ACCOUNT",
    "NEXUS_USDD_FEES_ACCOUNT",
]
for var in REQUIRED_ENV:
    if not os.getenv(var):
        raise ValueError(f"Required environment variable {var} is not set")

# Solana
RPC_URL = os.getenv("SOLANA_RPC_URL")
VAULT_KEYPAIR_PATH = os.getenv("VAULT_KEYPAIR")
VAULT_USDC_ACCOUNT = PublicKey.from_string(os.getenv("VAULT_USDC_ACCOUNT"))
USDC_MINT = PublicKey.from_string(os.getenv("USDC_MINT"))
SOL_MINT = PublicKey.from_string(os.getenv("SOL_MINT"))
MEMO_PROGRAM_ID = PublicKey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")

# Decimals
USDC_DECIMALS = int(os.getenv("USDC_DECIMALS", "6"))
USDD_DECIMALS = int(os.getenv("USDD_DECIMALS", "6"))

# Nexus
NEXUS_CLI = os.getenv("NEXUS_CLI_PATH", "./nexus")
NEXUS_TOKEN_NAME = os.getenv("NEXUS_TOKEN_NAME", "USDD")
NEXUS_RPC_HOST = os.getenv("NEXUS_RPC_HOST", "http://127.0.0.1:8399")
NEXUS_USDD_TREASURY_ACCOUNT = os.getenv("NEXUS_USDD_TREASURY_ACCOUNT")
NEXUS_USDD_LOCAL_ACCOUNT = os.getenv("NEXUS_USDD_LOCAL_ACCOUNT")
NEXUS_USDD_FEES_ACCOUNT = os.getenv("NEXUS_USDD_FEES_ACCOUNT")
NEXUS_PIN = os.getenv("NEXUS_PIN", "")
USDC_FEES_ACCOUNT = os.getenv("USDC_FEES_ACCOUNT")  # deprecated: USDC fees remain in vault

# Polling & State
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
PROCESSED_SIG_FILE = os.getenv("PROCESSED_SIG_FILE", "processed_sigs.json")
PROCESSED_NEXUS_FILE = os.getenv("PROCESSED_NEXUS_FILE", "processed_nexus_txs.json")
ATTEMPT_STATE_FILE = os.getenv("ATTEMPT_STATE_FILE", "attempt_state.json")
MAX_ACTION_ATTEMPTS = int(os.getenv("MAX_ACTION_ATTEMPTS", "3"))
ACTION_RETRY_COOLDOWN_SEC = int(os.getenv("ACTION_RETRY_COOLDOWN_SEC", "300"))

# Heartbeat
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() in ("1","true","yes","on")
NEXUS_HEARTBEAT_ASSET_ADDRESS = os.getenv("NEXUS_HEARTBEAT_ASSET_ADDRESS")
NEXUS_HEARTBEAT_ASSET_NAME = os.getenv("NEXUS_HEARTBEAT_ASSET_NAME")
HEARTBEAT_MIN_INTERVAL_SEC = max(10, int(os.getenv("HEARTBEAT_MIN_INTERVAL_SEC", str(POLL_INTERVAL))))
# Optional waterline fields to bound reprocessing
HEARTBEAT_WATERLINE_ENABLED = os.getenv("HEARTBEAT_WATERLINE_ENABLED", "true").lower() in ("1","true","yes","on")
HEARTBEAT_WATERLINE_SOLANA_FIELD = os.getenv("HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
HEARTBEAT_WATERLINE_NEXUS_FIELD = os.getenv("HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_usdd")
HEARTBEAT_WATERLINE_SAFETY_SEC = int(os.getenv("HEARTBEAT_WATERLINE_SAFETY_SEC", "120"))

# Fees (optional)
FLAT_FEE_USDC = os.getenv("FLAT_FEE_USDC", "0.1")  # fixed fee in USDC (e.g., 0.1)
FLAT_FEE_USDD = os.getenv("FLAT_FEE_USDD", "0.1")  # threshold and fee floor in USDD
def _to_units(s: str, decimals: int) -> int:
    from decimal import Decimal
    return int((Decimal(s) * (Decimal(10) ** decimals)).to_integral_value())
FLAT_FEE_USDC_UNITS = _to_units(FLAT_FEE_USDC, USDC_DECIMALS)
FLAT_FEE_USDD_UNITS = _to_units(FLAT_FEE_USDD, USDD_DECIMALS)

FEE_BPS_USDC_TO_USDD = int(os.getenv("FEE_BPS_USDC_TO_USDD", "10"))  # dynamic fee (0.1%) for successful USDC→USDD swaps
FEE_BPS_USDD_TO_USDC = int(os.getenv("FEE_BPS_USDD_TO_USDC", "0"))  # no dynamic fee on Nexus→Solana path by default
FEES_STATE_FILE = os.getenv("FEES_STATE_FILE", "fees_state.json")

# Fee conversions (scaffolding / optional)
FEE_CONVERSION_ENABLED = os.getenv("FEE_CONVERSION_ENABLED", "false").lower() in ("1","true","yes","on")
FEE_CONVERSION_MIN_USDC = int(os.getenv("FEE_CONVERSION_MIN_USDC", "0"))  # minimum USDC base units before attempting conversions
SOL_TOPUP_MIN_LAMPORTS = int(os.getenv("SOL_TOPUP_MIN_LAMPORTS", "0"))
SOL_TOPUP_TARGET_LAMPORTS = int(os.getenv("SOL_TOPUP_TARGET_LAMPORTS", "0"))
NEXUS_NXS_TOPUP_MIN = int(os.getenv("NEXUS_NXS_TOPUP_MIN", "0"))  # units TBD by Nexus, placeholder
BACKING_DEFICIT_BPS_ALERT = int(os.getenv("BACKING_DEFICIT_BPS_ALERT", "10"))  # >0.1% triggers fee transfer to vault
BACKING_DEFICIT_PAUSE_PCT = int(os.getenv("BACKING_DEFICIT_PAUSE_PCT", "90"))  # vault < 90% of circulating => pause
BACKING_RECONCILE_INTERVAL_SEC = int(os.getenv("BACKING_RECONCILE_INTERVAL_SEC", "3600"))  # mint USDD fees at most once per hour

# Fee accounts and ranges
# USDC fee token account already defined above
FEES_USDC_MIN = int(os.getenv("FEES_USDC_MIN", "0"))
FEES_USDC_MAX = int(os.getenv("FEES_USDC_MAX", "0"))
FEES_USDD_MIN = int(os.getenv("FEES_USDD_MIN", "0"))
FEES_USDD_MAX = int(os.getenv("FEES_USDD_MAX", "0"))

# Target accumulation ratio: 1 SOL for every 10000 NXS by default
TARGET_SOL_PER_NXS_NUM = int(os.getenv("TARGET_SOL_PER_NXS_NUM", "1"))
TARGET_SOL_PER_NXS_DEN = int(os.getenv("TARGET_SOL_PER_NXS_DEN", "10000"))
