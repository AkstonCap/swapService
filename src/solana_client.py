from asyncio import timeout
import json
import base64
import logging
from time import time
from typing import Any, Optional
from dataclasses import dataclass
import os
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey as PublicKey
from solders.keypair import Keypair
from solders.signature import Signature
from solders.instruction import Instruction as TransactionInstruction, AccountMeta
from solders.hash import Hash
from solders.transaction import Transaction, VersionedTransaction
from solders.message import Message
from struct import pack
import threading, queue
from . import state_db, nexus_client, nexus_memo
import time

from . import config, structured_logging


_LOG = structured_logging.get_logger("swapService.solana_client")


def _log(event: str, *, level: int = logging.INFO, **fields) -> None:
    """Best-effort secret-safe diagnostics that cannot alter payout state transitions."""
    try:
        structured_logging.emit(_LOG, level, event, **fields)
    except Exception:
        pass


# Expose last sent signature for higher-level idempotency logging (refund / quarantine / debit flows)
last_sent_sig: str | None = None


class PayoutCapExceeded(Exception):
    """Raised when a send is refused by the rolling 24h payout cap.

    Distinct from a send FAILURE: the payment is fine, we are just throttled. Callers
    must leave the item's status untouched and retry on a later cycle - treating this
    like a failure would divert a legitimate refund into quarantine over a temporary cap.
    """


@dataclass(frozen=True)
class SolanaDepositBacklogProgress:
    """One durably committed bounded vault-history page."""

    complete: bool
    admitted_count: int
    upper_timestamp: int | None
    next_before_signature: str | None
    provider: str = "core"
    next_pagination_token: str | None = None
    held_count: int = 0


class UnsupportedDepositEvidence(RuntimeError):
    """A valid successful transaction changed the vault in an unsupported way.

    The signature can be durably held for parser/operator replay without making an
    otherwise complete provider page look incomplete.  Malformed or missing evidence
    continues to raise ordinary RuntimeError and holds the whole page/cursor.
    """

    def __init__(self, reason: str, amount_units: int):
        super().__init__(reason)
        self.reason = reason
        if type(amount_units) is not int or amount_units <= 0:
            raise ValueError("unsupported deposit evidence requires proven positive principal")
        self.amount_units = amount_units

# SPL Token and ATA Program IDs (constants)
TOKEN_PROGRAM_ID = PublicKey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = PublicKey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


# Process-wide RPC client so HTTP keep-alive / connection pooling is reused across
# calls instead of opening a new TCP/TLS session on every RPC.
_shared_client: Optional[Client] = None
_shared_client_url: Optional[str] = None


def _get_client() -> Client:
    """Return a shared RPC client (recreated only if the configured URL changes)."""
    global _shared_client, _shared_client_url
    url = config.RPC_URL
    if _shared_client is None or _shared_client_url != url:
        _shared_client = Client(url)
        _shared_client_url = url
    return _shared_client


# --- Optional Helius JSON-RPC helpers -----------------------------------------------------------
def _helius_rpc_url() -> Optional[str]:
    """Build the Helius RPC URL from config or environment.

    An explicit Helius URL wins, followed by an explicit Helius-owned
    ``SOLANA_RPC_URL``. Only then may an API key synthesize a modern endpoint for the
    configured network. Official hostnames are validated against ``SOLANA_NETWORK``.
    """
    from urllib.parse import quote, urlparse

    rpc_url = str(getattr(config, "RPC_URL", "") or "").strip()
    rpc_network = _network_identity_for_url(rpc_url)
    explicit = str(
        getattr(config, "HELIUS_RPC_URL", "") or os.getenv("HELIUS_RPC_URL") or ""
    ).strip()
    if explicit:
        network = _network_identity_for_url(explicit)
        if rpc_network in {"mainnet", "devnet", "testnet"} and network != rpc_network:
            raise ValueError("Helius endpoint network conflicts with SOLANA_RPC_URL")
        return explicit

    rpc_host = (urlparse(rpc_url).hostname or "").lower()
    if rpc_host == "rpc.helius.xyz" or rpc_host.endswith(".helius-rpc.com"):
        _network_identity_for_url(rpc_url)
        return rpc_url

    key = str(
        getattr(config, "HELIUS_API_KEY", "") or os.getenv("HELIUS_API_KEY") or ""
    ).strip()
    if key:
        network = rpc_network
        if network not in {"mainnet", "devnet"}:
            raise ValueError("HELIUS_API_KEY shorthand requires an identifiable mainnet/devnet network")
        return f"https://{network}.helius-rpc.com/?api-key={quote(key, safe='')}"
    return None


def _helius_rpc_call(method: str, params=None, timeout_sec: Optional[float] = None):
    """Call a Helius JSON-RPC method and return .result.
    Raises on HTTP/RPC errors. Returns the `result` field when available, else the whole JSON.
    """
    url = _helius_rpc_url()
    if not url:
        raise RuntimeError("Helius RPC not configured: set HELIUS_RPC_URL or HELIUS_API_KEY")
    payload = {
        "jsonrpc": "2.0",
        "id": "swapService",
        "method": method,
        "params": params if params is not None else [],
    }
    to = timeout_sec if timeout_sec is not None else getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8)
    resp = requests.post(url, json=payload, timeout=to)
    resp.raise_for_status()
    js = resp.json()
    if isinstance(js, dict) and js.get("error"):
        raise RuntimeError(f"Helius RPC error: {js['error']}")
    if isinstance(js, dict) and "result" in js:
        return js["result"]
    return js


def _deposit_commitment() -> str:
    """Commitment for ingesting deposits / settling our own payouts.

    Defaults to 'finalized'. 'confirmed' is supermajority-voted but not rooted, so a
    deposit can be reorged away after we have minted supply against it - an irreversible
    loss, since Nexus cannot learn of a Solana reorg.
    """
    return str(getattr(config, "SOLANA_DEPOSIT_COMMITMENT", "finalized") or "finalized")


def _helius_deposit_options(
    *, limit: int, commitment: str, lower_timestamp: int, upper_timestamp: int,
    pagination_token: str | None = None,
) -> dict[str, Any]:
    """Build the single canonical Helius wire-query representation."""
    if (type(lower_timestamp) is not int or lower_timestamp < 0
            or type(upper_timestamp) is not int or upper_timestamp <= lower_timestamp):
        raise ValueError("Helius deposit query requires a fixed increasing timestamp range")
    if commitment not in {"confirmed", "finalized"}:
        raise ValueError("Helius deposit commitment must be confirmed or finalized")
    options: dict[str, Any] = {
        "limit": max(1, min(1000, int(limit))),
        "commitment": commitment,
        "transactionDetails": "full",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
        "sortOrder": "desc",
        "filters": {
            "blockTime": {"gte": lower_timestamp, "lte": upper_timestamp},
            "status": "succeeded",
        },
    }
    if pagination_token is not None:
        if not isinstance(pagination_token, str) or not pagination_token:
            raise RuntimeError("Invalid Helius deposit pagination token")
        options["paginationToken"] = pagination_token
    return options


def _helius_get_full_deposit_page(
    address: str,
    *,
    limit: int,
    pagination_token: str | None,
    commitment: str,
    lower_timestamp: int,
    upper_timestamp: int,
) -> tuple[list[dict], str | None]:
    """Read one trusted Helius page with core-RPC-equivalent transaction evidence.

    Helius is the transport and indexer here, not a substitute for immutable evidence:
    financial admission consumes only the complete ``transaction``/``meta`` structure
    documented for ``transactionDetails=full``.
    """
    options = _helius_deposit_options(
        limit=limit, commitment=commitment, lower_timestamp=lower_timestamp,
        upper_timestamp=upper_timestamp, pagination_token=pagination_token,
    )
    result = _helius_rpc_call("getTransactionsForAddress", [address, options])
    if not isinstance(result, dict):
        raise RuntimeError("Helius deposit result lacks full transaction envelope")
    transactions = result.get("data")
    next_token = result.get("paginationToken")
    if (not isinstance(transactions, list)
            or any(not isinstance(row, dict) for row in transactions)
            or (next_token is not None and (not isinstance(next_token, str) or not next_token))):
        raise RuntimeError("Invalid Helius full transaction page")
    return transactions, next_token


def _validate_deposit_page_order(
    page: list, previous_timestamp: int | None = None, *, timestamp_field: str = "blockTime"
) -> int | None:
    """Validate the whole page before an old entry can terminate a timestamp scan.

    A later entry (including on the next page) must never be newer. Providers that
    cannot satisfy this timestamp contract require a slot/cursor backfill protocol;
    silently sorting a malformed page would conceal incomplete history coverage.
    """
    for row in page:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid Solana deposit page entry")
        timestamp = row.get(timestamp_field, row.get("blockTime"))
        if (type(timestamp) is not int or timestamp <= 0
                or (previous_timestamp is not None and timestamp > previous_timestamp)):
            raise RuntimeError("Solana deposit page timestamp order is incomplete")
        previous_timestamp = timestamp
    return previous_timestamp


def _deposit_rpc_result(response):
    """Preserve empty results, but never turn a missing/error envelope into success."""
    body = _rpc_to_json(response)
    if not isinstance(body, dict) or "error" in body or "result" not in body:
        raise RuntimeError("Invalid Solana deposit RPC envelope")
    return body["result"]


def _base_unit_amount(balance: dict, *, field: str) -> int:
    """Return a strictly positive SPL base-unit balance from parsed RPC evidence."""
    token_amount = balance.get("uiTokenAmount")
    amount = token_amount.get("amount") if isinstance(token_amount, dict) else None
    if (not isinstance(amount, str) or not amount.isascii() or not amount.isdecimal()
            or not amount):
        raise RuntimeError(f"Invalid Solana {field} token balance")
    return int(amount)


def _account_key_at_index(message: dict, index: int) -> str:
    """Resolve an account index without accepting another account owned by the vault key."""
    account_keys = message.get("accountKeys")
    if not isinstance(account_keys, list) or index < 0 or index >= len(account_keys):
        raise RuntimeError("Missing Solana transaction account-key evidence")
    key = account_keys[index]
    if isinstance(key, str):
        value = key
    elif isinstance(key, dict):
        value = key.get("pubkey")
    else:
        value = None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Invalid Solana transaction account key")
    return value


