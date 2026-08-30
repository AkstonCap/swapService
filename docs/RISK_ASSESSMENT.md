# swapService — Whole-System Risk Assessment

**Date:** 2026-06-15
**Subject:** Custodial USDC (Solana) ↔ USDD (Nexus) bridge
**Scope:** The bridge as a *system* — trust model, solvency, fund-flow correctness, availability, economics, operator tooling — rather than a line-by-line code audit.
**Companion:** [`EVALUATION.md`](EVALUATION.md) holds the code-level findings and their fix history (§1–§11). This document covers systemic risk; where a risk is caused by a specific defect, the defect is cited.

> **Resolution update (2026-08-24):** the three Critical findings from
> [`DEVELOPMENT_REVIEW_2026-08-24.md`](DEVELOPMENT_REVIEW_2026-08-24.md)
> are repaired in the working tree with focused regression tests: incomplete
> Nexus lookups hold, unresolved deposits are subtracted as liabilities,
> automatic non-idempotent surplus actions are disabled, and failed Nexus
> enumeration cannot advance the waterline. Backing checks now fail closed.
> Deployment remains blocked pending independent/live verification and the
> remaining High findings.
>
> **Weekly review update (2026-08-28, `f614897`):** the original negative-lookup,
> unresolved-liability, explicit enumeration-failure, fail-open backing and mixed-decimal
> defects are closed in local logic, and the complete suite/CI are green. A durable,
> operator-only Nexus transfer ledger now provides strong at-most-once local controls, but
> automatic execution remains disabled pending crash-boundary and target-node evidence.
> Deployment is still hard-blocked: empty Nexus enumeration can advance the checkpoint
> without proven stable-range semantics, and reconciliation remains High because startup
> can print a green result when the producer is unhealthy and no authoritative remote
> transaction-history read-back has been demonstrated. See
> [`DEVELOPMENT_REVIEW_2026-08-28.md`](DEVELOPMENT_REVIEW_2026-08-28.md).
>
> **Weekly review update (2026-08-29, committed `5e7d3b8` plus a separately
> staged proposal):** returned unhealthy startup reconciliation is no longer
> printed as green, interrupted claimed Nexus transfers restart as
> `outcome_unknown`, and explicit production mode requires caps and alerting.
> Deployment remains hard-blocked. Reconciliation failures do not pause new
> exposure. The staged remote read-back now includes active destinations and
> fail-closes when one page cannot prove the requested history boundary, but the
> exact valid first-time-recipient case and target-node boundary/order semantics
> remain unproven. Invalid `SWAP_PRODUCTION_MODE` text also silently disables the
> gate. See
> [`DEVELOPMENT_REVIEW_2026-08-29.md`](DEVELOPMENT_REVIEW_2026-08-29.md).
>
> **Resolution update (2026-08-29, post-review):** invalid present
> `SWAP_PRODUCTION_MODE` text now fails configuration loading instead of falling back to
> development mode. A valid production admission-control rejection returns non-zero from
> the service entrypoint. The historical review above retains the finding as evidence; see
> [`EVALUATION.md`](EVALUATION.md) for the current remediation status.

**Method:** Static review of the money paths, state machine, polling loop, recovery logic, helper tooling, configuration, and documentation, plus targeted reasoning about Solana/Nexus finality and SQLite semantics. Arithmetic and SQL claims were executed in isolation to confirm them. **No live run was possible** — the runtime dependencies (`solana`, `solders`, `python-dotenv`) and RPC/Nexus access are unavailable in this environment.

---

## 1. Verdict

The bridge's *design* is sound in outline: bidirectional state machines, idempotency markers, on-chain waterline recovery, quarantine accounts, backing checks, bounded retries. Recent work (EVALUATION §8–§11) fixed the dead safety controls, the broken deposit ingestion, and the Solana RPC efficiency problems.

This review nonetheless found **five Critical defects that cause silent, unrecoverable fund loss or unbacked minting under ordinary operating conditions** — none of them rare edge cases. Beyond the individual defects, one systemic pattern dominates and is the most important conclusion of this assessment:

> **The documentation describes a materially safer system than the code implements.** Retry cooldowns, USDD-side quarantine, action reservations, on-chain double-debit checks, and roughly twenty tunable settings are documented as protections but are **never invoked**. An operator making risk decisions from `README.md` / `SECURITY.md` / `CONFIG.md` will believe controls are in force that do not exist. See §7.

**Recommendation: do not run this against mainnet funds until Tier 1 is resolved.** B-1, B-2 and B-4 trigger during normal traffic, not under attack.

> ### Status update (2026-06-15)
>
> **All findings B-1 through B-26 are now fixed or mitigated in code**, and the systemic
> "documented but not implemented" gap in §7 is closed: the retry cooldown, USDD
> quarantine, action reservations and on-chain debit verification now actually run, and the
> fee/minimum documentation matches the code.
>
> **B-27 (no tests/CI) is partially addressed.** `tests/test_smoke.py` imports every module,
> calls 22 real functions against a temp DB, and AST-checks call-site arity. It exists
> because a genuine `ImportError` (`from . import state_db, state` — no `state` module)
> survived both byte-compilation and stubbed unit tests, sitting on the critical path of
> the B-1 fix. **There is still no CI runner and no behavioural test suite.**
>
> **The remaining blocker is evidence, not code.** None of this has been exercised against
> a live Solana or Nexus node, and three fixes depend on external API behaviour read from
> documentation but never observed: that the Nexus CLI returns `contracts.reference` on
> `finance/transactions/token` (the whole B-3 resolver rests on it), that it accepts the
> configured heartbeat field names, and that Helius honours `commitment` on
> `getTransactionsForAddress`. Treat the correct claim as **"no known defects remain"**,
> not "verified correct". See §9.

| Tier | Theme | Count |
|------|-------|-------|
| 1 | Fund loss / unbacked mint (Critical) | 5 |
| 2 | Money correctness & finality (High) | 3 |
| 3 | Custody, availability & phantom safeguards (High) | 4 |
| 4 | Economics, operations & tooling (Medium) | 9 |
| 5 | Hygiene (Low) | 5 |

---

## 2. Tier 1 — Critical: Fund Loss & Unbacked Minting

