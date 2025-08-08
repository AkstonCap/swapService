#!/usr/bin/env python3
"""
swapService.py

Listens for USDC deposits into a Solana vault account (via Memo),
then mints USDD on a Nexus node. Also provides a helper to send
USDC out of the vault to a given address.
"""

import os
import time
import json
import subprocess
import base64
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from dotenv import load_dotenv

from solana.rpc.api import Client
from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.transaction import Transaction, TransactionInstruction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    transfer_checked,
    get_associated_token_address,
    create_associated_token_account,
)

# Load config from .env
load_dotenv()

# Validate required environment variables
required_env_vars = [
    "SOLANA_RPC_URL",
    "VAULT_KEYPAIR",
    "VAULT_USDC_ACCOUNT",
    "USDC_MINT",
    "NEXUS_PIN",
    "NEXUS_USDD_ACCOUNT",
]
for var in required_env_vars:
    if not os.getenv(var):
        raise ValueError(f"Required environment variable {var} is not set")

# Solana settings
RPC_URL = os.getenv("SOLANA_RPC_URL")  # e.g. https://api.mainnet-beta.solana.com or http://127.0.0.1:8899
VAULT_KEYPAIR = os.getenv("VAULT_KEYPAIR")  # path to vault keypair JSON
VAULT_USDC_ACCOUNT = PublicKey(os.getenv("VAULT_USDC_ACCOUNT"))  # your vault's USDC token-account
USDC_MINT = PublicKey(os.getenv("USDC_MINT"))  # mainnet USDC mint or local test-mint
MEMO_PROGRAM_ID = PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")  # latest Memo Program

# Token decimals (configurable)
USDC_DECIMALS = int(os.getenv("USDC_DECIMALS", "6"))
USDD_DECIMALS = int(os.getenv("USDD_DECIMALS", "6"))

# Optional refund fee (in USDC base units); default 0 (no deduction)
REFUND_USDC_FEE_EXPR = os.getenv("REFUND_USDC_FEE_BASE_UNITS", "0")  # may be decimal string; parsed later
# Optional refund fee (in USDD base units); default 0 (no deduction)
REFUND_USDD_FEE_EXPR = os.getenv("REFUND_USDD_FEE_BASE_UNITS", "0")  # may be decimal string; parsed later

# Swap fee on USDC (applies to both directions). Defaults: 0.1% (10 bps) and minimum 0.01 USDC
SWAP_USDC_FEE_BPS = int(os.getenv("SWAP_USDC_FEE_BPS", "10"))  # 10 bps = 0.10%
SWAP_USDC_FEE_MIN_DECIMAL = os.getenv("SWAP_USDC_FEE_MIN_DECIMAL", "0.01")  # parsed with USDC_DECIMALS

# Nexus settings
NEXUS_CLI = os.getenv("NEXUS_CLI_PATH", "./nexus")
NEXUS_TOKEN_NAME = os.getenv("NEXUS_TOKEN_NAME", "USDD")
NEXUS_RPC_HOST = os.getenv("NEXUS_RPC_HOST", "http://127.0.0.1:8399")
NEXUS_USDD_ACCOUNT = os.getenv("NEXUS_USDD_ACCOUNT")  # Your USDD account address for monitoring deposits

# Heartbeat (optional Nexus asset update)
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
NEXUS_HEARTBEAT_ASSET_ADDRESS = os.getenv("NEXUS_HEARTBEAT_ASSET_ADDRESS")  # Asset address to update
# Free if not more frequent than every 10s. Default to max(10, POLL_INTERVAL) below once POLL_INTERVAL is parsed.

# Polling / state
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
PROCESSED_SIG_FILE = os.getenv("PROCESSED_SIG_FILE", "processed_sigs.json")
# Attempt state to avoid fee-draining loops
ATTEMPT_STATE_FILE = os.getenv("ATTEMPT_STATE_FILE", "attempt_state.json")
MAX_ACTION_ATTEMPTS = int(os.getenv("MAX_ACTION_ATTEMPTS", "3"))
ACTION_RETRY_COOLDOWN_SEC = int(os.getenv("ACTION_RETRY_COOLDOWN_SEC", "300"))

