# swapService Independent Repository Evaluation — 2026-09-05

**Review window:** 2026-09-04 16:17:14 +0200 through 2026-09-05 16:06:23 +0200
**Baseline:** `7208f8b252bec85c7a70670477fce6a1faf32471`
**Reviewed head:** `c2d07aa990627da758f10be6c765a59039b05d4d`
**Scope:** 9 commits; 11 files; 627 insertions and 122 deletions
**Pre-review worktree:** clean; `main` matched `origin/main`
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The range correctly changes Nexus deposit enumeration from token-register history to the configured canonical treasury account, unifies runtime and recovery heartbeat parsing on one strict top-level DTO, and removes the lossy seven-day forward clamp from custody waterlines. These are meaningful local repairs to C-1, C-3, and C-4 from the prior review.

They do not clear deployment. Incoming Nexus state is still keyed by `txid`, so transactions containing multiple treasury CREDIT contracts cannot be represented. The new behavior rejects such transactions atomically and holds the live waterline; this prevents the prior silent first-sibling loss but creates a deliberate availability stop and is containment, not the required `(txid, contract_id)` repair. Recovery still accepts malformed qualifying evidence as complete, uses mutable offset pagination, and startup still ignores explicit recovery failure before exposing the service.

## Finding status

| Prior finding | Current status |
|---|---|
| C-1 — token-register rather than treasury scan | **Repaired locally; target-node acceptance outstanding** |
| C-2 — `txid`-only multi-CREDIT collapse | **Contained only; Critical remains open** |
| C-3 — heartbeat schema mismatch | **Repaired locally; startup enforcement remains blocked by H-3** |
| C-4 — lossy old-waterline clamp | **Repaired locally** |
| H-1 — malformed recovery evidence | **Open** |
| H-2 — mutable offset pagination | **Open** |
| H-3 — recovery failure not latched | **Open** |

## Severity-ordered findings

### Critical — incoming custody identity remains `txid`, not `(txid, contract_id)`

The Nexus lifecycle tables remain keyed by `txid` alone (`src/state_db.py:142-193`), and live/recovery duplicate sets remain txid-based (`src/swap_nexus.py:562-575`, `src/startup_recovery.py:83-109`). The new checks reject a transaction containing more than one CREDIT to the treasury (`src/swap_nexus.py:683-705`, `src/startup_recovery.py:64-81`).

This is correct interim fail-closed containment: neither live polling nor wipeout recovery silently persists only the first sibling. It is not a completed repair. A valid multi-CREDIT transaction cannot enter durable state, repeatedly holds its page and waterline, and can stop unrelated later deposits from progressing. Mixed fee/payable and over-cap/payable dispositions also remain unrepresentable.

**Required exit:** append-only migration of every incoming, pending, terminal, fee, refund/quarantine, memo and operator-intent reference to `(txid, contract_id)`; classify and persist each treasury CREDIT independently; prove two-payable, mixed-disposition, duplicate-page, migration, restart and crash cases.

### High — malformed qualifying recovery evidence can still be called complete

`fetch_deposits_since()` validates response containers and timestamps but does not require a non-empty txid, built-in integer contract id, strict source/destination addresses, exact positive amount, or strict confirmation metadata before returning `complete=True` (`src/nexus_client.py:2084-2117`).

Recovery then silently skips missing txids, coercively parses timestamp/confirmation values, converts malformed amounts to zero, and can break without an error or durable hold (`src/startup_recovery.py:26-36`, `:98-105`, `:125-142`). A malformed observed treasury CREDIT may therefore disappear from the reconstructed database while recovery reports no incompleteness.

**Required exit:** validate the full qualifying CREDIT schema in the producer and return an incomplete scan for every malformed relevant field. The consumer must have no silent `continue` or `break` path for observed custody evidence.

### High — mutable offset pagination still cannot prove a complete recovery range

Recovery pages with `offset=page * 100` and declares completion on a short/empty page or old timestamp (`src/nexus_client.py:2071-2117`). There is no snapshot identity, continuation token, stable high-water boundary, monotonic ordering check, duplicate detection, or equal-timestamp policy. A head insertion between page requests can shift a boundary transaction out while `complete=True` is returned.