### 🔴 B-1 — Deposits are silently skipped forever whenever payouts exceed deposits — ✅ **FIXED (2026-06-15)**

> **Resolution:** the balance-delta gate is deleted (ingestion now always runs), and waterline advancement moved into `_advance_solana_waterline()`, which enforces the invariant *never pass an unpersisted deposit*: it holds the waterline entirely if enumeration failed, pins it behind the oldest unprocessed deposit when work is pending, advances to `poll_start - safety` only when everything fetched is persisted, and never moves backwards. This also fixed a second instance of the same bug — the end-of-poll call passed its arguments in the wrong order (`update_heartbeat_asset(new_waterline, None, poll_start)`), writing a waterline value into `last_poll_timestamp` and setting the Solana waterline to *now*. All four branches verified by unit test against the real function.

**Where:** `src/swap_solana.py:33-45`

```python
current_bal = solana_client.get_token_account_balance(config.VAULT_USDC_ACCOUNT)
last_bal   = state_db.load_last_vault_balance()
delta      = current_bal - last_bal
skip_new_deposit_fetch = delta < config.MIN_DEPOSIT_SOLANA_UNITS
if skip_new_deposit_fetch:
    state_db.propose_solana_waterline(int(poll_start))
    nexus_client.update_heartbeat_asset(int(poll_start), None, int(poll_start))  # wline_sol = NOW
```

`delta` is used as a proxy for "did a deposit arrive?", but the vault balance moves on **outflows as well as inflows** — USDD→USDC payouts, USDC refunds and quarantine transfers all debit this same account. When outflows ≥ inflows in a cycle, `delta` is small or negative, so the service (a) skips fetching new deposits **and** (b) advances the Solana waterline to *now*.

On the next poll `_fetch_deposits_helius` stops at `ts <= since_ts` (`solana_client.py:241`), so any deposit that landed in the skipped window is **permanently below the waterline and never ingested**. The user's USDC sits in the vault with no USDD minted, no refund, and no record that the deposit ever happened. Nothing recovers it — startup recovery is itself waterline-bounded.

**Likelihood: high.** Any cycle where the bridge pays out more than it takes in — routine for a two-way bridge.

**Fix:** delete the balance-delta heuristic (a single scalar cannot distinguish in from out). Above all, **never advance a waterline on a path that did not fetch and durably persist deposits.**

---

### 🔴 B-2 — The watchdog does not cancel work, so pollers overlap and can double-mint — ✅ **FIXED (2026-06-15)**

> **Resolution, in three layers.** (1) `_run_with_watchdog` now tracks the thread it started per label and **refuses to start a second copy while the previous one is alive** — a thread still cannot be cancelled, but an over-budget poller now only delays its own next run instead of racing itself. (2) The USDC→USDD debit is wrapped in `reserve_action("usdc_to_usdd_debit", sig)`, finally putting the purpose-built reservation table to use; a concurrent worker is refused and skips the item. (3) B-12's singleton lock closes the cross-process variant. Verified by test: with one poller deliberately over budget, a second cycle is refused and only resumes after it finishes; a second `reserve_action` for the same sig returns `False`.

**Where:** `src/main.py:35-50`

```python
thread = threading.Thread(target=_wrapper, daemon=True)
thread.start(); thread.join(budget_sec)
if thread.is_alive():
    print(f"[watchdog] {label} exceeded {budget_sec}s budget; skipping remainder this cycle")
```

`join(timeout)` does not stop a thread. When a poller exceeds its budget the log says "skipping remainder" but **the thread keeps running**; the loop sleeps `POLL_INTERVAL` and starts *another* copy. Under RPC latency, Nexus CLI stalls or backlog, copies accumulate.

The per-item guard is a TOCTOU check — status is read, then a slow CLI validation runs, then the debit executes, and only *afterwards* is the status written:

```python
if state_db.get_unprocessed_sig_status(sig) != "ready for processing": continue  # CHECK
... nexus_client.is_valid_nexus_token_account(nexus_address)                            # slow CLI
result = nexus_client.debit_nexus_token_with_txid(...)                                  # ACT
state_db.update_unprocessed_sig_status(sig, "debited, awaiting confirmation")     # too late
```

Two workers both observe `ready for processing` and **both debit** — minting USDD twice against one deposit.

The purpose-built defence exists and is dead: `state_db.reserve_action()` / `release_reservation()` / `is_reserved()` (`state_db.py:662-724`) — an atomic `INSERT`-based reservation built for exactly this — has **zero call sites**, though the table is created and periodically swept.

`_safe_call` (`main.py:13-32`) has the same flaw: on timeout it raises, but the worker thread continues. A "timed-out" `debit_nexus_token_with_txid` may still execute while the caller treats it as failed.

**Fix:** wrap every money action in `reserve_action()`; make the watchdog cooperative (a stop flag the poller checks); never start a cycle while the previous one is alive.

---

### 🔴 B-3 — A crash or unparsed CLI response double-mints, or mints *and* refunds — ✅ **FIXED (2026-06-15)**

> **Resolution: intent-before-action, then let the chain decide.** A `reference` column was added to `unprocessed_sigs` (with migration). The unique per-attempt reference and a `debit in flight` status are now persisted **before** the CLI is invoked. Any non-definitive outcome — exception, timeout, or the `(False, None)` returned when the call succeeded but the body was unparsable — is recorded as `debit unverified` and **never refunded on that signal alone**.
>
> The batch `find_nexus_debits_by_references()` resolver runs each cycle (before the
> confirmation pass) and resolves those rows against the chain:
>
> | on-chain lookup | action |
> |---|---|
> | reference found | record the txid, proceed — never refund |
> | not found, failed or incomplete lookup | leave held; a bounded negative scan never proves non-execution |
> | no reference recorded (pre-upgrade row) | quarantine for manual review — never guess |
>
> **Superseded safety correction:** the former grace-window retry/refund policy was removed.
> Current code has no `DEBIT_VERIFY_GRACE_SEC`: only positive reference evidence resolves an
> ambiguous debit automatically; all other outcomes remain held for manual resolution.
>
> All five branches verified by test.
>
> **Follow-up hardening:** the unsafe, uncalled
> `was_nexus_debited_to_account_for_amount()` and other single-item history helpers were removed.
> The resolver uses only the batch reference lookup, whose explicit completeness result cannot turn
> a bounded negative scan into permission for another Nexus debit or a Solana-side refund.