def _extract_core_deposit_evidence(
    tx_data: dict, *, signature: str, vault_account: str, mint: str
) -> tuple[str | None, str | None, int]:
    """Prove one inbound classic-SPL transfer against the exact configured vault account.

    Ownership is deliberately not used to locate the vault: one owner can control many
    token accounts.  A transaction that cannot bind its balance delta, parsed transfer,
    mint and source token account to the configured vault remains an incomplete scan.
    """
    meta = tx_data.get("meta")
    tx_obj = tx_data.get("transaction")
    if not isinstance(meta, dict) or meta.get("err") is not None or not isinstance(tx_obj, dict):
        raise RuntimeError("Missing successful Solana transaction evidence")
    signatures = tx_obj.get("signatures")
    message = tx_obj.get("message")
    if (not isinstance(signatures, list) or signatures[:1] != [signature]
            or not isinstance(message, dict)):
        raise RuntimeError("Solana transaction signature/message mismatch")
    pre_balances = meta.get("preTokenBalances")
    post_balances = meta.get("postTokenBalances")
    if (not isinstance(pre_balances, list) or not isinstance(post_balances, list)
            or any(not isinstance(row, dict) for row in pre_balances + post_balances)):
        raise RuntimeError("Missing Solana token balance evidence")

    vault_indices = [
        index for index in range(len(message.get("accountKeys", [])))
        if _account_key_at_index(message, index) == vault_account
    ]
    if len(vault_indices) != 1:
        raise RuntimeError("Configured Solana vault account is ambiguous or absent")
    vault_index = vault_indices[0]
    matching_pre = [row for row in pre_balances if row.get("accountIndex") == vault_index]
    matching_post = [row for row in post_balances if row.get("accountIndex") == vault_index]
    if len(matching_pre) > 1 or len(matching_post) != 1:
        raise RuntimeError("Missing exact configured-vault token balance")

    raw_instructions = message.get("instructions")
    if not isinstance(raw_instructions, list):
        raise RuntimeError("Missing Solana transaction instructions")
    instructions = list(raw_instructions)
    inner_groups = meta.get("innerInstructions", [])
    if not isinstance(inner_groups, list):
        raise RuntimeError("Invalid Solana inner instruction evidence")
    for group in inner_groups:
        if not isinstance(group, dict) or not isinstance(group.get("instructions"), list):
            raise RuntimeError("Invalid Solana inner instruction evidence")
        instructions.extend(group["instructions"])

    post = matching_post[0]
    if post.get("mint") != mint:
        raise RuntimeError("Configured Solana vault mint mismatch")
    if matching_pre:
        pre = matching_pre[0]
        if pre.get("mint") != mint:
            raise RuntimeError("Configured Solana vault mint mismatch")
        pre_units = _base_unit_amount(pre, field="pre")
    else:
        expected_owner = post.get("owner")
        if not isinstance(expected_owner, str) or not expected_owner:
            raise RuntimeError("Missing exact configured-vault creation owner evidence")
        ata_creates: list[tuple[int, dict]] = []
        for index, instruction in enumerate(raw_instructions):
            if not isinstance(instruction, dict):
                raise RuntimeError("Invalid Solana transaction instruction")
            program = str(instruction.get("program") or instruction.get("programId") or "")
            parsed = instruction.get("parsed")
            info = parsed.get("info") if isinstance(parsed, dict) else None
            if (program in {"spl-associated-token-account", str(ASSOCIATED_TOKEN_PROGRAM_ID)}
                    and isinstance(parsed, dict)
                    and parsed.get("type") in {"create", "createIdempotent"}
                    and isinstance(info, dict) and info.get("account") == vault_account):
                if info.get("mint") != mint or info.get("wallet") != expected_owner:
                    raise RuntimeError("Conflicting configured-vault creation evidence")
                ata_creates.append((index, instruction))
        if len(ata_creates) > 1:
            raise RuntimeError("Duplicate independent configured-vault creation evidence")

        token_initializes: list[dict] = []
        for group in inner_groups:
            group_index = group.get("index")
            for instruction in group["instructions"]:
                if not isinstance(instruction, dict):
                    raise RuntimeError("Invalid Solana inner instruction evidence")
                program = str(instruction.get("program") or instruction.get("programId") or "")
                parsed = instruction.get("parsed")
                info = parsed.get("info") if isinstance(parsed, dict) else None
                is_init = (
                    program in {"spl-token", str(TOKEN_PROGRAM_ID)}
                    and isinstance(parsed, dict)
                    and parsed.get("type") in {
                        "initializeAccount", "initializeAccount2", "initializeAccount3",
                    }
                )
                if not is_init or not isinstance(info, dict):
                    continue
                related_to_ata = bool(ata_creates and group_index == ata_creates[0][0])
                if info.get("account") == vault_account or related_to_ata:
                    if (info.get("account") != vault_account or info.get("mint") != mint
                            or info.get("owner") != expected_owner):
                        raise RuntimeError("Conflicting configured-vault creation evidence")
                    token_initializes.append(instruction)
        if len(token_initializes) > 1:
            raise RuntimeError("Duplicate independent configured-vault creation evidence")
        if not ata_creates and not token_initializes:
            raise RuntimeError("Missing exact configured-vault creation evidence")
        pre_units = 0

    vault_delta = _base_unit_amount(post, field="post") - pre_units
    if vault_delta <= 0:
        return None, None, 0

    incoming: list[tuple[str, int]] = []
    memos: list[str] = []
    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise RuntimeError("Invalid Solana transaction instruction")
        program = str(instruction.get("program") or instruction.get("programId") or "")
        parsed = instruction.get("parsed")
        if program in {"spl-memo", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"} or program.startswith("Memo111"):
            memo = parsed if isinstance(parsed, str) else instruction.get("data")
            if not isinstance(memo, str) or not memo:
                raise RuntimeError("Invalid Solana memo evidence")
            memos.append(memo)
            continue
        if program not in {"spl-token", str(TOKEN_PROGRAM_ID)} or not isinstance(parsed, dict):
            continue
        transfer_type = parsed.get("type")
        if transfer_type not in {"transfer", "transferChecked"}:
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            raise RuntimeError("Invalid parsed SPL transfer")
        if info.get("destination") != vault_account:
            continue
        source = info.get("source")
        if transfer_type == "transferChecked":
            amount_obj = info.get("tokenAmount")
            amount = amount_obj.get("amount") if isinstance(amount_obj, dict) else None
            mint_matches = info.get("mint") == mint
        else:
            amount = info.get("amount")
            # Classic transfer omits mint; the exact destination balance metadata above
            # supplies it.  If a provider does include mint, it still must agree.
            mint_matches = info.get("mint") in {None, mint}
        if (not isinstance(source, str) or not source.strip() or source == vault_account
                or not mint_matches or not isinstance(amount, str)
                or not amount.isascii() or not amount.isdecimal() or not amount
                or int(amount) <= 0):
            raise RuntimeError("Invalid incoming SPL transfer evidence")
        incoming.append((source, int(amount)))

    if not incoming or sum(amount for _source, amount in incoming) != vault_delta:
        raise UnsupportedDepositEvidence("unsupported_vault_balance_change", vault_delta)
    sources = {source for source, _amount in incoming}
    if len(sources) != 1:
        raise UnsupportedDepositEvidence("ambiguous_incoming_sources", vault_delta)
    if len(memos) > 1:
        raise UnsupportedDepositEvidence("ambiguous_deposit_memos", vault_delta)
    return (memos[0] if memos else None), next(iter(sources)), vault_delta


def _network_identity_for_url(url: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    official_network = None
    if host in {"devnet.helius-rpc.com", "api.devnet.solana.com"}:
        official_network = "devnet"
    elif host in {"mainnet.helius-rpc.com", "rpc.helius.xyz", "api.mainnet-beta.solana.com"}:
        official_network = "mainnet"
    elif host == "api.testnet.solana.com":
        official_network = "testnet"
    configured = str(getattr(config, "SOLANA_NETWORK", "") or "").strip().lower()
    if configured:
        if configured not in {"mainnet", "devnet"}:
            raise ValueError("SOLANA_NETWORK must be mainnet or devnet")
        if official_network and configured != official_network:
            raise ValueError("official Solana RPC hostname conflicts with SOLANA_NETWORK")
        return configured
    if official_network:
        return official_network
    # Custom/proxied endpoints are bound without persisting query-string credentials.
    if not parsed.scheme or not host:
        raise ValueError("Solana RPC URL cannot identify a network endpoint")
    port = f":{parsed.port}" if parsed.port else ""
    return f"endpoint:{parsed.scheme.lower()}://{host}{port}{parsed.path or '/'}"


def _core_network_identity() -> str:
    return _network_identity_for_url(str(getattr(config, "RPC_URL", "") or ""))


def _deposit_query_identity(
    *, provider: str, network: str, vault_account: str, mint: str,
    commitment: str, lower_timestamp: int, upper_timestamp: int, page_size: int,
) -> str:
    import hashlib
    wire_options = (
        _helius_deposit_options(
            limit=page_size, commitment=commitment, lower_timestamp=lower_timestamp,
            upper_timestamp=upper_timestamp,
        )
        if provider == "helius" else {
            "limit": page_size, "commitment": commitment, "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0, "sortOrder": "desc",
        }
    )
    query = {
        "provider": provider, "network": network, "vault_account": vault_account,
        "mint": mint, "lower_timestamp": lower_timestamp,
        "upper_timestamp": upper_timestamp, "options": wire_options,
    }
    encoded = json.dumps(query, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _requires_finalized_deposit(amount_units: int) -> bool:
    threshold = int(getattr(config, "SOLANA_FINALIZED_ABOVE_UNITS", 0) or 0)
    return (
        _deposit_commitment() != "finalized"
        and threshold > 0
        and type(amount_units) is int
        and amount_units >= threshold
    )


def _deposit_hold_tuple(
    *, signature: str, block_timestamp: int, reason: str, evidence: dict,
    provider: str, query_identity: str, memo: str | None = None,
    from_address: str | None = None, amount_units: int | None = None,
    network: str, vault_account: str, mint: str, observed_commitment: str,
    finality_required: bool,
) -> tuple:
    return (
        signature, block_timestamp, memo, from_address, amount_units, reason,
        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        provider, query_identity, network, vault_account, mint, observed_commitment,
        int(finality_required),
    )


def _get_strictly_finalized_signatures(
    signatures: list[str], *, provider: str = "helius",
) -> set[str]:
    """Return rooted successful signatures, rejecting incomplete status enumeration."""
    unique = list(dict.fromkeys(signatures))
    if not unique:
        return set()
    for signature in unique:
        Signature.from_string(signature)
    if provider not in {"helius", "core"}:
        raise ValueError("unknown Solana deposit provider")
    values: list = []
    for offset in range(0, len(unique), 256):
        batch = unique[offset:offset + 256]
        if provider == "helius":
            result = _helius_rpc_call(
                "getSignatureStatuses", [batch, {"searchTransactionHistory": True}],
            )
            batch_values = result.get("value") if isinstance(result, dict) else None
        else:
            response = _rpc_call(
                _get_client().get_signature_statuses,
                [Signature.from_string(signature) for signature in batch],
                search_transaction_history=True,
                timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
            )
            batch_values = _rpc_get_value(response)
        if not isinstance(batch_values, list) or len(batch_values) != len(batch):
            raise RuntimeError("Solana finality status enumeration is incomplete")
        values.extend(batch_values)
    finalized: set[str] = set()
    for signature, status in zip(unique, values):
        if status is None:
            continue
        if not isinstance(status, dict) or "err" not in status:
            raise RuntimeError("Malformed Solana finality status evidence")
        if status["err"] is not None:
            raise RuntimeError("Successful deposit conflicts with failed signature status")
        commitment = status.get("confirmationStatus")
        if commitment == "finalized":
            finalized.add(signature)
        elif commitment != "confirmed":
            raise RuntimeError("Unexpected Solana finality status")
    return finalized


def _scan_incoming_deposits_helius(
    token_account_addr: str, *, since_ts: int, page_size: int,
    upper_timestamp: int,
) -> SolanaDepositBacklogProgress:
    """Commit one full-evidence Helius page and its exact provider continuation."""

    vault_account = str(token_account_addr)
    mint = str(getattr(config, "USDC_MINT"))
    commitment = _deposit_commitment()
    helius_url = _helius_rpc_url()
    if not helius_url:
        raise RuntimeError("Helius deposit provider is not configured")
    network = _network_identity_for_url(helius_url)
    cursor = state_db.get_helius_deposit_scan_cursor(vault_account)
    if cursor is None:
        fixed_upper = upper_timestamp
        request_token = None
        previous_timestamp = None
        query_identity = _deposit_query_identity(
            provider="helius", network=network, vault_account=vault_account, mint=mint,
            commitment=commitment, lower_timestamp=since_ts, upper_timestamp=fixed_upper,
            page_size=page_size,
        )
    else:
        fixed_upper = cursor["upper_timestamp"]
        request_token = cursor["pagination_token"]
        previous_timestamp = cursor["previous_timestamp"]
        query_identity = _deposit_query_identity(
            provider="helius", network=network, vault_account=vault_account, mint=mint,
            commitment=commitment, lower_timestamp=since_ts, upper_timestamp=fixed_upper,
            page_size=page_size,
        )
        expected = (
            network, mint, commitment, since_ts, query_identity,
        )
        actual = (
            cursor["network"], cursor["mint"], cursor["commitment"],
            cursor["lower_timestamp"], cursor["query_identity"],
        )
        if actual != expected:
            raise RuntimeError("existing Helius cursor conflicts with current query identity")

    try:
        transactions, next_token = _helius_get_full_deposit_page(
            vault_account, limit=page_size, pagination_token=request_token,
            commitment=commitment, lower_timestamp=since_ts,
            upper_timestamp=fixed_upper,
        )
        if len(transactions) > page_size:
            raise RuntimeError("Helius returned oversized deposit page")
        if not transactions and next_token is not None:
            raise RuntimeError("Helius returned empty deposit page with continuation")
        page_last_timestamp = _validate_deposit_page_order(
            transactions, previous_timestamp,
        )

        scanned: list[tuple[str, int]] = []
        parsed_candidates: list[tuple[str, int, str | None, str | None, int, dict]] = []
        holds: list[tuple] = []
        page_seen: set[str] = set()
        for evidence in transactions:
            tx_obj = evidence.get("transaction")
            meta = evidence.get("meta")
            block_timestamp = evidence.get("blockTime")
            signatures = tx_obj.get("signatures") if isinstance(tx_obj, dict) else None
            signature = signatures[0] if isinstance(signatures, list) and signatures else None
            if (not isinstance(signature, str) or not signature or signature in page_seen
                    or type(block_timestamp) is not int
                    or block_timestamp < since_ts or block_timestamp > fixed_upper
                    or not isinstance(meta, dict) or "err" not in meta):
                raise RuntimeError("Helius deposit identity/range/outcome is incomplete")
            Signature.from_string(signature)
            if meta["err"] is not None:
                raise RuntimeError("Helius succeeded filter returned a failed transaction")
            page_seen.add(signature)
            scanned.append((signature, block_timestamp))
            try:
                memo, source, amount_units = _extract_core_deposit_evidence(
                    evidence, signature=signature, vault_account=vault_account, mint=mint,
                )
            except UnsupportedDepositEvidence as exc:
                holds.append(_deposit_hold_tuple(
                    signature=signature, block_timestamp=block_timestamp,
                    reason=exc.reason, evidence=evidence, provider="helius",
                    query_identity=query_identity, amount_units=exc.amount_units,
                    network=network, vault_account=vault_account, mint=mint,
                    observed_commitment=commitment,
                    finality_required=commitment != "finalized",
                ))
                continue
            if amount_units > 0:
                parsed_candidates.append(
                    (signature, block_timestamp, memo, source, amount_units, evidence)
                )

        requires_finality = [
            candidate[0] for candidate in parsed_candidates
            if _requires_finalized_deposit(candidate[4])
        ]
        finalized = _get_strictly_finalized_signatures(
            requires_finality, provider="helius",
        ) if requires_finality else set()
        deposits: list[tuple[str, int, str | None, str | None, int]] = []
        for signature, block_timestamp, memo, source, amount_units, evidence in parsed_candidates:
            if signature in requires_finality and signature not in finalized:
                holds.append(_deposit_hold_tuple(
                    signature=signature, block_timestamp=block_timestamp,
                    reason="awaiting_finalized", evidence=evidence, provider="helius",
                    query_identity=query_identity, memo=memo, from_address=source,
                    amount_units=amount_units,
                    network=network, vault_account=vault_account, mint=mint,
                    observed_commitment=commitment, finality_required=True,
                ))
            else:
                deposits.append((signature, block_timestamp, memo, source, amount_units))

        complete = next_token is None
        admitted, held = state_db.commit_helius_deposit_scan_page(
            vault_account=vault_account, network=network, mint=mint,
            commitment=commitment, lower_timestamp=since_ts,
            upper_timestamp=fixed_upper, query_identity=query_identity,
            request_pagination_token=request_token,
            next_pagination_token=next_token,
            previous_timestamp=previous_timestamp,
            page_last_timestamp=page_last_timestamp,
            scanned_signatures=scanned, deposits=deposits, holds=holds,
            complete=complete,
        )
        return SolanaDepositBacklogProgress(
            complete, admitted, fixed_upper, None, provider="helius",
            next_pagination_token=next_token, held_count=held,
        )
    except Exception as exc:
        _log("solana_helius_cursor_scan_failed", level=logging.ERROR, error=str(exc))
        raise RuntimeError("Helius deposit scan incomplete; waterline must remain held") from exc


def replay_solana_deposit_holds(limit: int = 1000) -> int:
    """Fairly reparse holds without changing their frozen custody/finality provenance."""
    held_rows = state_db.get_solana_deposit_holds(limit)
    candidates: list[tuple[dict, str | None, str | None, int, bool]] = []
    current_vault = str(getattr(config, "VAULT_USDC_ACCOUNT"))
    current_mint = str(getattr(config, "USDC_MINT"))

    for row in held_rows:
        state_db.record_solana_deposit_hold_replay_attempt(row["signature"])
        provenance = {
            "network": row.get("network"),
            "vault_account": row.get("vault_account"),
            "mint": row.get("mint"),
            "observed_commitment": row.get("observed_commitment"),
        }
        if row["provider"] == "helius" and any(value is None for value in provenance.values()):
            audited = state_db.get_completed_helius_query_provenance(row["query_identity"])
            if audited is not None:
                provenance = audited
        # Pre-upgrade rows remain explicitly unknown; never backfill or relabel them.
        # They may nevertheless be resolved against the current exact vault/mint when a
        # fresh provider finality proof is obtained for the immutable signature/evidence.
        if (provenance["vault_account"] not in {None, current_vault}
                or provenance["mint"] not in {None, current_mint}):
            continue
        try:
            if row["provider"] == "helius":
                provider_url = _helius_rpc_url()
            elif row["provider"] == "core":
                provider_url = str(getattr(config, "RPC_URL", "") or "").strip()
            else:
                continue
            if not provider_url:
                continue
            current_network = _network_identity_for_url(provider_url)
        except Exception as exc:
            _log("solana_deposit_hold_network_unavailable", level=logging.WARNING,
                 sig=row.get("signature"), error=str(exc))
            continue
        if (provenance["network"] is not None
                and current_network != provenance["network"]):
            continue

        try:
            evidence = json.loads(row["evidence_json"])
            memo, source, amount_units = _extract_core_deposit_evidence(
                evidence, signature=row["signature"],
                vault_account=current_vault, mint=current_mint,
            )
        except UnsupportedDepositEvidence:
            continue
        except Exception as exc:
            _log("solana_deposit_hold_replay_failed", level=logging.WARNING,
                 sig=row.get("signature"), error=str(exc))
            continue
        if amount_units <= 0 or row.get("amount_units") != amount_units:
            continue
        observed = provenance["observed_commitment"]
        needs_finalized = (
            row["reason"] == "awaiting_finalized"
            or row.get("finality_required") != 0
            or observed != "finalized"
        )
        candidates.append((row, memo, source, amount_units, needs_finalized))

    # Validate every provider batch before moving any financial row. A malformed later
    # batch therefore cannot leave an earlier batch partially promoted.
    finalized_by_provider: dict[str, set[str]] = {}
    for provider in {row[0]["provider"] for row in candidates}:
        signatures = [
            row[0]["signature"] for row in candidates
            if row[0]["provider"] == provider and row[4]
        ]
        if signatures:
            finalized_by_provider[provider] = _get_strictly_finalized_signatures(
                signatures, provider=provider,
            )

    promoted = 0
    for row, memo, source, amount_units, needs_finalized in candidates:
        if (needs_finalized
                and row["signature"] not in finalized_by_provider.get(row["provider"], set())):
            continue
        promoted += int(state_db.promote_solana_deposit_hold(
            row["signature"], memo=memo, from_address=source, amount_units=amount_units,
        ))
    return promoted


def scan_incoming_deposits_with_durable_cursor(
    token_account_addr: str, *, since_ts: int, page_size: int,
    upper_timestamp: int | None = None,
) -> SolanaDepositBacklogProgress:
    """Use trusted Helius when configured; preserve provider-specific durable cursors."""
    if type(since_ts) is not int or since_ts < 0:
        raise ValueError("Solana scan lower timestamp must be a nonnegative integer")
    if type(page_size) is not int or page_size <= 0 or page_size > 1000:
        raise ValueError("Solana scan page size must be between 1 and 1000")
    if upper_timestamp is None:
        upper_timestamp = int(time.time())
    if type(upper_timestamp) is not int or upper_timestamp <= since_ts:
        raise ValueError("Solana scan upper timestamp must exceed its lower timestamp")

    vault_account = str(token_account_addr)
    helius_cursor = state_db.get_helius_deposit_scan_cursor(vault_account)
    core_cursor = state_db.get_solana_deposit_scan_cursor(vault_account)
    if (core_cursor is not None
            and any(core_cursor.get(field) is None
                    for field in ("network", "commitment", "query_identity"))):
        if not state_db.discard_preupgrade_solana_deposit_scan_cursor(vault_account, since_ts):
            raise RuntimeError("pre-upgrade Solana cursor conflicts with current checkpoint")
        core_cursor = None
    if helius_cursor is not None and core_cursor is not None:
        raise RuntimeError("conflicting Helius and core deposit cursors require operator review")
    if helius_cursor is not None:
        if not _helius_rpc_url():
            raise RuntimeError("Helius cursor cannot be resumed through core RPC")
        return _scan_incoming_deposits_helius(
            vault_account, since_ts=since_ts, page_size=page_size,
            upper_timestamp=upper_timestamp,
        )
    if core_cursor is not None:
        return _scan_incoming_deposits_core(
            vault_account, since_ts=since_ts, page_size=page_size,
        )
    if _helius_rpc_url():
        return _scan_incoming_deposits_helius(
            vault_account, since_ts=since_ts, page_size=page_size,
            upper_timestamp=upper_timestamp,
        )
    return _scan_incoming_deposits_core(
        vault_account, since_ts=since_ts, page_size=page_size,
    )


def _scan_incoming_deposits_core(
    token_account_addr: str, *, since_ts: int, page_size: int,
) -> SolanaDepositBacklogProgress:
    """Commit one exact-cursor core-RPC history page before advancing its cursor.

    `getSignaturesForAddress(before=...)` establishes a fixed descending range even
    when new signatures arrive at the head.  The public timestamp waterline remains
    held until a range reaches strictly below its lower timestamp; equal timestamps
    are scanned rather than used as a pagination boundary.
    """
    if type(since_ts) is not int or since_ts < 0:
        raise ValueError("Solana scan lower timestamp must be a nonnegative integer")

    if type(page_size) is not int or page_size <= 0 or page_size > 1000:
        raise ValueError("Solana scan page size must be between 1 and 1000")

    vault_account = str(token_account_addr)
    mint = str(getattr(config, "USDC_MINT"))
    network = _core_network_identity()
    commitment = _deposit_commitment()
    cursor = state_db.get_solana_deposit_scan_cursor(vault_account)
    if cursor is not None:
        expected_identity = _deposit_query_identity(
            provider="core", network=network, vault_account=vault_account, mint=mint,
            commitment=commitment, lower_timestamp=since_ts,
            upper_timestamp=cursor["upper_timestamp"], page_size=page_size,
        )
        if (cursor["mint"] != mint or cursor["lower_timestamp"] != since_ts
                or cursor["network"] != network or cursor["commitment"] != commitment
                or cursor["query_identity"] != expected_identity):
            raise RuntimeError(
                "existing Solana scan cursor conflicts with current deployment/waterline"
            )
    request_before = cursor["before_signature"] if cursor else None
    expected_upper_timestamp = cursor["upper_timestamp"] if cursor else None
    previous_timestamp = cursor["previous_timestamp"] if cursor else None

    try:
        client = _get_client()
        args: dict[str, Any] = {
            "limit": page_size,
            "commitment": _deposit_commitment(),
        }
        if request_before is not None:
            args["before"] = Signature.from_string(request_before)
        response = _rpc_call(
            client.get_signatures_for_address,
            PublicKey.from_string(vault_account),
            timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
            **args,
        )
        entries = _deposit_rpc_result(response)
        if not isinstance(entries, list) or len(entries) > page_size:
            raise RuntimeError("invalid Solana cursor signature page")
        page_last_timestamp = _validate_deposit_page_order(entries, previous_timestamp)

        upper_timestamp = (
            expected_upper_timestamp
            if expected_upper_timestamp is not None
            else (entries[0]["blockTime"] if entries else None)
        )
        core_query_identity = (
            _deposit_query_identity(
                provider="core", network=network, vault_account=vault_account, mint=mint,
                commitment=commitment, lower_timestamp=since_ts,
                upper_timestamp=upper_timestamp, page_size=page_size,
            )
            if upper_timestamp is not None else None
        )

        deposits: list[tuple[str, int, str | None, str | None, int]] = []
        holds: list[tuple] = []
        seen: set[str] = set()
        reached_lower = False
        last_signature = None
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("invalid Solana cursor signature entry")
            signature = entry.get("signature")
            block_time = entry.get("blockTime")
            if (not isinstance(signature, str) or not signature or signature in seen
                    or type(block_time) is not int or block_time <= 0 or "err" not in entry):
                raise RuntimeError("incomplete Solana cursor signature evidence")
            Signature.from_string(signature)
            seen.add(signature)
            last_signature = signature

            if block_time < since_ts:
                reached_lower = True
                break
            if entry["err"] is not None:
                continue
            if entry.get("confirmationStatus") != _deposit_commitment():
                if entry.get("confirmationStatus") != "finalized":
                    raise RuntimeError("Solana cursor signature commitment not satisfied")
            tx_response = _rpc_call(
                client.get_transaction,
                Signature.from_string(signature),
                encoding="jsonParsed",
                commitment=_deposit_commitment(),
                max_supported_transaction_version=0,
                timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 12),
            )
            tx_data = _deposit_rpc_result(tx_response)
            if not isinstance(tx_data, dict):
                raise RuntimeError("missing Solana cursor transaction")
            try:
                memo, from_address, amount_units = _extract_core_deposit_evidence(
                    tx_data, signature=signature, vault_account=vault_account, mint=mint,
                )
            except UnsupportedDepositEvidence as exc:
                holds.append(_deposit_hold_tuple(
                    signature=signature, block_timestamp=block_time, reason=exc.reason,
                    evidence=tx_data, provider="core",
                    query_identity=str(core_query_identity),
                    amount_units=exc.amount_units, network=network,
                    vault_account=vault_account, mint=mint,
                    observed_commitment=commitment,
                    finality_required=commitment != "finalized",
                ))
                continue
            if amount_units > 0:
                candidate = (signature, block_time, memo, from_address, amount_units)
                if (_requires_finalized_deposit(amount_units)
                        and entry.get("confirmationStatus") != "finalized"):
                    holds.append(_deposit_hold_tuple(
                        signature=signature, block_timestamp=block_time,
                        reason="awaiting_finalized", evidence=tx_data, provider="core",
                        query_identity=str(core_query_identity),
                        memo=memo, from_address=from_address, amount_units=amount_units,
                        network=network, vault_account=vault_account, mint=mint,
                        observed_commitment=commitment, finality_required=True,
                    ))
                else:
                    deposits.append(candidate)

        if upper_timestamp is None:
            # An empty history proves the range only when no continuation was ever
            # required.  There is no timestamp to publish, so retain the heartbeat.
            return SolanaDepositBacklogProgress(True, 0, None, None)
        complete = reached_lower or len(entries) < page_size
        next_before = None if complete else last_signature
        if not complete and not next_before:
            raise RuntimeError("Solana cursor page has no exact continuation")
        admitted = state_db.commit_solana_deposit_scan_page(
            vault_account=vault_account, mint=mint, network=network,
            commitment=commitment, query_identity=str(core_query_identity),
            lower_timestamp=since_ts,
            request_before_signature=request_before, next_before_signature=next_before,
            upper_timestamp=upper_timestamp, previous_timestamp=previous_timestamp,
            page_last_timestamp=page_last_timestamp,
            scanned_signature_count=len(entries),
            deposits=deposits, holds=holds, complete=complete,
        )
        return SolanaDepositBacklogProgress(
            complete, admitted, upper_timestamp, next_before,
            provider="core", held_count=len(holds),
        )
    except Exception as exc:
        _log("solana_cursor_scan_failed", level=logging.ERROR, error=str(exc))
        raise RuntimeError("Solana cursor scan incomplete; waterline must remain held") from exc


def process_unprocessed_solana_deposits(limit: int = 1000, timeout: float = 8.0) -> list:
    """
    Process unprocessed deposit signatures from DB.
    Fetches oldest unprocessed sigs up to limit, validates memo format "nexus:<address>",
    checks the destination Nexus token account, runs idempotency checks, debits if valid,
    and updates status accordingly.
    
    Returns: Number of sigs processed.
    """
    from . import state_db, nexus_client

    # 1. Fetch unprocessed sigs (oldest first)
    unprocessed = state_db.filter_unprocessed_sigs({
        'status': 'ready for processing',
        'limit': limit
    })
    if not unprocessed:
        return 0
    
    proc_count_swap = 0
    proc_count_refund = 0
    proc_count_quar = 0
    proc_count_mic = 0

    processing_secs = 0
    timestamp_start = time.monotonic()
    current_timestamp = time.monotonic()
    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_solana, status, txid in unprocessed[:limit]:
        processing_secs = current_timestamp - timestamp_start
        if processing_secs >= timeout:
            break
        try:
            # 2. Check existing status "ready for processing"
            if state_db.get_unprocessed_sig_status(sig) != "ready for processing":
                continue

            # 3. Run idempotency checks: already processed?
            if state_db.is_processed_sig(sig) or state_db.is_quarantined_sig(sig) or state_db.is_refunded_sig(sig):
                state_db.remove_unprocessed_sig(sig)
                continue

            # 4. Validate memo format
            prefix = str(getattr(config, "DEPOSIT_MEMO_PREFIX", "nexus:"))
            if not memo or not memo.lower().startswith(prefix.lower()):
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid memo
                proc_count_refund += 1
                continue

            nexus_address = memo[len(prefix):].strip()
            if not nexus_address:
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid memo
                proc_count_refund += 1
                continue

            # 5. Check Nexus Nexus token account validity
            if not nexus_client.is_valid_nexus_token_account(nexus_address):
                state_db.update_unprocessed_sig_status(sig, "to be refunded") # invalid account
                proc_count_refund += 1
                continue

            # 5b. Per-swap size cap: refund oversized deposits rather than minting
            # against them. Bounds the blast radius of a bug or a hostile deposit.
            max_swap = int(getattr(config, "MAX_SWAP_SOLANA_UNITS", 0) or 0)
            if max_swap > 0 and int(amount_solana or 0) > max_swap:
                from . import alerts
                alerts.warning("swap_over_cap",
                               "deposit exceeds MAX_SWAP_USDC; refunding instead of swapping",
                               sig=sig, amount_units=int(amount_solana or 0), cap_units=max_swap)
                state_db.update_unprocessed_sig_status(sig, "to be refunded")
                proc_count_refund += 1
                continue

            # 6. Calculate amount minus fees
            # Base units, exact integer math (no float / scientific-notation hazard).
            net_amount = nexus_client.get_nexus_send_amount_units(amount_solana)
            if net_amount <= 0:
                # Bug #12 fix: Track the fee (entire deposit amount is kept as fee)
                state_db.add_fee_entry(
                    sig=sig,
                    txid=None,
                    kind="micro_deposit_fee",
                    amount_usdc_units=int(amount_solana),
                    amount_usdd_units=None
                )
                state_db.mark_processed_sig(sig, timestamp, int(amount_solana), None, 0, "processed, amount after fees <= 0", None)
                state_db.remove_unprocessed_sig(sig)
                proc_count_mic += 1
                continue

            # 7. Debit the Nexus-side token if valid.
            # Cross-cycle/cross-thread guard: only one worker may act on this deposit.
            # The literal state_db.DEBIT_RESERVATION_KIND is deliberately NOT renamed: it is a row
            # value in the `reservations` table, not a code identifier. Renaming it would
            # make a live reservation written by the previous build invisible to this one,
            # so a process that crashed mid-debit could be re-debited after the upgrade.
            # See DEBIT_RESERVATION_KIND in state_db for the frozen-name rationale.
            if not state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, sig, ttl_sec=600):
                continue

            # Bug #9 fix: next_reference() atomically increments to prevent duplicate references.
            reference = state_db.next_reference()

            # Persist INTENT before touching the chain. If we crash here, or the CLI
            # answer is unreadable, the reference is on disk and the outcome can be
            # resolved against the chain (resolve_unverified_debits) instead of guessed.
            # Guessing is what previously produced a double mint, or a mint AND a refund.
            state_db.set_unprocessed_sig_debit_intent(sig, reference, net_amount)
            state_db.record_attempt(state_db.debit_attempt_key(sig))

            try:
                result = nexus_client.debit_nexus_token_with_txid(nexus_address, net_amount, reference)
            except Exception as e:
                # Timeout or transport failure: the debit may still have executed.
                state_db.update_unprocessed_sig_status(sig, "debit unverified")
                _log("nexus_debit_outcome_unknown", level=logging.WARNING, sig=sig,
                     reference=reference, error=str(e))
                continue

            if result[0] and result[1]:
                proc_count_swap += 1
                state_db.update_unprocessed_sig_txid(sig, str(result[1]))
                state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")
            else:
                # The CLI reported failure OR returned an unparsable body. Both are
                # AMBIGUOUS - debit_nexus_token_with_txid returns (False, None) when the call
                # succeeded but no txid could be parsed. Never refund on this signal.
                state_db.update_unprocessed_sig_status(sig, "debit unverified")
                _log("nexus_debit_outcome_unknown", level=logging.WARNING, sig=sig,
                     reference=reference, reason="missing_remote_txid")
        except Exception as e:
            _log("solana_deposit_processing_failed", level=logging.ERROR, sig=sig, error=str(e))
            continue

        current_timestamp = time.monotonic()

    return [proc_count_swap, proc_count_refund, proc_count_quar, proc_count_mic]


def _is_token_account_for_mint(token_account_addr: str, mint: PublicKey) -> bool:
    """Return True if the address is an SPL token account for the given mint."""
    try:
        client = _get_client()
        resp = _rpc_call(client.get_account_info, PublicKey.from_string(token_account_addr), encoding="jsonParsed")
        val = _rpc_get_value(resp)
        if not val or not isinstance(val, dict):
            return False
        if val.get("owner") != str(TOKEN_PROGRAM_ID):
            return False
        data = val.get("data", {})
        parsed = data.get("parsed") if isinstance(data, dict) else None
        if not isinstance(parsed, dict):
            return False
        info = parsed.get("info") or {}
        if not isinstance(info, dict):
            return False
        mint_str = info.get("mint")
        return str(mint_str) == str(mint)
    except Exception:
        return False
    

def _is_solana_wallet_with_ata(wallet_address: str) -> bool:
    """Return True if the address is a Solana wallet with an existing associated token account."""
    try:
        client = _get_client()

        # 1. Validate the wallet address exists (basic check)
        wallet_resp = _rpc_call(client.get_account_info, PublicKey.from_string(wallet_address))
        wallet_val = _rpc_get_value(wallet_resp)
        if not wallet_val or not isinstance(wallet_val, dict):
            return False # wallet doesn't exist
        
        # 2. Derive the expected token ATA address
        owner = PublicKey.from_string(wallet_address)
        ata_address = get_associated_token_address(owner, config.USDC_MINT)

        # 3. Check if the ATA account exists and is valid token account
        ata_resp = _rpc_call(client.get_account_info, ata_address, encoding="jsonParsed")
        ata_val = _rpc_get_value(ata_resp)
        if not ata_val or not isinstance(ata_val, dict):
            return False # ATA doesn't exist
        
        # Confirmed it's owned by Token Program and has correct mint
        if ata_val.get("owner") != str(TOKEN_PROGRAM_ID):
            return False

        data = ata_val.get("data", {})
        parsed = data.get("parsed") if isinstance(data, dict) else None
        if not isinstance(parsed, dict):
            return False
        
        info = parsed.get("info") or {}
        if not isinstance(info, dict):
            return False

        mint_str = info.get("mint")
        return str(mint_str) == str(config.USDC_MINT)

    except Exception:
        return False


def process_solana_deposits_refunding(limit: int = 1000, timeout: float = 8.0) -> int:

    from . import state_db, nexus_client

    # 1. Fetch unprocessed sigs (oldest first)
    unprocessed = state_db.filter_unprocessed_sigs({
        'status_like': '%to be refunded%',
        'limit': limit
    })
    if not unprocessed:
        return 0
    
    processed_count = 0
    processing_secs = 0
    timestamp_start = time.monotonic()
    current_timestamp = time.monotonic()
    
    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in unprocessed[:limit]:
        
        processing_secs = current_timestamp - timestamp_start
        if processing_secs >= timeout:
            break

        try:
            # 2. Check status "to be refunded"
            if state_db.get_unprocessed_sig_status(sig) != "to be refunded":
                continue

            # 3. Run idempotency checks: already processed?
            if state_db.is_processed_sig(sig) or state_db.is_quarantined_sig(sig):
                state_db.remove_unprocessed_sig(sig)
                continue
            
            # 4. Check refund net amount. Persisted money must remain an exact base-unit integer.
            if type(amount_usdc_units) is not int or amount_usdc_units <= 0:
                state_db.update_unprocessed_sig_status(sig, "refund submission held")
                _log("solana_refund_invalid_source_units", level=logging.ERROR, sig=sig)
                continue
            net_amount = amount_usdc_units - int(config.SWAP_PAIR.fees.refund_solana_units)
            if net_amount <= 0:
                # Bug #12 fix: Track the fee (entire deposit amount is kept as fee for failed refunds)
                state_db.add_fee_entry(
                    sig=sig,
                    txid=None,
                    kind="refund_micro_fee",
                    amount_usdc_units=int(amount_usdc_units),
                    amount_usdd_units=None
                )
                state_db.mark_processed_sig(sig, timestamp, amount_usdc_units, None, 0, "processed, amount after fees <= 0", None)
                # (sig, timestamp, amount_usdc_units, txid, amount_nexus_debited, status, reference)
                state_db.remove_unprocessed_sig(sig)
                continue

            # 5. Resolve the exact token account before freezing the obligation.  A
            # wallet owner is not the transfer recipient: its existing ATA is.
            destination_address = _resolve_solana_token_destination(from_address)
            if destination_address is None:
                state_db.update_unprocessed_sig_status(sig, "to be quarantined")
                continue

            # 6. A prior legacy attempt has no durable pre-RPC obligation. A positive
            # memo match may be adopted, but a bounded miss is never permission to
            # submit again; hold it for operator evidence instead.
            refund_key = state_db.refund_attempt_key(sig)
            if state_db.get_attempt_count(refund_key) > 0:
                existing_refund = find_signature_with_memo(f"refundSig:{sig}")
                if existing_refund:
                    state_db.update_unprocessed_sig_status(sig, "refund sent, awaiting confirmation")
                    state_db.mark_refunded_sig(sig, timestamp, from_address, amount_usdc_units, memo,
                                                existing_refund, net_amount, "awaiting confirmation")
                    processed_count += 1
                else:
                    state_db.update_unprocessed_sig_status(sig, "refund submission held")
                    _log("solana_refund_legacy_attempt_held", level=logging.WARNING, sig=sig)
                continue

            # 7. Reserve the exact rolling-cap capacity and persist the source before
            # RPC. A later timeout/crash retains this reservation and becomes a hold.
            payout_memo = _solana_sig_disposition_memo("refund", sig)
            if not state_db.prepare_solana_sig_disposition(
                source_sig=sig, kind="refund", timestamp=timestamp,
                from_address=from_address, destination_address=destination_address,
                amount_usdc_units=amount_usdc_units, memo=memo, payout_memo=payout_memo,
                payout_units=net_amount,
                cap_units=int(getattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 0) or 0),
            ):
                _log("solana_refund_budget_or_claim_refused", level=logging.WARNING, sig=sig)
                continue
            state_db.record_attempt(refund_key)
            ok, refund_signature = send_solana_token_to_account_with_sig(
                destination_address, net_amount, memo=payout_memo,
            )
            if not ok or not refund_signature:
                _log("solana_refund_submission_held", level=logging.ERROR, sig=sig)
                continue
            if not state_db.record_solana_sig_disposition_submission(
                source_sig=sig, kind="refund", payout_signature=refund_signature,
            ):
                _log("solana_refund_submission_ledger_hold", level=logging.ERROR,
                     sig=sig, refund_signature=refund_signature)
                continue
            processed_count += 1

        except Exception as e:
            _log("solana_refund_processing_failed", level=logging.ERROR, sig=sig, error=str(e))
            continue

        current_timestamp = time.monotonic()

    return processed_count


