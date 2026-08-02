# Configuration Reference (swapService)

Canonical, human‑readable reference for all environment variables consumed by the service (`config.py`) plus a few operational conventions. For a quick starting template see `.env.example`.

Legend:
- Req: Required at startup (service raises if missing)
- Type: str | int | bool | decimal (token units) | pubkey
- Default: Value assumed if unset (blank = none / must supply)

## Core Required

| Var | Req | Type | Default | Purpose / Notes |
|-----|-----|------|---------|-----------------|
| SOLANA_RPC_URL | Y | str |  | HTTPS RPC endpoint (rate limit mindful). |
| VAULT_KEYPAIR | Y | path |  | JSON keypair file for Solana vault signer. |
| VAULT_USDC_ACCOUNT | Y | pubkey |  | SPL USDC token account (ATA) holding liquidity. |
| USDC_MINT | Y | pubkey |  | USDC mint (mainnet or devnet). |
| SOL_MINT | Y | pubkey |  | SOL mint (native wrapper constant). Needed for potential fee conversions. |
| NEXUS_PIN | Y | str |  | PIN unlocking Nexus CLI operations. |
| NEXUS_USDD_TREASURY_ACCOUNT | Y | str |  | USDD treasury account receiving user USDD credits & paying refunds. |
| SOL_MAIN_ACCOUNT | Y | pubkey |  | Base SOL account (used in some balance / backing logic). |

## Decimals
| Var | Req | Type | Default | Notes |
|-----|-----|------|---------|-------|
| USDC_DECIMALS | N | int | 6 | Override only if non‑standard wrapped mint. |
| USDD_DECIMALS | N | int | 6 | Nexus USDD decimals. |

## Nexus Accounts (Optional / Conditional)
| Var | Req | Type | Default | Notes |
|-----|-----|------|---------|-------|
| NEXUS_CLI_PATH | N | path | ./nexus | Executable path. Ensure chmod +x. |
| NEXUS_SESSION | N | str |  | If your CLI wrapper enforces session tokens. Not read directly by service code now. |
| NEXUS_USDD_LOCAL_ACCOUNT | N | str |  | Receives micro USDD credits / congestion fees. |
| NEXUS_USDD_QUARANTINE_ACCOUNT | N | str |  | Destination for quarantined failed USDD refunds. If unset, quarantined USDD stays in the treasury and keeps counting toward the backing ratio. |
| NEXUS_USDD_FEES_ACCOUNT | N | str |  | If separating fee accrual from local account. |
| NEXUS_TOKEN_NAME | N | str | USDD | Sanity validation of token name on mint/credit path. |
| NEXUS_RPC_HOST | N | str | http://127.0.0.1:8399 | Node / gateway host if used. |

## Poll Intervals & State
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| POLL_INTERVAL | int | 10 | Legacy/global fallback if chain‑specific not set. |
| SOLANA_POLL_INTERVAL | int | POLL_INTERVAL | Poll cadence (s) for Solana path. Faster (~12–20s) recommended. |
| NEXUS_POLL_INTERVAL | int | POLL_INTERVAL | Poll cadence (s) for Nexus path. Match/block (~50–60s) to reduce empties. |
| STATE_DB_PATH | str | swap_service.db | SQLite database path for all state persistence. |
| FEES_STATE_FILE | str | fees_state.json | Legacy USDC fee accumulator file (fees.py). |

> **Note:** Prior JSONL file config vars (`PROCESSED_SIG_FILE`, `UNPROCESSED_SIGS_FILE`, etc.) are deprecated. All state is now stored in the SQLite database at `STATE_DB_PATH`. Fee tracking uses both `fee_entries` table and optional `FEES_STATE_FILE`.

