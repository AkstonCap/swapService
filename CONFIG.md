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
| NEXUS_PIN | Y | str |  | PIN authorizing Nexus profile operations. Never log it; in production it is sent only in an HTTPS POST body, never a child process argument. |
| NEXUS_USDD_TREASURY_ACCOUNT | Y | str |  | USDD treasury account receiving user USDD credits & paying refunds. |
| SOL_MAIN_ACCOUNT | Y | pubkey |  | Base SOL account (used in some balance / backing logic). |

## Bridged Token Pair
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| SOLANA_TOKEN_MINT | pubkey |  | Mint of the Solana-side token. Alias: `USDC_MINT`. |
| SOLANA_VAULT_ACCOUNT | pubkey |  | Vault SPL token account (ATA) for that mint. Alias: `VAULT_USDC_ACCOUNT`. |
| SOLANA_TOKEN_SYMBOL | str | USDC | Display ticker; published in the registration record. |
| SOLANA_TOKEN_DECIMALS | int | 6 | Alias: `USDC_DECIMALS`. |
| NEXUS_TOKEN_NAME | str | USDD | Display name passed to `finance/debit/token from=<token>`. |
| NEXUS_TOKEN_REGISTER_ADDRESS | str |  | Immutable address returned by trusted `finance/get/token`; required for terminal DEBIT read-back. Absent/mismatched values hold rather than finalize. |
| NEXUS_TOKEN_DECIMALS | int | 6 | Alias: `USDD_DECIMALS`. |
| DEPOSIT_MEMO_PREFIX | str | `nexus:` | Memo prefix depositors use to name their Nexus destination. |
| SERVICE_PROVIDER | str |  | Operator name/domain, published on-chain. |
| SERVICE_VERSION | str | 1.0.0 | Published on-chain. |
| SERVICE_CONTACT | str |  | URL or contact handle, published on-chain. |

## Decimals
| Var | Req | Type | Default | Notes |
|-----|-----|------|---------|-------|
| USDC_DECIMALS | N | int | 6 | Override only if non‑standard wrapped mint. |
| USDD_DECIMALS | N | int | 6 | Nexus USDD decimals. |