def process_solana_deposits_quarantine(limit: int = 1000, timeout: float = 25.0) -> int:

    from . import state_db, nexus_client

    # 1. Fetch unprocessed sigs to be quarantined (oldest first)
    # Include 'quarantine failed': it was previously written and then never selected
    # again by any pass, stranding the row (and the funds) in unprocessed_sigs forever.
    unprocessed = state_db.filter_unprocessed_sigs({
        'status_in': ('to be quarantined', 'quarantine failed'),
        'limit': limit
    })
    if not unprocessed:
        return 0
    
    processed_count = 0
    processing_secs = 0
    timestamp_start = time.monotonic()
    current_timestamp = time.monotonic()
    
    # filter_unprocessed_sigs returns: (sig, timestamp, memo, from_address, amount_usdc_units, status, txid)
    for sig, timestamp, memo, from_address, amount_usdc_units, status, txid in unprocessed[:limit]:
        
        processing_secs = current_timestamp - timestamp_start
        if processing_secs >= timeout:
            break

        try:
            # 2. Re-check status (either pending or a previously failed attempt)
            if state_db.get_unprocessed_sig_status(sig) not in ("to be quarantined", "quarantine failed"):
                continue
            # Bound the retry so a permanently failing quarantine cannot spin every cycle.
            if not state_db.should_attempt(state_db.quarantine_send_attempt_key(sig)):
                continue
            state_db.record_attempt(state_db.quarantine_send_attempt_key(sig))

            # 3. Run idempotency checks: already processed?
            if state_db.is_processed_sig(sig) or state_db.is_refunded_sig(sig):
                state_db.remove_unprocessed_sig(sig)
                continue
            
            # 4. Check quarantine net amount. Persisted money must be exact base units.
            if type(amount_usdc_units) is not int or amount_usdc_units <= 0:
                state_db.update_unprocessed_sig_status(sig, "quarantine submission held")
                _log("solana_quarantine_invalid_source_units", level=logging.ERROR, sig=sig)
                continue
            net_amount = amount_usdc_units - int(config.SWAP_PAIR.fees.refund_solana_units)

            # 4b. Nothing left after the fee: there is nothing to move, so finalise here.
            # A durable disposition requires positive exact output units; otherwise an
            # awaiting-confirmation row could carry no signature and remain stuck forever.
            if net_amount <= 0:
                state_db.add_fee_entry(sig=sig, txid=None, kind="quarantine_micro_fee",
                                       amount_usdc_units=int(amount_usdc_units), amount_usdd_units=None)
                state_db.mark_processed_sig(sig, timestamp, int(amount_usdc_units), None, 0,
                                            "processed, amount after fees <= 0", None)
                state_db.remove_unprocessed_sig(sig)
                processed_count += 1
                continue

            # 5. A prior legacy attempt is ambiguous unless a positive memo match
            # proves it. A bounded absence must become a visible hold, not a resend.
            quar_key = state_db.quarantine_attempt_key(sig)
            if state_db.get_attempt_count(quar_key) > 0:
                existing_quar = find_signature_with_memo(f"quarantinedSig:{sig}")
                if existing_quar:
                    state_db.update_unprocessed_sig_status(sig, "quarantine sent, awaiting confirmation")
                    state_db.mark_quarantined_sig(sig, timestamp, from_address, amount_usdc_units, memo,
                                                   existing_quar, net_amount, "awaiting confirmation")
                    processed_count += 1
                else:
                    state_db.update_unprocessed_sig_status(sig, "quarantine submission held")
                    _log("solana_quarantine_legacy_attempt_held", level=logging.WARNING, sig=sig)
                continue

            # 6. Resolve the exact token-account recipient before freezing the
            # obligation.  Quarantine is configured as a token account in production;
            # allowing an owner here preserves local/test compatibility while keeping
            # the actual ATA immutable in the durable record.
            destination_address = _resolve_solana_token_destination(config.USDC_QUARANTINE_ACCOUNT)
            if destination_address is None:
                state_db.update_unprocessed_sig_status(sig, "quarantine submission held")
                _log("solana_quarantine_destination_invalid", level=logging.ERROR, sig=sig)
                continue

            # 7. Freeze the exact obligation and reserve capacity before RPC. The
            # reservation survives any unreturned/ambiguous send outcome.
            payout_memo = _solana_sig_disposition_memo("quarantine", sig)
            if not state_db.prepare_solana_sig_disposition(
                source_sig=sig, kind="quarantine", timestamp=timestamp,
                from_address=from_address, destination_address=destination_address,
                amount_usdc_units=amount_usdc_units, memo=memo, payout_memo=payout_memo,
                payout_units=net_amount,
                cap_units=int(getattr(config, "DAILY_PAYOUT_CAP_SOLANA_UNITS", 0) or 0),
            ):
                _log("solana_quarantine_budget_or_claim_refused", level=logging.WARNING, sig=sig)
                continue
            state_db.record_attempt(quar_key)
            ok, quarantine_signature = send_solana_token_to_account_with_sig(
                destination_address, net_amount, memo=payout_memo,
            )
            if not ok or not quarantine_signature:
                _log("solana_quarantine_submission_held", level=logging.ERROR, sig=sig)
                continue
            if not state_db.record_solana_sig_disposition_submission(
                source_sig=sig, kind="quarantine", payout_signature=quarantine_signature,
            ):
                _log("solana_quarantine_submission_ledger_hold", level=logging.ERROR,
                     sig=sig, quarantine_signature=quarantine_signature)
                continue
            processed_count += 1

        except Exception as e:
            _log("solana_quarantine_processing_failed", level=logging.ERROR, sig=sig, error=str(e))
            continue

        current_timestamp = time.monotonic()

    return processed_count


