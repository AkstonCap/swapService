# Security Guide (swapService)

Focused reference for running the swap service securely. Complements `SETUP.md` (operations) and `CONFIG.md` (variable reference).

## Objectives
- Preserve solvency / backing (no unintended mint or drain).
- Prevent double processing / replay.
- Reduce DoS / spam impact.
- Ensure recoverability after crashes.

## Supported security boundary

One process protects one configured pair: one classic SPL Token Program mint and one Nexus token
register, assembled as `config.SWAP_PAIR`. `SOLANA_TOKEN_MINT`, `SOLANA_VAULT_ACCOUNT`,
`SOLANA_TOKEN_SYMBOL`, `SOLANA_TOKEN_DECIMALS`, `NEXUS_TOKEN_NAME`,
`NEXUS_TOKEN_REGISTER_ADDRESS`, `NEXUS_TREASURY_ACCOUNT`, and `NEXUS_TOKEN_DECIMALS` are the
canonical pair settings. Symbols are display metadata; authorization and reconciliation use the
mint/register identities and custody addresses.

Multi-pair routing, provider schema v2, additional destination chains, and Token-2022 are not
implemented. `src/solana_client.py` uses the classic Token Program id in account validation, ATA
derivation, and transfer instructions. Do not advertise or configure a Token-2022 mint.

## Key Material & Secrets
| Item | Guidance |
|------|----------|
| Solana Vault Keypair | Store outside repo; restrict permissions (0600). Consider hardware signer if volume grows. |
| Nexus PIN / Session | Both are credentials — on a `multiuser=1` node the session id plus the PIN authorises spending. Both are redacted from logs and alerts. Never log them. In `SWAP_PRODUCTION_MODE=true`, the service requires `NEXUS_API_URL` with `https`, `NEXUS_API_USER`, and `NEXUS_API_PASSWORD`; it sends the PIN/session only in the authenticated HTTPS POST body, never in a child-process argv. Keep the endpoint local or firewall/VPN-restricted, validate its TLS certificate, and use environment-variable injection (systemd drop-in / Docker secret). The CLI fallback is development-only; local shell access remains sensitive because it can read the service environment. |
| Backups | Encrypted offsite copy of keypair + state files daily. |

## File Permissions
- Restrict directory to service user.
- `vault-keypair.json` and any additional key files: mode 600.
- State database (`swap_service.db`): writable only by service user (avoid accidental edits).

## State Integrity
- SQLite runs in WAL mode (enabled in `state_db.init_db()`) for crash-safe persistence.
- A startup `flock` prevents a second instance from sharing the state DB (`SWAP_LOCK_PATH`).
- Do not manually edit the database unless fully aware of consequences (risk: double payout). Instead, use quarantine tables and reconcile manually.
- Maintain checksum (optional) of state directory for tamper detection.

## Idempotency Controls
- Solana→Nexus: the Solana signature is the persisted source identity; a unique Nexus `reference`
  and the legacy-valued `reservations.kind="usdc_to_usdd_debit"` are stored before the debit.
- Nexus→Solana: admission and lifecycle state preserve `(txid, contract_id)`. The owner-verified
  mapping uses the literal v1 fields `txid_toService` and `receival_account`; new payouts carry
  `nexus_txid:<txid>:<contract_id>`.
- Before a Solana payout RPC, `payout_solana_units` and `payout_fee_nexus_units` are frozen on the
  exact source row. Terminal state requires successful finalized transaction evidence matching the
  source memo, signature, vault signer/source, configured mint, recipient, and integer output.
- Persisted USDC/USDD database columns, status values, reservation kinds, and retry-key prefixes are
  compatibility contracts. Renaming one without an explicit migration can reset retry exclusion or
  hide an in-flight reservation.
- Do not manually edit processed markers unless fully aware of consequences (risk: double payout). Instead, quarantine and reconcile manually.

## DoS & Spam Mitigation
- Thresholds: `MIN_DEPOSIT_SOLANA_TOKEN`, `MIN_CREDIT_NEXUS_TOKEN`, and
  `DUST_CREDIT_NEXUS_TOKEN` bound micro work. The `*_USDC`/`*_USDD` spellings are legacy aliases.
- Accepted below-minimum credits are retained fully as fees; true dust is ignored.
- `MAX_CREDITS_PER_LOOP` is checked between Nexus transactions, not between sibling CREDIT
  contracts; it is not a strict per-contract batch ceiling. Accepted below-minimum credits count
  toward processing and still perform owner lookup.
- `MAX_DEPOSITS_PER_LOOP`, `MICRO_CREDIT_COUNT_AGAINST_LIMIT`, and
  `SKIP_OWNER_LOOKUP_FOR_MICRO_USDD` are parsed but do not control these current admission paths.
  Use actual fetch/time budgets; do not assume micro aggregation or owner-lookup skipping.
- Consider raising `SOLANA_POLL_INTERVAL` or lowering `SOLANA_MAX_TX_FETCH_PER_POLL` under sustained attack.

## Refund Safety
- Attempts are bounded by `MAX_ACTION_ATTEMPTS`, with `ACTION_RETRY_COOLDOWN_SEC` enforced between
  them. After exhaustion, the configured Solana token may move to `SOLANA_QUARANTINE_ACCOUNT`
  (`USDC_QUARANTINE_ACCOUNT` is the legacy alias); Nexus→Solana credits instead remain held because
  automatic Nexus refunds and quarantine moves are disabled.
- Any held-credit Nexus disposition debit must first create a durable intent containing source,
  destination, exact base units and a unique reference. Timeout, non-zero CLI exit or unparsed
  output is `outcome_unknown` and requires positive chain-reference resolution; it is never retried blindly.
