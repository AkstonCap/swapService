import os
from dotenv import load_dotenv
from solders.pubkey import Pubkey as PublicKey

load_dotenv()

REQUIRED_ENV = [
    "SOLANA_RPC_URL",
    "VAULT_KEYPAIR",
    "VAULT_USDC_ACCOUNT",
    "USDC_MINT",
    "SOL_MINT",
    "NEXUS_PIN",
    "NEXUS_USDD_TREASURY_ACCOUNT",
    "SOL_MAIN_ACCOUNT",
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
SOL_MAIN_ACCOUNT = PublicKey.from_string(os.getenv("SOL_MAIN_ACCOUNT"))

# Decimals
USDC_DECIMALS = int(os.getenv("USDC_DECIMALS", "6"))
USDD_DECIMALS = int(os.getenv("USDD_DECIMALS", "6"))

# Nexus
NEXUS_CLI = os.getenv("NEXUS_CLI_PATH", "./nexus")
NEXUS_TOKEN_NAME = os.getenv("NEXUS_TOKEN_NAME", "USDD")
NEXUS_RPC_HOST = os.getenv("NEXUS_RPC_HOST", "http://127.0.0.1:8399")
NEXUS_USDD_TREASURY_ACCOUNT = os.getenv("NEXUS_USDD_TREASURY_ACCOUNT")
NEXUS_USDD_LOCAL_ACCOUNT = os.getenv("NEXUS_USDD_LOCAL_ACCOUNT")
NEXUS_USDD_QUARANTINE_ACCOUNT = os.getenv("NEXUS_USDD_QUARANTINE_ACCOUNT")
# Optional USDD fees account (if you separately account for accrued fees on Nexus)
NEXUS_USDD_FEES_ACCOUNT = os.getenv("NEXUS_USDD_FEES_ACCOUNT")
NEXUS_PIN = os.getenv("NEXUS_PIN", "")
USDC_FEES_ACCOUNT = os.getenv("USDC_FEES_ACCOUNT")  # deprecated: USDC fees remain in vault

# Polling & State
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # legacy/global fallback
# Optional chain-specific poll intervals (seconds). Default to POLL_INTERVAL if unset.
SOLANA_POLL_INTERVAL = int(os.getenv("SOLANA_POLL_INTERVAL", str(POLL_INTERVAL)))
NEXUS_POLL_INTERVAL = int(os.getenv("NEXUS_POLL_INTERVAL", str(POLL_INTERVAL)))
MAX_ACTION_ATTEMPTS = int(os.getenv("MAX_ACTION_ATTEMPTS", "3"))
ACTION_RETRY_COOLDOWN_SEC = int(os.getenv("ACTION_RETRY_COOLDOWN_SEC", "300"))

# Timeout and hang prevention
SOLANA_RPC_TIMEOUT_SEC = int(os.getenv("SOLANA_RPC_TIMEOUT_SEC", "8"))
SOLANA_TX_FETCH_TIMEOUT_SEC = int(os.getenv("SOLANA_TX_FETCH_TIMEOUT_SEC", "12"))
SOLANA_POLL_TIME_BUDGET_SEC = int(os.getenv("SOLANA_POLL_TIME_BUDGET_SEC", "15"))
SOLANA_MAX_TX_FETCH_PER_POLL = int(os.getenv("SOLANA_MAX_TX_FETCH_PER_POLL", "120"))
NEXUS_CLI_TIMEOUT_SEC = int(os.getenv("NEXUS_CLI_TIMEOUT_SEC", "20"))
NEXUS_POLL_TIME_BUDGET_SEC = int(os.getenv("NEXUS_POLL_TIME_BUDGET_SEC", "15"))
METRICS_BUDGET_SEC = int(os.getenv("METRICS_BUDGET_SEC", "5"))
STALE_ROW_SEC = int(os.getenv("STALE_ROW_SEC", "86400"))  # 24 hours
METRICS_INTERVAL_SEC = int(os.getenv("METRICS_INTERVAL_SEC", "30"))

# Timeout thresholds
REFUND_TIMEOUT_SEC = int(os.getenv("REFUND_TIMEOUT_SEC", "3600"))  # 1 hour default
STALE_DEPOSIT_QUARANTINE_SEC = int(os.getenv("STALE_DEPOSIT_QUARANTINE_SEC", "86400"))  # 24h default
USDC_CONFIRM_TIMEOUT_SEC = int(os.getenv("USDC_CONFIRM_TIMEOUT_SEC", "600"))  # 10 minutes default for USDD->USDC confirmations

# Heartbeat
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() in ("1","true","yes","on")
NEXUS_HEARTBEAT_ASSET_ADDRESS = os.getenv("NEXUS_HEARTBEAT_ASSET_ADDRESS")
NEXUS_HEARTBEAT_ASSET_NAME = os.getenv("NEXUS_HEARTBEAT_ASSET_NAME")
HEARTBEAT_MIN_INTERVAL_SEC = max(10, int(os.getenv("HEARTBEAT_MIN_INTERVAL_SEC", str(POLL_INTERVAL))))
# Optional waterline fields to bound reprocessing
HEARTBEAT_WATERLINE_ENABLED = os.getenv("HEARTBEAT_WATERLINE_ENABLED", "true").lower() in ("1","true","yes","on")
HEARTBEAT_WATERLINE_SOLANA_FIELD = os.getenv("HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
HEARTBEAT_WATERLINE_NEXUS_FIELD = os.getenv("HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_usdd")
HEARTBEAT_WATERLINE_SAFETY_SEC = int(os.getenv("HEARTBEAT_WATERLINE_SAFETY_SEC", "120"))  # safety margin (seconds) subtracted from waterline when filtering

# Fees (optional)
# Flat fees (in token units before conversion to base units):
# - FLAT_FEE_USDC: Charged on USDD->USDC swap direction
# - FLAT_FEE_USDD: Charged on USDC->USDD swap direction (also used as USDC refund fee since 1:1 parity)
FLAT_FEE_USDC = os.getenv("FLAT_FEE_USDC", "0.5")  # fixed fee in USDC token units for USDD->USDC swaps
FLAT_FEE_USDD = os.getenv("FLAT_FEE_USDD", "0.1")  # flat fee in USDD/USDC token units for USDC->USDD swaps & USDC refunds
def _to_units(s: str, decimals: int) -> int:
    from decimal import Decimal
    return int((Decimal(s) * (Decimal(10) ** decimals)).to_integral_value())
FLAT_FEE_USDC_UNITS = _to_units(FLAT_FEE_USDC, USDC_DECIMALS)
# FLAT_FEE_USDC_UNITS_REFUND uses FLAT_FEE_USDD value since USDC/USDD have same decimals and 1:1 parity
# This is the fee deducted when refunding USDC to sender (on failed USDC->USDD swaps)
FLAT_FEE_USDC_UNITS_REFUND = _to_units(FLAT_FEE_USDD, USDC_DECIMALS)

# Single dynamic fee setting (bps of USDC amount). Applies to both directions.
DYNAMIC_FEE_BPS = int(os.getenv("DYNAMIC_FEE_BPS", "10"))  # 10 bps = 0.1%
FEES_STATE_FILE = os.getenv("FEES_STATE_FILE", "fees_state.json")

# Nexus congestion fee for USDD refunds (token units)
NEXUS_CONGESTION_FEE_USDD = os.getenv("NEXUS_CONGESTION_FEE_USDD", "0.001")

# Anti-DoS protections
MIN_DEPOSIT_USDC = os.getenv("MIN_DEPOSIT_USDC", "0.100101")  # minimum deposit to process as swap
MIN_DEPOSIT_USDC_UNITS = _to_units(MIN_DEPOSIT_USDC, USDC_DECIMALS)
MIN_CREDIT_USDD = os.getenv("MIN_CREDIT_USDD", "0.500501")  # minimum credit to process as swap
MIN_CREDIT_USDD_UNITS = _to_units(MIN_CREDIT_USDD, USDD_DECIMALS)
MAX_DEPOSITS_PER_LOOP = int(os.getenv("MAX_DEPOSITS_PER_LOOP", "100"))  # batch processing limit
MAX_CREDITS_PER_LOOP = int(os.getenv("MAX_CREDITS_PER_LOOP", "100"))  # batch processing limit for USDD credits
MICRO_DEPOSIT_FEE_PCT = int(os.getenv("MICRO_DEPOSIT_FEE_PCT", "100"))  # 100% fee for sub-minimum deposits
MICRO_CREDIT_FEE_PCT = int(os.getenv("MICRO_CREDIT_FEE_PCT", "100"))  # 100% fee for sub-minimum credits
IGNORE_MICRO_USDC = True

# Advanced micro-credit handling
# If true, build a Nexus WHERE clause (instead of simple field filter) to server-side filter transactions.
USE_NEXUS_WHERE_FILTER_USDD = os.getenv("USE_NEXUS_WHERE_FILTER_USDD", "true").lower() in ("1","true","yes","on")
# If true we skip expensive owner lookups for micro credits below threshold.
SKIP_OWNER_LOOKUP_FOR_MICRO_USDD = os.getenv("SKIP_OWNER_LOOKUP_FOR_MICRO_USDD", "true").lower() in ("1","true","yes","on")
# If false, micro credits do not count against MAX_CREDITS_PER_LOOP (lets us drain real swaps faster under spam).
MICRO_CREDIT_COUNT_AGAINST_LIMIT = os.getenv("MICRO_CREDIT_COUNT_AGAINST_LIMIT", "false").lower() in ("1","true","yes","on")

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

# Quarantine account for failed refunds (USDC token account we own)
USDC_QUARANTINE_ACCOUNT = os.getenv("USDC_QUARANTINE_ACCOUNT")

# Target accumulation ratio: 1 SOL for every 10000 NXS by default
TARGET_SOL_PER_NXS_NUM = int(os.getenv("TARGET_SOL_PER_NXS_NUM", "1"))
TARGET_SOL_PER_NXS_DEN = int(os.getenv("TARGET_SOL_PER_NXS_DEN", "10000"))

# Backing surplus mint threshold: when ratio > 1 + margin and vault USDC > this, mint to bring back to 1
_SURPLUS_THRESH_USDC = os.getenv("BACKING_SURPLUS_MINT_THRESHOLD_USDC", "20")
try:
    from decimal import Decimal as _D
    BACKING_SURPLUS_MINT_THRESHOLD_USDC_UNITS = int((_D(_SURPLUS_THRESH_USDC) * (_D(10) ** USDC_DECIMALS)).to_integral_value())
except Exception:
    BACKING_SURPLUS_MINT_THRESHOLD_USDC_UNITS = 20 * (10 ** USDC_DECIMALS)
