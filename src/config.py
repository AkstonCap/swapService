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
NEXUS_PIN = os.getenv("NEXUS_PIN", "")
USDC_FEES_ACCOUNT = os.getenv("USDC_FEES_ACCOUNT")  # deprecated: USDC fees remain in vault

# Polling & State
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
PROCESSED_SIG_FILE = os.getenv("PROCESSED_SIG_FILE", "processed_sigs.json")
PROCESSED_NEXUS_FILE = os.getenv("PROCESSED_NEXUS_FILE", "processed_nexus_txs.json")
ATTEMPT_STATE_FILE = os.getenv("ATTEMPT_STATE_FILE", "attempt_state.json")
FAILED_REFUNDS_FILE = os.getenv("FAILED_REFUNDS_FILE", "failed_refunds.jsonl")
MAX_ACTION_ATTEMPTS = int(os.getenv("MAX_ACTION_ATTEMPTS", "3"))
ACTION_RETRY_COOLDOWN_SEC = int(os.getenv("ACTION_RETRY_COOLDOWN_SEC", "300"))

# USDC->USDD pipeline files (JSONL lines)
UNPROCESSED_SIGS_FILE = os.getenv("UNPROCESSED_SIGS_FILE", "unprocessed_sigs.json")
PROCESSED_SWAPS_FILE = os.getenv("PROCESSED_SWAPS_FILE", "processed_sigs.json")
NON_DEPOSITS_FILE = os.getenv("NON_DEPOSITS_FILE", "non_deposits.json")
REFERENCE_COUNTER_FILE = os.getenv("REFERENCE_COUNTER_FILE", "reference_counter.json")
REFUND_TIMEOUT_SEC = int(os.getenv("REFUND_TIMEOUT_SEC", "3600"))  # 1 hour default
STALE_DEPOSIT_QUARANTINE_SEC = int(os.getenv("STALE_DEPOSIT_QUARANTINE_SEC", "86400"))  # 24h default
REFUNDED_SIGS_FILE = os.getenv("REFUNDED_SIGS_FILE", "refunded_sigs.json")
USDC_CONFIRM_TIMEOUT_SEC = int(os.getenv("USDC_CONFIRM_TIMEOUT_SEC", "600"))  # 10 minutes default for USDD->USDC confirmations
STALE_ROW_SEC = int(os.getenv("STALE_ROW_SEC", str(24*3600)))  # 24h default; stale rows moved to manual review
SOLANA_RPC_TIMEOUT_SEC = int(os.getenv("SOLANA_RPC_TIMEOUT_SEC", "8"))  # per-call soft timeout safeguard
NEXUS_CLI_TIMEOUT_SEC = int(os.getenv("NEXUS_CLI_TIMEOUT_SEC", "12"))  # generic CLI timeout (overridable)
SOLANA_TX_FETCH_TIMEOUT_SEC = int(os.getenv("SOLANA_TX_FETCH_TIMEOUT_SEC", "3"))  # per get_transaction budget
SOLANA_POLL_TIME_BUDGET_SEC = int(os.getenv("SOLANA_POLL_TIME_BUDGET_SEC", "10"))  # overall per-iteration time slice
SOLANA_MAX_TX_FETCH_PER_POLL = int(os.getenv("SOLANA_MAX_TX_FETCH_PER_POLL", "120"))  # cap transactions decoded each poll

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
# One flat USDC fee (token units, e.g., 0.1 USDC) and one dynamic fee (bps of USDC amount),
# used consistently across both swap directions.
FLAT_FEE_USDC = os.getenv("FLAT_FEE_USDC", "0.1")  # fixed fee in USDC token units
FLAT_FEE_USDD = os.getenv("FLAT_FEE_USDD", "0.1")  # tiny routing threshold in USDD token units
def _to_units(s: str, decimals: int) -> int:
    from decimal import Decimal
    return int((Decimal(s) * (Decimal(10) ** decimals)).to_integral_value())
FLAT_FEE_USDC_UNITS = _to_units(FLAT_FEE_USDC, USDC_DECIMALS)
FLAT_FEE_USDD_UNITS = _to_units(FLAT_FEE_USDD, USDD_DECIMALS)

# Single dynamic fee setting (bps of USDC amount). Applies to both directions.
DYNAMIC_FEE_BPS = int(os.getenv("DYNAMIC_FEE_BPS", "10"))  # 10 bps = 0.1%
FEES_STATE_FILE = os.getenv("FEES_STATE_FILE", "fees_state.json")

# Nexus congestion fee for USDD refunds (token units)
NEXUS_CONGESTION_FEE_USDD = os.getenv("NEXUS_CONGESTION_FEE_USDD", "0.001")

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