Changing the query target to the treasury fixes scope, not this enumeration property. Multi-page recovery must remain fail closed until the target Nexus node offers a stable-range protocol and the behavior is captured in fixtures and live acceptance tests.

### High — startup logs and ignores recovery failure

`perform_startup_recovery()` can return `recovery_incomplete` or `error` for incompatible heartbeat schema, incomplete history, multi-CREDIT identity and rebuild exceptions (`src/startup_recovery.py:52-81`, `:414-475`). `main.run()` only prints the summary; exceptions are also printed and ignored (`src/main.py:355-361`). Missing heartbeat data and all-zero waterlines still enter a bounded Solana-only fallback without declaring Nexus recovery incomplete (`src/startup_recovery.py:399-409`, `:433-442`).

The later reconciliation pause is not equivalent: reconciliation cannot reconstruct a Nexus liability omitted after local database loss, and a later `healthy=True` result can clear that separate latch.

**Required exit:** abort startup non-zero or latch a distinct recovery exposure pause on every incomplete chain range or invalid heartbeat. Only explicit complete enumeration plus durable representation of every observed liability may clear it.

### Medium — local treasury-query repair still lacks external semantics evidence

Both live and recovery paths now build `register/transactions/finance:account/... address=<canonical treasury>` through `treasury_deposit_history_command()` (`src/nexus_client.py:2033-2069`, `src/swap_nexus.py:539-552`). This is the correct local response to the prior scope defect, and tests assert command construction.

The tests mock transport and do not prove the target node's response shape, account-to-account CREDIT coverage, below/boundary/above-threshold capture, ordering, equal timestamps, concurrent insertion, caching, finality, or stable pagination. C-1 is therefore repaired in local code but remains a release gate until the target-node matrix passes.

### Medium — heartbeat and old-waterline repairs are sound but not sufficient

One strict top-level parser now serves creation, validation, update, both pollers and recovery. It rejects the obsolete nested schema and conflated field names (`src/nexus_client.py:1736-1823`, `:1846-1981`; `src/startup_recovery.py:411-426`). Recovery now passes the exact published nonzero custody checkpoints into rebuild functions without moving old waterlines forward (`src/startup_recovery.py:425-459`).

These close C-3 and C-4 locally. They do not make startup safe while H-3 permits callers to ignore the resulting explicit errors.

## Verification

| Check | Exact result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | **PASS — 154 passed, 19 subtests passed in 13.35s** |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q src *.py tests` | **PASS — exit 0** |
| `python3 -m pip check` | **PASS — No broken requirements found** |
| `python3 scripts/check_markdown_links.py` | **PASS — Local Markdown links: OK** |
| GitHub Actions on reviewed head | **PASS — [run 33961339438](https://github.com/distordialabs-brutus/swapService/actions/runs/33961339438)** |
| Pre-review `git diff --check 7208f8b..c2d07aa` | **FAIL — three Markdown hard-break lines and `src/startup_recovery.py:427` had trailing whitespace; corrected in the review commit** |
| Live Nexus/Solana matrix | **Not run** |

No live RPC request, transaction, transfer, or fund operation was performed.

## Required repair order

1. **P0:** migrate incoming Nexus custody identity to `(txid, contract_id)` and process every treasury CREDIT independently.
2. **P0:** reject malformed qualifying recovery evidence atomically; remove every silent skip/break and coercive identity/finality parser.
3. **P0:** make any recovery error or incomplete range abort startup or latch a non-clearable recovery pause.
4. **P0:** replace mutable offset recovery with a proven stable-range protocol; remain incomplete when the target cannot provide one.
5. **P1:** make fee classification crash-atomic, report unmatched canonical emissions directly, and add producer-level regression fixtures for every fail-closed reason.
6. **P1:** run the target Nexus account-history, pagination, equal-timestamp, concurrent-insert, multi-contract, finality, crash/restart and heartbeat matrix plus the Solana acceptance matrix on the exact candidate commit.
7. Keep automatic Nexus refund/quarantine execution disabled and admit no real funds until all release gates pass.