# Heartbeat interval (respect on-chain free threshold of >=10s)
HEARTBEAT_MIN_INTERVAL_SEC = max(10, int(os.getenv("HEARTBEAT_MIN_INTERVAL_SEC", str(POLL_INTERVAL))))

# Load processed signatures to avoid double-processing
if os.path.exists(PROCESSED_SIG_FILE):
    with open(PROCESSED_SIG_FILE, "r") as f:
        processed_sigs = set(json.load(f))
else:
    processed_sigs = set()

# Load processed Nexus transactions to avoid double-processing
PROCESSED_NEXUS_FILE = os.getenv("PROCESSED_NEXUS_FILE", "processed_nexus_txs.json")
if os.path.exists(PROCESSED_NEXUS_FILE):
    with open(PROCESSED_NEXUS_FILE, "r") as f:
        processed_nexus_txs = set(json.load(f))
else:
    processed_nexus_txs = set()

# Load action attempt state
if os.path.exists(ATTEMPT_STATE_FILE):
    try:
        with open(ATTEMPT_STATE_FILE, "r") as f:
            attempt_state = json.load(f)
    except Exception:
        attempt_state = {}
else:
    attempt_state = {}

# Last heartbeat update time (unix seconds)
last_heartbeat_update = 0


def save_state():
    with open(PROCESSED_SIG_FILE, "w") as f:
        json.dump(list(processed_sigs), f)
    with open(PROCESSED_NEXUS_FILE, "w") as f:
        json.dump(list(processed_nexus_txs), f)
    try:
        with open(ATTEMPT_STATE_FILE, "w") as f:
            json.dump(attempt_state, f)
    except Exception:
        pass


# --- Helpers -----------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _should_attempt(action_key: str) -> bool:
    rec = attempt_state.get(action_key)
    if not rec:
        return True
    attempts = int(rec.get("attempts", 0))
    last = int(rec.get("last", 0))
    if attempts >= MAX_ACTION_ATTEMPTS:
        return False
    if (_now() - last) < ACTION_RETRY_COOLDOWN_SEC:
        return False
    return True


def _record_attempt(action_key: str):
    rec = attempt_state.get(action_key, {"attempts": 0, "last": 0})
    rec["attempts"] = int(rec.get("attempts", 0)) + 1
    rec["last"] = _now()
    attempt_state[action_key] = rec
    save_state()


def scale_amount(amount: int, src_decimals: int, dst_decimals: int) -> int:
    """Scale integer base-unit amount between different decimals."""
    if src_decimals == dst_decimals:
        return int(amount)
    if src_decimals < dst_decimals:
        return int(amount) * (10 ** (dst_decimals - src_decimals))
    return int(amount) // (10 ** (src_decimals - dst_decimals))


def parse_amount_to_base_units(val, decimals: int) -> int:
    """Parse an amount that may be int (already base units) or decimal string into base units."""
    try:
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            return int(Decimal(str(val)).scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))
        s = str(val)
        if "." in s:
            return int(Decimal(s).scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))
        return int(s)
    except Exception:
        return 0


def compute_usdc_swap_fee(amount_usdc_units: int) -> int:
    """Compute the USDC swap fee in base units: max(bps, min), capped at amount."""
    try:
        bps = max(0, int(SWAP_USDC_FEE_BPS))
    except Exception:
        bps = 0
    bps_fee = (int(amount_usdc_units) * bps) // 10000
    min_units = parse_amount_to_base_units(SWAP_USDC_FEE_MIN_DECIMAL, USDC_DECIMALS)
    fee = max(bps_fee, max(0, min_units))
    return min(fee, int(amount_usdc_units))