## Timeouts / Budgets
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| SOLANA_DEPOSIT_COMMITMENT | string | finalized | Commitment for ingesting deposits and settling our own payouts. `confirmed` is not rooted and can be reorged after USDD is minted against it (permanently unbacked supply). Relax only deliberately. |
| SOLANA_FINALIZED_ABOVE_UNITS | int | 0 | If the commitment above is relaxed, deposits ≥ this many USDC base units still require finalization. 0 disables the carve-out. |
| DEBIT_VERIFY_GRACE_SEC | int | 300 | How long an ambiguous USDD debit is verified against the chain before concluding it never executed. |
| SOLANA_RPC_TIMEOUT_SEC | int | 8 | Per RPC HTTP call. |
| SOLANA_TX_FETCH_TIMEOUT_SEC | int | 12 | Individual tx signature fetch. |
| SOLANA_POLL_TIME_BUDGET_SEC | int | 15 | Soft cap per Solana loop. |
| SOLANA_MAX_TX_FETCH_PER_POLL | int | 120 | Upper bound; tune with spam. |
| NEXUS_CLI_TIMEOUT_SEC | int | 20 | CLI process timeout. |
| NEXUS_POLL_TIME_BUDGET_SEC | int | 15 | Soft cap per Nexus loop. |
| METRICS_BUDGET_SEC | int | 5 | Budget for metrics gathering. |
| METRICS_INTERVAL_SEC | int | 30 | Emit frequency. |
| REFUND_TIMEOUT_SEC | int | 3600 | Seconds to wait for mapping (USDD→USDC) before refund path. |
| STALE_DEPOSIT_QUARANTINE_SEC | int | 86400 | Max age before deposit forced to refund/quarantine. |
| USDC_CONFIRM_TIMEOUT_SEC | int | 600 | Wait for outbound USDC confirmation. |
| STALE_ROW_SEC | int | 86400 | Age trigger for stale state record handling. |
| HEARTBEAT_MIN_INTERVAL_SEC | int | max(10,POLL) | Prevent spam updates (>=10s). |
| HEARTBEAT_WATERLINE_SAFETY_SEC | int | 120 | Safety margin subtracted when filtering old items. |
| ACTION_RETRY_COOLDOWN_SEC | int | 300 | Minimum seconds between retry attempts of the same action (now enforced). |

## Fees & Thresholds
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| FLAT_FEE_USDC | decimal | **0.5** | Fixed fee on the **USDD→USDC** path (deducted from the USDC output). Named for the output token, not the input. |
| FLAT_FEE_USDD | decimal | 0.1 | Tiny USDD threshold / micro gating. |
| DYNAMIC_FEE_BPS | int | 10 | Applied both directions on success (0 = disable). |
| MIN_DEPOSIT_USDC | decimal | 0.2 | Minimum USDC swapped (= 2x FLAT_FEE_USDD, nets ~0.0998 USDD). Values below 2x the flat fee are raised to it at startup and logged. |
| MIN_CREDIT_USDD | decimal | 1.0 | Minimum USDD swapped (= 2x FLAT_FEE_USDC, nets ~0.499 USDC). Values below 2x the flat fee are raised to it at startup and logged. Credits below it are recorded and booked as fees. |
| DUST_CREDIT_USDD | decimal | 0.01 | Anti-DoS dust floor. Credits below this are ignored entirely (no state, no accounting). Credits between this and MIN_CREDIT_USDD are recorded so the funds remain traceable. |
| MICRO_DEPOSIT_FEE_PCT | int | 100 | Percent of micro deposit retained (100 = all). |
| MICRO_CREDIT_FEE_PCT | int | 100 | Percent of micro credit retained. |
| NEXUS_CONGESTION_FEE_USDD | decimal | 0.001 | Deducted on Nexus refunds (covers on‑chain cost). |

## Exposure Caps & Alerting
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| MAX_SWAP_USDC | decimal | 0 | Largest single USDC→USDD deposit accepted; larger is refunded. 0 disables. |
| MAX_SWAP_USDD | decimal | 0 | Largest single USDD→USDC credit accepted; larger is refunded. 0 disables. |
| DAILY_PAYOUT_CAP_USDC | decimal | 0 | Rolling 24h ceiling on total outbound USDC, enforced at the single send choke point. 0 disables. |
| ALERT_WEBHOOK_URL | str |  | Alerts POSTed here as JSON. Without a channel, safety signals only reach stdout. |
| ALERT_COMMAND | str |  | Executable receiving the same JSON on stdin. |
| ALERT_MIN_INTERVAL_SEC | int | 300 | Per-event dedupe window. |

