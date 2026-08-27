# Security Guide (swapService)

Focused reference for running the swap service securely. Complements `SETUP.md` (operations) and `CONFIG.md` (variable reference).

## Objectives
- Preserve solvency / backing (no unintended mint or drain).
- Prevent double processing / replay.
- Reduce DoS / spam impact.
- Ensure recoverability after crashes.

## Key Material & Secrets
| Item | Guidance |
|------|----------|
| Solana Vault Keypair | Store outside repo; restrict permissions (0600). Consider hardware signer if volume grows. |
| Nexus PIN / Session | Both are credentials — on a `multiuser=1` node the session id plus the PIN authorises spending. Both are redacted from logs and alerts. Never log. Use environment variable injection (systemd drop‑in / Docker secret). **Known limitation:** the PIN is currently passed as a Nexus CLI *argument*, so it is visible to any local user via `ps` / `/proc/<pid>/cmdline` for the duration of the call. Treat local shell access to the host as equivalent to holding the PIN. |
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
- Solana: on‑chain memo includes Nexus address; `processed_sigs` database table prevents replay on restart.
- Nexus: credit txid + owner + asset mapping triple must align; mismatched owner mapping is ignored.
- Do not manually edit processed markers unless fully aware of consequences (risk: double payout). Instead, quarantine and reconcile manually.

## DoS & Spam Mitigation
- Thresholds: `MIN_DEPOSIT_USDC` / `MIN_CREDIT_USDD` discard micro attempts early; keep them aggressive enough.
- Micro fee policy (100%): converts spam into negligible cost overhead.
- Per-loop caps: `MAX_DEPOSITS_PER_LOOP`, `MAX_CREDITS_PER_LOOP` prevent runaway processing within a single cycle.
- Owner lookup skipping for micros curtails expensive Nexus queries.
- Consider raising `SOLANA_POLL_INTERVAL` or lowering `SOLANA_MAX_TX_FETCH_PER_POLL` under sustained attack.

## Refund Safety
- Attempts are bounded by `MAX_ACTION_ATTEMPTS`, with `ACTION_RETRY_COOLDOWN_SEC` enforced between them. After exhaustion, USDC may move to `USDC_QUARANTINE_ACCOUNT`; USDD→USDC credits instead remain held because automatic Nexus refunds and quarantine moves are disabled.
- Any future Nexus account debit must first create a durable intent containing source, destination, exact base units and a unique reference. Timeout, non-zero CLI exit or unparsed output is `outcome_unknown` and requires positive chain-reference resolution; it is never retried blindly.
- The only supported Nexus held-credit disposition is `nexus_transfer_operator.py`: a named
  operator prepares an intent, confirms its exact reference, authorizes it, then issues one
  debit. Finalization also requires the exact positively observed remote txid and records
  immutable authorization/execution/disposition evidence in SQLite. See `SETUP.md`.
- Keep quarantine accounts separate from active treasury/vault to simplify reconciliation and avoid accidental reuse.

## Heartbeat & Liveness
- Optional heartbeat asset shows last poll timestamp; monitor externally (alert if stale > 3 × max(intervals)).
- Waterlines: enable to bound historical scan after assured data integrity, reducing time for catchup after downtime.

## Exposure Limits
- `MAX_SWAP_USDC` / `MAX_SWAP_USDD` cap a single swap; oversized items are refunded.
- `DAILY_PAYOUT_CAP_USDC` is a rolling 24h ceiling enforced at the one function every USDC
  payment passes through, so a runaway loop or stolen key cannot drain the vault at once.
- Deposits are ingested at `finalized` by default; a reorged `confirmed` deposit would
  otherwise leave permanently unbacked USDD.

## Logging & Monitoring
- Configure `ALERT_WEBHOOK_URL` or `ALERT_COMMAND`. Without one, backing-deficit pauses,
  unbacked-mint discrepancies and halted pollers are only visible on stdout.
- Capture stdout/stderr to log aggregation (with rotation). Remove sensitive env echoing.
- Monitor: processed swaps per hour, refund counts, micro credit ratio, backlog queue length, RPC error rate.
- Alerting: high refund failure rate, backing ratio breach (< BACKING_DEFICIT_PAUSE_PCT), stale heartbeat.

## Backing & Reconciliation
- Periodically audit vault USDC vs issued USDD (accounting for fees & quarantined amounts).
- Use `BACKING_DEFICIT_BPS_ALERT` & `BACKING_DEFICIT_PAUSE_PCT` to fail safe (pause swaps) on deficit.
- Surplus logic (mint to local) only when threshold met; review before enabling.

## Secrets Rotation
- Rotate Solana keypair cautiously: drain funds to new account, update env, restart, archive old key offline.
- Nexus credentials: rotate PIN/session; ensure no hardcoded secrets in scripts.

## Crash Recovery Checklist
1. Stop service if running partially.
2. Backup state directory.
3. Review last N lines of log around crash for partially executed action (send/mint). 
4. Re-run service: built-in idempotency should skip already completed steps.
5. Compare on-chain balances vs internal fee state; adjust if discrepancy.

## Hardening Roadmap (Advanced)
- Run Solana RPC privately or via authenticated provider (rate limit & data consistency).
- Containerize with read‑only root fs, bind‑mount writable state dir only.
- Add integrity hash of config at startup; alert on drift.
- Consider WebSocket subscription to reduce polling attack surface.

## Threat Model Snapshot
| Threat | Mitigation |
|--------|------------|
| Replay / double payout | Processed signature / txid markers & idempotent logic. |
| Spam micros | Threshold + 100% fee + aggregation. |
| Key compromise | File perms, minimal SOL exposure, optional HSM. |
| Refund abuse (craft invalid for free liquidity) | Flat fees on USDC path, congestion fee on Nexus refunds, attempt caps. |
| State tampering | File perms + optional checksums + offsite backups. |
| Resource exhaustion | Per-loop caps, time budgets, separate intervals. |

---
See `CONFIG.md` for variable details and `SETUP.md` for operational walkthrough.