**Where:** `src/solana_client.py` (debit step); `src/nexus_client.py:151-177`

The USDD debit executes **before** any durable state write. A crash in that window leaves the row `ready for processing`, so the deposit is **debited again** on restart. Nothing detects it: startup recovery reconstructs only `nexus_txid:` (USDD→USDC) and `refundSig:` memos — there is **no reconstruction of USDC→USDD debits** — and the per-attempt unique `reference` guarantees no on-chain dedup.

Worse, an unparsed response is reported as failure:

```python
code, out, err = _run(cmd, ...)
if code != 0:  return (False, None)
txid = ...parse...
if not txid:   return (False, None)   # the CLI SUCCEEDED; we report failure
```

The caller then marks the deposit `to be refunded` — so the service **mints the USDD and refunds the USDC**: a guaranteed double loss. (The `[DEBIT_NO_TXID]` branch that looks like it handles this is unreachable, since the function already returned `False`.) A CLI timeout is the same class of bug — reduced but not eliminated by the timeout increase in EVALUATION §8.

The former on-chain account/amount double-debit check was uncalled and could not safely compare
Nexus decimal contract amounts with base units. It has been removed; the durable intent/reference
resolver is the only automatic ambiguity path.

**Fix:** persist intent (+ reference) *before* invoking the CLI; on any ambiguous outcome — non-zero exit, unparsed output, timeout — treat state as **unknown** and resolve it against the chain before retrying or refunding.

---

### 🔴 B-4 — The published USDD minimum is 5× below the enforced one; credits in the gap are silently destroyed — ✅ **FIXED (2026-06-15)**

> **Resolution:** three-band classification replaces the single silent `continue`. A new `DUST_CREDIT_USDD` floor (default `0.01`) keeps the anti-DoS behaviour for genuine spam; credits **between the dust floor and `MIN_CREDIT_USDD` are now recorded** — a `processed_txids` row plus a `below_min_credit_usdd` fee entry capturing sender, amount and txid — so the funds are traceable and manually resolvable instead of vanishing. The server-side `where` filters in `swap_nexus.py`, `nexus_client.fetch_deposits_since()` and `startup_recovery.py` were lowered to the dust floor so the gap band is actually fetched, and the three inconsistent hardcoded `100101` fallbacks were removed in favour of the config value. `README.md` (5 places), `CONFIG.md` and `.env.example` now state the real `0.500501` minimum. Band classification and the recording path verified by unit test.
>
> The enforced minimum was **not** lowered to the documented `0.100101`: that value sits below the `FLAT_FEE_USDC` (0.5) on this path, so swaps there net ≤ 0 (see B-13). Whether the gap band should be *refunded* rather than kept as fees is a policy decision left to the operator — the funds are now recorded either way.

**Where:** `src/config.py:104` vs `README.md:17,30,74,133,175`

| Source | Minimum USDD credit |
|---|---|
| `src/config.py:104` | **`0.500501`** |
| `README.md` (5 places), `.env.example:87`, `CONFIG.md:77` | `0.100101` |

A user who follows the README and sends between **0.100101 and 0.500500 USDD** hits `src/swap_nexus.py:691-694`:

```python
if amount_dec < min_credit_threshold:
    # Ignore micro credit entirely: no state writes, no fee accounting.
    continue
```

The credit is dropped with **no DB row, no fee entry, no refund, no log line**. The USDD sits in the treasury with nothing tying it to the sender, and the same threshold is pushed into the server-side query filter, so those credits are never even fetched. `README.md:157` compounds the harm — "Are sub-threshold amounts lost? … do not send below the published minimum" — when the user *did* follow the published minimum.

Three different fallback constants coexist: `swap_nexus.py:691`, `nexus_client.py:845` and `startup_recovery.py:103` all default to `100101`, disagreeing with `config.py`'s own `0.500501`.

**Fix:** reconcile code and docs on one value, and **never drop a credit silently** — record every treasury credit, then classify it (swap / fee / refund).

---

### 🔴 B-5 — The heartbeat waterline field name is inconsistent, so heartbeat updates fail permanently — ✅ **FIXED (2026-06-15)**

> **Resolution:** everything now agrees on `last_safe_timestamp_nexus`, the name already used by `ASSET_STANDARD.md`, `create_heartbeat_asset.py` and the writer — only `config.py` and `.env.example` disagreed, and both were corrected. The writer no longer hardcodes field names; it uses `HEARTBEAT_WATERLINE_NEXUS_FIELD` / `HEARTBEAT_WATERLINE_SOLANA_FIELD`, so the setting is finally honoured. A new `validate_heartbeat_asset()` runs at startup and reports loudly if the asset lacks any field the service will write (listing the names it actually has), instead of letting every update fail silently. The read path uses the configured name with a fallback to the legacy one, and now logs when it halts.

**Where:** `src/config.py:77` vs `src/nexus_client.py:776` vs `create_heartbeat_asset.py:182`

| Source | Nexus waterline field |
|---|---|
| `src/config.py:77` (default) | `last_safe_timestamp_usdd` |
| `.env.example:123` (shipped) | `last_safe_timestamp_usdd` |
| `src/nexus_client.py:776` (**hardcoded**) | `last_safe_timestamp_nexus` |
| `create_heartbeat_asset.py:182` (default) | `last_safe_timestamp_nexus` |

The writer ignores `config.HEARTBEAT_WATERLINE_NEXUS_FIELD` entirely and hardcodes `last_safe_timestamp_nexus=`. An operator who follows the shipped `.env.example` creates an asset carrying `last_safe_timestamp_usdd`; because `format=basic` **locks the field set at creation**, the update writes to a field that does not exist.

The update is a single atomic CLI call, so its failure also drops `last_poll_timestamp` and `last_safe_timestamp_solana`, and `state_db.update_heartbeat()` is never reached. **The heartbeat and both waterlines freeze at their initial values.** The public liveness signal defined in `ASSET_STANDARD.md` stays frozen, and the recovery anchor never advances. The read path (`swap_nexus.py:600`) tolerates *both* names, which masks the write-side breakage.

Combined with B-6 (the poller aborts when the heartbeat is unusable), a fresh deployment can be wedged from day one.

