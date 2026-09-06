# swapService Independent Repository Evaluation — 2026-09-04 21:05 CEST

**Baseline:** `1e1314288a126c7ae120bdd1f007d93052bf6399`
**Reviewed head:** `7208f8b252bec85c7a70670477fce6a1faf32471`
**Scope:** 3 commits; 9 files; 715 insertions and 126 deletions
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The reviewed recovery commit adds useful containment: incomplete page fetches no longer create partial recovery rows, positive inexact credits are retained as manual holds, sibling destinations are revalidated, and live/recovery classification uses one exact-unit policy. The provider-v2 documents remain explicitly prospective and do not misrepresent that architecture as deployed.

Those gains do not make Nexus deposit admission or wipeout recovery safe. Independent source inspection and executable probes found four Critical skipped-liability paths. The green test suite does not exercise the transaction topology or heartbeat schema that trigger them.

## Critical findings

### C-1 — The Nexus deposit scanner queries the token register, not the treasury account

`poll_nexus_deposits()` and `fetch_deposits_since()` call:

```text
register/transactions/finance:token ... name=<token-name>
```

at `src/swap_nexus.py:542-551` and `src/nexus_client.py:1969-1977`. On current LLL-TAO, `register/transactions` resolves one register and removes every contract whose `address`, `from`, and `to` do not equal that register. A normal user deposit CREDIT references the sender token account and treasury token account, not the token-supply register. A short or empty response is then accepted as complete.

Upstream source evidence was inspected at LLL-TAO commit `8af9c3387244b4d396c0e00ee81cea78bb9c0177`:

- [`register/transactions.cpp`](https://github.com/Nexusoft/LLL-TAO/blob/8af9c3387244b4d396c0e00ee81cea78bb9c0177/src/TAO/API/commands/register/transactions.cpp#L31-L47) resolves the selected register.
- [The contract filter](https://github.com/Nexusoft/LLL-TAO/blob/8af9c3387244b4d396c0e00ee81cea78bb9c0177/src/TAO/API/commands/register/transactions.cpp#L94-L151) retains only contracts whose address/from/to equals it.

**Impact:** ordinary treasury credits can be absent from both live admission and database-wipeout recovery while enumeration appears successful.

**Required exit:** enumerate the canonical treasury account's authoritative transaction history, validate captured target-node fixtures, and prove below-threshold/boundary/above-threshold credits plus account-to-account credits are all returned before any waterline can advance.

### C-2 — Deposit identity is only `txid`; multiple treasury CREDIT contracts collapse

Nexus transactions can contain more than one CREDIT contract. Upstream `BuildContracts()` iterates every contract in a referenced transaction and appends each valid credit; `finance/credit` submits the resulting vector:

- [`build.cpp`](https://github.com/Nexusoft/LLL-TAO/blob/8af9c3387244b4d396c0e00ee81cea78bb9c0177/src/TAO/API/build.cpp#L415-L438)
- [`finance/credit.cpp`](https://github.com/Nexusoft/LLL-TAO/blob/8af9c3387244b4d396c0e00ee81cea78bb9c0177/src/TAO/API/commands/finance/credit.cpp#L29-L41)

The service instead keys `unprocessed_txids`, `processed_txids`, and refund/quarantine records by `txid` alone (`src/state_db.py:142-193`). Recovery breaks after the first qualifying contract (`src/startup_recovery.py:92-163`); live polling also suppresses or overwrites later contracts after adding the txid to its in-memory sets (`src/swap_nexus.py:703-848`).

Executed fixture:

```text
one transaction:
  contract 0 -> treasury, sender-a, 3.000000 tokens
  contract 1 -> treasury, sender-b, 4.000000 tokens
expected liability: 7,000,000 base units across two contract identities
live persisted:      3,000,000 base units in one row
recovery persisted:  3,000,000 base units in one row
```

**Impact:** the second user's liability is unrepresented and can become permanently unpayable once the shared txid is processed or falls behind a waterline. Mixed dispositions in one transaction can also create contradictory processed/pending state.

**Required exit:** migrate every Nexus-side state table and durable action reference to at least `(txid, contract_id)`, classify and persist each treasury CREDIT independently, and test two payable, mixed fee/payable, mixed over-cap/payable, duplicate-page, restart, and migration cases.

**Interim containment (current branch):** while the state schema remains txid-only, both live
admission and wipeout recovery reject a transaction containing more than one CREDIT to the
canonical treasury. Live polling emits a Critical alert and holds its waterline; recovery returns
an incomplete-scan error before writing any reconstructed row. This prevents a known two-credit
transaction from being silently reduced to its first sibling, but it does **not** satisfy the
required `(txid, contract_id)` migration or make the deployment gate pass.

### C-3 — Runtime heartbeat fields and recovery's parser use incompatible schemas

The current basic heartbeat record publishes and validates top-level fields named `last_safe_timestamp_nexus` and `last_safe_timestamp_solana` (`src/nexus_client.py:1728-1767,1900-1925`). Recovery instead parses `heartbeat["data"]` for `nexus_waterline` and `solana_waterline` (`src/startup_recovery.py:392-405`).

Executed fixture using the exact top-level schema accepted by `validate_heartbeat_asset()`:

```text
standard_rebuild_calls: []
recovery result: fallback_mode=true
```

The fallback scans Solana memos only; it does not reconstruct Nexus deposit liabilities (`src/startup_recovery.py:271-357`).

**Impact:** normal configured heartbeat data bypasses Nexus wipeout reconstruction, yet startup continues.

**Required exit:** define one heartbeat DTO and parser shared by create, validate, update, poll, and recovery paths; reject missing/incompatible fields; add a full create/read/recover fixture using captured target-node JSON.

### C-4 — Wipeout recovery deliberately moves old waterlines forward

`perform_startup_recovery()` clamps a waterline older than `MAX_WATERLINE_LOOKBACK_SEC` to `now - max_lookback` (`src/startup_recovery.py:407-418`). This is a lossy availability shortcut inside a function documented as complete database-wipeout recovery.

Executed 30-day-old heartbeat fixture with the default seven-day cap:

```text
original waterline: 1997408000
scan begins at:      1999395200
durable history silently omitted: 23 days
```

**Impact:** after database loss, valid liabilities older than the cap are intentionally skipped.

**Required exit:** never move a custody checkpoint forward for workload control. Page/resume the original range, remain paused until complete, or require an explicit audited manual disposition for every omitted interval.

## High findings

### H-1 — Malformed recovery evidence can still be called complete and silently skipped

`fetch_deposits_since()` does not require a non-empty txid or exact valid CREDIT fields before returning `complete=True` (`src/nexus_client.py:1992-2025`). `_rebuild_nexus_from_waterline()` silently `continue`s a missing txid and silently drops non-finite/malformed amounts that are not finite positive values (`src/startup_recovery.py:79-125`).

Executed missing-txid fixture:

```text
scan_complete=true
scan_deposit_count=1
recovery error absent
pending rows=[]
```

Recovery must reject the entire scan as incomplete when any transaction or qualifying contract cannot be represented durably.

### H-2 — Live offset pagination can report complete while omitting a transaction

The recovery scanner uses live `offset` pages and declares completeness after an old timestamp or short/empty page (`src/nexus_client.py:1979-2027`). It does not prove snapshot identity, monotonic order, or page continuity. An executed two-page mutation fixture inserted one new head item between page requests; the scanner returned `complete=True` while omitting the treasury transaction at the shifted boundary.

This remains a live target-node release gate even after local code is changed: target caching, equal timestamps, concurrent inserts, reorg/drop behavior, and finality must be observed directly.

### H-3 — Recovery failure does not latch or abort service exposure

`_rebuild_nexus_from_waterline()` returns an `error`, but `main.run()` only prints a summary and does not derive a startup pause from recovery completeness (`src/main.py:355-361`). Exceptions are also printed and ignored. The later balance reconciliation cannot reconstruct unknown incoming Nexus liabilities from a wiped local database.

**Required exit:** startup must remain paused or exit non-zero until both chain recoveries explicitly prove complete and every observed liability is durable.

## Medium findings

1. **Fee recovery is not crash-atomic.** `add_fee_entry()` commits separately before `mark_processed_txid()` (`src/startup_recovery.py:129-145`, `src/state_db.py:1865-1884`). A crash between writes duplicates the fee entry on replay because the fee journal has no unique source-contract key.
2. **Reconciliation hides canonical unmatched emissions until a completed row proves the source.** `verified_mint_sources` starts empty and gates active matching and surplus classification despite an already-configured canonical token register (`src/balance_reconciler.py:380-438`). Results remain unhealthy, which is fail-closed, but the actual unrecorded amount is hidden from discrepancy reporting.
3. **Mint-history finality metadata is coerced rather than validated.** `find_nexus_mint_debits_since()` applies `int()` to timestamps and confirmation counts (`src/nexus_client.py:1371-1380`). Values such as `10.9` can therefore satisfy a ten-confirmation policy after truncation, unlike the stricter direct-transaction lookup. Require built-in non-negative integers and reject booleans, floats, and numeric strings.
4. **Recovery completeness tests mock away the producer under review.** The incomplete-scan regression injects a fabricated `DepositScan(..., False, "page_fetch_failed")`; it does not exercise `fetch_deposits_since()` through failure after a populated page, page-budget exhaustion, empty results, malformed metadata, or unstable ordering. Direct producer-level regressions are required for every fail-closed reason and for multi-CREDIT identity.

## Documentation corrections applied during this review

- `ASSET_STANDARD.md` now distinguishes the two incompatible fixed-field v1 creators instead of describing only the legacy heartbeat helper as the single current implementation.
- `SETUP.md` now includes the immutable `nexus_token_register_address` emitted by `register_service.py` in its published-field inventory.

## Positive controls verified

- `DepositScan.complete=False` prevents partial recovery rows on CLI, parse, and page-budget failure.
- Positive inexact treasury credits are retained as manual holds during recovery.
- Every inspected sibling CREDIT revalidates its own destination.
- Ordinary dust, below-minimum, fee-only, payable, and over-cap decisions share one exact-unit classifier.
- Canonical pair custody identities are consumed by several admission/reconciliation/publication paths.
- Provider-v2 documentation is explicitly labelled planned/documentation-only.
- No added-line secret, shell-injection, `eval`/`exec`, pickle, or formatted-SQL pattern was detected.

## Verification

| Check | Result |
|---|---|
| `python -m pytest -q` | **145 passed, 19 subtests passed** |
| Python byte-compilation | **PASS** |
| Dependency consistency | **PASS — no broken requirements** |
| Local Markdown links | **PASS** |
| Token-pair literal inventory | **PASS — 651 active lines current** |
| Whitespace/conflict check | **PASS** |
| GitHub Actions on reviewed head | **PASS — [run 33908593661](https://github.com/distordialabs-brutus/swapService/actions/runs/33908593661)** |
| Live Nexus/Solana matrix | **NOT RUN** |

The test suite is green but incomplete with respect to the four Critical fixtures above. No live RPC call, chain transaction, or fund operation was performed.

## Required repair order

1. **P0:** replace the token-register deposit scan with authoritative canonical treasury-account enumeration and add captured target-node fixtures.
2. **P0:** migrate Nexus deposit/action identity from `txid` to `(txid, contract_id)` and process every treasury CREDIT independently.
3. **P0:** unify heartbeat read/write/recovery schema and remove lossy waterline clamping.
4. **P0:** make any incomplete or incompatible startup recovery latch exposure or exit non-zero.
5. **P1:** reject malformed recovery schema atomically; require exact integer finality metadata; make classification writes idempotent and transactional.
6. **P1:** add direct producer-level regressions for every enumeration failure mode, mutable page boundaries, and multi-CREDIT identity.
7. **P1:** run the target Nexus pagination, multi-contract, finality, crash/restart, heartbeat, and wipeout matrix plus the Solana acceptance matrix on the exact candidate commit.
8. Keep automatic Nexus refund/quarantine execution disabled and admit no real funds until all release gates pass.