## Micro / Advanced Handling Flags
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| USE_NEXUS_WHERE_FILTER_USDD | bool | true | Attempt server‑side WHERE filtering for micro skip. |
| SKIP_OWNER_LOOKUP_FOR_MICRO_USDD | bool | true | Avoid expensive owner queries for tiny credits. |
| MICRO_CREDIT_COUNT_AGAINST_LIMIT | bool | false | If true micro credits consume per-loop quota. |

## Heartbeat & Waterlines
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| HEARTBEAT_ENABLED | bool | true | Enable updating heartbeat asset field. |
| NEXUS_HEARTBEAT_ASSET_ADDRESS | str |  | Asset address to update. |
| NEXUS_HEARTBEAT_ASSET_NAME | str |  | (Optional) Name; may be used by tooling. |
| HEARTBEAT_WATERLINE_ENABLED | bool | true | Enforce skipping items older than waterline. |
| HEARTBEAT_WATERLINE_SOLANA_FIELD | str | last_safe_timestamp_solana | Field name on asset. |
| HEARTBEAT_WATERLINE_NEXUS_FIELD | str | last_safe_timestamp_nexus | Field name on asset. MUST match the asset (format=basic locks fields at creation); a mismatch makes every heartbeat update fail atomically. |

## Fee Conversion / Backing (Optional Feature Gate)
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| FEE_CONVERSION_ENABLED | bool | false | Enable periodic conversions / top‑ups. |
| FEE_CONVERSION_MIN_USDC | int | 0 | Minimum base units before attempt. |
| SOL_TOPUP_MIN_LAMPORTS | int | 0 | Trigger threshold. |
| SOL_TOPUP_TARGET_LAMPORTS | int | 0 | Refill target. |
| NEXUS_NXS_TOPUP_MIN | int | 0 | Placeholder threshold for NXS. |
| BACKING_DEFICIT_BPS_ALERT | int | 10 | Alert when backing < 99.9%. |
| BACKING_DEFICIT_PAUSE_PCT | int | 90 | Pause swaps if backing ratio < this. |
| BACKING_RECONCILE_INTERVAL_SEC | int | 3600 | Minimum spacing between reconcile mints. |
| BACKING_SURPLUS_MINT_THRESHOLD_USDC | decimal | 20 | Only mint when vault > threshold. |
| TARGET_SOL_PER_NXS_NUM | int | 1 | Target SOL numerator. |
| TARGET_SOL_PER_NXS_DEN | int | 10000 | Target SOL denominator. |

## Quarantine & Accounts
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| USDC_QUARANTINE_ACCOUNT | pubkey |  | Holds USDC from failed refund attempts. |

## Operational Philosophy
- All monetary thresholds are enforced before expensive lookups (DoS mitigation).
- Idempotency uses on‑chain memos (Solana) plus processed sets; Nexus path uses txid + owner + asset mapping.
- Micro traffic is downgraded: immediate fee capture, optional owner lookup skip, aggregated reporting.

## Adding New Variables
1. Add to `config.py` with sane default.  
2. Document here with description + default.  
3. Update `.env.example`.  
4. (If sensitive) do NOT add a real value—leave placeholder.  

## Minimal Required Set (Barebones)
At a minimum your `.env` must define: `SOLANA_RPC_URL`, `VAULT_KEYPAIR`, `VAULT_USDC_ACCOUNT`, `USDC_MINT`, `SOL_MINT`, `NEXUS_PIN`, `NEXUS_USDD_TREASURY_ACCOUNT`, `SOL_MAIN_ACCOUNT`.

## Validation Behavior
`config.py` raises on startup if any required var is missing; optional vars fall back to defaults above. Boolean parsing: values in ("1","true","yes","on") are treated as True case‑insensitively.

---
See `SECURITY.md` for secure handling recommendations (permissions, rotation, secrets hygiene).