**Fix:** use the configured field name on the write path; validate at startup that the heartbeat asset actually contains the fields the service intends to write, and fail loudly if not.

---

## 3. Tier 2 — High: Money Correctness & Finality

### 🟠 B-6 — Amounts are computed in binary floating point and reach the CLI in scientific notation — ✅ **FIXED (2026-06-15)**

> **Resolution:** the debit path is now exact integer/Decimal arithmetic end to end. `get_nexus_send_amount_units()` returns **base units** via `Decimal` with `ROUND_DOWN`, and `debit_nexus_token_with_txid()` now takes base units and formats them with `_format_nexus_amount()` (fixed-point, never exponent form). All live call sites use base units. The former backing-reconcile mint and `mint_nexus_to_local()` paths were removed because automatic surplus movement is safety-disabled. Verified against the previously-failing inputs:
>
> | deposit (USDC units) | old float string | new CLI string |
> |---|---|---|
> | 100,101 | `8.989999999847731e-07` | `0` |
> | 100,200 | `9.979999999999711e-05` | `0.000099` |
> | 150,000 | `0.04984999999999999` | `0.04985` |
> | 3,333,333 | `3.229999667` (9 dp on a 6-dp token) | `3.229999` |
>
> The fee-tracking scale in `check_unconfirmed_debits` (`int(float * 10**decimals)`) is now integer subtraction of base units. `unprocessed_txids` also gained an exact `amount_usdd_units` column (with migration), populated at insert and preferred for refund amounts.
>
> **Correction to this report's original claim.** It stated that storing money as SQLite `REAL` was *actively* producing off-by-one drift on refunds. On measurement that is wrong: the refund path parses via `Decimal(str(value))`, and `str()` round-trips a float to its shortest exact decimal, so `8.29 → 8290000` correctly. Only the **naive** `int(value * 10**6)` form drifts (`8.29 → 8289999`), and that form appeared in fee tracking, not refunds. The drift claim therefore applied to the fee path (now fixed); the new `amount_usdd_units` column is **defence-in-depth** — it removes the dependence on float repr round-tripping and on every future caller remembering to use `Decimal(str(...))` — not the repair of an active bug. Converting the remaining `REAL` record-keeping columns to `INTEGER` is still worthwhile but is not urgent.

**Where:** `src/nexus_client.py:135-148`; money columns typed `REAL` at `state_db.py:22,66,80,92,105`

`get_nexus_send_amount()` performs float arithmetic and returns a float that is interpolated directly into `f"amount={amount_usdd}"`. Executing the real formula:

| deposit (USDC base units) | string sent to the Nexus CLI |
|---|---|
| 100,101 (the advertised minimum) | `amount=8.989999999847731e-07` |
| 100,200 | `amount=9.979999999999711e-05` |
| 150,000 | `amount=0.04984999999999999` |
| 999,999,999,999 | `amount=998999.899999001` |

Two problems: **scientific notation** that a decimal-amount parser is unlikely to accept, and **17-digit binary artifacts** far below USDD's 6 decimals. The rest of the codebase uses `Decimal` with `ROUND_DOWN` (`swap_nexus.py`, `nexus_client._format_nexus_amount`) — this path is the outlier. Storing balances as SQLite `REAL` compounds it, since amounts round-trip through float and are re-scaled with `int(amount * 10**6)`, producing off-by-one accounting drift.

**Fix:** `Decimal` end-to-end, format via `_format_nexus_amount()`, store integer base units (`INTEGER`).

---

### 🟠 B-7 — USDD is minted against `confirmed` Solana state, not `finalized` — ✅ **FIXED (2026-06-15)**

> **Resolution:** ingestion commitment is now configurable and **defaults to `finalized`** (`SOLANA_DEPOSIT_COMMITMENT`), applied on the Helius path and both core-RPC fallbacks. Settlement of our *own* payouts follows the same strictness — when ingesting at `finalized`, `get_signatures_confirmation()` accepts only `finalized`, so a merely-`confirmed` refund can no longer be marked complete and then reorged away.
>
> For operators who deliberately trade safety for latency, `SOLANA_FINALIZED_ABOVE_UNITS` keeps a hard floor: deposits at or above that size are withheld until finalized even when the global commitment is relaxed.
>
> That carve-out introduced a subtle hazard worth recording: a withheld deposit is **not** written to the DB, so nothing would have kept the waterline behind it — it would have been skipped forever, re-creating B-1 through a different door. `process_helius_deposits()` therefore returns the oldest deferred timestamp and `_advance_solana_waterline()` pins the waterline behind it. Verified by test.

**Where:** `src/solana_client.py:248` (`commitment="confirmed"`)

Deposits are ingested at `confirmed` — supermajority-voted but **not rooted**, and still reorg-able — and USDD is minted on Nexus off the back of it. A reorged-out deposit leaves the USDD unbacked, permanently and irreversibly (Nexus cannot learn of a Solana reorg). Custodial bridges normally credit only on `finalized`, trading ~13 s of latency for irreversibility.

**Fix:** credit at `finalized`; if lower latency is required, allow `confirmed` only below a value threshold.

---

### 🟠 B-8 — The heartbeat asset is both a single point of failure and the trust anchor for scanning — ✅ **FIXED (2026-06-15)**

> **Resolution:** an unreadable heartbeat no longer halts ingestion silently. The poller falls back to the last known-good waterline in the local `heartbeat` table and raises a `heartbeat_unreadable_fallback` alert; only when there is no local fallback does it stop, and then it fires `heartbeat_unreadable` at CRITICAL. A waterline that is somehow ahead of now (corrupt or hand-edited asset) is clamped instead of silently skipping every future deposit.

**Where:** `src/swap_solana.py:26-31`

```python
heartbeat = nexus_client.get_heartbeat_asset()
if not heartbeat: return
wline_sol = heartbeat.get("last_safe_timestamp_solana")
if wline_sol is None: return
```

The entire Solana ingestion path **halts silently** — indistinguishable from "nothing to do" — if the Nexus node is unreachable, the CLI errors, the asset name is misconfigured, or the field is absent (which B-5 makes likely). A Nexus-side outage stops USDC deposit processing while the service still looks healthy.