def extract_memo_from_instructions(instructions):
    """Extract memo text from jsonParsed transaction instructions."""
    for ix in instructions:
        prog = ix.get("program")
        if prog == "spl-memo":
            parsed = ix.get("parsed")
            if isinstance(parsed, dict):
                info = parsed.get("info", {})
                memo = info.get("memo")
                if isinstance(memo, str):
                    return memo
            elif isinstance(parsed, str):
                return parsed
        # Fallback: match by programId and base64-decode data
        if ix.get("programId") == str(MEMO_PROGRAM_ID) and "data" in ix:
            try:
                return base64.b64decode(ix["data"]).decode("utf-8")
            except Exception:
                continue
    return None


def get_nexus_account_info(nexus_addr: str):
    """Fetch Nexus account info JSON for an address; returns dict or None."""
    cmd = [NEXUS_CLI, "register/get/finance:account", f"address={nexus_addr}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return None
        try:
            return json.loads(res.stdout)
        except Exception:
            return None
    except subprocess.TimeoutExpired:
        print(f"Timeout validating Nexus address: {nexus_addr}")
        return None
    except Exception as e:
        print(f"Error validating Nexus address {nexus_addr}: {e}")
        return None


def _dict_get_case_insensitive(d: dict, key: str):
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def is_expected_nexus_token(account_info: dict, expected_token: str) -> bool:
    """Check if the Nexus account is for the expected token (e.g., USDD)."""
    if not isinstance(account_info, dict):
        return False
    # Flat keys
    for key in ("ticker", "token", "symbol", "name"):
        v = _dict_get_case_insensitive(account_info, key)
        if isinstance(v, str) and v.upper() == expected_token.upper():
            return True
    # Nested common containers
    for container in ("result", "account", "data"):
        inner = _dict_get_case_insensitive(account_info, container)
        if isinstance(inner, dict) and is_expected_nexus_token(inner, expected_token):
            return True
    return False


def create_memo_ix(text: str) -> TransactionInstruction:
    """Build a Memo program instruction with given UTF-8 text."""
    return TransactionInstruction(program_id=MEMO_PROGRAM_ID, keys=[], data=text.encode("utf-8"))


def refund_usdc_to_source(source_token_account: str, amount: int, reason: str) -> bool:
    """Refund USDC back to the sender's token account with a memo explaining the reason.
    Optionally deduct REFUND_USDC_FEE_BASE_UNITS (in base units) from the amount.
    """
    try:
        with open(VAULT_KEYPAIR, "r") as f:
            kp_data = json.load(f)
        if isinstance(kp_data, list):
            vault_kp = Keypair.from_secret_key(bytes(kp_data))
        else:
            raise ValueError("Unsupported vault keypair format; expected JSON array of ints.")

        client = Client(RPC_URL)
        dest_token_acc = PublicKey(source_token_account)

        refund_fee_units = parse_amount_to_base_units(REFUND_USDC_FEE_EXPR, USDC_DECIMALS)
        refund_amount = max(0, int(amount) - max(0, refund_fee_units))
        if refund_amount <= 0:
            print("Refund amount is zero after fee deduction; skipping refund")
            return False

        tx = Transaction()
        tx.fee_payer = vault_kp.public_key
        tx.add(
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=VAULT_USDC_ACCOUNT,
                mint=USDC_MINT,
                dest=dest_token_acc,
                owner=vault_kp.public_key,
                amount=refund_amount,
                decimals=USDC_DECIMALS,
                signers=[],
            )
        )
        # Attach memo with reason (truncate to reasonable length)
        memo_text = reason
        if len(memo_text) > 120:
            memo_text = memo_text[:117] + "..."
        tx.add(create_memo_ix(memo_text))

        resp = client.send_transaction(tx, vault_kp)
        sig = resp.get("result") if isinstance(resp, dict) else resp
        try:
            Client(RPC_URL).confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"Refunded USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error refunding USDC: {e}")
        return False


