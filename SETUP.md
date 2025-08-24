# swapService Operator & Setup Guide

This document contains the full installation, configuration, architecture, security, and troubleshooting details for the USDC ↔ USDD bidirectional swap service. User-facing swap instructions now live in `README.md`.

## Contents
- Overview
- Architecture & Flow
- Installation
- Environment Configuration
- Solana Setup
- Nexus Setup & Asset Mapping
- Running & Operational Loops
- Fees & Economics
- Idempotency & State
- Performance & Polling Strategy
- Troubleshooting
- Pointers (Security / Config)

---
## Overview
A Python service that automates swaps between USDC (Solana) and USDD (Nexus). It enforces:
- Strict memo / asset mapping validation
- Automatic refunds on invalid input
- Idempotent sends (memo signatures & processed markers)
- Micro-amount DoS resistance (thresholds & fee-only treatment)
- Heartbeat asset updates (optional)

## Architecture & Flow
### USDC → USDD
1. User sends USDC to vault token account with memo `nexus:<NEXUS_USDD_ACCOUNT>`.
2. Service parses signature, validates memo & Nexus account.
3. Computes fees, mints / debits USDD to recipient.
4. Writes processed markers; refunds on invalid cases.

### USDD → USDC
1. User sends USDD to treasury.
2. User publishes or updates Nexus Asset containing `txid_toService` and `receival_account`.
3. Service polls treasury transactions; for each credit above threshold it queries assets by `txid_toService` & owner.
4. Valid mapping -> send USDC to receival account (ATA required). Missing mapping -> pending until timeout -> refund.
5. Micro credits below `MIN_CREDIT_USDD` treated as fees (aggregated fee-only entries).

### State & Files
- Append-only JSONL for processed / unprocessed sets.
- attempt_state.json tracks retries.
- Heartbeat asset optionally stores `last_poll_timestamp` and per-chain waterlines.

## Installation
Requirements: Python 3.10+, pip, Solana CLI (optional for ops), Nexus CLI.

```bash
python -m pip install -r requirements.txt
```

(Optional) virtual environment recommended.

## Environment Configuration
Copy `.env.example` to `.env` then fill required variables.

Key required:
- SOLANA_RPC_URL
- VAULT_KEYPAIR
- VAULT_USDC_ACCOUNT
- USDC_MINT
- SOL_MINT (default okay)
- NEXUS_PIN
- NEXUS_USDD_TREASURY_ACCOUNT

Optional chain-specific intervals: `SOLANA_POLL_INTERVAL`, `NEXUS_POLL_INTERVAL`.

## Solana Setup
1. Create vault keypair.
2. Create USDC ATA for vault.
3. Fund SOL for fees + initial USDC liquidity.
4. (Optional) Quarantine account for failed refunds.

## Nexus Setup & Asset Mapping
- Run Nexus CLI with access to your signature chain.
- Treasury account (USDD) configured as `NEXUS_USDD_TREASURY_ACCOUNT`.
- For USDD→USDC swaps, users publish assets with mapping fields; service uses `register/list/assets` queries.

## Running
```
python swapService.py
```
Startup prints vault / treasury balances, recovery results, and begins polling.

## Fees & Economics
Vars:
- FLAT_FEE_USDC, FLAT_FEE_USDD
- DYNAMIC_FEE_BPS
- MICRO_DEPOSIT_FEE_PCT, MICRO_CREDIT_FEE_PCT (100% fee for sub-threshold by default)
- MIN_DEPOSIT_USDC, MIN_CREDIT_USDD thresholds

## Idempotency & State
- Solana: memo uniqueness + processed_sigs cache; pre-send crash recovery scans for memo.
- Nexus: asset mapping search by txid + owner, processed markers, refund attempt state.
- References: integer counters used internally (not user-facing) for audit.

## Performance & Polling Strategy
- Separate intervals: fast Solana (12–20s), Nexus aligned to ~block time (50–60s) to reduce empty polls.
- Per-loop caps: `SOLANA_MAX_TX_FETCH_PER_POLL`, `MAX_DEPOSITS_PER_LOOP`, `MAX_CREDITS_PER_LOOP`.
- Micro aggregation reduces write amplification.
- Future: optional WebSocket subscription to cut signature polls.

## Troubleshooting (Highlights)
See also `SECURITY.md` for security incidents & hardening.

Missing asset mapping: ensure asset includes both `txid_toService` and `receival_account` before timeout.
High RPC usage: increase `SOLANA_POLL_INTERVAL`, reduce max fetch caps, or enable delta skip.
Stalled waterline: investigate unprocessed rows with old timestamps; they may be quarantined or awaiting mapping.
Refund loop failures: inspect `FAILED_REFUNDS_FILE` entries; cross-check on-chain balances.

## Pointers
- Full security guidance: `SECURITY.md`
- Exhaustive configuration reference: `CONFIG.md`
- User swap instructions: `README.md`

## Appendix: Configuration Variables
See `.env.example` for the exhaustive, annotated list. Highlights:
- Heartbeat: HEARTBEAT_ENABLED, *_WATERLINE_* fields
- Backing management: BACKING_* vars
- Poll time budgets: *_POLL_TIME_BUDGET_SEC
- Adaptive (future extension): USE_NEXUS_WHERE_FILTER_USDD, SKIP_OWNER_LOOKUP_FOR_MICRO_USDD

---
LICENSE: Provided as-is; no warranty.