The waterline is also *self-written on-chain state*: a bad write (as in B-1) is durable, and there is no independent lower bound, no check that it never passes un-ingested deposits, and no clamp on forward jumps.

**Fix:** treat heartbeat failure as an alertable error with a last-known-good local fallback; refuse to advance past the oldest un-persisted deposit; clamp forward jumps.

---

## 4. Tier 3 — High: Custody, Availability & Phantom Safeguards

### 🟠 B-9 — Unmitigated custodial trust concentration

All bridge value sits behind two secrets that live on the service host:

| Secret | Authority | Blast radius |
|---|---|---|
| `vault-keypair.json` | Signs every USDC outflow | Entire USDC vault drainable |
| `NEXUS_PIN` + session | `finance/debit/token from=USDD` — **mint authority** | Unlimited USDD issuance; destroys the peg |

There is **no** multisig, threshold signing or HSM; **no** per-transaction, daily or cumulative withdrawal cap; **no** payout-destination allowlist; and **no** independent proof-of-reserves — the 1:1 invariant is checked only by the service auditing itself (`fees.maintain_backing_and_bounds`), so a compromise that can mint also controls the check that would detect it. `NEXUS_PIN` is additionally passed as a **command-line argument** on every state-changing call, readable by any local user via `/proc/<pid>/cmdline` or `ps` (EVALUATION H-4, still open).

---

### 🟠 B-10 — The documented retry cooldown does not exist — ✅ **FIXED (2026-06-15)**

> **Resolution:** `should_attempt()` now honours `ACTION_RETRY_COOLDOWN_SEC` (it stored `last_timestamp` but never read it) and defaults `max_attempts` from `config.MAX_ACTION_ATTEMPTS` rather than a hardcoded 3. Crucially, "cooling down" and "budget spent" are now distinguishable: a new `attempts_exhausted()` drives the terminal quarantine/refund decision, so a cooldown no longer causes a premature quarantine. All call sites updated.

**Where:** `config.py:51` (`ACTION_RETRY_COOLDOWN_SEC`, default 300) — **never referenced in `src/`**

`README.md:219` states "`ACTION_RETRY_COOLDOWN_SEC` between attempts" and `SECURITY.md` cites attempt caps *plus cooldowns* as the defence against fee-draining loops. In reality `should_attempt()` (`state_db.py:745`) compares a **counter only** — it stores `last_timestamp` but never reads it. Retries therefore fire on every poll cycle (as fast as `POLL_INTERVAL`, default 10 s) until the cap, with no backoff.

Related: no caller passes `config.MAX_ACTION_ATTEMPTS` to `should_attempt()`; all five call sites rely on the hardcoded `max_attempts=3` default, so that setting is silently inert too (it coincidentally matches).

---

### 🟠 B-11 — USDD-side quarantine is not implemented — ✅ **FIXED (2026-06-15)**

> **Resolution:** `nexus_client.quarantine_nexus_token()` actually transfers the credited USDD from the treasury to `NEXUS_USDD_QUARANTINE_ACCOUNT`, and every quarantine path in `swap_nexus` now routes through a `_quarantine_txid()` helper that moves the funds, records the full row, and alerts. If the quarantine account is unset the status is recorded as `quarantined (USDD NOT moved)` rather than implying segregation that did not happen — so the backing ratio is no longer silently overstated.

**Where:** `config.py:39` (`NEXUS_USDD_QUARANTINE_ACCOUNT`) — **never referenced in `src/`**

`README.md:225`, `CONFIG.md`, `SETUP.md` and `SECURITY.md` all state that USDD from exhausted refunds is moved to a separate quarantine account so it does not distort the backing ratio. The code only writes a DB status string (`mark_quarantined_txid`, `swap_nexus.py`); **no USDD is ever transferred anywhere.** The funds remain in the live treasury while the operator believes they have been segregated — so the backing ratio is overstated by exactly the quarantined amount, and the reconciliation in §2 of `SECURITY.md` is computed on a false premise. (The USDC-side quarantine *is* implemented.)

---

### 🟠 B-12 — Nothing prevents two instances running at once — ✅ **FIXED (2026-06-15)**

> **Resolution:** `acquire_singleton_lock()` takes an exclusive non-blocking `flock` (default `<STATE_DB_PATH>.lock`, override with `SWAP_LOCK_PATH`) at startup and refuses to run if another instance holds it; the handle is held for the process lifetime. On platforms without `fcntl` it warns explicitly that single-instance is not enforced rather than pretending to be safe. Verified: a second process attempting the same lock is refused.

No lockfile, PID file or `flock` anywhere. A double `systemd` start, a manual run alongside the service, or a restart that fails to reap the old process yields two processes sharing one SQLite file — the same race as B-2 but cross-process and without even in-process ordering. With the `reservations` table unused, there is no mutual exclusion at all.

**Fix:** exclusive `flock` at startup; exit if held.

---

## 5. Tier 4 — Medium: Economics, Operations & Tooling

