# swapService — Functionality & Security Evaluation

**Date:** 2026-06-14
**Scope:** Full source tree under `src/` plus entrypoint, helper scripts, configuration, and documentation.
**Method:** Manual static review of the code paths that move funds (Solana USDC ↔ Nexus USDD), the SQLite state layer, the polling/maintenance loop, startup recovery, and the supporting docs. No live/dynamic testing was performed (the service requires a funded Solana vault keypair, a Nexus node + CLI, and RPC access that are not available in this environment).

This document is a fresh, independent assessment. It complements—rather than restates—the existing [`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) (a prior code/doc audit, mostly addressed) and [`SECURITY.md`](SECURITY.md) (an operator hardening guide). Where this review found issues **not** covered by those documents, they are flagged as **new**.

> **Update (2026-06-14):** Most findings below have since been remediated in code. See [§8 Resolution Status](#8-resolution-status-2026-06-14) for the per-finding state and what remains open. The body of the report (§3–§4) preserves the original findings for context.
>
> **Reading order (2026-06-15):** this document is the **code-level** history (F/H/L/N/R
> findings, §1–§11). Line numbers in §3–§4 refer to the pre-fix code and no longer match.
> For the current state of the service, start with `RISK_ASSESSMENT.md`.
>
> **See also (2026-06-15):** [`RISK_ASSESSMENT.md`](RISK_ASSESSMENT.md) — a whole-system risk assessment (trust model, solvency, availability, economics, operator tooling). It reports **five further Critical fund-loss / unbacked-mint defects** not covered here, and is the document to read before any mainnet deployment.

---

## 1. Executive Summary

swapService is a bidirectional, custodial bridge between USDC on Solana and USDD on Nexus. The architecture is thoughtfully designed: explicit per-item state machines, idempotency markers persisted in SQLite, on-chain "waterline" checkpoints for crash recovery, quarantine accounts for unrecoverable funds, bounded retry attempts with cooldowns, per-RPC-call timeouts, and a watchdog around each poller. SQL is parameterized throughout (no SQL-injection surface), and external CLI calls avoid the shell.

However, the **implementation does not yet match the design's safety guarantees**. The review found several defects that range from *"the service cannot start cleanly on a fresh deployment"* to *"the two headline solvency-protection mechanisms are dead code that silently never executes."* Two of the most important safety features advertised in `SECURITY.md`—the **backing-deficit auto-pause** and the **double-mint balance reconciliation**—call functions that do not exist and are swallowed by broad `except` handlers, so they never run.

**Overall verdict:** The codebase is a solid *architectural* foundation but is **not production-ready** in its current state. The Critical and High findings below should be resolved, and an end-to-end test on devnet/testnet should be performed, before this handles real funds.

| Severity | Count |
|----------|-------|
| 🔴 Critical | 3 |
| 🟠 High | 4 |
| 🟡 Medium | 5 |
| 🔵 Low / Hardening | 6 |

---

## 2. What Works Well (Strengths)

These are genuine positives observed during the review:

- **Clear modular separation.** `config` / `state_db` / `solana_client` / `nexus_client` / `swap_solana` / `swap_nexus` / `fees` / `startup_recovery` / `balance_reconciler` each own a coherent slice of responsibility.
- **Explicit state machines.** Deposit and credit lifecycles use named statuses (`ready for processing`, `debited, awaiting confirmation`, `to be refunded`, `to be quarantined`, etc.), which makes the flow auditable.
- **Idempotency markers.** `processed_sigs` / `processed_txids` / `refunded_sigs` / `quarantined_sigs` tables guard against replay, and `send_usdc_to_token_account_with_sig()` short-circuits on a recognized `nexus_txid:` memo (`solana_client.py:1460`).
- **Front-running resistance.** USDD→USDC resolves the receival account by **both** `txid_toService` *and* `owner`, and re-checks `asset_owner == owner` before paying out (`swap_nexus.py:117`). Good defense-in-depth.
- **Parameterized SQL everywhere** — no string interpolation into queries; no SQL-injection surface.
- **No shell usage** — every `subprocess.run(...)` receives an argv list, not `shell=True`, eliminating classic shell-injection.
- **Resilience scaffolding** — per-call RPC timeouts (`_rpc_call`), a per-poller watchdog (`main._run_with_watchdog`), bounded attempts (`should_attempt`/`record_attempt`), waterline-based recovery, and graceful SIGINT/SIGTERM shutdown.
- **DoS/dust handling** — minimum thresholds, 100%-fee micro policy, per-loop caps, and owner-lookup skipping for micro credits.
- **Pinned dependencies** — `requirements.txt` pins exact versions (`solana==0.36.9`, `solders==0.26.0`, `requests==2.32.3`, `python-dotenv==1.0.1`), aiding reproducibility.

---

## 3. Functional Evaluation

### 🔴 F-1 (Critical, NEW) — Database schema is never initialized

**Where:** `state_db.init_db()` is defined (`state_db.py:8`) but **never called** anywhere in the codebase. A repo-wide search finds only the definition and a reference in `.github/copilot-instructions.md`; neither `swapService.py`, `src/main.py`, nor any module invokes it, and there is no `src/__init__.py`.

**Impact:** `sqlite3.connect()` creates an *empty* database file if one does not exist, but the tables (`unprocessed_sigs`, `processed_txids`, `fee_entries`, …) are only created inside `init_db()`. On a fresh deployment the first query raises `sqlite3.OperationalError: no such table: …`. Because most state calls are wrapped in broad `try/except` that print and continue, the service will appear to "run" (heartbeat, loop) while **silently processing nothing and persisting nothing**. `swap_service.db` is also not shipped in the repo, so there is no pre-baked schema to fall back on.

**Fix:** Call `state_db.init_db()` once at startup (e.g., the first line of `main.run()`), and make table creation idempotent (it already uses `CREATE TABLE IF NOT EXISTS`).

---

### 🔴 F-2 (Critical, NEW) — Backing-deficit auto-pause is dead code

**Where:** `fees.maintain_backing_and_bounds()` is called every loop iteration (`main.py:169`) and is the mechanism that is supposed to **pause swaps when the vault falls below `BACKING_DEFICIT_PAUSE_PCT`% of circulating USDD** (`SECURITY.md` "Backing & Reconciliation"). It calls `nexus_client.get_circulating_usdd_units()` (`fees.py:201`), **but that function does not exist** — the real function is `get_circulating_usdd()` (`nexus_client.py:652`).

**Impact:** Every invocation raises `AttributeError`, which is caught by the function's own `except` (`fees.py:212`) and returns `False` ("do not pause"). **The solvency circuit-breaker never fires.** If the vault is ever drained or under-collateralized, the service keeps minting/paying out instead of halting. This is the single most important safety control in the design, and it is inert.

**Fix:** Rename the call to `get_circulating_usdd()` (units are already returned as an int). Add a unit/integration test that forces a deficit and asserts `maintain_backing_and_bounds()` returns `True`.

---

### 🔴 F-3 (Critical, NEW) — Double-mint balance reconciliation is dead code

**Where:** `main.py:136` (startup) and `main.py:200` (every ~10 min) call `balance_reconciler.run_balance_reconciliation(dry_run=True)`. **`balance_reconciler.py` defines no such function** — its public entry points are `reconcile_account_trades`, `reconcile_multiple`, and `run_single`.

**Impact:** Both calls raise `AttributeError`, caught by surrounding `try/except`, so the advertised double-mint / surplus-USDD detection (`README.md` "Loop-Safety", `SECURITY.md`) **never runs**. The operator is shown either "Balance reconciliation error" once at startup or nothing thereafter, and silently loses the detection capability.

**Fix:** Implement `run_balance_reconciliation(dry_run=...)` (or repoint the call sites at the existing reconciliation API) and surface non-zero deltas as alerts rather than swallowing them.

---

### 🟠 F-4 (High, NEW) — Refund/quarantine confirmations effectively never finalize

**Where:** `check_sig_confirmations(100, 8.0)` and `check_quarantine_confirmations(100, 8.0)` are called with `min_confirmations = 100` (`swap_solana.py:73,77`). Both read the numeric `confirmations` field from `getSignatureStatuses` and treat `None` as "not confirmed yet" (`solana_client.py:1025-1046`, `1102-1123`).

**Impact:** Solana's `getSignatureStatuses` caps `confirmations` at ~32 and then returns **`null` once the transaction is rooted/finalized** (and the whole entry drops out of the status cache after ~150 slots unless `searchTransactionHistory` is set). So the value will essentially never reach 100, and once finalized it becomes `None` → the code skips it forever. Refund and quarantine rows stay `awaiting confirmation` indefinitely, and the originating `unprocessed_sig` is never removed (removal only happens after "confirmation"). This is a state leak and, combined with **S-2**, contributes to double-payout risk.

**Fix:** Treat `confirmationStatus == "finalized"` (or `confirmations is None` *with a present status*) as confirmed, and use a realistic threshold (e.g., `confirmed`/`finalized` commitment) instead of a raw count of 100.

---

### 🟡 F-5 (Medium, NEW) — Periodic backing-reconcile mint targets a non-existent function

**Where:** `main.py:189` calls `nexus_client.debit_usdd(config.NEXUS_USDD_FEES_ACCOUNT, surplus, 'FEE_RECONCILE')`. There is no `debit_usdd()` in `nexus_client` (the closest is `debit_usdd_with_txid()`).

**Impact:** The surplus→fees mint path raises `AttributeError` (swallowed). The feature is gated behind `BACKING_SURPLUS_MINT_THRESHOLD_USDC_UNITS > 0` and "no pending deposits", so it is not always reached, but when it is, it never works.

**Fix:** Call `debit_usdd_with_txid(...)` (and handle the returned `(ok, txid)` tuple) or add a thin `debit_usdd()` wrapper.

---

### 🟡 F-6 (Medium) — Fee-conversion path references several non-existent functions

**Where:** `fees.process_fee_conversions()` calls `nexus_client.get_circulating_usdd_units()` (`fees.py:107`) and `nexus_client.mint_usdd_to_local()` (`fees.py:127`), neither of which exist.

**Impact:** If an operator ever sets `FEE_CONVERSION_ENABLED=true`, this path throws immediately. It is `false` by default, so this is latent rather than active, but it is a trap for operators who enable the documented feature.

**Fix:** Reconcile the helper names, or remove/guard the fee-conversion scaffolding until it is implemented and tested.

---

### Functional issues already noted in `AUDIT_FINDINGS.md` and still present

- **PROC-1 (Medium):** A single failed/timed-out Nexus debit immediately marks the deposit `to be refunded` with no debit-level retry (`solana_client.py:681-685`). This is more dangerous than the prior audit implies — see **H-3** below.
- **API-2 (Low):** Memo creation uses `Memo111…` while the Helius parser only checks `MemoSq4g…` (`solana_client.py:274` vs `1174`); a deposit using the newer memo program may be missed in the fast path.

---

## 4. Security Evaluation

### 🟠 H-1 (High, NEW) — Vault key, state DB, and fee journals are not git-ignored

**Where:** `.gitignore` ignores `.env` (line 135) and mypy artifacts, but **does not ignore `vault-keypair.json`, `swap_service.db`, `fees_state.json`, or `fee_events.jsonl`** — all of which the docs instruct operators to place in the repo root. `SETUP.md`/`README.md` even show `solana-keygen new -o ./vault-keypair.json`.

**Impact:** Following the documentation puts the **Solana vault private key** at the default path used by setup commands, where it is *not* excluded from version control. A single `git add -A` could commit the key that controls all vault USDC. The state DB and fee journals (which contain user addresses and amounts) are similarly exposed.

**Fix:** Add to `.gitignore`:
```gitignore
vault-keypair.json
*.json.key
*.db
swap_service.db
fees_state.json
fee_events.jsonl
```
Store the key outside the working tree (the documented `0600` guidance is good but not enforced by `.gitignore`).

---

### 🟠 H-2 (High, NEW) — Crash window allows double refunds (recovery scans the wrong memo prefix)

**Where:** The active refund path is `send_usdc(from_address, net_amount, memo=f"refund:{sig}")` (`solana_client.py:818`). Idempotency for that path relies entirely on a DB status flip to `refund sent, awaiting confirmation` that happens **after** the on-chain send. `send_usdc()` itself performs no on-chain idempotency check. Meanwhile startup recovery only reconstructs refund markers from memos prefixed **`refundSig:`** (`solana_client.py:1844`, `1714`), and the alternate helper `refund_usdc_to_source()` (unused by the main loop) is the only one that writes that prefix.

**Impact:** If the process crashes/restarts in the window between sending the refund and writing the DB status, the deposit row is still `to be refunded`, recovery cannot match the `refund:` memo it actually wrote, and the next loop **refunds again** — a direct loss of vault USDC. This is narrow but real, and **F-4** (rows never leaving `awaiting confirmation`) keeps related state alive longer than expected.

**Fix:** Make the refund send memo and the recovery scanner use the *same* prefix, and add an on-chain idempotency pre-check (scan for an existing `refund:<sig>` memo before sending), mirroring the `nexus_txid:` short-circuit already used on the USDD→USDC side.

---

### 🟠 H-3 (High) — Short debit timeout can desynchronize Nexus state

**Where:** `debit_usdd_with_txid()` and `debit_account_with_txid()` run the Nexus CLI with `timeout=5` (`nexus_client.py:167`, `322`), whereas `transfer_usdd_between_accounts()` uses `timeout=30` (`nexus_client.py:297`).

**Impact:** A debit that takes longer than 5 s is killed locally and reported as failure, but the Nexus node may have **already accepted/queued the transaction**. The caller then marks the deposit `to be refunded` (per PROC-1) — so the user can receive **both** the (eventually-confirmed) USDD debit **and** a USDC refund. This is a classic "timeout ≠ failure" double-spend on financial RPCs.

**Fix:** Use a generous, consistent timeout for state-changing calls; on timeout, treat the result as *unknown* and verify via `was_usdd_debited_to_account_for_amount()` / txid lookup before refunding. Apply attempt-tracking to the debit stage (addresses PROC-1 too).

---

### 🟡 H-4 (Medium, NEW) — `NEXUS_PIN` is passed as a command-line argument

**Where:** Every state-changing Nexus call appends `f"pin={config.NEXUS_PIN}"` to the argv (e.g., `nexus_client.py:166`, `295`, `319`, `544`, `722`).

**Impact:** Process arguments are world-readable on Linux via `/proc/<pid>/cmdline` and `ps -ef`. Any local user (or a compromised side-car/monitoring agent) can read the live PIN while a debit is in flight. `README.md` states the service "masks the PIN in CLI logs," but that only addresses *application* logging — it does not protect the OS process table. The PIN combined with the session is sufficient to move USDD.

**Fix:** Prefer passing the PIN via stdin or an environment variable the CLI can read, or via a Nexus session unlocked out-of-band, so it never appears in argv. At minimum, document the local-user trust requirement explicitly.

---

### 🟡 H-5 (Medium) — SQLite concurrency: WAL is documented but never enabled; cross-connection races

**Where:** Every `state_db` helper opens its own `sqlite3.connect(DB_PATH)` with no `PRAGMA journal_mode=WAL`, no `busy_timeout`, and no shared connection. `SECURITY.md` claims "SQLite database with WAL mode provides crash-safe persistence," but **WAL is never set in code**. Pollers and maintenance run inside watchdog/`_safe_call` threads.

**Impact:** Under concurrent access the default rollback-journal mode can raise `database is locked`, and `next_reference()` (`state_db.py:1003`) does an `UPDATE … value+1` then a separate `SELECT` on different connections — two interleaved callers can read the same value, yielding **duplicate or skipped Nexus debit references**. References feed idempotency, so collisions weaken replay protection.

**Fix:** Enable `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=…;` on connect, and make `next_reference()` atomic (single `UPDATE … RETURNING value`, or guard with a process-level lock).

---

### 🟡 H-6 (Medium) — Connection leak in `mark_quarantined_sig`

**Where:** `state_db.mark_quarantined_sig()` calls `conn.commit()` but **omits `conn.close()`** (`state_db.py:460`); the next `def` begins immediately. It also returns `None` rather than a status.

**Impact:** Each USDC-side quarantine leaks a SQLite connection/file handle. Over a long-running process with many quarantines this can exhaust file descriptors and aggravates the locking issues in **H-5**.

**Fix:** Add `conn.close()` (ideally wrap all `state_db` helpers in `with closing(sqlite3.connect(...))` or a context-managed helper).

---

### 🔵 Low / Hardening findings

- **L-1 (NEW) — Broad exception swallowing reduces visibility.** Many fund-path helpers (`get_account_info`, `find_asset_*`, the confirmation checkers, `maintain_backing_and_bounds`) catch `Exception` and return a benign default. This is what allowed F-2/F-3/F-5 to hide. Log at `WARNING`/`ERROR` with the exception type, and let truly unexpected errors surface.
- **L-2 (NEW) — Argument-injection defense-in-depth.** User-influenced values (the `nexus:<addr>` memo, asset `receival_account`) are passed as `key=value` argv tokens to the Nexus CLI. The no-shell design makes classic injection unlikely, and validators (`is_valid_usdd_account`, `is_valid_usdc_token_account`) gate most uses, but the address is interpolated into argv *before* some validations. Validate against a strict base58/charset+length whitelist immediately on extraction.
- **L-3 — ✅ FIXED.** `update_heartbeat_asset` may dereference `None`. `data.get("success")` runs on the result of `_parse_json_lenient`, which can return `None` (`nexus_client.py:741`); it's inside a `try`, so it degrades to "False" rather than crashing, but it's fragile.
- **L-4 (NEW) — Type/contract sloppiness.** `balance_reconciler._fee_net_usdd()` is annotated `-> int` but returns a float division result (`balance_reconciler.py:65-71`); `mark_quarantined_sig` returns nothing. These small mismatches make the code harder to reason about.
- **L-5 — Logging is `print`-based.** No levels, no rotation, no structured fields; secrets could appear in tracebacks. Adopt the `logging` module with redaction (aligns with `SECURITY.md` "Logging & Monitoring").
- **L-6 — 🟡 PARTIALLY ADDRESSED (`tests/test_smoke.py` added; no CI yet).** No automated tests. There is no test suite; every finding above (especially the dead-code calls F-2/F-3/F-5) would be caught by even minimal smoke/import tests or a `python -c "import src.main"` CI step plus a devnet end-to-end run.

---

## 5. Findings Summary

| ID | Severity | Area | Title | Location |
|----|----------|------|-------|----------|
| F-1 | 🔴 Critical | Functionality | DB schema never initialized (`init_db` uncalled) | `state_db.py:8`; `main.py` |
| F-2 | 🔴 Critical | Safety | Backing-deficit auto-pause is dead code | `fees.py:201`; `main.py:169` |
| F-3 | 🔴 Critical | Safety | Double-mint reconciliation is dead code | `balance_reconciler.py`; `main.py:136,200` |
| H-1 | 🟠 High | Secrets | Vault key / DB / fee journals not git-ignored | `.gitignore` |
| H-2 | 🟠 High | Funds | Crash window → double refund (memo-prefix mismatch) | `solana_client.py:818,1844` |
| H-3 | 🟠 High | Funds | 5 s debit timeout can desync Nexus state | `nexus_client.py:167,322` |
| F-4 | 🟠 High | Functionality | Refund/quarantine confirmations never finalize | `solana_client.py:1025,1102`; `swap_solana.py:73,77` |
| H-4 | 🟡 Medium | Secrets | `NEXUS_PIN` exposed in process args | `nexus_client.py` (multiple) |
| H-5 | 🟡 Medium | Integrity | WAL never enabled; `next_reference` race | `state_db.py` (all connects) |
| H-6 | 🟡 Medium | Integrity | Connection leak in `mark_quarantined_sig` | `state_db.py:460` |
| F-5 | 🟡 Medium | Functionality | Reconcile mint calls non-existent `debit_usdd` | `main.py:189` |
| F-6 | 🟡 Medium | Functionality | Fee-conversion path calls non-existent helpers | `fees.py:107,127` |
| L-1…L-6 | 🔵 Low | Various | See section 4 | — |

---

## 6. Prioritized Recommendations

**Before touching real funds (blockers):**
1. Wire up `state_db.init_db()` at startup (**F-1**).
2. Fix the two dead safety controls — `get_circulating_usdd()` (**F-2**) and a real `run_balance_reconciliation()` (**F-3**) — and make their failures *loud*.
3. Fix confirmation finalization (**F-4**) and the refund double-pay window/memo-prefix mismatch (**H-2**).
4. Make state-changing Nexus calls timeout-safe and verify-before-refund (**H-3**).
5. Harden `.gitignore` and move the vault key out of the working tree (**H-1**).

**Soon after:**
6. Stop passing the PIN in argv (**H-4**); enable WAL + atomic references (**H-5**); fix the connection leak (**H-6**); repair/guard the fee-conversion + reconcile-mint paths (**F-5/F-6**).

**Process / quality:**
7. Replace blanket `except: pass`/silent defaults with leveled logging (**L-1, L-5**).
8. Add a minimal CI: import-all smoke test (would have caught F-2/F-3/F-5/F-6 instantly), unit tests for fee math and state transitions, and a scripted devnet/testnet end-to-end run of both swap directions plus the refund and quarantine paths.

---

## 7. Caveats

This was a static review; findings about runtime behavior (e.g., `getSignatureStatuses` returning `null`, "no such table" on first query) are based on reading the code and the documented behavior of the Solana RPC and SQLite, not on executing the service. A live test pass on devnet/testnet is strongly recommended and would likely surface additional edge cases in the multi-stage refund/quarantine state machines. Line numbers in §3–§5 refer to the original (pre-fix) repository state.

---

## 8. Resolution Status (2026-06-14)

The following fixes were applied on branch `claude/elegant-bohr-kqlicw`. Each was byte-compiled; `state_db` changes were additionally exercised with a functional test (schema creation → 17 tables, WAL active, monotonic references, no connection leak).

| ID | Status | What changed |
|----|--------|--------------|
| F-1 | ✅ Fixed | `main.run()` now calls `state_db.init_db()` before any state access. |
| F-2 | ✅ Fixed | `fees.py` calls the real `nexus_client.get_circulating_usdd()`; the backing-deficit pause path executes. |
| F-3 | ✅ Fixed | Implemented `balance_reconciler.run_balance_reconciliation(dry_run=...)` (+ `_distinct_mint_recipient_accounts`), reusing `reconcile_account_trades` to flag accounts with positive trade delta. |
| F-4 | ✅ Fixed | Confirmation checks now key off `confirmationStatus` (`finalized`/`confirmed`), with the numeric count as a fallback — finalized refunds/quarantines no longer stall forever. |
| F-5 | ✅ Fixed | Reconcile-mint calls `debit_usdd_with_txid` with base→token unit conversion and proper `(ok, txid)` tuple handling. |
| F-6 | ✅ Fixed | Added `nexus_client.mint_usdd_to_local()`; `get_circulating_usdd` rename also covers the fee-conversion path. |
| H-1 | ✅ Fixed | `.gitignore` now excludes `vault-keypair.json`, `*-keypair.json`, `*.db`, `swap_service.db`, `fees_state.json`, `fee_events.jsonl`. |
| H-2 | ✅ Fixed | Active refund/quarantine sends now use the `refundSig:` / `quarantinedSig:` memo prefixes that startup recovery scans, plus an on-chain `find_signature_with_memo` pre-check before sending (closes the crash-window double-pay). |
| H-3 | 🟡 Mitigated | Debit CLI timeouts raised from 5 s to `NEXUS_CLI_TIMEOUT_SEC` (≈30 s), drastically reducing spurious timeouts. **Open:** a definitive "verify-on-chain before refunding after an ambiguous timeout" (and debit-stage attempt gating, PROC-1) is still recommended. |
| H-5 | 🟡 Mitigated | WAL enabled persistently in `init_db()`; `next_reference()` now serializes via `BEGIN IMMEDIATE` + `busy_timeout`. **Open:** a global per-connection `busy_timeout` (connection factory) is still worth rolling out across all `state_db` helpers. |
| H-6 | ✅ Fixed | `mark_quarantined_sig` now closes its connection. |
| H-4 | ⚠️ Open (by design) | `NEXUS_PIN` is still passed as a CLI argument. Changing this safely depends on what the Nexus CLI build supports (stdin/env/session-unlock) and could break all money operations if guessed; left for the operator to wire up. Documented as a local-host trust requirement. |
| L-1…L-6 | ◻️ Open | Hardening items (structured logging, address-charset validation, type-annotation cleanups, automated CI/tests). Recommended but not blocking. |

**Net effect:** all three Critical and three of four High findings are fully resolved; the remaining High (H-3) is materially mitigated. The two intentionally-deferred items (H-4, and the L-series) are documented with rationale. A devnet/testnet end-to-end run remains the recommended final gate before handling real funds.

---

## 9. Second-Pass Review (2026-06-14, post-fix)

A fresh review of the patched tree was performed — including the patches themselves. It surfaced one pre-existing Critical bug that the §8 fixes *activated*, plus a performance regression introduced by the H-2 fix. Both are now resolved.

| ID | Severity | Status | Detail |
|----|----------|--------|--------|
| N-1 | 🔴 Critical | ✅ Fixed | **Unit mismatch in backing/solvency math.** `nexus_client.get_circulating_usdd()` returns *token units* (Nexus `currentsupply` is human-readable, e.g. `4002.0` — confirmed in `Nexus API docs/COMMANDS/FINANCE.MD:395`), but it was compared against and subtracted from the *base-unit* vault balance in `fees.maintain_backing_and_bounds` and `main.run` (reconcile + metrics). The mismatch is a factor of `10**USDD_DECIMALS` (1e6). This was latent before because those call paths were dead code; fixing **F-2/F-5** made them live, turning the reconcile-mint surplus into a ~1e6 over-estimate (it would mint roughly the entire vault balance as USDD). **Fix:** added `nexus_client.get_circulating_usdd_units()` (base units) and pointed all backing math at it; `get_circulating_usdd()` is retained for display only. |
| N-2 | 🟡 Medium | ✅ Fixed | **Per-refund signature scan (introduced by H-2).** The H-2 on-chain idempotency pre-check ran `find_signature_with_memo` (up to ~50 `getTransaction` calls) on *every* refund and quarantine, which could blow the 8 s processing budget under load. **Fix:** the scan is now gated behind `get_attempt_count(...) > 0` and an attempt is recorded before sending, so the common first-attempt path skips the scan while crash/retry recovery still works. |
| N-3 | 🔵 Low | ◻️ Open | `run_balance_reconciliation` (the new F-3 code) scans all mint-recipient accounts over full history with `waterline_ts=0`; cost is roughly O(accounts × table-size). It is read-only and the periodic call is wrapped in a 15 s `_safe_call`, but the startup call is unguarded. Consider a bounded default waterline if the DB grows large. |
| N-4 | 🔵 Low | ◻️ Open | Startup line `USDD Circulating Supply: {usdd_amount} USDD )` has a stray `)` and `get_circulating_usdd()` truncates fractional supply (`int(dec)`) — cosmetic/display only; backing math is unaffected (it uses the new base-unit accessor). |

**Verification (this pass):** all edited files byte-compile; `state_db` schema/WAL/reference/quarantine behavior was exercised against a temp DB (17 tables, WAL active, monotonic references, no leak); every cross-module reference and the math-vs-display split for circulating supply were confirmed by static checks. Full runtime/import and end-to-end testing still require the Solana/Nexus dependencies and live RPC, so a devnet/testnet pass remains the recommended final gate.

**Revised net effect:** Critical F-1/F-2/F-3 and the newly-found Critical N-1 are resolved; High H-1/H-2/H-6 resolved, H-3/H-5 mitigated; Medium N-2 resolved. Remaining open items are H-4 (by design), N-3/N-4 (low), and the L-series hardening — none blocking, but the live test gate stands.

---

## 10. Third-Pass Review — Solana RPC Efficiency (2026-06-14)

A targeted pass on Solana RPC usage. It found one Critical correctness bug in the deposit-ingestion path (which is also the largest source of wasted RPC calls) plus several efficiency issues.

| ID | Severity | Status | Detail |
|----|----------|--------|--------|
| R-1 | 🔴 Critical | ✅ Fixed | **Deposit ingestion was broken.** `poll_solana_deposits` passed the Helius result (a list of **tuples** `(sig, ts, memo, from, amount)`) into `process_filtered_deposits`, which calls `tx_info.get("signature")` — tuples have no `.get`, so it raised `AttributeError` every non-micro cycle (caught by the poll's outer handler → `POLL_SOLANA_ERROR`). Net effect: **new USDC deposits were never ingested**, so USDC→USDD swaps silently stopped. **Fix:** added `process_helius_deposits()` which consumes the tuples directly; `poll_solana_deposits` now calls it. |
| R-2 | 🟠 High (efficiency) | ✅ Fixed | **Enriched fast path was negated by a re-fetch.** Even had R-1 not crashed, `process_filtered_deposits` re-fetched **every** deposit with `get_transaction` (N extra calls) to extract memo/sender — data the Helius enriched call already returned. The new `process_helius_deposits()` does **zero** per-deposit RPC, keeping ingestion at the 1–2 enriched calls (or the core-RPC fallback). |
| R-3 | 🟡 Medium | ✅ Fixed | **No connection reuse.** 18 call sites each constructed `Client(config.RPC_URL)`, so solana-py opened a fresh `requests.Session` (new TCP/TLS) per call. **Fix:** a process-wide `_get_client()` singleton; all sites reuse it (HTTP keep-alive / pooling). |
| R-6 | 🟡 Medium | ✅ Fixed | **Every send blocked on confirmation.** `_build_and_send_legacy_tx` called `confirm_transaction` inline after each send, blocking the loop (up to `SOLANA_RPC_TIMEOUT_SEC`) even though refunds, quarantines, and USDD→USDC sends all have dedicated confirmation passes. Under a backlog this capped throughput at a few sends per cycle. **Fix:** removed the inline confirm; confirmations are tracked by the existing passes. |
| R-4 | 🟠 High (efficiency) | ✅ Fixed (§11) | **USDD→USDC confirmation was O(N×50).** `process_unprocessed_txids` Priority 3 confirms each awaiting send via `find_signature_with_memo`, which scans up to 50 `getTransaction` calls **per item, per cycle**. The send already returns its signature (`send_usdc_to_token_account_with_sig`), but it is discarded because `unprocessed_txids` has no `sig` column, forcing the memo re-scan (the Priority 2 "recovery" path exists for the same reason). **Recommend:** add a `sig` column, persist the returned signature, and confirm via a single `get_signature_statuses` — replacing ~50 calls/item with 1. (Schema change; deferred to avoid a migration in this pass.) |
| R-5 | 🔵 Low | ✅ Fixed (§11) | `check_sig_confirmations` / `check_quarantine_confirmations` call `get_signature_statuses([one_sig])` per row. `getSignatureStatuses` accepts up to 256 sigs per call — batching turns N calls into 1. |
| R-7 | 🔵 Low | ✅ Fixed (§11) | The vault USDC balance is fetched via separate `get_token_account_balance` RPCs ~5×/loop (poll start + end, `maintain_backing_and_bounds`, reconcile, metrics). A short per-cycle cache (or fetch-once-and-pass) would cut redundant calls. |
| R-8 | 🔵 Low | ✅ Fixed (§11) | `process_filtered_deposits` and `fetch_filtered_token_account_transaction_history` (both N+1) are now unused by the poll loop and are candidates for removal to avoid future mis-wiring. |

**Effect on the per-cycle RPC budget (Helius configured, steady state):** deposit ingestion drops from *broken / N+1* to the **1–2 enriched calls**; sends no longer each block on a confirmation round-trip; and all calls now share one keep-alive connection. The largest remaining win is **R-4** (persist the send signature) — recommended next, but it needs a small schema migration so it was left for a dedicated change.

**Verification (this pass):** all edited files byte-compile; the new `process_helius_deposits` tuple-ingestion + idempotency was exercised against a temp DB; `_get_client()` confirmed to be the sole client constructor (no recursion) with all 18 sites reusing it. Runtime/RPC behavior still needs a live devnet/testnet run.

---

## 11. Fourth Pass — R-4 / R-5 / R-7 / R-8 Implemented (2026-06-15)

The remaining RPC-efficiency recommendations from §10 are now implemented.

| ID | What changed |
|----|--------------|
| **R-4** | Added a `sig` column to `unprocessed_txids` (with an `init_db()` migration that `ALTER`s pre-existing DBs). The USDD→USDC send now persists the returned signature (`update_unprocessed_txid(..., sig=sig)`), and Priority 3 confirms it with a single `get_signatures_confirmation([sig])` instead of `find_signature_with_memo` (which scanned up to 50 `getTransaction` calls per item per cycle). Memo-scan remains only as a crash/legacy fallback when no `sig` is recorded; the previously-dead "lost signature" recovery was repaired to use the `nexus_txid:` memo and persist the recovered sig. **~50 calls/item → 1.** |
| **R-5** | Added `get_signatures_confirmation(sigs, min_confirmations)` which batches `getSignatureStatuses` (up to 256 sigs/call) and converts to `Signature` objects (the old per-row calls passed raw strings). `check_sig_confirmations` and `check_quarantine_confirmations` now make **one batched call** instead of one RPC per awaiting row, and key off `confirmationStatus`. |
| **R-7** | `get_token_account_balance` gained an opt-in `max_age_sec` short cache (successful reads only). The maintenance reads (`maintain_backing_and_bounds`, reconcile, metrics, fee-conversion) pass `max_age_sec=5` so coincident vault reads in one loop collapse to a single RPC; the deposit-delta poll and startup deliberately stay uncached for freshness. |
| **R-8** | Removed the now-unused `process_filtered_deposits` and `fetch_filtered_token_account_transaction_history` (legacy N+1 paths) to prevent future mis-wiring. |

**Verification (this pass):** all `src/*.py` byte-compile; a temp-DB test confirmed the fresh schema has `sig`, the migration adds `sig` to a legacy table, and `add/update/get_unprocessed_txids_as_dicts` round-trip it; static checks confirmed the confirmation functions have no dangling `client` reference, that Priority 3 uses the direct status check with memo-scan only as fallback, and that the removed functions have no remaining references. Live RPC/runtime behavior (and the standing devnet/testnet end-to-end gate) is unchanged and still recommended before real funds.

**Open items remaining:** H-4 (PIN passed via argv — needs the operator's choice of Nexus CLI mechanism), N-3/N-4 (low/cosmetic), and the L-series hardening (structured logging, address-charset validation, CI/tests).