def mint_usdd(to_nexus_addr: str, amount: int, usdc_txid: str):
    """
    Calls local nexus node to send `amount` USDD to the given Nexus address.
    Includes the USDC transaction ID as reference.
    Uses: ./nexus finance/debit/account from=USDD to=<nexus_addr> amount=<amount> reference=<reference> pin=<pin>
    """
    pin = os.getenv("NEXUS_PIN", "")
    if not pin:
        print("ERROR: NEXUS_PIN environment variable not set")
        return False

    cmd = [
        NEXUS_CLI,
        "finance/debit/account",
        "from=USDD",
        f"to={to_nexus_addr}",
        f"amount={amount}",
        f"reference=USDC_TX:{usdc_txid}",
        f"pin={pin}",
    ]
    print(
        ">>> Sending USDD on Nexus:", cmd[:-1] + ["pin=***"]
    )  # Hide PIN in logs
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            print("ERROR sending USDD:", res.stderr)
            return False
        else:
            print("USDD send successful:", res.stdout)
            return True
    except subprocess.TimeoutExpired:
        print("ERROR: Nexus CLI timeout")
        return False
    except Exception as e:
        print(f"ERROR executing Nexus CLI: {e}")
        return False


def send_usdc(to_solana_addr: str, amount: int):
    """
    Sends `amount` USDC (in base units) from vault to given Solana address.
    If a wallet address is provided, the recipient's USDC ATA will be created if missing.
    """
    try:
        with open(VAULT_KEYPAIR, "r") as f:
            kp_data = json.load(f)
        if isinstance(kp_data, list):
            vault_kp = Keypair.from_secret_key(bytes(kp_data))
        else:
            raise ValueError("Unsupported vault keypair format; expected JSON array of ints.")

        client = Client(RPC_URL)
        recipient_owner = PublicKey(to_solana_addr)
        dest_ata = get_associated_token_address(owner=recipient_owner, mint=USDC_MINT)

        # Check if recipient ATA exists
        ata_info = client.get_account_info(dest_ata).get("result", {}).get("value")

        tx = Transaction()
        tx.fee_payer = vault_kp.public_key
        if ata_info is None:
            tx.add(
                create_associated_token_account(
                    payer=vault_kp.public_key, owner=recipient_owner, mint=USDC_MINT
                )
            )

        tx.add(
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=VAULT_USDC_ACCOUNT,
                mint=USDC_MINT,
                dest=dest_ata,
                owner=vault_kp.public_key,
                amount=amount,
                decimals=USDC_DECIMALS,
                signers=[],
            )
        )

        # Sign & send, then try to confirm
        resp = client.send_transaction(tx, vault_kp)
        sig = resp.get("result") if isinstance(resp, dict) else resp
        try:
            Client(RPC_URL).confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        print(f"Sent USDC tx sig: {sig}")
        return True
    except Exception as e:
        print(f"Error sending USDC: {e}")
        return False


def get_nexus_sender_address(tx: dict) -> Optional[str]:
    """Best-effort extraction of the sender Nexus address from a transaction object."""
    if not isinstance(tx, dict):
        return None
    # Common keys
    for key in ("from", "sender", "source", "addressFrom", "origin"):
        val = _dict_get_case_insensitive(tx, key)
        if isinstance(val, str) and val:
            return val
    # Nested containers we might see
    for container in ("result", "tx", "transaction", "data", "details"):
        inner = _dict_get_case_insensitive(tx, container)
        if isinstance(inner, dict):
            addr = get_nexus_sender_address(inner)
            if addr:
                return addr
    return None


