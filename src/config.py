import os
from dotenv import load_dotenv
from solders.pubkey import Pubkey as PublicKey

load_dotenv()

# Each entry is a tuple of accepted spellings; the first is preferred.
REQUIRED_ENV = [
    ("SOLANA_RPC_URL",),
    ("VAULT_KEYPAIR",),
    ("SOLANA_VAULT_ACCOUNT", "VAULT_USDC_ACCOUNT"),
    ("SOLANA_TOKEN_MINT", "USDC_MINT"),
    ("SOL_MINT",),
    ("NEXUS_PIN",),
    ("NEXUS_USDD_TREASURY_ACCOUNT",),
    ("SOL_MAIN_ACCOUNT",),
]
for _names in REQUIRED_ENV:
    if not any(os.getenv(n) for n in _names):
        raise ValueError(
            f"Required environment variable {_names[0]} is not set"
            + (f" (alias: {', '.join(_names[1:])})" if len(_names) > 1 else "")
        )

# --- Bridged token pair -------------------------------------------------------------
# This bridge is token-agnostic: the operator chooses which Solana SPL token is bridged
# against which Nexus token. The historical variable names say USDC/USDD because that was
# the first deployment; the generic aliases below are preferred in new configs and both
# are accepted, so existing .env files keep working.
#
#   Solana side : SOLANA_TOKEN_MINT + SOLANA_VAULT_ACCOUNT  (aliases: USDC_MINT, VAULT_USDC_ACCOUNT)
#   Nexus  side : NEXUS_TOKEN_NAME  + NEXUS_USDD_TREASURY_ACCOUNT
#
# Internal identifiers now say `solana` and `nexus` rather than naming the original pair.
# Three categories deliberately keep the old spelling, because in each case the name is
# not a code identifier at all but a value that already exists outside this process:
#
#   1. Environment variables an operator has already set (`VAULT_USDC_ACCOUNT`, ...) and
#      the module attributes that mirror them one-for-one. Generic aliases are defined
#      alongside each, and new configs should use those.
#   2. Column names in the state database (`amount_usdc_units`, `circulating_usdd_units`,
#      ...). Renaming them means an ALTER TABLE migration over live fund records.
#   3. Persisted row VALUES with a safety property attached - retry-budget keys, the
#      debit reservation kind, and the status strings. See the frozen-key block at the
#      top of `state_db` for why a rename there could re-debit an in-flight swap.
#
# Everything else - functions, locals, derived constants, log fields, dashboard keys -
# reads generically, and the VALUES are fully configurable.
def _first_env(*names, default=None):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

# Solana
RPC_URL = os.getenv("SOLANA_RPC_URL")
VAULT_KEYPAIR_PATH = os.getenv("VAULT_KEYPAIR")
_vault_acct = _first_env("SOLANA_VAULT_ACCOUNT", "VAULT_USDC_ACCOUNT")
_sol_mint = _first_env("SOLANA_TOKEN_MINT", "USDC_MINT")
if not _vault_acct:
    raise ValueError("Required environment variable SOLANA_VAULT_ACCOUNT (or VAULT_USDC_ACCOUNT) is not set")
if not _sol_mint:
    raise ValueError("Required environment variable SOLANA_TOKEN_MINT (or USDC_MINT) is not set")
VAULT_USDC_ACCOUNT = PublicKey.from_string(_vault_acct)
USDC_MINT = PublicKey.from_string(_sol_mint)
# Generic aliases (same objects)
SOLANA_VAULT_ACCOUNT = VAULT_USDC_ACCOUNT
SOLANA_TOKEN_MINT = USDC_MINT
# Display ticker for the Solana-side token; used in logs, the dashboard and the on-chain
# service record. Purely cosmetic - the mint above is what is enforced.
SOLANA_TOKEN_SYMBOL = os.getenv("SOLANA_TOKEN_SYMBOL", "USDC")
SOL_MINT = PublicKey.from_string(os.getenv("SOL_MINT"))
SOL_MAIN_ACCOUNT = PublicKey.from_string(os.getenv("SOL_MAIN_ACCOUNT"))