## Nexus Accounts (Optional / Conditional)
| Var | Req | Type | Default | Notes |
|-----|-----|------|---------|-------|
| NEXUS_CLI_PATH | N | path | ./nexus | CLI fallback used only outside production; executable path when no API URL is configured. |
| NEXUS_API_URL | Production: Y | https URL |  | Nexus HTTPS API base URL, e.g. `https://127.0.0.1:8443`. Must not embed credentials, a query, or a fragment. |
| NEXUS_API_USER | Production: Y | str |  | HTTP Basic-auth user configured as `apiuser` in `nexus.conf`. |
| NEXUS_API_PASSWORD | Production: Y | secret |  | HTTP Basic-auth password configured as `apipassword`; never log it. |
| NEXUS_MULTIUSER | N | bool | false | Set true only if `nexus.conf` has `multiuser=1`. Controls whether `session=<id>` is included in the HTTPS POST body for session-scoped API calls. |
| NEXUS_SESSION | N | str |  | Session id from `sessions/create/local`. **Required when NEXUS_MULTIUSER=true** — every finance/*, assets/*, market/*, supply/* call needs it. Never sent in single-user mode (the API rejects it). Credential: redacted from logs and alerts; production sends it only in the HTTPS request body. |
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
| SOLANA_RPC_TIMEOUT_SEC | int | 8 | Per RPC HTTP call. |
| SOLANA_TX_FETCH_TIMEOUT_SEC | int | 12 | Individual tx signature fetch. |
| SOLANA_POLL_TIME_BUDGET_SEC | int | 15 | Soft cap per Solana loop. |
| SOLANA_MAX_TX_FETCH_PER_POLL | int | 120 | Upper bound; tune with spam. |
| NEXUS_CLI_TIMEOUT_SEC | int | 20 | CLI process timeout. |
| NEXUS_POLL_TIME_BUDGET_SEC | int | 15 | Soft cap per Nexus loop. |
| NEXUS_TRANSFER_MIN_CONFIRMATIONS | int | 10 | Shared minimum for direct Nexus transaction read-back before mint or transfer evidence can become terminal. **Must be > 0.** Current startup code does not enforce that bound, so zero/negative values are a release blocker and forbidden in production. Validate the production value against target-node finality semantics. |
| METRICS_BUDGET_SEC | int | 5 | Budget for metrics gathering. |
| METRICS_INTERVAL_SEC | int | 30 | Emit frequency. |
| REFUND_TIMEOUT_SEC | int | 3600 | Seconds to wait for mapping (USDD→USDC) before refund path. |
| STALE_DEPOSIT_QUARANTINE_SEC | int | 86400 | Max age before deposit forced to refund/quarantine. |
| SOLANA_CONFIRM_TIMEOUT_SEC | int | 600 | Wait for outbound USDC confirmation. |
| STALE_ROW_SEC | int | 86400 | Age trigger for stale state record handling. |
| HEARTBEAT_MIN_INTERVAL_SEC | int | max(10,POLL) | Prevent spam updates (>=10s). |
| HEARTBEAT_WATERLINE_SAFETY_SEC | int | 120 | Safety margin subtracted when filtering old items. |
| ACTION_RETRY_COOLDOWN_SEC | int | 300 | Minimum seconds between retry attempts of the same action (now enforced). |

## Fees & Thresholds
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| FLAT_FEE_USDC | decimal | **0.5** | Fixed fee on the **Nexus→Solana** path, represented in Solana output base units. Must be exactly representable at both configured token precisions. |
| FLAT_FEE_USDD | decimal | 0.1 | Fixed fee on the **Solana→Nexus** path, represented in Nexus output base units; the same token value is converted independently to Solana base units for refunds. Must be exactly representable at both configured precisions. |
| DYNAMIC_FEE_BPS | int | 10 | Applied to the input amount in each direction, in that input token’s base-unit scale (0 = disable). |
| MIN_DEPOSIT_USDC | decimal | 0.2 | Minimum Solana-side input (default = 2x the Solana-equivalent `FLAT_FEE_USDD`); values below the floor are raised at startup and logged. |
| MIN_CREDIT_USDD | decimal | 1.0 | Minimum Nexus-side input (default = 2x the Nexus-equivalent `FLAT_FEE_USDC`); values below the floor are raised at startup and logged. Credits below it are recorded and booked as fees. |
| DUST_CREDIT_USDD | decimal | 0.01 | Nexus-side anti-DoS floor (default = one tenth of the Nexus-equivalent `FLAT_FEE_USDC`). Credits below it are ignored; credits up to `MIN_CREDIT_USDD` remain traceable. |
| MICRO_DEPOSIT_FEE_PCT | int | 100 | Percent of micro deposit retained (100 = all). |
| MICRO_CREDIT_FEE_PCT | int | 100 | Percent of micro credit retained. |
| NEXUS_CONGESTION_FEE_USDD | decimal | 0.001 | **Currently inert.** Intended to cover the on-chain cost of a Nexus refund, but no code path deducts it: all four refund sites return the full credited amount, so the operator absorbs the Nexus transaction cost. Set it if you like — nothing reads it beyond the unused `_apply_congestion_fee()` helper. |

## Production Admission, Exposure Caps & Alerting

`SWAP_PRODUCTION_MODE` defaults to `false` for local development and test networks. Its accepted
values are `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off` (case-insensitive; surrounding
whitespace is accepted). Any other present value fails startup rather than silently disabling the
production gate. When it is `true`, startup refuses to open the state database or start polling
unless both single-swap caps, the daily Solana payout cap, one alert route, and both chain-side
quarantine destinations are configured with non-zero/non-empty values. This is a configuration
admission check, not proof that the alert route is deliverable; test the configured channel as part
of the live acceptance matrix.

| Var | Type | Default | Notes |
|-----|------|---------|-------|
| SWAP_PRODUCTION_MODE | bool | false | Enables mandatory production startup controls. |
| MAX_SWAP_USDC | decimal | 0 | Largest single USDC→USDD deposit accepted; larger is refunded. 0 disables. Required > 0 in production mode. |
| MAX_SWAP_USDD | decimal | 0 | Largest single USDD→USDC credit accepted; larger is refunded. 0 disables. Required > 0 in production mode. |
| DAILY_PAYOUT_CAP_USDC | decimal | 0 | Rolling 24h ceiling on total outbound USDC, enforced at the single send choke point. 0 disables. Required > 0 in production mode. |
| ALERT_WEBHOOK_URL | str |  | Alerts POSTed here as JSON. Required in production mode unless `ALERT_COMMAND` is set. |
| ALERT_COMMAND | str |  | Executable receiving the same JSON on stdin. Required in production mode unless `ALERT_WEBHOOK_URL` is set. |
| ALERT_MIN_INTERVAL_SEC | int | 300 | Per-event dedupe window. |
| USDC_QUARANTINE_ACCOUNT | pubkey |  | Self-owned Solana SPL token account for failed USDC refunds. Required in production mode. |
| NEXUS_USDD_QUARANTINE_ACCOUNT | str |  | Self-owned Nexus token account for the separately authorized durable-intent quarantine disposition. Required in production mode. |

## Operator Dashboard (read-only UI)
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| DASHBOARD_HOST | str | 127.0.0.1 | Bind address. Anything non-loopback requires DASHBOARD_TOKEN; the dashboard refuses to start otherwise. |
| DASHBOARD_PORT | int | 8787 | |
| DASHBOARD_TOKEN | str |  | Bearer token. Required for non-loopback binds; recommended always. Supply only in `Authorization: Bearer <token>` (normally injected by a TLS reverse proxy); query-string tokens are rejected. |

## Micro / Advanced Handling Flags
| Var | Type | Default | Notes |
|-----|------|---------|-------|
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

## Backing Safety Monitoring
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| BACKING_DEFICIT_BPS_ALERT | int | 10 | Alert threshold for backing deficit. |
| BACKING_DEFICIT_PAUSE_PCT | int | 90 | Pause new swaps if backing ratio < this. |
| BACKING_RECONCILE_INTERVAL_SEC | int | 3600 | Minimum spacing between read-only backing/surplus checks. |
| BACKING_SURPLUS_MINT_THRESHOLD_USDC | decimal | 20 | Minimum vault balance for a read-only backing-surplus operator alert. It does not authorize a mint. |

## Quarantine & Accounts
| Var | Type | Default | Notes |
|-----|------|---------|-------|
| USDC_QUARANTINE_ACCOUNT | pubkey |  | Holds USDC from failed refund attempts. Required in production mode; repeated above with other admission controls. |
| NEXUS_USDD_QUARANTINE_ACCOUNT | str |  | Holds Nexus-side credits for a separately authorized durable-intent quarantine disposition. Required in production mode; repeated above with other admission controls. |

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
At a minimum your `.env` must define: `SOLANA_RPC_URL`, `VAULT_KEYPAIR`, `VAULT_USDC_ACCOUNT`, `USDC_MINT`, `NEXUS_PIN`, `NEXUS_USDD_TREASURY_ACCOUNT`, `SOL_MAIN_ACCOUNT`.

## Validation Behavior
`config.py` raises on startup if any required var is missing; optional vars fall back to defaults above. Boolean parsing: values in ("1","true","yes","on") are treated as True case‑insensitively.

---
See [docs/SECURITY.md](docs/SECURITY.md) for secure handling recommendations (permissions,
rotation, and secrets hygiene).