- The only supported Nexus held-credit disposition is `nexus_transfer_operator.py`: a named
  operator prepares an intent, confirms its exact reference, authorizes it, then issues one
  debit. Finalization also requires the exact positively observed remote txid and records
  immutable authorization/execution/disposition evidence in SQLite. See `SETUP.md`.
- Keep quarantine accounts separate from active treasury/vault to simplify reconciliation and avoid accidental reuse.

## Heartbeat & Liveness
- The current runtime selects its v1 heartbeat/registration by `NEXUS_HEARTBEAT_ASSET_NAME`, not
  by `NEXUS_HEARTBEAT_ASSET_ADDRESS`. A fresh `last_poll_timestamp` proves only liveness.
- Canonical waterline defaults are `last_safe_timestamp_solana` and
  `last_safe_timestamp_nexus`. Custom configured names must exactly match the fixed basic-asset
  field set.
- Startup is fail closed: a readable heartbeat, positive checkpoints, and complete Solana and Nexus
  reconstruction are mandatory before any poller or other chain activity. Missing/zero checkpoints,
  malformed/legacy payout evidence, and incomplete scans abort startup.
- Live empty Nexus enumeration does not advance the checkpoint. Any nonzero mutable offset page
  request also holds because it cannot prove a snapshot-stable range.

## Exposure Limits
- `MAX_SWAP_USDC` / `MAX_SWAP_USDD` are legacy-named current settings that cap the configured
  Solana/Nexus inputs. On the Nexus side, "refund" means an operator hold until a separately
  authorized durable disposition; it is not an automatic treasury debit.
- **Rolling payout-cap protocol:** `DAILY_PAYOUT_CAP_USDC` is enforced through an append-only
  SQLite obligation ledger for every automated Solana payout, refund and quarantine movement.
  Capacity is reserved with the exact source before RPC, retained across submitted/unknown states,
  and settled only from the exact confirmed signature. Local code therefore refuses a cap breach
  without sending; a primary Nexus credit is marked `payout cap held`, emits the rate-limited
  `solana_payout_cap_held` critical alert, and is visible in the dashboard/API until the same durable
  claim path can reserve capacity. Target-chain timeout, crash/restart and finality acceptance remain
  required.
- Even a correctly centralized service cap cannot stop an attacker who has stolen the signer
  key and submits transactions outside the service.
- Deposits are ingested at `finalized` by default; a reorged `confirmed` deposit could otherwise
  leave permanently unbacked Nexus-token supply.

## Logging & Monitoring
- Configure `ALERT_WEBHOOK_URL` or `ALERT_COMMAND`. Without one, backing-deficit pauses,
  unbacked-mint discrepancies and halted pollers remain local to the service host.
- Operator alerts plus Nexus/Solana deposit lifecycle transitions are emitted as one
  JSON object per line on stdout, with UTC timestamp, severity, stable event name and
  contextual fields. Collect stdout/stderr with rotation; do not parse human terminal prose.
- Structured logging redacts Nexus PIN/session/API-password material, Solana keypair paths and
  alert webhook URLs before output. Do not add raw chain credentials to arbitrary log messages.
- Monitor: processed swaps per hour, refund counts, micro credit ratio, backlog queue length, RPC error rate.
- Alerting: high refund failure rate, backing ratio breach (< BACKING_DEFICIT_PAUSE_PCT), stale heartbeat.

## Backing & Reconciliation
- Periodically audit the configured Solana vault against issued Nexus supply (accounting for fees,
  held credits, independently configured decimals, and quarantined amounts).
- Use `BACKING_DEFICIT_BPS_ALERT` & `BACKING_DEFICIT_PAUSE_PCT` to fail safe (pause swaps) on deficit.
- Reconciliation failure, malformed/incomplete evidence, or discrepancy latches new exposure paused;
  only a later explicitly healthy read-back clears it.
- Surplus handling is alert-only. There is no automated Solana DEX swap or Nexus mint/rebalance path.

## Secrets Rotation
- Rotate Solana keypair cautiously: drain funds to new account, update env, restart, archive old key offline.
- Nexus credentials: rotate PIN/session; ensure no hardcoded secrets in scripts.

## Crash Recovery Checklist
1. Stop service if running partially.
2. Backup state directory.
3. Review last N lines of log around crash for partially executed action (send/mint). 
4. Re-run only with the configured named heartbeat available and both positive waterlines. Startup
   must report `recovery_complete=true`; any incomplete result is a stop condition, not permission
   for a bounded fallback scan.
5. Verify `sending`/awaiting rows against full finalized payout evidence and frozen terms. Never
   infer non-payment from a missing signature lookup and never manually resubmit an ambiguous send.
6. Compare on-chain balances and exact source liabilities with the internal payout/fee state. Hold
   and investigate discrepancies; do not "adjust" terminal rows without an audited migration.

## Hardening Roadmap (Advanced)
- Run Solana RPC privately or via authenticated provider (rate limit & data consistency).
- Containerize with read‑only root fs, bind‑mount writable state dir only.
- Add integrity hash of config at startup; alert on drift.
- Consider WebSocket subscription to reduce polling attack surface.

## Threat Model Snapshot
| Threat | Mitigation |
|--------|------------|
| Replay / double payout | Processed signature / txid markers & idempotent logic. |
| Spam micros | Dust filtering, full retention of accepted sub-minimum credits, and actual processing budgets. |
| Key compromise | File perms, minimal SOL exposure, optional HSM. |
| Refund abuse (craft invalid for free liquidity) | Solana-side refund fee, bounded attempts, and operator-authorized Nexus dispositions. |
| State tampering | File perms + optional checksums + offsite backups. |
| Resource exhaustion | Per-loop caps, time budgets, separate intervals. |

---
See `CONFIG.md` for variable details and `SETUP.md` for operational walkthrough.