def refund_usdd_to_sender(sender_nexus_addr: str, amount_usdd_units: int, reason: str) -> bool:
    """Refund USDD back to the sender on Nexus with a reference explaining the reason.
    Optionally deduct REFUND_USDD_FEE_BASE_UNITS (in base units) from the amount.
    """
    pin = os.getenv("NEXUS_PIN", "")
    if not pin:
        print("ERROR: NEXUS_PIN environment variable not set")
        return False

    refund_fee_units = parse_amount_to_base_units(REFUND_USDD_FEE_EXPR, USDD_DECIMALS)
    refund_amount = max(0, int(amount_usdd_units) - max(0, refund_fee_units))
    if refund_amount <= 0:
        print("Refund USDD amount is zero after fee deduction; skipping refund")
        return False

    # Truncate reason for safety
    ref = f"REFUND_USDC_INVALID: {reason}"
    if len(ref) > 120:
        ref = ref[:117] + "..."

    cmd = [
        NEXUS_CLI,
        "finance/debit/account",
        "from=USDD",
        f"to={sender_nexus_addr}",
        f"amount={refund_amount}",
        f"reference={ref}",
        f"pin={pin}",
    ]
    print(
        ">>> Refunding USDD on Nexus:", cmd[:-1] + ["pin=***"]
    )  # Hide PIN in logs
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            print("ERROR refunding USDD:", res.stderr)
            return False
        print("USDD refund successful:", res.stdout)
        return True
    except subprocess.TimeoutExpired:
        print("ERROR: Nexus CLI timeout (refund)")
        return False
    except Exception as e:
        print(f"ERROR executing Nexus CLI (refund): {e}")
        return False


def update_heartbeat_asset(force: bool = False) -> None:
    """Optionally update a Nexus Asset with last_poll_timestamp to signal liveness.
    Free on-chain if not updated more often than every 10 seconds.
    Controlled by HEARTBEAT_ENABLED, NEXUS_HEARTBEAT_ASSET_ADDRESS, HEARTBEAT_MIN_INTERVAL_SEC.
    """
    global last_heartbeat_update
    if not HEARTBEAT_ENABLED or not NEXUS_HEARTBEAT_ASSET_ADDRESS:
        return
    now = _now()
    if not force and (now - last_heartbeat_update) < HEARTBEAT_MIN_INTERVAL_SEC:
        return

    pin = os.getenv("NEXUS_PIN", "")
    cmd = [
        NEXUS_CLI,
        "assets/update/asset",
        f"address={NEXUS_HEARTBEAT_ASSET_ADDRESS}",
        "format=basic",
        f"last_poll_timestamp={now}",
    ]
    if pin:
        cmd.append(f"pin={pin}")

    try:
        print(
            "↻ Updating Nexus heartbeat asset:", cmd[:-1] + ["pin=***"] if pin else cmd
        )
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            # Log but don't raise; liveness update failure shouldn't stop swaps
            print("Heartbeat update failed:", res.stderr.strip() or res.stdout.strip())
            return
        last_heartbeat_update = now
        # Optionally print a short success message
        out = (res.stdout or "").strip()
        if out:
            print("Heartbeat updated:", out)
    except subprocess.TimeoutExpired:
        print("Heartbeat update timeout")
    except Exception as e:
        print(f"Heartbeat update error: {e}")