| ID | Risk | Detail |
|----|------|--------|
| **B-13** ✅ | **FIXED** — config now enforces a floor of 2x the flat fee for both minimums and logs when it raises a configured value; docs corrected. | `MIN_DEPOSIT_USDC = 0.100101` against a `0.1` flat fee leaves `0.0000009` USDD — **below one base unit (1e-6)** — yet it passes the `net_amount > 0` check and is recorded as a *successful swap*. `README.md:30-32` publishes that minimum without noting the output is zero. The USDD→USDC minimum (`0.500501`) sits essentially *at* its own break-even too (`FLAT_FEE_USDC` 0.5). Both minimums must be set meaningfully above their fees. |
| **B-14** ✅ | **FIXED** — `FLAT_FEE_USDC` documented as 0.5 (and as the USDD→USDC fee), `NEXUS_CONGESTION_FEE_USDD` as 0.001, across README/CONFIG/.env.example. | `FLAT_FEE_USDC` is `0.5` in `config.py:84` but documented as `0.1` in `.env.example:80`, `CONFIG.md:73` and `README.md:339,549`. It applies to the USDD→USDC direction, so users are quoted a fee one-fifth of the real one. `NEXUS_CONGESTION_FEE_USDD` is likewise 10× off between code (`0.001`) and `.env.example`/README (`0.01`). |
| **B-15** ✅ | **FIXED** — new `src/alerts.py` with webhook + command channels, per-event rate limiting, PIN redaction and off-hot-path delivery; wired into backing pause, unbacked-mint discrepancy, heartbeat failure, quarantine and cap breaches. | Backing checks and balance reconciliation only `print()` to stdout (`main.py:139,207`). There is **no alerting channel of any kind** — no webhook, email or pager. An unbacked-mint discrepancy, a backing pause, or a wedged poller is invisible unless a human is watching a terminal. |
| **B-16** ✅ | **FIXED** — pausing no longer `continue`s the loop. Pollers run in `paused` mode: no new debits and no new USDC sends, but refunds, quarantine and confirmations keep running. A failed backing check now fails safe to paused. | When `maintain_backing_and_bounds()` returns `True` the loop `continue`s, halting *all* processing including refunds and quarantine of already-stuck user funds, with no notification and no manual override. |
| **B-17** ✅ | **FIXED** — `MAX_SWAP_USDC`/`MAX_SWAP_USDD` refuse oversized items into the refund path, and USDD→USDC now checks vault liquidity before marking a swap ready (holding it rather than burning attempts). | No `MAX_SWAP` or per-address limit. Oversized deposits are accepted and only fail at payout, dropping into refund/quarantine; a single large or hostile deposit can consume the vault or wedge the queue. |
| **B-18** ✅ | **FIXED** — the DB `fee_entries` table is now the single source of truth; `fees.add_solana_fee()` writes to it, `get_solana_fees()` reads from it, and `reconcile_accounting()` reports drift against the legacy JSON without silently "correcting" either side. | `fees.py` maintains `fees_state.json` + `fee_events.jsonl` while `state_db.fee_entries` records fees separately (9 call sites). Neither is reconciled against the other or against on-chain balances, so fee figures are not trustworthy for accounting. |
| **B-19** ✅ | **FIXED** — `mark_quarantined_txid()` persists the full row (timestamp/amount/from/to/owner/status) preserving prior detail on re-mark, and the viewer formats token amounts exactly instead of `int(float(x)*1e6)`. Reconstructed USDC-side quarantines now go to `quarantined_sigs` instead of being mislabelled as Nexus txids. | `quarantine_viewer.py:270,283` sums `amount_usdd` from `quarantined_txids`, but the only writer — `state_db.mark_quarantined_txid()` (`state_db.py:590`) — inserts **only `(txid, sig)`**. Every other column is permanently `NULL`, so quarantined USDD always displays as **0**. An operator sizing the stuck-funds backlog concludes nothing is at risk. The same table's `txid` column is also fed Solana signatures by `startup_recovery.py:229,313` while the viewer labels it "Nexus TxID". |
| **B-20** ✅ | **FIXED** — `sanitize()` strips control/ANSI characters from all untrusted text before it reaches a terminal, `csv_safe()` neutralises `= + - @` formula leaders on export, and CSV exports are git-ignored. `--usdc --usdd` together no longer silently hides the USDD table. | Depositor-supplied `memo` text is rendered raw to the terminal (`quarantine_viewer.py:100`) and written unescaped to CSV (`:304`). A memo containing ANSI escapes (`\x1b[2J`, `\r`) can erase or forge rows in the very table used to authorise manual fund recovery; one starting with `=`/`+`/`-` becomes a live formula on CSV export. Exports also land in the CWD with no `csv` pattern in `.gitignore`, so customer addresses/amounts can be committed. |
| **B-21** ✅ | **FIXED** — added `--dry-run`, a typed confirmation (`--yes` for non-interactive), and an existing-asset probe (`--force` to override). A JSON error body with exit 0 now returns non-zero, as does an unparsable address. The PIN is redacted by key rather than position and scrubbed from echoed CLI output. Field names are validated and rejected if they collide with Nexus API parameters such as `pin`. Success output now points at `NEXUS_HEARTBEAT_ASSET_NAME`, which is what the service actually reads. | It issues `assets/create/asset` (~1 NXS, immutable once created) with **no confirmation, no `--dry-run`, no idempotency check and no network guard**; re-running burns another NXS and creates a conflicting asset. It checks only `returncode != 0`, so a Nexus CLI error returned in a JSON body **exits 0 with a success message**. It also passes the PIN in `argv`, masks it positionally (`cmd[:-1] + ["pin=***"]`, safe only by accident), and echoes raw CLI output that may contain the PIN. |

---

## 6. Tier 5 — Low / Hygiene

| ID | Detail |
|----|--------|
| **B-22** ✅ | **FIXED** — quarantine now finalises a non-positive net as a fee instead of marking the row `awaiting confirmation` with a NULL signature (which the confirmation pass filters out, leaving it stuck forever). Original text: **quarantine lacks the `net_amount <= 0` guard** the refund path has. `send_solana_token()` returns `(True, None)` for a non-positive amount, so the row is marked `quarantine sent, awaiting confirmation` with a `NULL` signature and **never confirms — stuck in `unprocessed_sigs` forever**. Note the R-5 batching change (EVALUATION §11) filters `quarantine_sig IS NOT NULL`, which made this failure *silent* rather than log-spamming; the row was stuck before and after, but it now needs the guard to be visible. |
| **B-23** ✅ | **FIXED** — Priority 4 now re-checks `asset_owner == owner` explicitly, matching Priority 1. Original text: **inconsistent owner verification.** Priority 1 explicitly re-checks `str(asset_owner) == str(owner)` before payout (`swap_nexus.py:117`); the Priority 4 recovery path pays out relying only on the query filter, with no explicit re-check. Not currently exploitable, but the defence-in-depth is asymmetric. |
| **B-24** ✅ | **FIXED** — `is_valid_nexus_token_account()` and all four `name=USDD` CLI arguments now use `config.NEXUS_TOKEN_NAME`. Original text: **hardcoded ticker.** `is_valid_nexus_token_account()` compares `info.get("ticker") != "USDD"` literally (`nexus_client.py:74`) rather than `config.NEXUS_TOKEN_NAME`, so the token name is not actually configurable. |
| **B-25** ✅ | **MITIGATED** — the address is kept (users of *this* deployment need it) but now carries an explicit warning to verify it against the on-chain heartbeat and to replace it when forking. Original text: **README publishes a live vault address.** `README.md:30,36` hardcode `Bg1MUQDMjAuXSAFr8izhGCUUhsrta1EjHcTvvgFnJEzZ` in the user instructions where every other doc uses `<VAULT_USDC_ACCOUNT>`. Not a secret, but anyone deploying a fork ships instructions that send user funds to the **original operator's vault**. |
| **B-26** 🟡 | **PARTIALLY ADDRESSED** — money-path failures now raise operator alerts (`src/alerts.py`) rather than only printing, and `update_heartbeat_asset` no longer dereferences a possibly-`None` parse result. A general move to the `logging` module with levels is still outstanding. Original text: **broad exception swallowing** throughout the money paths is what allowed B-2, B-3 and the previously-found dead-code calls to persist unnoticed. Money-path errors should be logged with type and context, not silently defaulted. |

