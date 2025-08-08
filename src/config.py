import os
from dotenv import load_dotenv
from solana.publickey import PublicKey

load_dotenv()

REQUIRED_ENV = [
    "SOLANA_RPC_URL",
    "VAULT_KEYPAIR",
    "VAULT_USDC_ACCOUNT",
    "USDC_MINT",
    "NEXUS_PIN",
    "NEXUS_USDD_ACCOUNT",
]
for var in REQUIRED_ENV:
    if not os.getenv(var):
        raise ValueError(f"Required environment variable {var} is not set")

# Solana
RPC_URL = os.getenv("SOLANA_RPC_URL")
VAULT_KEYPAIR_PATH = os.getenv("VAULT_KEYPAIR")
VAULT_USDC_ACCOUNT = PublicKey(os.getenv("VAULT_USDC_ACCOUNT"))
USDC_MINT = PublicKey(os.getenv("USDC_MINT"))
MEMO_PROGRAM_ID = PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")

# Decimals
USDC_DECIMALS = int(os.getenv("USDC_DECIMALS", "6"))
USDD_DECIMALS = int(os.getenv("USDD_DECIMALS", "6"))

# Nexus
NEXUS_CLI = os.getenv("NEXUS_CLI_PATH", "./nexus")
NEXUS_TOKEN_NAME = os.getenv("NEXUS_TOKEN_NAME", "USDD")
NEXUS_RPC_HOST = os.getenv("NEXUS_RPC_HOST", "http://127.0.0.1:8399")
NEXUS_USDD_ACCOUNT = os.getenv("NEXUS_USDD_ACCOUNT")
NEXUS_PIN = os.getenv("NEXUS_PIN", "")

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
HEARTBEAT_MIN_INTERVAL_SEC = max(10, int(os.getenv("HEARTBEAT_MIN_INTERVAL_SEC", str(POLL_INTERVAL))))