def poll_nexus_usdd_deposits():
    """
    Poll Nexus for new USDD deposits into our USDD account.
    If we see a memo with a Solana address, send USDC to that address.
    If the Solana address is invalid or sending fails, refund USDD to sender (minus optional fee).
    """
    cmd = [NEXUS_CLI, "finance/transaction/account", f"address={NEXUS_USDD_ACCOUNT}"]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            print("Error fetching USDD transactions:", res.stderr)
            return

        # Parse JSON response
        transactions = json.loads(res.stdout)
        if not isinstance(transactions, list):
            transactions = [transactions]  # Handle single transaction response

        for tx in transactions:
            tx_id = tx.get("txid")
            if not tx_id or tx_id in processed_nexus_txs:
                continue

            mark_processed = True

            # Check if this is a credit (incoming) transaction
            if tx.get("type") == "CREDIT" and tx.get("confirmation", 0) > 0:
                raw_amount = tx.get("amount", 0)
                usdd_units = parse_amount_to_base_units(raw_amount, USDD_DECIMALS)
                amount_usdc_units = scale_amount(usdd_units, USDD_DECIMALS, USDC_DECIMALS)
                reference = tx.get("reference", "") or ""
                sender_nexus_addr = get_nexus_sender_address(tx)

                # Look for Solana address in reference (solana:<SOLANA_ADDRESS>)
                if reference.startswith("solana:"):
                    solana_addr = reference.split("solana:", 1)[1].strip()
                    print(
                        f"→ USDD deposit of {usdd_units} (base units); sending {amount_usdc_units} USDC base units to {solana_addr}"
                    )

                    # Validate Solana address strictly
                    valid_pk = True
                    try:
                        _ = PublicKey(solana_addr)
                    except Exception:
                        valid_pk = False

                    if not valid_pk:
                        print(f"✗ Invalid Solana address format: {solana_addr}")
                        refund_key = f"refund_usdd:{tx_id}"
                        if _should_attempt(refund_key) and sender_nexus_addr:
                            _record_attempt(refund_key)
                            if refund_usdd_to_sender(
                                sender_nexus_addr, usdd_units, reason="Invalid Solana address in reference"
                            ):
                                print("✓ Refunded USDD to sender")
                            else:
                                print("✗ Failed to refund USDD to sender")
                                mark_processed = False
                        else:
                            print("↷ Skipping refund attempt (cooldown/max attempts reached)")
                    else:
                        # Apply USDC swap fee
                        fee_usdc_units = compute_usdc_swap_fee(amount_usdc_units)
                        net_usdc_units = max(0, amount_usdc_units - fee_usdc_units)
                        if net_usdc_units <= 0:
                            print("✗ Deposit below minimum after USDC fee; refunding USDD to sender")
                            refund_key = f"refund_usdd:{tx_id}"
                            if _should_attempt(refund_key) and sender_nexus_addr:
                                _record_attempt(refund_key)
                                if refund_usdd_to_sender(
                                    sender_nexus_addr,
                                    usdd_units,
                                    reason="Deposit below minimum after USDC fee",
                                ):
                                    print("✓ Refunded USDD to sender")
                                else:
                                    print("✗ Failed to refund USDD to sender")
                                    mark_processed = False
                            else:
                                print("↷ Skipping refund attempt (cooldown/max attempts reached or missing sender)")
                        else:
                            print(
                                f"→ Applying USDC fee {fee_usdc_units} base units; net send {net_usdc_units} base units"
                            )
                            send_key = f"send_usdc:{tx_id}"
                            if _should_attempt(send_key):
                                _record_attempt(send_key)
                                if send_usdc(solana_addr, net_usdc_units):
                                    print(
                                        f"✓ Successfully sent {net_usdc_units} USDC base units (after fee) to {solana_addr}"
                                    )
                                else:
                                    print(
                                        f"✗ Failed to send USDC to {solana_addr}; attempting USDD refund if allowed"
                                    )
                                    refund_key = f"refund_usdd:{tx_id}"
                                    if _should_attempt(refund_key) and sender_nexus_addr:
                                        _record_attempt(refund_key)
                                        if refund_usdd_to_sender(
                                            sender_nexus_addr, usdd_units, reason="USDC send failed"
                                        ):
                                            print("✓ Refunded USDD to sender")
                                        else:
                                            print("✗ Failed to refund USDD to sender")
                                            mark_processed = False
                                    else:
                                        print(
                                            "↷ Skipping refund attempt (cooldown/max attempts reached or missing sender)"
                                        )
                            else:
                                print("↷ Skipping USDC send attempt (cooldown/max attempts reached)")
                else:
                    print(f"No Solana address found in reference: {reference}")

            if mark_processed:
                processed_nexus_txs.add(tx_id)

    except json.JSONDecodeError as e:
        print(f"Error parsing Nexus transaction JSON: {e}")
    except subprocess.TimeoutExpired:
        print("Timeout fetching USDD transactions")
    except Exception as e:
        print(f"Error polling USDD deposits: {e}")