---

## 7. Cross-Cutting: Documented ≠ Implemented

The single most important systemic finding is that a large set of advertised safeguards and tunables do nothing. Each was verified by name-search across `src/`:

**Safety mechanisms that exist as code but are never invoked**
- `state_db.reserve_action()` / `release_reservation()` / `is_reserved()` — the anti-double-processing lock (B-2)
- `nexus_client.was_nexus_debited_to_account_for_amount()` — the on-chain double-debit check (B-3)

**Settings documented as protections that have no effect**
- `ACTION_RETRY_COOLDOWN_SEC` — no cooldown logic exists (B-10)
- `NEXUS_USDD_QUARANTINE_ACCOUNT` — no USDD is ever moved (B-11)
- `MAX_ACTION_ATTEMPTS` — never passed to `should_attempt()`; the hardcoded `3` wins
- `HEARTBEAT_MIN_INTERVAL_SEC` — README claims the interval is enforced; it is not
- `HEARTBEAT_WATERLINE_NEXUS_FIELD` — ignored by the writer (B-5)
- `SOLANA_POLL_INTERVAL` / `NEXUS_POLL_INTERVAL` — only the global `POLL_INTERVAL` is read
- `SOLANA_MAX_TX_FETCH_PER_POLL`, `MAX_DEPOSITS_PER_LOOP`, `MICRO_DEPOSIT_FEE_PCT`, `MICRO_CREDIT_FEE_PCT`, `SKIP_OWNER_LOOKUP_FOR_MICRO_USDD`, `MICRO_CREDIT_COUNT_AGAINST_LIMIT`, `NEXUS_RPC_HOST`, `METRICS_BUDGET_SEC`, `STALE_ROW_SEC`, `BACKING_DEFICIT_BPS_ALERT` — all defined, documented, unused
- `SOL_MINT` — **removed** with the dormant SOL/NXS conversion path; it is no longer
  required at startup or exposed in the operator template.

**Settings the code reads that are not in `config.py`** (each silently falls back to a hardcoded literal, so setting them in `.env` does nothing): `UNPROCESSED_PROCESS_BUDGET_SEC` (documented in `.env.example`), `UNPROCESSED_TXIDS_PROCESS_BUDGET_SEC`, `POLL_HELIUS_LIMIT`, `NEXUS_MAX_PAGES`, `FEE_EVENTS_FILE`, `VAULT_OWNER`.

**Documented but entirely absent from the template:** `HELIUS_RPC_URL` / `HELIUS_API_KEY` — the code's preferred fast path and prominent in `SETUP.md`, yet nowhere in `.env.example`, `CONFIG.md` or `config.py`. An operator copying the template silently gets the slow fallback.

**Automated value movement removed (2026-08-30):** the dormant fee-conversion/rebalance
feature gate and its Solana DEX, SOL/NXS top-up and Nexus mint helpers have been removed. Backing
surplus is now alert-only for operator review, so no configuration edit can revive that automatic
cross-chain value movement without a new, reviewed durable-intent implementation.

**Root cause — B-27 (historical):** this report predates the current pytest and CI gate.
Current test/CI evidence and remaining release gates are maintained in the authoritative
[`EVALUATION.md`](EVALUATION.md).

**Compliance/governance (flagged, not assessed):** no sanctions/blocklist screening, no per-address limits, no exportable audit trail, and no documented key-rotation, incident-response or emergency-stop procedure. Material regulatory exposure for a custodial service, and out of scope for a code review.

---

## 8. Prioritized Remediation Roadmap

**Gate 0 — blocking, before any mainnet funds — ✅ COMPLETE (2026-06-15)**
1. ✅ **B-1** — balance-delta skip removed; waterline advancement now provably safe.
2. ✅ **B-2** — non-overlapping pollers, `reserve_action()` on the debit, singleton lock.
3. ✅ **B-3** — intent persisted before the debit; ambiguity resolved against the chain, never guessed.
4. ✅ **B-4** — dust floor added, gap credits recorded, docs reconciled with code.
5. ✅ **B-5** — configured heartbeat fields honoured; startup validation fails loudly.
6. ✅ **B-12** — startup singleton lock.

> **Gate 0 is closed on code, but not on evidence.** Every fix above was verified by unit
> tests against the real functions; **none has been exercised against a live Solana or
> Nexus node.** The devnet/testnet run in §9 is now the gating item — in particular the
> B-3 resolver, whose correctness depends on the Nexus CLI actually returning
> `contracts.reference` on `finance/transactions/token`. Gate 1 (B-6 float money math,
> B-7 finality) remains open and still matters before meaningful volume.

**Gate 1 — before meaningful volume**
7. ✅ **B-6 done** — `Decimal`/integer money math end-to-end; exact base units for refunds.
8. ✅ **B-7 done** — ingestion and payout settlement default to `finalized`, with a size carve-out.
9. **B-8** heartbeat failure must alert and fall back, not silently halt.
10. **B-10/B-11** implement the cooldown and the USDD quarantine transfer — or delete the claims from the docs.
11. **B-13/B-14** set minimums above fees; correct every published fee and minimum.
12. **B-15** real alerting on discrepancy, pause and halt.