# Decimals for each side of the pair
USDC_DECIMALS = int(_first_env("SOLANA_TOKEN_DECIMALS", "USDC_DECIMALS", default="6"))
USDD_DECIMALS = int(_first_env("NEXUS_TOKEN_DECIMALS", "USDD_DECIMALS", default="6"))
SOLANA_TOKEN_DECIMALS = USDC_DECIMALS
NEXUS_TOKEN_DECIMALS = USDD_DECIMALS


# --- Cross-side unit conversion ------------------------------------------------------
# The two sides of the bridge back each other 1:1 in TOKEN units, but they are stored and
# moved in BASE units, and the two scales are only the same when the decimals happen to
# match. The original USDC/USDD pair was 6dp on both sides, so a lot of the backing math
# subtracted one directly from the other. That is wrong for any other pair: with an 8dp
# Solana token against a 6dp Nexus token, a fully-backed vault looks 100x over-collateralised,
# and the surplus logic would mint unbacked supply against the difference.
#
# Anything comparing the two sides must convert first, through these helpers.

def rescale_units(amount, src_decimals: int, dst_decimals: int, round_up: bool = False) -> int:
    """Re-express a base-unit amount from one token's decimals into another's.

    `round_up` matters when scaling DOWN, because the remainder cannot be represented.
    Pass it when the amount is a liability being compared against backing: rounding the
    liability up keeps the comparison conservative, so a rounding remainder can never make
    an under-collateralised vault look solvent.
    """
    amount = int(amount or 0)
    if src_decimals == dst_decimals:
        return amount
    if src_decimals < dst_decimals:
        return amount * (10 ** (dst_decimals - src_decimals))
    divisor = 10 ** (src_decimals - dst_decimals)
    if round_up:
        return -(-amount // divisor)  # ceiling division, correct for negatives too
    return amount // divisor


def nexus_units_to_solana(units, round_up: bool = True) -> int:
    """Nexus base units -> Solana base units. Defaults to the conservative direction:
    the usual caller is measuring circulating supply (a liability) against the vault."""
    return rescale_units(units, USDD_DECIMALS, USDC_DECIMALS, round_up=round_up)


def solana_units_to_nexus(units, round_up: bool = False) -> int:
    """Solana base units -> Nexus base units."""
    return rescale_units(units, USDC_DECIMALS, USDD_DECIMALS, round_up=round_up)

# Nexus
NEXUS_CLI = os.getenv("NEXUS_CLI_PATH", "./nexus")
NEXUS_TOKEN_NAME = os.getenv("NEXUS_TOKEN_NAME", "USDD")
NEXUS_RPC_HOST = os.getenv("NEXUS_RPC_HOST", "http://127.0.0.1:8399")
NEXUS_USDD_TREASURY_ACCOUNT = os.getenv("NEXUS_USDD_TREASURY_ACCOUNT")
NEXUS_TREASURY_ACCOUNT = NEXUS_USDD_TREASURY_ACCOUNT  # generic alias
# Memo prefix a depositor puts on the Solana transfer to name their Nexus destination,
# e.g. "nexus:8Cuy...". Configurable so an operator can namespace their bridge.
DEPOSIT_MEMO_PREFIX = os.getenv("DEPOSIT_MEMO_PREFIX", "nexus:")

# --- Public service identity (published in the on-chain registration asset) ----------
SERVICE_PROVIDER = os.getenv("SERVICE_PROVIDER", "")          # operator name / domain
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")
SERVICE_CONTACT = os.getenv("SERVICE_CONTACT", "")            # url or contact handle
NEXUS_USDD_LOCAL_ACCOUNT = os.getenv("NEXUS_USDD_LOCAL_ACCOUNT")
NEXUS_USDD_QUARANTINE_ACCOUNT = os.getenv("NEXUS_USDD_QUARANTINE_ACCOUNT")
# Optional Nexus-side fees account (if you separately account for accrued fees on Nexus)
NEXUS_USDD_FEES_ACCOUNT = os.getenv("NEXUS_USDD_FEES_ACCOUNT")
NEXUS_PIN = os.getenv("NEXUS_PIN", "")
# Nexus multiuser mode. With `multiuser=1` in nexus.conf the node supports several
# signature chains at once, and EVERY call to a user-scoped API (finance/*, assets/*,
# market/*, supply/*) must carry `session=<id>`. In single-user mode the session must
# NOT be supplied at all - the API docs are explicit about this - so it cannot simply be
# sent unconditionally. `register/*` is a public register read and never takes a session.
NEXUS_MULTIUSER = os.getenv("NEXUS_MULTIUSER", "false").lower() in ("1", "true", "yes", "on")
# Session id returned by `sessions/create/local` when multiuser=1. Treat as a credential:
# combined with the PIN it authorises spending.
NEXUS_SESSION = os.getenv("NEXUS_SESSION", "")
USDC_FEES_ACCOUNT = os.getenv("USDC_FEES_ACCOUNT")  # deprecated: USDC fees remain in vault

# Polling & State
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # legacy/global fallback
# Optional chain-specific poll intervals (seconds). Default to POLL_INTERVAL if unset.
SOLANA_POLL_INTERVAL = int(os.getenv("SOLANA_POLL_INTERVAL", str(POLL_INTERVAL)))
NEXUS_POLL_INTERVAL = int(os.getenv("NEXUS_POLL_INTERVAL", str(POLL_INTERVAL)))
MAX_ACTION_ATTEMPTS = int(os.getenv("MAX_ACTION_ATTEMPTS", "3"))
ACTION_RETRY_COOLDOWN_SEC = int(os.getenv("ACTION_RETRY_COOLDOWN_SEC", "300"))

# Timeout and hang prevention
# Commitment used when INGESTING deposits and when treating our own payouts as settled.
# 'confirmed' is supermajority-voted but NOT rooted and can still be reorged: minting the Nexus-side token
# against a reorged deposit leaves permanently unbacked supply, and Nexus cannot learn of a
# Solana reorg. Default to 'finalized' (~13s slower, irreversible). Lower it only if you
# accept that risk, and preferably only below SOLANA_FINALIZED_ABOVE_UNITS.
SOLANA_DEPOSIT_COMMITMENT = os.getenv("SOLANA_DEPOSIT_COMMITMENT", "finalized")
# Deposits at or above this size ALWAYS require 'finalized', even if the commitment above
# is relaxed. 0 disables the carve-out (i.e. the commitment above applies to every amount).
SOLANA_FINALIZED_ABOVE_UNITS = int(os.getenv("SOLANA_FINALIZED_ABOVE_UNITS", "0"))

SOLANA_RPC_TIMEOUT_SEC = int(os.getenv("SOLANA_RPC_TIMEOUT_SEC", "8"))
SOLANA_TX_FETCH_TIMEOUT_SEC = int(os.getenv("SOLANA_TX_FETCH_TIMEOUT_SEC", "12"))
SOLANA_POLL_TIME_BUDGET_SEC = int(os.getenv("SOLANA_POLL_TIME_BUDGET_SEC", "15"))
SOLANA_MAX_TX_FETCH_PER_POLL = int(os.getenv("SOLANA_MAX_TX_FETCH_PER_POLL", "120"))
NEXUS_CLI_TIMEOUT_SEC = int(os.getenv("NEXUS_CLI_TIMEOUT_SEC", "20"))
NEXUS_POLL_TIME_BUDGET_SEC = int(os.getenv("NEXUS_POLL_TIME_BUDGET_SEC", "15"))
# Per-cycle budget for draining the queued Nexus->Solana entries. Previously read via
# getattr() with a hardcoded fallback and never defined here, so the documented
# UNPROCESSED_PROCESS_BUDGET_SEC had no effect at all; both spellings now work.
UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC = int(_first_env(
    "UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC", "UNPROCESSED_PROCESS_BUDGET_SEC", default="30"))
METRICS_BUDGET_SEC = int(os.getenv("METRICS_BUDGET_SEC", "5"))
STALE_ROW_SEC = int(os.getenv("STALE_ROW_SEC", "86400"))  # 24 hours
METRICS_INTERVAL_SEC = int(os.getenv("METRICS_INTERVAL_SEC", "30"))

# Timeout thresholds
REFUND_TIMEOUT_SEC = int(os.getenv("REFUND_TIMEOUT_SEC", "3600"))  # 1 hour default
STALE_DEPOSIT_QUARANTINE_SEC = int(os.getenv("STALE_DEPOSIT_QUARANTINE_SEC", "86400"))  # 24h default
SOLANA_CONFIRM_TIMEOUT_SEC = int(_first_env("SOLANA_CONFIRM_TIMEOUT_SEC",
                                            "USDC_CONFIRM_TIMEOUT_SEC", default="600"))  # 10 minutes default for Nexus->Solana confirmations
# How long to keep verifying an ambiguous Nexus-side debit against the chain before concluding
# it never executed. Must comfortably exceed Nexus block/propagation time.
DEBIT_VERIFY_GRACE_SEC = int(os.getenv("DEBIT_VERIFY_GRACE_SEC", "300"))

# Heartbeat
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() in ("1","true","yes","on")
NEXUS_HEARTBEAT_ASSET_ADDRESS = os.getenv("NEXUS_HEARTBEAT_ASSET_ADDRESS")
NEXUS_HEARTBEAT_ASSET_NAME = os.getenv("NEXUS_HEARTBEAT_ASSET_NAME")
HEARTBEAT_MIN_INTERVAL_SEC = max(10, int(os.getenv("HEARTBEAT_MIN_INTERVAL_SEC", str(POLL_INTERVAL))))
# Optional waterline fields to bound reprocessing
HEARTBEAT_WATERLINE_ENABLED = os.getenv("HEARTBEAT_WATERLINE_ENABLED", "true").lower() in ("1","true","yes","on")
HEARTBEAT_WATERLINE_SOLANA_FIELD = os.getenv("HEARTBEAT_WATERLINE_SOLANA_FIELD", "last_safe_timestamp_solana")
# Must match the field actually present on the heartbeat asset. `format=basic` locks the
# field set at creation, so a mismatch makes EVERY heartbeat update fail atomically
# (taking last_poll_timestamp and the Solana waterline with it). Canonical name per
# ASSET_STANDARD.md and create_heartbeat_asset.py is `last_safe_timestamp_nexus`.
HEARTBEAT_WATERLINE_NEXUS_FIELD = os.getenv("HEARTBEAT_WATERLINE_NEXUS_FIELD", "last_safe_timestamp_nexus")
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
FLAT_FEE_TO_SOLANA_UNITS = _to_units(FLAT_FEE_USDC, USDC_DECIMALS)
# FLAT_FEE_TO_NEXUS_UNITS uses FLAT_FEE_USDD value since USDC/USDD have same decimals and 1:1 parity
# This is the fee deducted when refunding USDC to sender (on failed Solana->Nexus swaps)
FLAT_FEE_TO_NEXUS_UNITS = _to_units(FLAT_FEE_USDD, USDC_DECIMALS)

# Single dynamic fee setting (bps of Solana amount). Applies to both directions.
DYNAMIC_FEE_BPS = int(os.getenv("DYNAMIC_FEE_BPS", "10"))  # 10 bps = 0.1%
FEES_STATE_FILE = os.getenv("FEES_STATE_FILE", "fees_state.json")

# Nexus congestion fee for Nexus refunds (token units)
NEXUS_CONGESTION_FEE_USDD = os.getenv("NEXUS_CONGESTION_FEE_USDD", "0.001")

# Anti-DoS protections
# Default is DERIVED from the flat fee (2x), not a fixed dollar figure: a hardcoded "0.2"
# would mean 0.2 BTC on a wBTC bridge. An explicit MIN_DEPOSIT_USDC still wins.
_MIN_DEPOSIT_ENV = _first_env("MIN_DEPOSIT_SOLANA_TOKEN", "MIN_DEPOSIT_USDC")
_MIN_DEPOSIT_SOLANA_CONFIGURED = (_to_units(_MIN_DEPOSIT_ENV, USDC_DECIMALS)
                                if _MIN_DEPOSIT_ENV else 2 * FLAT_FEE_TO_NEXUS_UNITS)
MIN_DEPOSIT_USDC = _MIN_DEPOSIT_ENV or "(2x flat fee)"
# A minimum at or below the flat fee means the user nets ~nothing while the swap is still
# recorded as successful (with a 6-decimal pair, 0.100101 in against a 0.1 fee netted 0.0000009 - below one
# base unit). Enforce a floor of 2x the flat fee so the output is always at least the fee.
MIN_DEPOSIT_SOLANA_UNITS = max(_MIN_DEPOSIT_SOLANA_CONFIGURED, 2 * FLAT_FEE_TO_NEXUS_UNITS)
MIN_DEPOSIT_SOLANA_RAISED = MIN_DEPOSIT_SOLANA_UNITS > _MIN_DEPOSIT_SOLANA_CONFIGURED
# Minimum Nexus credit that is swapped for USDC. Must stay ABOVE the Nexus->Solana fee
# (FLAT_FEE_USDC + dynamic), or the swap nets <= 0 and the whole credit becomes a fee.
# Keep README.md / CONFIG.md / .env.example in sync with this value: users who follow a
# documented minimum lower than this one previously had their credit silently destroyed.
_MIN_CREDIT_ENV = _first_env("MIN_CREDIT_NEXUS_TOKEN", "MIN_CREDIT_USDD")
_MIN_CREDIT_NEXUS_CONFIGURED = (_to_units(_MIN_CREDIT_ENV, USDD_DECIMALS)
                               if _MIN_CREDIT_ENV else 2 * FLAT_FEE_TO_SOLANA_UNITS)
MIN_CREDIT_USDD = _MIN_CREDIT_ENV or "(2x flat fee)"
# Same floor rule as MIN_DEPOSIT_USDC: this direction's flat fee is FLAT_FEE_USDC.
MIN_CREDIT_NEXUS_UNITS = max(_MIN_CREDIT_NEXUS_CONFIGURED, 2 * FLAT_FEE_TO_SOLANA_UNITS)
MIN_CREDIT_NEXUS_RAISED = MIN_CREDIT_NEXUS_UNITS > _MIN_CREDIT_NEXUS_CONFIGURED
# Anti-DoS dust floor. Credits BELOW this are ignored entirely (no state, no accounting).
# Credits between this floor and MIN_CREDIT_USDD are real user funds: they are recorded
# and booked as fees rather than dropped without trace.
# Spam floor, also derived so it scales with the token's denomination (1/10 of the fee).
_DUST_ENV = _first_env("DUST_CREDIT_NEXUS_TOKEN", "DUST_CREDIT_USDD")
DUST_CREDIT_NEXUS_UNITS = (_to_units(_DUST_ENV, USDD_DECIMALS) if _DUST_ENV
                          else max(1, FLAT_FEE_TO_SOLANA_UNITS // 10))
DUST_CREDIT_USDD = _DUST_ENV or "(flat fee / 10)"
MAX_DEPOSITS_PER_LOOP = int(os.getenv("MAX_DEPOSITS_PER_LOOP", "100"))  # batch processing limit
MAX_CREDITS_PER_LOOP = int(os.getenv("MAX_CREDITS_PER_LOOP", "100"))  # batch processing limit for Nexus credits
MICRO_DEPOSIT_FEE_PCT = int(os.getenv("MICRO_DEPOSIT_FEE_PCT", "100"))  # 100% fee for sub-minimum deposits
MICRO_CREDIT_FEE_PCT = int(os.getenv("MICRO_CREDIT_FEE_PCT", "100"))  # 100% fee for sub-minimum credits
IGNORE_MICRO_USDC = True

# Advanced micro-credit handling
# Credits are always enumerated without a server-side amount predicate.  Nexus transaction
# contracts are nested arrays and a heuristic WHERE filter could silently omit a real credit
# while permitting the poller to advance its waterline.  Dust/minimum policy is applied only
# after the complete result is inspected locally.
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
BACKING_RECONCILE_INTERVAL_SEC = int(os.getenv("BACKING_RECONCILE_INTERVAL_SEC", "3600"))  # mint the Nexus-side token fees at most once per hour

# Fee accounts and ranges
# Solana-side fee token account already defined above
FEES_USDC_MIN = int(os.getenv("FEES_USDC_MIN", "0"))
FEES_USDC_MAX = int(os.getenv("FEES_USDC_MAX", "0"))
FEES_USDD_MIN = int(os.getenv("FEES_USDD_MIN", "0"))
FEES_USDD_MAX = int(os.getenv("FEES_USDD_MAX", "0"))

# Quarantine account for failed refunds (token account we own)
USDC_QUARANTINE_ACCOUNT = os.getenv("USDC_QUARANTINE_ACCOUNT")

# --- Exposure caps (defence in depth against a bug or a compromised key) ---
# Largest single swap accepted. Oversized items are refunded rather than paid out.
# 0 disables the cap.
MAX_SWAP_USDC = os.getenv("MAX_SWAP_USDC", "0")
MAX_SWAP_SOLANA_UNITS = _to_units(MAX_SWAP_USDC, USDC_DECIMALS)
MAX_SWAP_USDD = os.getenv("MAX_SWAP_USDD", "0")
MAX_SWAP_NEXUS_UNITS = _to_units(MAX_SWAP_USDD, USDD_DECIMALS)
# Rolling 24h ceiling on total outbound Solana-side payouts. Enforced independently of the polling loop,
# so a runaway loop or a stolen key cannot drain the vault in one go. 0 disables.
DAILY_PAYOUT_CAP_USDC = os.getenv("DAILY_PAYOUT_CAP_USDC", "0")
DAILY_PAYOUT_CAP_SOLANA_UNITS = _to_units(DAILY_PAYOUT_CAP_USDC, USDC_DECIMALS)

# --- Alerting (operator notification) ---
# Without one of these, discrepancies/pauses/halts are only visible on stdout.
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")      # POSTed a JSON body
ALERT_COMMAND = os.getenv("ALERT_COMMAND")              # argv0; receives JSON on stdin
ALERT_MIN_INTERVAL_SEC = int(os.getenv("ALERT_MIN_INTERVAL_SEC", "300"))  # per-event dedupe

# Target accumulation ratio: 1 SOL for every 10000 NXS by default
TARGET_SOL_PER_NXS_NUM = int(os.getenv("TARGET_SOL_PER_NXS_NUM", "1"))
TARGET_SOL_PER_NXS_DEN = int(os.getenv("TARGET_SOL_PER_NXS_DEN", "10000"))

# Backing surplus mint threshold: when ratio > 1 + margin and the vault > this, mint to bring back to 1
_SURPLUS_THRESH_SOLANA = os.getenv("BACKING_SURPLUS_MINT_THRESHOLD_USDC", "20")
try:
    from decimal import Decimal as _D
    BACKING_SURPLUS_MINT_THRESHOLD_SOLANA_UNITS = int((_D(_SURPLUS_THRESH_SOLANA) * (_D(10) ** USDC_DECIMALS)).to_integral_value())
except Exception:
    BACKING_SURPLUS_MINT_THRESHOLD_SOLANA_UNITS = 20 * (10 ** USDC_DECIMALS)
