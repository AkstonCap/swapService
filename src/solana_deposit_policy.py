"""Exact Solana-input admission policy shared by execution and public terms.

The parsed MICRO_DEPOSIT_FEE_PCT setting is intentionally absent: no supported
percentage disposition exists. Positive deposits that cannot be paid under these
terms remain full-principal liabilities in an explicit non-sendable hold.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

POLICY_VERSION = 1
PAYABLE = "payable"
HOLD_BELOW_MINIMUM = "hold_below_minimum"
HOLD_NONPOSITIVE_OUTPUT = "hold_nonpositive_output"
REFUND_OVERSIZED = "refund_oversized"
_DECISIONS = {
    PAYABLE,
    HOLD_BELOW_MINIMUM,
    HOLD_NONPOSITIVE_OUTPUT,
    REFUND_OVERSIZED,
}


def _exact_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class SolanaDepositTerms:
    minimum_input_units: int
    maximum_input_units: int
    input_decimals: int
    output_decimals: int
    flat_output_fee_units: int
    fee_basis_points: int

    def __post_init__(self) -> None:
        _exact_int(self.minimum_input_units, "minimum_input_units", minimum=1)
        _exact_int(self.maximum_input_units, "maximum_input_units")
        _exact_int(self.input_decimals, "input_decimals")
        _exact_int(self.output_decimals, "output_decimals")
        _exact_int(self.flat_output_fee_units, "flat_output_fee_units")
        _exact_int(self.fee_basis_points, "fee_basis_points")

    def as_dict(self) -> dict[str, int]:
        return {
            "minimum_input_units": self.minimum_input_units,
            "maximum_input_units": self.maximum_input_units,
            "input_decimals": self.input_decimals,
            "output_decimals": self.output_decimals,
            "flat_output_fee_units": self.flat_output_fee_units,
            "fee_basis_points": self.fee_basis_points,
        }


@dataclass(frozen=True)
class SolanaDepositDecision:
    decision: str
    output_units: int
    terms: SolanaDepositTerms

    @property
    def is_policy_hold(self) -> bool:
        return self.decision in {HOLD_BELOW_MINIMUM, HOLD_NONPOSITIVE_OUTPUT}


def terms_from_config(config: Any) -> SolanaDepositTerms:
    """Read only canonical executable fields; parsed micro percentages are unsupported."""
    pair = config.SWAP_PAIR
    return SolanaDepositTerms(
        minimum_input_units=config.MIN_DEPOSIT_SOLANA_UNITS,
        maximum_input_units=config.MAX_SWAP_SOLANA_UNITS,
        input_decimals=pair.solana.decimals,
        output_decimals=pair.nexus.decimals,
        flat_output_fee_units=pair.fees.flat_to_nexus_units,
        fee_basis_points=pair.fees.basis_points,
    )


def _gross_output_units(input_units: int, terms: SolanaDepositTerms) -> int:
    if terms.input_decimals == terms.output_decimals:
        return input_units
    if terms.input_decimals < terms.output_decimals:
        return input_units * 10 ** (terms.output_decimals - terms.input_decimals)
    return input_units // 10 ** (terms.input_decimals - terms.output_decimals)


def output_units(input_units: int, terms: SolanaDepositTerms) -> int:
    """Preserve the established floor-rescale, then flat-plus-bps fee calculation."""
    amount = _exact_int(input_units, "input_units", minimum=1)
    gross = _gross_output_units(amount, terms)
    dynamic_fee = gross * terms.fee_basis_points // 10_000
    return max(0, gross - terms.flat_output_fee_units - dynamic_fee)


def classify(input_units: int, terms: SolanaDepositTerms) -> SolanaDepositDecision:
    """Classify one positive exact input without floats or mutable external state."""
    amount = _exact_int(input_units, "input_units", minimum=1)
    calculated_output = output_units(amount, terms)
    if amount < terms.minimum_input_units:
        decision = HOLD_BELOW_MINIMUM
    elif terms.maximum_input_units and amount > terms.maximum_input_units:
        decision = REFUND_OVERSIZED
    elif calculated_output <= 0:
        decision = HOLD_NONPOSITIVE_OUTPUT
    else:
        decision = PAYABLE
    return SolanaDepositDecision(decision, calculated_output, terms)


def freeze_evidence(
    *, signature: str, timestamp: int, memo: str, from_address: str | None,
    input_units: int, decision: SolanaDepositDecision,
) -> str:
    if not isinstance(signature, str) or not signature:
        raise ValueError("signature is required")
    _exact_int(timestamp, "timestamp", minimum=1)
    if not isinstance(memo, str) or not isinstance(from_address, (str, type(None))):
        raise ValueError("source memo/account evidence has invalid types")
    if decision.decision not in _DECISIONS:
        raise ValueError("unsupported Solana deposit decision")
    evidence = {
        "policy_version": POLICY_VERSION,
        "signature": signature,
        "timestamp": timestamp,
        "memo": memo,
        "from_address": from_address,
        "input_units": input_units,
        "decision": decision.decision,
        "output_units": decision.output_units,
        "terms": decision.terms.as_dict(),
    }
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"))


def parse_frozen_evidence(evidence: str) -> dict[str, Any]:
    """Strictly validate frozen source, terms and the recomputed policy decision."""
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("duplicate policy evidence field")
            parsed[key] = value
        return parsed

    if not isinstance(evidence, str):
        raise ValueError("policy evidence must be text")
    try:
        parsed = json.loads(
            evidence,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite policy value: {value}")
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid policy evidence JSON") from exc
    required = {
        "policy_version", "signature", "timestamp", "memo", "from_address",
        "input_units", "decision", "output_units", "terms",
    }
    if not isinstance(parsed, dict) or set(parsed) != required:
        raise ValueError("policy evidence fields are incomplete")
    if parsed["policy_version"] != POLICY_VERSION or type(parsed["policy_version"]) is not int:
        raise ValueError("unsupported policy evidence version")
    terms_data = parsed["terms"]
    if not isinstance(terms_data, dict) or set(terms_data) != {
        "minimum_input_units", "maximum_input_units", "input_decimals",
        "output_decimals", "flat_output_fee_units", "fee_basis_points",
    }:
        raise ValueError("policy terms fields are incomplete")
    terms = SolanaDepositTerms(**terms_data)
    if (not isinstance(parsed["signature"], str) or not parsed["signature"]
            or type(parsed["timestamp"]) is not int or parsed["timestamp"] <= 0
            or not isinstance(parsed["memo"], str)
            or not isinstance(parsed["from_address"], (str, type(None)))
            or type(parsed["input_units"]) is not int or parsed["input_units"] <= 0
            or type(parsed["output_units"]) is not int or parsed["output_units"] < 0
            or not isinstance(parsed["decision"], str)):
        raise ValueError("policy source/decision evidence is malformed")
    expected = classify(parsed["input_units"], terms)
    if (parsed["decision"], parsed["output_units"]) != (
        expected.decision, expected.output_units,
    ):
        raise ValueError("policy evidence conflicts with exact calculation")
    parsed["terms_object"] = terms
    return parsed