def send_solana_token(destination: str, amount_base_units: int, memo: str | None = None) -> tuple[bool, str | None]:
    """Disabled legacy sender retained only for import compatibility.

    This helper had a non-atomic cap check followed by RPC and best-effort ledger
    recording. Every runtime financial caller must instead reserve a durable source
    obligation before using an RPC-only sender.
    """
    _log("legacy_solana_sender_refused", level=logging.ERROR,
         destination=str(destination), amount_units=amount_base_units)
    return False, None


def check_sig_confirmations(min_confirmations: int, timeout: float) -> int:
    """Check confirmation status for Solana refund transactions.
    
    This function queries refunded_sigs for entries awaiting confirmation,
    then checks the REFUND signature (not the original deposit signature)
    for on-chain confirmation status.
    """
    # Query refunded_sigs table for entries awaiting confirmation
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, refund_sig FROM refunded_sigs
        WHERE status = 'awaiting confirmation' AND refund_sig IS NOT NULL
        LIMIT 1000
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return 0
    
    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    for deposit_sig, refund_sig in rows:
        if time.monotonic() - time_start > timeout:
            break
        if refund_sig and _solana_sig_disposition_has_exact_evidence(
            refund_sig, "refund", deposit_sig
        ):
            # Exact successful finalized transfer evidence, not status alone.
            try:
                settled = state_db.confirm_solana_sig_disposition(
                    source_sig=deposit_sig, kind="refund", payout_signature=refund_sig,
                )
                if settled is True:
                    processed_count += 1
                    _log("solana_refund_confirmed", deposit_sig=deposit_sig,
                         refund_signature=refund_sig)
                    continue
                if settled is False:
                    _log("solana_refund_confirmation_held", level=logging.ERROR,
                         deposit_sig=deposit_sig, refund_signature=refund_sig)
                    continue
                _log("solana_refund_confirmation_held", level=logging.ERROR,
                     deposit_sig=deposit_sig, refund_signature=refund_sig,
                     reason="missing_durable_disposition_evidence")
            except Exception as e:
                _log("solana_refund_confirmation_persist_failed", level=logging.ERROR,
                     deposit_sig=deposit_sig, error=str(e))
        # If confirmations is None, skip (not confirmed yet)

    return processed_count