def poll_solana_deposits():
    """
    Poll Solana for new token-transfer signatures into VAULT_USDC_ACCOUNT.
    If we see a memo with a Nexus address, validate it and call mint_usdd().
    If invalid or wrong token, refund to sender's source token account with memo.
    """
    try:
        client = Client(RPC_URL)
        # get up to 100 most recent signatures
        sigs = client.get_signatures_for_address(
            VAULT_USDC_ACCOUNT, limit=100, commitment="confirmed"
        )["result"]
    except Exception as e:
        print(f"Error fetching signatures: {e}")
        return

    for entry in sigs:
        sig = entry["signature"]
        if sig in processed_sigs:
            continue

        mark_processed = True

        # Fetch full tx details
        try:
            tx = Client(RPC_URL).get_transaction(sig, encoding="jsonParsed")["result"]
            if not tx:
                continue
        except Exception as e:
            print(f"Error fetching transaction {sig}: {e}")
            continue

        # 1) Find SPL token transfer to vault (transfer or transferChecked)
        for instr in tx["transaction"]["message"]["instructions"]:
            if instr.get("program") == "spl-token" and instr.get("parsed"):
                p = instr["parsed"]
                if p.get("type") in ("transfer", "transferChecked") and p.get("info", {}).get(
                    "destination"
                ) == str(VAULT_USDC_ACCOUNT):
                    info = p["info"]
                    if "amount" in info:
                        amount_usdc_units = int(info["amount"])  # already base units
                    elif "tokenAmount" in info and isinstance(info["tokenAmount"], dict):
                        amount_usdc_units = int(info["tokenAmount"].get("amount", 0))
                    else:
                        continue

                    source_token_acc = info.get("source")

                    # 2) Find Memo instruction for Nexus address
                    memo_data = extract_memo_from_instructions(
                        tx["transaction"]["message"]["instructions"]
                    )
                    if not memo_data:
                        # No memo provided; refund the USDC to sender
                        print("No memo found; initiating refund to sender")
                        reason = (
                            f"Refund: missing memo 'nexus:<addr>'. Ref tx of {amount_usdc_units} USDC."
                        )
                        refund_key = f"refund_usdc:{sig}"
                        if _should_attempt(refund_key) and source_token_acc:
                            _record_attempt(refund_key)
                            if refund_usdc_to_source(
                                source_token_acc, amount_usdc_units, reason
                            ):
                                print("✓ Refund sent to sender's token account")
                            else:
                                print("✗ Refund failed")
                                mark_processed = False
                        else:
                            print("↷ Skipping refund attempt (cooldown/max attempts reached)")
                        break

                    if memo_data.startswith("nexus:"):
                        nexus_addr = memo_data.split("nexus:", 1)[1].strip()
                        print(
                            f"→ Deposit of {amount_usdc_units} USDC base units; checking Nexus address {nexus_addr}"
                        )

                        # Validate Nexus address exists and is for expected token
                        acct_info = get_nexus_account_info(nexus_addr)
                        if not acct_info or not is_expected_nexus_token(acct_info, NEXUS_TOKEN_NAME):
                            print(
                                f"✗ Nexus address invalid or wrong token (expected {NEXUS_TOKEN_NAME}). Initiating refund."
                            )
                            reason = (
                                f"Refund: invalid Nexus addr or not {NEXUS_TOKEN_NAME}. Ref tx of {amount_usdc_units} USDC."
                            )
                            refund_key = f"refund_usdc:{sig}"
                            if _should_attempt(refund_key) and source_token_acc:
                                _record_attempt(refund_key)
                                if refund_usdc_to_source(
                                    source_token_acc, amount_usdc_units, reason
                                ):
                                    print("✓ Refund sent to sender's token account")
                                else:
                                    print("✗ Refund failed")
                                    mark_processed = False
                            else:
                                print("↷ Skipping refund attempt (cooldown/max attempts reached)")
                        else:
                            # Apply USDC swap fee and mint net amount as USDD
                            fee_usdc_units = compute_usdc_swap_fee(amount_usdc_units)
                            net_usdc_units = max(0, amount_usdc_units - fee_usdc_units)
                            if net_usdc_units <= 0:
                                print("✗ Deposit below minimum after USDC fee; refunding USDC to sender")
                                reason = (
                                    "Refund: deposit below minimum after USDC fee."
                                )
                                refund_key = f"refund_usdc:{sig}"
                                if _should_attempt(refund_key) and source_token_acc:
                                    _record_attempt(refund_key)
                                    if refund_usdc_to_source(
                                        source_token_acc, amount_usdc_units, reason
                                    ):
                                        print("✓ Refund sent to sender's token account")
                                    else:
                                        print("✗ Refund failed")
                                        mark_processed = False
                                else:
                                    print(
                                        "↷ Skipping refund attempt (cooldown/max attempts reached)"
                                    )
                            else:
                                print(
                                    f"→ Applying USDC fee {fee_usdc_units} base units; net to convert {net_usdc_units} base units"
                                )
                                print(f"→ Nexus address validated, minting to {nexus_addr}")
                                usdd_units = scale_amount(
                                    net_usdc_units, USDC_DECIMALS, USDD_DECIMALS
                                )
                                mint_key = f"mint_usdd:{sig}"
                                if _should_attempt(mint_key):
                                    _record_attempt(mint_key)
                                    if mint_usdd(nexus_addr, usdd_units, sig):
                                        print(
                                            f"✓ Successfully minted {usdd_units} USDD base units to {nexus_addr} (after fee)"
                                        )
                                    else:
                                        print(f"✗ Failed to mint USDD to {nexus_addr}")
                                        # Don't mark as processed if minting failed
                                        mark_processed = False
                                else:
                                    print("↷ Skipping mint attempt (cooldown/max attempts reached)")
                    else:
                        # Bad memo format; refund USDC back
                        print("Bad memo format; initiating refund to sender:", memo_data)
                        reason = (
                            f"Refund: invalid memo format. Expect 'nexus:<addr>'. Ref tx of {amount_usdc_units} USDC."
                        )
                        refund_key = f"refund_usdc:{sig}"
                        if _should_attempt(refund_key) and source_token_acc:
                            _record_attempt(refund_key)
                            if refund_usdc_to_source(
                                source_token_acc, amount_usdc_units, reason
                            ):
                                print("✓ Refund sent to sender's token account")
                            else:
                                print("✗ Refund failed")
                                mark_processed = False
                        else:
                            print("↷ Skipping refund attempt (cooldown/max attempts reached)")
                        break
                    break

        if mark_processed:
            processed_sigs.add(sig)
    save_state()


def main():
    print("🌐 Starting bidirectional swap service")
    print(f"   Solana RPC: {RPC_URL}")
    print(f"   USDC Vault: {VAULT_USDC_ACCOUNT}")
    print(f"   USDD Account: {NEXUS_USDD_ACCOUNT}")
    print("   Monitoring:")
    print("   - USDC → USDD: Solana deposits with Nexus address in memo")
    print("   - USDD → USDC: USDD deposits with Solana address in reference")

    try:
        while True:
            # Poll for USDC deposits (Solana → USDD)
            poll_solana_deposits()

            # Poll for USDD deposits (USDD → USDC)
            poll_nexus_usdd_deposits()

            # Save state after each polling cycle
            save_state()

            # Update on-chain heartbeat (free when not more often than every 10s)
            update_heartbeat_asset()

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("Shutting down…")
        save_state()


if __name__ == "__main__":
    main()