**Gate 2 — operational maturity**
13. **B-9** withdrawal caps, mint authority off the hot path, PIN out of argv, reserve attestation.
14. **B-16…B-21** refund-safe pause, swap caps, unified fee ledger, fix the quarantine viewer's schema assumptions, sanitise memos in operator tooling, add guards to `create_heartbeat_asset.py`.
15. **B-27** tests + CI: import smoke test, fee-math and unit-conversion tests, state-machine transition tests, and a devnet end-to-end run covering both directions plus refund, quarantine, restart-recovery and concurrent-poller behaviour.
16. Purge or implement the dead configuration surface in §7 so the documentation matches reality.

---

## 9. Caveats

This is a static assessment. Runtime claims — the scientific-notation CLI string, the concurrency race, the reorg exposure, the waterline skip, the heartbeat field mismatch — follow from reading the code together with documented Solana, Nexus and SQLite semantics; the arithmetic and SQL were executed in isolation to confirm the values shown. They were **not** observed against a live node, because the runtime dependencies and RPC/Nexus access are unavailable in this environment.

A devnet/testnet run exercising both directions plus refund, quarantine, restart-recovery and deliberately overlapping pollers remains the essential final gate, and would likely surface further edge cases in the multi-stage state machines.

---

## 10. Audit — Functionality, Performance & Security (2026-06-15, current code)

**Conflict of interest, stated up front.** Nearly all of the money-path code reviewed here
was written or rewritten during the same engagement that produced this document. This is
therefore **partly self-review, and weaker evidence than an independent audit.** To reduce
reliance on my own judgement this pass leaned on checks that do not depend on it: `pyflakes`,
an AST call-site arity check, a real-function smoke test, and **measured** benchmarks rather
than asserted ones. An independent reviewer should still go over the diff.

### 10.1 Functionality — 2 defects found, both fixed

| ID | Severity | Finding |
|----|----------|---------|
| **A-1** | 🟠 High | **A transient payout-cap breach permanently quarantined a user's refund.** *(Introduced by the B-9 cap work — my own regression.)* `send_solana_token()` returned `False` both when a send genuinely failed and when it was refused by the rolling 24h cap. The refund path maps `False` → `to be quarantined`, so hitting the cap converted a routine refund into a manual-intervention event. **Fixed:** the cap now raises `PayoutCapExceeded`, which callers treat as *defer and retry later*, leaving the status untouched. Verified by test: with the cap deliberately breached the row stays `to be refunded`. A follow-on flaw in that same fix — the generic `except` swallowing and re-wrapping the new exception, hiding whether the cap tripped or the check itself broke — was also corrected. |
| **A-2** | 🟠 High | **`quarantine failed` was a dead-end state.** The status was written on a failed quarantine send, but the quarantine pass selects `status_like '%to be quarantined%'`, which does not match it, and nothing else reads it. Affected rows sat in `unprocessed_sigs` forever — funds neither refunded nor quarantined, and no alert. *(Pre-existing.)* **Fixed:** added a `status_in` filter; the pass now picks up both states, with `should_attempt()` bounding the retry. |

### 10.2 Performance — measured, not assumed

Benchmarked against a seeded 20,000-row `unprocessed_sigs` table:

| ID | Finding | Measured | Status |
|----|---------|----------|--------|
| **P-1** | **No indexes on any hot column.** Every poll filters by `status` and orders by `timestamp`; only `payouts` had an index. | status filter **4.29 → 1.62 ms** (2.6×), pending-verification **3.73 → 1.20 ms** (3.1×), full `get_unprocessed_sigs()` scan **34 ms** | ✅ Fixed — 7 indexes added in `init_db()` |
| **P-2** | **`resolve_unverified_debits()` spawned one Nexus CLI subprocess per row**, each pulling the same page of transactions. | N rows → N process spawns | ✅ Fixed — `find_nexus_debits_by_references()` does one call for the batch |
| **P-3** | **`get_transaction_confirmations()` fetched the *entire* USDD transaction history with no `limit`**, once per unconfirmed debit row. | unbounded × N rows | ✅ Fixed — bounded to 200 and batched via `get_transactions_confirmations()` |
| **P-4** | `process_unprocessed_txids()` re-reads the full `unprocessed_txids` table **8 times** per invocation (once per priority stage); `main` reads all of `unprocessed_sigs` 3× per cycle. | ~34 ms per full scan at 20k rows | ◻️ Open — mitigated by P-1's indexes; a single fetch reused across stages would be the real fix but is a riskier refactor |
| **P-5** | `poll_nexus_deposits()` performs an owner lookup (`get_account_info`, one CLI subprocess) **per credit**. | 1 spawn per credit | ◻️ Open — bounded by `MAX_CREDITS_PER_LOOP`; batching needs a Nexus API that supports it |

**Non-finding, worth recording:** I expected the connection-per-call pattern in `state_db`
(a fresh `sqlite3.connect()` in every helper) to be a bottleneck. Measured at **0.022 ms**
per connect+close — negligible. Left alone.

### 10.3 Security — no new defects

Re-checked the surfaces added during this engagement:

- **SQL injection** — none. The one dynamically-built clause (`status IN (…)`) interpolates
  only `?` placeholders; all values are bound. Everything else is parameterised.
- **Command injection** — none. No `shell=True` anywhere; every `subprocess.run` takes an argv list.
- **Alert channel** — no attacker-controlled text (deposit memos) is forwarded off-host; the
  PIN is redacted before delivery. One counterparty address (`destination`) can appear in a
  `payout_cap_exceeded` payload, so treat the webhook endpoint as receiving user data.
- **Static analysis** — `pyflakes` reports no undefined names across `src/`, the helper
  scripts and the test.

**Unchanged and still open:** the `NEXUS_PIN` is passed as a CLI argument and is readable by
any local user via `ps` / `/proc/<pid>/cmdline` (B-9). This needs a decision on what the
Nexus CLI supports; guessing risks breaking every money operation.

### 10.4 Verdict

Two High-severity functional defects were found and fixed — notably **one I introduced**,
which is the clearest argument for the independent review recommended above. Performance
work was driven by measurement, and one assumption (connection churn) was disproved rather
than acted on. No new security defects.

**The overall position is unchanged and remains the binding constraint:** the supportable
claim is *"no known defects remain"*, not *"verified correct"*. Nothing here has run against
a live Solana or Nexus node, and A-1 is a reminder that new safety code can introduce its
own fund-handling regressions. A devnet run — both directions plus refund, quarantine,
cap breach, restart recovery and finality hold — is still the gate before mainnet funds.