def check_quarantine_confirmations(min_confirmations: int, timeout: float) -> int:
    """Check confirmation status for Solana-side quarantine transactions.
    
    Bug #8 fix: This function queries quarantined_sigs for entries awaiting confirmation,
    then checks the QUARANTINE signature for on-chain confirmation status.
    Only after confirmation is the deposit removed from unprocessed_sigs.
    """
    # Query quarantined_sigs table for entries awaiting confirmation
    conn = state_db.sqlite3.connect(state_db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sig, quarantine_sig FROM quarantined_sigs
        WHERE status = 'awaiting confirmation' AND quarantine_sig IS NOT NULL
        LIMIT 1000
    """)
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return 0
    
    processed_count = 0
    time_start = time.monotonic()
    current_time = time_start
    for deposit_sig, quarantine_sig in rows:
        if time.monotonic() - time_start > timeout:
            break
        if quarantine_sig and _solana_sig_disposition_has_exact_evidence(
            quarantine_sig, "quarantine", deposit_sig
        ):
            # Exact successful finalized transfer evidence, not status alone.
            try:
                settled = state_db.confirm_solana_sig_disposition(
                    source_sig=deposit_sig, kind="quarantine", payout_signature=quarantine_sig,
                )
                if settled is True:
                    processed_count += 1
                    _log("solana_quarantine_confirmed", deposit_sig=deposit_sig,
                         quarantine_signature=quarantine_sig)
                    continue
                if settled is False:
                    _log("solana_quarantine_confirmation_held", level=logging.ERROR,
                         deposit_sig=deposit_sig, quarantine_signature=quarantine_sig)
                    continue
                _log("solana_quarantine_confirmation_held", level=logging.ERROR,
                     deposit_sig=deposit_sig, quarantine_signature=quarantine_sig,
                     reason="missing_durable_disposition_evidence")
            except Exception as e:
                _log("solana_quarantine_confirmation_persist_failed", level=logging.ERROR,
                     deposit_sig=deposit_sig, error=str(e))
        # If confirmations is None, skip (not confirmed yet)

    return processed_count


def _rpc_to_json(resp):
    try:
        if isinstance(resp, dict):
            return resp
        tj = getattr(resp, "to_json", None)
        if callable(tj):
            return json.loads(tj())
    except Exception:
        pass

def _rpc_get_result(resp):
    js = _rpc_to_json(resp)
    if isinstance(js, dict):
        return js.get("result") or js.get("value") or js
    # Fallback to .value on typed responses
    val = getattr(resp, "value", None)
    return val if val is not None else resp


def _rpc_get_value(resp):
    res = _rpc_get_result(resp)
    if isinstance(res, dict):
        v = res.get("value", None)
        return v if v is not None else res
    return res


def transfer_checked(*, program_id: PublicKey, source: PublicKey, mint: PublicKey, dest: PublicKey,
                     owner: PublicKey, amount: int, decimals: int, signers: list) -> TransactionInstruction:
    """Minimal TransferChecked instruction builder (no multisig signers support)."""
    if signers:
        raise NotImplementedError("Multisig owners not supported without spl.token installed")
    data = pack("<BQB", 12, int(amount), int(decimals))  # 12 = TransferChecked
    keys = [
        AccountMeta(pubkey=source, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=dest, is_signer=False, is_writable=True),
        AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
    ]
    return TransactionInstruction(program_id=program_id, accounts=keys, data=data)


def _memo_ix(memo: str | None) -> TransactionInstruction | None:
    if not memo:
        return None
    try:
        memo_prog = PublicKey.from_string("Memo111111111111111111111111111111111111111")
        data = bytes(memo, "utf-8")
        return TransactionInstruction(program_id=memo_prog, accounts=[], data=data)
    except Exception:
        return None


def get_associated_token_address(*, owner: PublicKey, mint: PublicKey) -> PublicKey:
    # In solders, find_program_address is on Pubkey
    seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
    ata, _ = PublicKey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
    return ata


def _rpc_call(method, *args, timeout: Optional[float] = None, **kwargs):
    """Run an RPC client method in a thread with timeout to avoid hangs."""
    if timeout is None:
        timeout = getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8)
    q: "queue.Queue[tuple[bool, object]]" = queue.Queue(maxsize=1)
    def _runner():
        try:
            res = method(*args, **kwargs)
            q.put((True, res))
        except Exception as e:  # pragma: no cover
            q.put((False, e))
    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    try:
        ok, val = q.get(timeout=timeout)
    except Exception:  # timeout
        raise TimeoutError(f"RPC call timeout after {timeout}s: {getattr(method, '__name__', method)}")
    if ok:
        return val
    raise val  # re-raise exception from thread


def _get_latest_blockhash_str(client: Client) -> Optional[str]:
    try:
        resp = _rpc_call(client.get_latest_blockhash, timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))
    except Exception:
        return None
    # Prefer to_json for stability
    js = _rpc_to_json(resp)
    if isinstance(js, dict):
        try:
            return (((js.get("result") or {}).get("value") or {}).get("blockhash"))
        except Exception:
            pass
    # Fallback to typed .value
    try:
        val = getattr(resp, "value", None)
        if val is not None:
            bh = getattr(val, "blockhash", None)
            if bh is not None:
                return str(bh)
    except Exception:
        pass
    return None


def _build_and_send_legacy_tx(instructions: list[TransactionInstruction], kp: Keypair) -> str:
    """Build, sign (legacy) and send a transaction using solders; return signature string.
    Wrapped with per-step RPC timeouts.
    """
    client = _get_client()
    bh = _get_latest_blockhash_str(client)
    if not bh:
        raise RuntimeError("Failed to fetch recent blockhash")
    recent = Hash.from_string(bh)
    tx = Transaction.new_signed_with_payer(instructions, kp.pubkey(), [kp], recent)
    send_resp = _rpc_call(client.send_raw_transaction, bytes(tx), timeout=getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8))
    sig = _rpc_get_result(send_resp)
    if not isinstance(sig, str):
        raise RuntimeError(f"Failed to send tx, unexpected response: {send_resp}")
    # Do NOT block on confirmation here: every send path has a dedicated confirmation
    # pass (check_sig_confirmations / check_quarantine_confirmations / Nexus->Solana
    # Priority 3). Blocking here would throttle throughput to a few sends per cycle.
    global last_sent_sig
    last_sent_sig = sig
    return sig


def _get_vault_secret_bytes() -> bytes:
    with open(config.VAULT_KEYPAIR_PATH, "r") as f:
        data = json.load(f)
    if isinstance(data, list):
        return bytes(data)
    raise ValueError("Unsupported keypair format; expected JSON array of ints")


def load_vault_keypair() -> Keypair:
    return Keypair.from_bytes(_get_vault_secret_bytes())


def load_vault_solders_keypair():
    return load_vault_keypair()


def get_vault_sol_balance() -> int:
    """Return vault wallet SOL balance in lamports."""
    try:
        client = _get_client()
        kp = load_vault_keypair()
        resp = _rpc_call(client.get_balance, kp.pubkey())
        val = _rpc_get_value(resp)
        if isinstance(val, dict):
            return int(val.get("value") or 0)
        if isinstance(val, int):
            return int(val)
        return 0
    except Exception:
        return 0


# Short-lived cache for hot, repeated balance reads (the maintenance block reads the
# vault balance for the backing check, reconcile, and metrics within one loop iteration).
_balance_cache: dict[str, tuple[float, int]] = {}


def get_token_account_balance(token_account_addr: str, *, max_age_sec: float = 0.0) -> int:
    """Return the SPL token account balance in base units.

    Pass ``max_age_sec`` > 0 to accept a recently-cached value (only successful reads
    are cached). Callers needing a fresh value (e.g. the deposit-delta poll) omit it.
    """
    key = str(token_account_addr)
    if max_age_sec > 0:
        ent = _balance_cache.get(key)
        if ent is not None and (time.monotonic() - ent[0]) <= max_age_sec:
            return ent[1]
    try:
        client = _get_client()
        resp = _rpc_call(client.get_token_account_balance, PublicKey.from_string(key))
        val = _rpc_get_value(resp)
        amt = None
        if isinstance(val, dict):
            amt = val.get("amount")
        result = int(amt or 0)
        _balance_cache[key] = (time.monotonic(), result)
        return result
    except Exception:
        return 0


def transfer_solana_token_between_accounts(source_token_account: str, dest_token_account: str, amount_base_units: int) -> bool:
    """Transfer the bridged token between two token accounts owned by the vault wallet."""
    try:
        if amount_base_units <= 0:
            return True
        kp = load_vault_keypair()
        ix = transfer_checked(
            program_id=TOKEN_PROGRAM_ID,
            source=PublicKey.from_string(source_token_account),
            mint=config.USDC_MINT,
            dest=PublicKey.from_string(dest_token_account),
            owner=kp.pubkey(),
            amount=amount_base_units,
            decimals=config.USDC_DECIMALS,
            signers=[],
        )
        sig = _build_and_send_legacy_tx([ix], kp)
        _log("solana_internal_transfer_submitted", signature=sig,
             amount_units=int(amount_base_units))
        return True
    except Exception as e:
        _log("solana_internal_transfer_failed", level=logging.ERROR, error=str(e))
        return False


def check_timestamp_unpr_sigs() -> int | None:
    """
    Find the block time (timestamp) of the oldest unprocessed sig in DB and propose it as a new waterline.
    This can be used for recovery or waterline adjustment based on unprocessed entries.
    Returns the proposed waterline timestamp (int), or None if no unprocessed sigs found.
    """
    # NOTE: this used to be `from . import state_db, state` - there is no `state` module,
    # so every call raised ImportError, aborting the poll before the waterline/heartbeat
    # could be updated. Function-level imports like this are invisible to byte-compilation.
    from . import state_db

    # Fetch the oldest unprocessed sig (limit=1, sorted by timestamp ASC)
    unprocessed = state_db.filter_unprocessed_sigs({'limit': 1})
    if not unprocessed:
        return None
    
    # Extract timestamp from the oldest sig (index 1 in tuple)
    oldest_timestamp = unprocessed[0][1]
    
    # Propose the waterline as oldest_timestamp - 1 to ensure the oldest sig is included in the next poll
    new_waterline = oldest_timestamp - 1
    
    # Propose the new waterline
    state_db.propose_solana_waterline(new_waterline)
    _log("solana_waterline_proposed", proposed_waterline=new_waterline,
         oldest_unprocessed_timestamp=oldest_timestamp)
    
    return new_waterline


def swap_token_for_sol_via_jupiter(amount_solana_base_units: int, slippage_bps: int = 50) -> bool:
    """Swap token->SOL using Jupiter. Returns True on success.
    Requires: config.USDC_MINT (the configured Solana mint) and a vault keypair holding a
    token account for it.
    """
    try:
        if amount_solana_base_units <= 0:
            return False
        client = _get_client()
        kp = load_vault_keypair()
        owner = kp.pubkey()

        # Jupiter Quote API v6
        base = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": str(config.USDC_MINT),
            "outputMint": "So11111111111111111111111111111111111111112",
            "amount": str(int(amount_solana_base_units)),
            "slippageBps": str(int(slippage_bps)),
            "onlyDirectRoutes": "false",
        }
        q = requests.get(base, params=params, timeout=15)
        q.raise_for_status()
        qd = q.json()
        routes = qd.get("data") or []
        if not routes:
            _log("solana_jupiter_route_unavailable", level=logging.WARNING)
            return False
        route = routes[0]

        # Jupiter Swap API v6: get swap transaction
        swap_url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "quoteResponse": route,
            "userPublicKey": str(owner),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 0,
            "dynamicComputeUnitLimit": True,
        }
        s = requests.post(swap_url, json=payload, timeout=20)
        s.raise_for_status()
        sd = s.json()
        swap_tx_b64 = sd.get("swapTransaction")
        if not swap_tx_b64:
            _log("solana_jupiter_swap_missing_transaction", level=logging.ERROR)
            return False

        tx_bytes = base64.b64decode(swap_tx_b64)
        vtx = VersionedTransaction.from_bytes(tx_bytes)
        vtx.sign([kp])
        raw = bytes(vtx)
        send_resp = client.send_raw_transaction(raw)
        sig = _rpc_get_result(send_resp)
        if not isinstance(sig, str):
            _log("solana_jupiter_swap_invalid_response", level=logging.ERROR)
            return False
        try:
            client.confirm_transaction(sig, commitment="confirmed")
        except Exception:
            pass
        _log("solana_jupiter_swap_submitted", signature=sig,
             amount_units=int(amount_solana_base_units))
        return True
    except Exception as e:
        _log("solana_jupiter_swap_failed", level=logging.ERROR, error=str(e))
        return False




def ensure_send_token(to_owner_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send Solana base units to a Solana owner address. Requires recipient ATA to already exist.
    If memo is provided, attach it to the transaction for idempotency tracing.
    """
    try:
        kp = load_vault_keypair()
        owner = PublicKey.from_string(to_owner_addr)
        dest_ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        client = _get_client()
        ata_info = _rpc_get_value(_rpc_call(client.get_account_info, dest_ata))
        if ata_info is None:
            _log("solana_recipient_ata_missing", level=logging.WARNING,
                 destination=to_owner_addr)
            return False
        if amount_base_units <= 0:
            return True
        ixs = [transfer_checked(
            program_id=TOKEN_PROGRAM_ID,
            source=config.VAULT_USDC_ACCOUNT,
            mint=config.USDC_MINT,
            dest=dest_ata,
            owner=kp.pubkey(),
            amount=amount_base_units,
            decimals=config.USDC_DECIMALS,
            signers=[],
        )]
        mix = _memo_ix(memo)
        if mix:
            ixs.append(mix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        _log("solana_owner_payout_submitted", signature=sig, destination=to_owner_addr,
             amount_units=int(amount_base_units), token=config.SOLANA_TOKEN_SYMBOL)
        return True
    except Exception as e:
        _log("solana_owner_payout_failed", level=logging.ERROR,
             destination=to_owner_addr, token=config.SOLANA_TOKEN_SYMBOL, error=str(e))
        return False





def send_solana_token_to_account_with_sig(dest_token_account_addr: str, amount_base_units: int, memo: str | None = None) -> tuple[bool, str | None]:
    """Submit Solana base units directly to an existing token account.

    This RPC helper deliberately owns no Nexus idempotency or terminal state.  The
    caller must persist an exact source intent before submission and finalize it only
    after independently validating the returned transaction evidence.
    """
    
    try:
        if amount_base_units <= 0:
            return True, None
        if not _is_token_account_for_mint(dest_token_account_addr, config.USDC_MINT):
            _log("solana_payout_destination_invalid", level=logging.WARNING,
                 destination=dest_token_account_addr, reason="wrong_token_account_or_mint")
            return False, None
        kp = load_vault_keypair()
        dest = PublicKey.from_string(dest_token_account_addr)
        ixs = [
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=dest,
                owner=kp.pubkey(),
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        ]
        mix = _memo_ix(memo)
        if mix:
            ixs.append(mix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        _log("solana_token_account_payout_submitted", signature=sig,
             destination=dest_token_account_addr, amount_units=int(amount_base_units),
             token=config.SOLANA_TOKEN_SYMBOL)
        return True, sig
    except Exception as e:
        _log("solana_token_account_payout_failed", level=logging.ERROR, error=str(e))
        return False, None


def send_solana_token_owner_or_account_with_sig(
    destination: str, amount_base_units: int, memo: str | None = None,
) -> tuple[bool, str | None]:
    """Submit to an existing token account or to an owner's existing ATA.

    This is an RPC-only helper. Callers must have already persisted a durable
    obligation and must record the returned signature before treating the send as
    retryable or settled.
    """
    if _is_token_account_for_mint(destination, config.USDC_MINT):
        return send_solana_token_to_account_with_sig(destination, amount_base_units, memo)
    if not _is_solana_wallet_with_ata(destination):
        _log("solana_payout_destination_invalid", level=logging.WARNING,
             destination=destination, reason="missing_token_account_or_ata")
        return False, None
    try:
        destination = str(get_associated_token_address(
            owner=PublicKey.from_string(destination), mint=config.USDC_MINT,
        ))
    except Exception as exc:
        _log("solana_payout_destination_invalid", level=logging.WARNING,
             destination=destination, error=str(exc))
        return False, None
    return send_solana_token_to_account_with_sig(destination, amount_base_units, memo)


def ensure_send_token_to_account(dest_token_account_addr: str, amount_base_units: int, memo: str | None = None) -> bool:
    ok, _sig = send_solana_token_to_account_with_sig(dest_token_account_addr, amount_base_units, memo)
    return ok


def ensure_send_token_owner_or_ata(addr_maybe_owner_or_token: str, amount_base_units: int, memo: str | None = None) -> bool:
    """Send the Solana-side token to either a Solana owner address (deriving ATA) or a direct token account address."""
    try:
        if _is_token_account_for_mint(addr_maybe_owner_or_token, config.USDC_MINT):
            return ensure_send_token_to_account(addr_maybe_owner_or_token, amount_base_units, memo)
        return ensure_send_token(addr_maybe_owner_or_token, amount_base_units, memo)
    except Exception as e:
        _log("solana_recipient_resolution_failed", level=logging.ERROR,
             destination=addr_maybe_owner_or_token, error=str(e))
        return False


def is_valid_solana_token_account(addr: str) -> bool:
    """Public helper: True if addr is a valid SPL token account for the configured mint."""
    return _is_token_account_for_mint(addr, config.USDC_MINT)


def _resolve_solana_token_destination(address: object) -> str | None:
    """Resolve one validated token-account recipient before a durable send claim."""
    if not isinstance(address, str) or not address:
        return None
    if _is_token_account_for_mint(address, config.USDC_MINT):
        return address
    if not _is_solana_wallet_with_ata(address):
        return None
    try:
        return str(get_associated_token_address(
            owner=PublicKey.from_string(address), mint=config.USDC_MINT,
        ))
    except Exception:
        return None


def _solana_sig_disposition_memo(kind: str, source_sig: str) -> str:
    """Versioned, deterministic memo binding a disposition to its incoming deposit."""
    if kind not in {"refund", "quarantine"} or not isinstance(source_sig, str) or not source_sig:
        raise ValueError("invalid Solana disposition memo identity")
    return f"swapService:v1:{kind}:{source_sig}"


def _solana_sig_disposition_has_exact_evidence(
    signature: str, kind: str, source_sig: str,
) -> bool:
    """Require exact successful finalized transaction evidence before settlement."""
    try:
        expected = state_db.get_solana_sig_disposition_evidence(
            source_sig=source_sig, kind=kind, payout_signature=signature,
        )
    except Exception:
        return False
    if expected is None:
        return False
    destination, amount_units, memo = expected
    if not isinstance(signature, str) or not signature:
        return False
    try:
        response = _rpc_call(
            _get_client().get_transaction,
            Signature.from_string(signature),
            encoding="jsonParsed",
            commitment="finalized",
            max_supported_transaction_version=0,
            timeout=getattr(
                config, "SOLANA_TX_FETCH_TIMEOUT_SEC",
                getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
            ),
        )
        tx_value = _rpc_get_result(response)
    except Exception:
        return False
    if not isinstance(tx_value, dict):
        return False
    transaction = tx_value.get("transaction")
    signatures = transaction.get("signatures") if isinstance(transaction, dict) else None
    if not isinstance(signatures, list) or not signatures or str(signatures[0]) != signature:
        return False
    meta = tx_value.get("meta")
    if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
        return False
    message = transaction.get("message") if isinstance(transaction, dict) else None
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if not isinstance(instructions, list) or not _is_attributable_vault_transaction(tx_value):
        return False
    matching_memos = 0
    transfers: list[tuple[str, int]] = []
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        program = str(instruction.get("programId") or instruction.get("program") or "")
        if program in {"spl-token", str(TOKEN_PROGRAM_ID)}:
            parsed = instruction.get("parsed")
            info = parsed.get("info") if isinstance(parsed, dict) else None
            if (not isinstance(info, dict)
                    or not isinstance(parsed, dict)
                    or parsed.get("type") not in {"transfer", "transferChecked"}
                    or str(info.get("source") or "") != str(config.VAULT_USDC_ACCOUNT)
                    or str(info.get("mint") or "") != str(config.USDC_MINT)):
                continue
            raw_amount = info.get("amount")
            if isinstance(info.get("tokenAmount"), dict):
                raw_amount = info["tokenAmount"].get("amount")
            amount_text = str(raw_amount or "")
            if not amount_text.isdigit() or (len(amount_text) > 1 and amount_text.startswith("0")):
                continue
            transfers.append((str(info.get("destination") or ""), int(amount_text)))
            continue
        if not (program.startswith("Memo111") or program == "spl-memo"):
            continue
        data = instruction.get("data", instruction.get("parsed"))
        candidates = [data] if isinstance(data, str) else []
        if isinstance(data, str):
            try:
                candidates.append(base64.b64decode(data, validate=True).decode("utf-8"))
            except Exception:
                pass
        matching_memos += sum(candidate == memo for candidate in candidates)
    return matching_memos == 1 and transfers == [(destination, amount_units)]


def get_nexus_payout_evidence(
    signature: str,
    txid: str,
    contract_id: int,
) -> nexus_memo.NexusPayoutEvidence | None:
    """Read one finalized signature and return exact attributable payout evidence.

    A status response is intentionally insufficient: finality, success, the direct
    transaction signature, exact composite memo and vault transfer are all checked in
    the transaction returned by ``getTransaction``.
    """
    expected_memo = nexus_memo.parse_nexus_payout_memo(
        f"nexus_txid:{txid}:{contract_id}"
    )
    if (not isinstance(signature, str) or not signature
            or expected_memo is None or expected_memo.is_legacy):
        return None
    try:
        from solders.signature import Signature

        signature_obj = Signature.from_string(signature)
        client = _get_client()
        response = _rpc_call(
            client.get_transaction,
            signature_obj,
            encoding="jsonParsed",
            commitment="finalized",
            max_supported_transaction_version=0,
            timeout=getattr(
                config, "SOLANA_TX_FETCH_TIMEOUT_SEC",
                getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
            ),
        )
        tx_value = _rpc_get_result(response)
    except Exception:
        return None
    if not isinstance(tx_value, dict):
        return None
    transaction = tx_value.get("transaction")
    transaction_signatures = (
        transaction.get("signatures") if isinstance(transaction, dict) else None
    )
    if (not isinstance(transaction_signatures, list) or not transaction_signatures
            or str(transaction_signatures[0]) != signature):
        return None

    message = transaction.get("message")
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if not isinstance(instructions, list):
        return None
    payout_memos: list[nexus_memo.NexusPayoutMemo | None] = []
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        program = instruction.get("programId") or instruction.get("program")
        if not program or not (
            str(program).startswith("Memo111")
            or str(program) == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
            or str(program) == "spl-memo"
        ):
            continue
        data = instruction.get("data", instruction.get("parsed"))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        candidates = [data] if isinstance(data, str) else []
        if isinstance(data, str):
            try:
                candidates.append(base64.b64decode(data, validate=True).decode("utf-8"))
            except Exception:
                pass
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.startswith("nexus_txid:"):
                payout_memos.append(nexus_memo.parse_nexus_payout_memo(candidate))
    if payout_memos != [expected_memo]:
        return None
    return _extract_nexus_payout_evidence(
        tx_value, signature=signature, memo=expected_memo
    )


def find_nexus_payout_evidence(
    memo: str,
    search_limit: int = 50,
) -> nexus_memo.NexusPayoutEvidence | None:
    """Recover one unambiguous finalized payout with full transaction evidence."""
    expected = nexus_memo.parse_nexus_payout_memo(memo)
    if (expected is None or expected.is_legacy
            or type(search_limit) is not int or search_limit <= 0):
        return None
    try:
        client = _get_client()
    except Exception:
        return None

    addresses = [str(config.VAULT_USDC_ACCOUNT)]
    vault_owner = getattr(config, "VAULT_OWNER", None)
    if vault_owner:
        addresses.append(str(vault_owner))
    seen: set[str] = set()
    matches: list[nexus_memo.NexusPayoutEvidence] = []
    for address in dict.fromkeys(addresses):
        try:
            response = _rpc_call(
                client.get_signatures_for_address,
                PublicKey.from_string(address),
                limit=search_limit,
                commitment="finalized",
            )
            entries = _rpc_get_value(response)
        except Exception:
            return None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            signature = entry.get("signature")
            if (not isinstance(signature, str) or not signature or signature in seen
                    or entry.get("confirmationStatus") != "finalized"
                    or entry.get("err") is not None):
                continue
            seen.add(signature)
            evidence = get_nexus_payout_evidence(
                signature, expected.txid, expected.contract_id
            )
            if evidence is not None:
                matches.append(evidence)
                if len(matches) > 1:
                    return None
    return matches[0] if len(matches) == 1 else None


def find_signature_with_memo(memo: str, search_limit: int = 50) -> Optional[str]:
    """Return a finalized signature for generic refund/quarantine memo paths.

    Nexus payout callers should consume :func:`find_nexus_payout_evidence`; this
    compatibility wrapper retains the signature-only API for existing non-Nexus uses.
    """
    if not isinstance(memo, str) or not memo:
        return None
    payout_memo = None
    if memo.startswith("nexus_txid:"):
        payout_memo = nexus_memo.parse_nexus_payout_memo(memo)
        if payout_memo is None or payout_memo.is_legacy:
            return None
        evidence = find_nexus_payout_evidence(memo, search_limit)
        return evidence.solana_signature if evidence is not None else None
    try:
        client = _get_client()
    except Exception:
        return None
    addresses: list[str] = []
    try:
        if config.VAULT_USDC_ACCOUNT:
            addresses.append(str(config.VAULT_USDC_ACCOUNT))
    except Exception:
        pass
    # Optionally include vault wallet (owner) if present
    try:
        if getattr(config, "VAULT_OWNER", None):
            addresses.append(str(config.VAULT_OWNER))
    except Exception:
        pass
    seen = set()
    for addr in addresses:
        try:
            resp = _rpc_call(
                client.get_signatures_for_address,
                PublicKey.from_string(addr),
                limit=search_limit,
                commitment="finalized",
            )
            js = _rpc_get_value(resp)
            if isinstance(js, list):
                sig_list = js
            else:
                sig_list = []
        except Exception:
            continue
        for entry in sig_list:
            if not isinstance(entry, dict):
                continue
            sig = entry.get("signature")
            if (not isinstance(sig, str) or not sig or sig in seen
                    or entry.get("err") is not None
                    or entry.get("confirmationStatus") != "finalized"):
                continue
            seen.add(sig)
            try:
                signature_obj = Signature.from_string(sig)
                tx_resp = _rpc_call(
                    client.get_transaction,
                    signature_obj,
                    encoding="jsonParsed",
                    timeout=getattr(
                        config, "SOLANA_TX_FETCH_TIMEOUT_SEC",
                        getattr(config, "SOLANA_RPC_TIMEOUT_SEC", 8),
                    ),
                )
                tx_val = _rpc_get_result(tx_resp)
            except Exception:
                continue
            if not isinstance(tx_val, dict):
                continue
            meta = tx_val.get("meta")
            tx_obj = tx_val.get("transaction")
            msg = tx_obj.get("message") if isinstance(tx_obj, dict) else None
            insts = msg.get("instructions") if isinstance(msg, dict) else None
            if (not isinstance(meta, dict) or meta.get("err") is not None
                    or not isinstance(insts, list)
                    or not _is_attributable_vault_transaction(tx_val)):
                continue
            for ix in insts:
                if not isinstance(ix, dict):
                    continue
                prog = ix.get("programId") or ix.get("program")
                if not prog or not (
                    str(prog).startswith("Memo111")
                    or str(prog) == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
                    or str(prog) == "spl-memo"
                ):
                    continue
                data = ix.get("data", ix.get("parsed"))
                if isinstance(data, list) and len(data) == 1:
                    data = data[0]
                candidates = [data] if isinstance(data, str) else []
                if isinstance(data, str):
                    try:
                        candidates.append(base64.b64decode(data, validate=True).decode("utf-8"))
                    except Exception:
                        pass
                if memo not in candidates:
                    continue
                if payout_memo is not None and _extract_nexus_payout_evidence(
                    tx_val, signature=sig, memo=payout_memo
                ) is None:
                    continue
                return sig
    return None


def _is_attributable_vault_transaction(tx_value: object) -> bool:
    """Require the configured vault authority to be an actual transaction signer."""
    if not isinstance(tx_value, dict):
        return False
    transaction = tx_value.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    account_keys = message.get("accountKeys") if isinstance(message, dict) else None
    if not isinstance(account_keys, list):
        return False
    authority = str(config.SOL_MAIN_ACCOUNT)
    for account in account_keys:
        if isinstance(account, dict):
            if str(account.get("pubkey") or "") == authority and account.get("signer") is True:
                return True
    return False


def _extract_nexus_payout_evidence(
    tx_value: object,
    *,
    signature: str,
    memo: nexus_memo.NexusPayoutMemo,
) -> nexus_memo.NexusPayoutEvidence | None:
    """Bind an exact composite memo to one exact successful vault token transfer."""
    if memo.contract_id is None or not _is_attributable_vault_transaction(tx_value):
        return None
    if not isinstance(tx_value, dict) or not isinstance(tx_value.get("meta"), dict):
        return None
    meta = tx_value["meta"]
    if "err" not in meta or meta["err"] is not None:
        return None
    transaction = tx_value.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if not isinstance(instructions, list):
        return None

    transfers: list[tuple[str, int]] = []
    for instruction in instructions:
        if not isinstance(instruction, dict) or str(instruction.get("program") or "") != "spl-token":
            continue
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in {"transfer", "transferChecked"}:
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        if str(info.get("source") or "") != str(config.VAULT_USDC_ACCOUNT):
            continue
        mint = info.get("mint")
        if str(mint or "") != str(config.USDC_MINT):
            continue
        destination = info.get("destination")
        raw_amount = info.get("amount")
        token_amount = info.get("tokenAmount")
        if isinstance(token_amount, dict):
            raw_amount = token_amount.get("amount")
        if not isinstance(destination, str) or not destination.strip():
            continue
        if isinstance(raw_amount, bool):
            continue
        amount_text = str(raw_amount or "")
        if not amount_text.isdigit() or (len(amount_text) > 1 and amount_text.startswith("0")):
            continue
        amount_units = int(amount_text)
        if amount_units <= 0:
            continue
        transfers.append((destination, amount_units))

    if len(transfers) != 1:
        return None
    destination, amount_units = transfers[0]
    return nexus_memo.NexusPayoutEvidence(
        txid=memo.txid,
        contract_id=memo.contract_id,
        solana_signature=signature,
        to_token_account=destination,
        amount_solana_units=amount_units,
    )


def _parse_solana_disposition_memo(memo: object) -> tuple[str, str] | None:
    """Parse exactly one current, versioned refund/quarantine source identity."""
    if not isinstance(memo, str):
        return None
    parts = memo.split(":")
    if len(parts) != 4 or parts[:2] != ["swapService", "v1"]:
        return None
    kind, source_signature = parts[2:]
    if kind not in {"refund", "quarantine"}:
        return None
    try:
        Signature.from_string(source_signature)
    except Exception:
        return None
    return kind, source_signature


def _extract_solana_disposition_evidence(
    tx_value: object,
    *,
    signature: str,
    timestamp: int,
    kind: str,
    source_signature: str,
) -> dict[str, object] | None:
    """Bind a current disposition memo to one exact successful vault transfer."""
    expected_memo = _solana_sig_disposition_memo(kind, source_signature)
    if (type(timestamp) is not int or timestamp <= 0
            or not _is_attributable_vault_transaction(tx_value)
            or not isinstance(tx_value, dict)):
        return None
    transaction = tx_value.get("transaction")
    signatures = transaction.get("signatures") if isinstance(transaction, dict) else None
    meta = tx_value.get("meta")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if (not isinstance(signatures, list) or signatures != [signature]
            or not isinstance(meta, dict) or meta.get("err") is not None
            or not isinstance(instructions, list)):
        return None

    transfers: list[tuple[str, int]] = []
    matching_memos = 0
    for instruction in instructions:
        if not isinstance(instruction, dict):
            return None
        program = str(instruction.get("programId") or instruction.get("program") or "")
        if program in {"spl-token", str(TOKEN_PROGRAM_ID)}:
            parsed = instruction.get("parsed")
            info = parsed.get("info") if isinstance(parsed, dict) else None
            if (not isinstance(parsed, dict) or not isinstance(info, dict)
                    or parsed.get("type") not in {"transfer", "transferChecked"}
                    or str(info.get("source") or "") != str(config.VAULT_USDC_ACCOUNT)
                    or str(info.get("mint") or "") != str(config.USDC_MINT)):
                continue
            destination = info.get("destination")
            raw_amount = info.get("amount")
            if isinstance(info.get("tokenAmount"), dict):
                raw_amount = info["tokenAmount"].get("amount")
            amount_text = str(raw_amount or "")
            if (not isinstance(destination, str) or not destination.strip()
                    or not amount_text.isdigit()
                    or (len(amount_text) > 1 and amount_text.startswith("0"))):
                return None
            amount_units = int(amount_text)
            if amount_units <= 0:
                return None
            transfers.append((destination, amount_units))
        elif (program.startswith("Memo111")
              or program == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
              or program == "spl-memo"):
            data = instruction.get("data", instruction.get("parsed"))
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            candidates = [data] if isinstance(data, str) else []
            if isinstance(data, str):
                try:
                    candidates.append(base64.b64decode(data, validate=True).decode("utf-8"))
                except Exception:
                    pass
            matching_memos += sum(candidate == expected_memo for candidate in candidates)

    if matching_memos != 1 or len(transfers) != 1:
        return None
    destination, amount_units = transfers[0]
    return {
        "kind": kind,
        "source_signature": source_signature,
        "solana_signature": signature,
        "destination_token_account": destination,
        "amount_solana_units": amount_units,
        "timestamp": timestamp,
    }


def _complete_solana_disposition_source_evidence(
    client: Any,
    evidence: dict[str, object],
) -> dict[str, object] | None:
    """Attach the exact finalized incoming deposit named by a disposition memo."""
    source_signature = evidence.get("source_signature")
    payout_timestamp = evidence.get("timestamp")
    if not isinstance(source_signature, str) or type(payout_timestamp) is not int:
        return None
    source_response = _rpc_call(
        client.get_transaction,
        Signature.from_string(source_signature),
        encoding="jsonParsed",
        commitment="finalized",
        max_supported_transaction_version=0,
        timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 6),
    )
    source_tx = _rpc_get_result(source_response)
    if not isinstance(source_tx, dict):
        return None
    source_timestamp = source_tx.get("blockTime")
    if (type(source_timestamp) is not int or source_timestamp <= 0
            or source_timestamp > payout_timestamp):
        return None
    try:
        source_memo, source_token_account, source_amount = _extract_core_deposit_evidence(
            source_tx,
            signature=source_signature,
            vault_account=str(config.VAULT_USDC_ACCOUNT),
            mint=str(config.USDC_MINT),
        )
    except Exception as exc:
        _log(
            "solana_disposition_source_evidence_rejected",
            level=logging.WARNING,
            source_signature=source_signature,
            reason=structured_logging.redact(f"{type(exc).__name__}: {exc}"),
        )
        return None
    payout_amount = evidence.get("amount_solana_units")
    destination = evidence.get("destination_token_account")
    kind = evidence.get("kind")
    if (not isinstance(source_token_account, str) or not source_token_account
            or type(source_amount) is not int or source_amount <= 0
            or type(payout_amount) is not int or payout_amount > source_amount):
        return None
    if kind == "refund" and destination != source_token_account:
        return None
    if kind == "quarantine":
        quarantine_account = str(getattr(config, "USDC_QUARANTINE_ACCOUNT", "") or "")
        if not quarantine_account or destination != quarantine_account:
            return None
    return {
        **evidence,
        "source_timestamp": source_timestamp,
        "source_token_account": source_token_account,
        "source_amount_solana_units": source_amount,
        "source_memo": source_memo,
    }


def scan_recent_memos(search_limit: int = 400) -> dict:
    """Scan recent signatures for the vault account collecting structured memos.
    Returns dict: {
        'nexus_txids': { txid: signature },
        'refund_sigs': { deposit_sig: refund_tx_sig },
    }
    Best effort; ignores errors.
    """
    out = {
        "nexus_payouts": {},
        "legacy_nexus_txids": {},
        "malformed_nexus_memos": [],
        "refund_sigs": {},
        "complete": False,
        "reason": "bounded_recent_scan",
    }
    try:
        client = _get_client()
    except Exception:
        return out
    try:
        resp = _rpc_call(client.get_signatures_for_address, PublicKey.from_string(str(config.VAULT_USDC_ACCOUNT)), limit=search_limit)
    except Exception:
        return out
    entries = _rpc_get_value(resp)
    if not isinstance(entries, list):
        return out
    for ent in entries:
        try:
            sig = ent.get("signature") if isinstance(ent, dict) else None
            if (not sig or ent.get("err") is not None
                    or ent.get("confirmationStatus") != "finalized"):
                continue
            # Fetch transaction (short timeout)
            try:
                signature_obj = Signature.from_string(sig)
                tx_resp = _rpc_call(client.get_transaction, signature_obj, encoding="jsonParsed", timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 6))
            except Exception:
                continue
            tx_val = _rpc_get_result(tx_resp)
            # Inspect instructions for memo
            try:
                tx_obj = tx_val.get("transaction") if isinstance(tx_val, dict) else None
                msg = (tx_obj or {}).get("message") if isinstance(tx_obj, dict) else None
                insts = (msg or {}).get("instructions") if isinstance(msg, dict) else None
            except Exception:
                insts = None
            memos: list[str] = []
            if isinstance(insts, list):
                for ix in insts:
                    try:
                        prog = ix.get("programId") or ix.get("program")
                        if prog and str(prog).startswith("Memo111"):
                            data = ix.get("data")
                            if isinstance(data, list) and data:
                                data = data[0]
                            if isinstance(data, str):
                                memos.append(data)
                    except Exception:
                        continue
            # Fallback logs
            if not memos:
                try:
                    logs = (tx_val.get("meta") or {}).get("logMessages") if isinstance(tx_val, dict) else None
                    if isinstance(logs, list):
                        for lg in logs:
                            if isinstance(lg, str) and ("nexus_txid:" in lg or "refundSig:" in lg):
                                memos.append(lg)
                except Exception:
                    pass
            for m in memos:
                if isinstance(m, str) and m.startswith("nexus_txid:"):
                    parsed = nexus_memo.parse_nexus_payout_memo(m)
                    if parsed is None:
                        out["malformed_nexus_memos"].append(m)
                    elif parsed.is_legacy:
                        out["legacy_nexus_txids"].setdefault(parsed.txid, sig)
                    else:
                        evidence = _extract_nexus_payout_evidence(
                            tx_val, signature=sig, memo=parsed
                        )
                        if evidence is None:
                            out["malformed_nexus_memos"].append(m)
                        else:
                            out["nexus_payouts"].setdefault(
                                (parsed.txid, parsed.contract_id), evidence
                            )
                if "refundSig:" in m:
                    try:
                        dsig = m.split("refundSig:", 1)[1].strip().split()[0]
                        if dsig and dsig not in out["refund_sigs"]:
                            out["refund_sigs"][dsig] = sig
                    except Exception:
                        pass
        except Exception:
            continue
    return out


def scan_memos_since_timestamp(since_timestamp: int, max_signatures: int = 10000) -> dict:
    """Enumerate finalized vault transactions without turning gaps into absence proof."""
    out = {
        "nexus_payouts": {},
        "nexus_payout_timestamps": {},
        "legacy_nexus_txids": {},
        "malformed_nexus_memos": [],
        "refund_sigs": {},
        "quarantined_sigs": {},
        "solana_dispositions": {},
        "deposits": [],
        "complete": False,
        "reason": None,
    }

    def incomplete(reason: str) -> dict:
        out["nexus_payouts"].clear()
        out["nexus_payout_timestamps"].clear()
        out["legacy_nexus_txids"].clear()
        out["refund_sigs"].clear()
        out["quarantined_sigs"].clear()
        out["solana_dispositions"].clear()
        out["complete"] = False
        out["reason"] = reason
        return out

    if (isinstance(since_timestamp, bool) or not isinstance(since_timestamp, int)
            or since_timestamp <= 0):
        return incomplete("invalid_waterline")
    if (isinstance(max_signatures, bool) or not isinstance(max_signatures, int)
            or max_signatures <= 0):
        return incomplete("invalid_signature_budget")

    try:
        client = _get_client()
    except Exception:
        return incomplete("client_unavailable")

    fetched_count = 0
    before_sig = None
    batch_size = 1000
    previous_block_time: int | None = None

    while fetched_count < max_signatures:
        request_limit = min(batch_size, max_signatures - fetched_count)
        params: dict[str, Any] = {
            "limit": request_limit,
            "commitment": "finalized",
        }
        if before_sig:
            params["before"] = before_sig
        try:
            resp = _rpc_call(
                client.get_signatures_for_address,
                PublicKey.from_string(str(config.VAULT_USDC_ACCOUNT)),
                **params,
            )
        except Exception as exc:
            _log("solana_memo_scan_failed", level=logging.ERROR, error=str(exc))
            return incomplete("signature_page_fetch_failed")

        entries = _rpc_get_value(resp)
        if not isinstance(entries, list):
            return incomplete("invalid_signature_page")
        if not entries:
            out["complete"] = True
            return out

        # Validate the entire page and its boundary with the prior page before an old
        # timestamp is allowed to terminate enumeration.
        for ordered_entry in entries:
            if not isinstance(ordered_entry, dict):
                return incomplete("invalid_signature_entry")
            ordered_time = ordered_entry.get("blockTime")
            if (isinstance(ordered_time, bool) or not isinstance(ordered_time, int)
                    or ordered_time <= 0):
                return incomplete("invalid_block_time")
            if previous_block_time is not None and ordered_time > previous_block_time:
                return incomplete("nonmonotonic_signature_page")
            previous_block_time = ordered_time

        reached_waterline = False
        for ent in entries:
            if not isinstance(ent, dict):
                return incomplete("invalid_signature_entry")
            sig = ent.get("signature")
            block_time = ent.get("blockTime")
            if not isinstance(sig, str) or not sig.strip():
                return incomplete("invalid_signature")
            if (isinstance(block_time, bool) or not isinstance(block_time, int)
                    or block_time <= 0):
                return incomplete("invalid_block_time")
            if block_time < since_timestamp:
                reached_waterline = True
                break
            if ent.get("confirmationStatus") != "finalized":
                return incomplete("signature_not_finalized")
            if ent.get("err") is not None:
                continue

            try:
                signature_obj = Signature.from_string(sig)
            except Exception:
                return incomplete("invalid_signature")
            try:
                tx_resp = _rpc_call(
                    client.get_transaction,
                    signature_obj,
                    encoding="jsonParsed",
                    commitment="finalized",
                    max_supported_transaction_version=0,
                    timeout=getattr(config, "SOLANA_TX_FETCH_TIMEOUT_SEC", 6),
                )
            except Exception:
                return incomplete("transaction_fetch_failed")

            tx_val = _rpc_get_result(tx_resp)
            if not isinstance(tx_val, dict):
                return incomplete("invalid_transaction")
            meta = tx_val.get("meta")
            tx_obj = tx_val.get("transaction")
            msg = tx_obj.get("message") if isinstance(tx_obj, dict) else None
            insts = msg.get("instructions") if isinstance(msg, dict) else None
            if not isinstance(meta, dict) or "err" not in meta or not isinstance(insts, list):
                return incomplete("invalid_transaction")
            if meta.get("err") is not None:
                continue

            memos: list[str] = []
            vault_outflows = 0
            inspected_instructions = [(ix, False) for ix in insts]
            inner_groups = meta.get("innerInstructions")
            if inner_groups is None:
                inner_groups = []
            if not isinstance(inner_groups, list):
                return incomplete("invalid_inner_instructions")
            for group in inner_groups:
                if not isinstance(group, dict) or not isinstance(group.get("instructions"), list):
                    return incomplete("invalid_inner_instructions")
                inspected_instructions.extend((ix, True) for ix in group["instructions"])
            for ix, is_inner in inspected_instructions:
                if not isinstance(ix, dict):
                    return incomplete("invalid_instruction")
                parsed = ix.get("parsed")
                info = parsed.get("info") if isinstance(parsed, dict) else None
                prog = ix.get("programId") or ix.get("program")
                is_token = str(prog) in {"spl-token", str(TOKEN_PROGRAM_ID)}
                if is_token and isinstance(parsed, dict):
                    if not isinstance(info, dict) or not isinstance(parsed.get("type"), str):
                        return incomplete("invalid_solana_token_instruction")
                    if parsed["type"] in {"transfer", "transferChecked"} and (
                        not isinstance(info.get("source"), str) or not info["source"].strip()
                    ):
                        return incomplete("invalid_solana_token_instruction")
                # Opaque token instructions cannot prove absence of a vault debit.
                # Unknown programs explicitly touching the vault are equally held.
                accounts = ix.get("accounts", [])
                if not isinstance(parsed, dict) and (
                    is_token
                    or (isinstance(accounts, list) and str(config.VAULT_USDC_ACCOUNT) in accounts)
                ):
                    return incomplete("unsupported_solana_vault_instruction")
                if (isinstance(info, dict)
                        and str(info.get("source") or "") == str(config.VAULT_USDC_ACCOUNT)):
                    if is_inner:
                        return incomplete("unsupported_inner_solana_vault_spend")
                    vault_outflows += 1
                if is_inner:
                    continue
                if not prog or not (
                    str(prog).startswith("Memo111")
                    or str(prog) == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
                    or str(prog) == "spl-memo"
                ):
                    continue
                data = ix.get("data", ix.get("parsed"))
                if isinstance(data, list) and len(data) == 1:
                    data = data[0]
                if not isinstance(data, str):
                    return incomplete("invalid_memo_instruction")
                memos.append(data)

            # Enumeration is not complete accounting when a vault debit has no
            # understood business identity. In particular, encoded/new-version
            # disposition memos must not bypass the startup reconstruction hold.
            if vault_outflows > 1 or (vault_outflows and len(memos) != 1):
                return incomplete("unclassified_solana_vault_spend")
            if vault_outflows and not any(memo.startswith((
                "swapService:v1:", "nexus_txid:", "refundSig:", "quarantinedSig:",
            )) for memo in memos):
                return incomplete("unclassified_solana_vault_spend")

            for memo in memos:
                if memo.startswith("swapService:v1:"):
                    disposition = _parse_solana_disposition_memo(memo)
                    if disposition is None:
                        return incomplete("malformed_solana_disposition_memo")
                    kind, source_signature = disposition
                    evidence = _extract_solana_disposition_evidence(
                        tx_val,
                        signature=sig,
                        timestamp=block_time,
                        kind=kind,
                        source_signature=source_signature,
                    )
                    if evidence is None:
                        return incomplete("missing_solana_disposition_transfer_evidence")
                    try:
                        evidence = _complete_solana_disposition_source_evidence(client, evidence)
                    except Exception:
                        return incomplete("solana_disposition_source_fetch_failed")
                    if evidence is None:
                        return incomplete("missing_solana_disposition_source_evidence")
                    identity = (kind, source_signature)
                    if any(
                        known_source == source_signature and known_kind != kind
                        for known_kind, known_source in out["solana_dispositions"]
                    ):
                        return incomplete("conflicting_solana_disposition_identity")
                    existing = out["solana_dispositions"].get(identity)
                    if existing is not None and existing != evidence:
                        return incomplete("duplicate_solana_disposition_identity")
                    out["solana_dispositions"][identity] = evidence
                elif memo.startswith("nexus_txid:"):
                    parsed = nexus_memo.parse_nexus_payout_memo(memo)
                    if parsed is None:
                        out["malformed_nexus_memos"].append(memo)
                        return incomplete("malformed_nexus_payout_memo")
                    if parsed.is_legacy:
                        existing = out["legacy_nexus_txids"].get(parsed.txid)
                        if existing is not None and existing != sig:
                            return incomplete("duplicate_nexus_payout_identity")
                        out["legacy_nexus_txids"][parsed.txid] = sig
                    else:
                        evidence = _extract_nexus_payout_evidence(
                            tx_val, signature=sig, memo=parsed
                        )
                        if evidence is None:
                            return incomplete("missing_nexus_payout_transfer_evidence")
                        identity = (parsed.txid, parsed.contract_id)
                        existing = out["nexus_payouts"].get(identity)
                        existing_timestamp = out["nexus_payout_timestamps"].get(identity)
                        if (existing is not None
                                and existing.solana_signature != evidence.solana_signature):
                            return incomplete("duplicate_nexus_payout_identity")
                        if existing_timestamp is not None and existing_timestamp != block_time:
                            return incomplete("conflicting_nexus_payout_timestamp")
                        out["nexus_payouts"][identity] = evidence
                        out["nexus_payout_timestamps"][identity] = block_time
                elif memo.startswith("refundSig:"):
                    deposit_sig = memo[len("refundSig:"):]
                    if not deposit_sig or deposit_sig.strip() != deposit_sig or any(
                        ch.isspace() for ch in deposit_sig
                    ):
                        return incomplete("malformed_refund_memo")
                    out["refund_sigs"].setdefault(deposit_sig, sig)
                elif memo.startswith("quarantinedSig:"):
                    deposit_sig = memo[len("quarantinedSig:"):]
                    if not deposit_sig or deposit_sig.strip() != deposit_sig or any(
                        ch.isspace() for ch in deposit_sig
                    ):
                        return incomplete("malformed_quarantine_memo")
                    out["quarantined_sigs"][deposit_sig] = sig

        fetched_count += len(entries)
        if reached_waterline or len(entries) < request_limit:
            out["complete"] = True
            return out
        cursor = entries[-1].get("signature")
        if not isinstance(cursor, str) or not cursor:
            return incomplete("invalid_pagination_cursor")
        try:
            before_sig = Signature.from_string(cursor)
        except Exception:
            return incomplete("invalid_pagination_cursor")

    return incomplete("pagination_truncated")


## Memo extraction removed.


def has_token_ata(owner_addr: str) -> bool:
    """Return True if the owner's associated token account exists."""
    try:
        client = _get_client()
        owner = PublicKey.from_string(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        info = _rpc_get_value(_rpc_call(client.get_account_info, ata))
        return info is not None
    except Exception:
        return False


def derive_token_ata(owner_addr: str) -> str | None:
    """Derive the expected associated token account address for a given owner (string form). Returns None on failure."""
    try:
        owner = PublicKey.from_string(owner_addr)
        ata = get_associated_token_address(owner=owner, mint=config.USDC_MINT)
        return str(ata)
    except Exception:
        return None


def refund_solana_token_to_source(source_token_account: str, amount_base_units: int, reason: str, deposit_sig: str | None = None) -> bool:
    """Refund funds back to the sender's token account.
    Adds memo refundSig:<deposit_sig> if deposit_sig provided for idempotent replay detection.
    """
    # Check if this refund was already processed by checking the reason for signature context
    if ":" in reason and len(reason.split(":")) >= 2:
        potential_sig = reason.split(":")[-1]
        if len(potential_sig) > 40 and state_db.is_refunded_sig(potential_sig):
            return True  # Already refunded this signature
    
    try:
        kp = load_vault_keypair()
        dest_token_acc = PublicKey.from_string(source_token_account)
        ixs = [
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=dest_token_acc,
                owner=kp.pubkey(),
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            ),
        ]
        memo_ix = None
        if deposit_sig:
            memo_ix = _memo_ix(f"refundSig:{deposit_sig}")
        if memo_ix:
            ixs.append(memo_ix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        _log("solana_refund_submitted", signature=sig, amount_units=int(amount_base_units),
             deposit_sig=deposit_sig)
        return True
    except Exception as e:
        _log("solana_refund_submission_failed", level=logging.ERROR,
             token=config.SOLANA_TOKEN_SYMBOL, error=str(e))
        return False


def move_solana_token_to_quarantine(amount_base_units: int, note: str | None = None, deposit_sig: str | None = None) -> bool:
    """Move the deposit to quarantine with structured memo for later idempotency.
    Memo precedence:
      quarantinedSig:<deposit_sig>
      quarantined:<note>
      quarantined
    """
    try:
        dest = getattr(config, "USDC_QUARANTINE_ACCOUNT", None)
        if not dest:
            _log("solana_quarantine_not_configured", level=logging.ERROR)
            return False
        kp = load_vault_keypair()
        ixs = [
            transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=config.VAULT_USDC_ACCOUNT,
                mint=config.USDC_MINT,
                dest=PublicKey.from_string(dest),
                owner=kp.pubkey(),
                amount=amount_base_units,
                decimals=config.USDC_DECIMALS,
                signers=[],
            )
        ]
        memo_txt = None
        if deposit_sig:
            memo_txt = f"quarantinedSig:{deposit_sig}"
        elif note:
            memo_txt = f"quarantined:{note}"
        else:
            memo_txt = "quarantined"
        mix = _memo_ix(memo_txt)
        if mix:
            ixs.append(mix)
        sig = _build_and_send_legacy_tx(ixs, kp)
        _log("solana_quarantine_submitted", signature=sig, amount_units=int(amount_base_units),
             memo=memo_txt)
        return True
    except Exception as e:
        _log("solana_quarantine_submission_failed", level=logging.ERROR, error=str(e))
    return False
