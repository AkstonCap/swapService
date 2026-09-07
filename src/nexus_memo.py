"""Strict identity parsing for Nexus-to-Solana payout memos."""
from __future__ import annotations

from dataclasses import dataclass
import re


_NEXUS_TXID_RE = re.compile(r"[0-9a-f]{128}")
_CONTRACT_ID_RE = re.compile(r"(?:0|[1-9][0-9]*)")
_PAYOUT_PREFIX = "nexus_txid:"


@dataclass(frozen=True)
class NexusPayoutEvidence:
    """Attributable successful Solana transfer for one Nexus source CREDIT."""

    txid: str
    contract_id: int
    solana_signature: str
    to_token_account: str
    amount_solana_units: int


@dataclass(frozen=True)
class NexusPayoutMemo:
    """The immutable Nexus CREDIT identity carried by a Solana payout."""

    txid: str
    contract_id: int | None

    @property
    def is_legacy(self) -> bool:
        return self.contract_id is None


def is_canonical_nexus_txid(value: object) -> bool:
    """Return whether *value* is a canonical lowercase Nexus uint512 txid."""
    return isinstance(value, str) and _NEXUS_TXID_RE.fullmatch(value) is not None


def parse_nexus_payout_memo(value: object) -> NexusPayoutMemo | None:
    """Parse an exact payout memo without repairing or guessing source identity.

    Legacy ``nexus_txid:<txid>`` memos are represented with ``contract_id=None`` so
    recovery can hold them for explicit resolution instead of inventing a sibling id.
    """
    if not isinstance(value, str) or not value.startswith(_PAYOUT_PREFIX):
        return None
    payload = value[len(_PAYOUT_PREFIX):]
    parts = payload.split(":")
    if len(parts) == 1:
        return NexusPayoutMemo(parts[0], None) if is_canonical_nexus_txid(parts[0]) else None
    if len(parts) != 2:
        return None
    txid, raw_contract_id = parts
    if not is_canonical_nexus_txid(txid) or _CONTRACT_ID_RE.fullmatch(raw_contract_id) is None:
        return None
    contract_id = int(raw_contract_id)
    if contract_id > 0xFFFFFFFF:
        return None
    return NexusPayoutMemo(txid, contract_id)
