# Independent Financial State-Machine Review — 2026-09-04

**Review cutoff:** `2026-09-03T16:24:34+02:00`
**Baseline:** `96d034522fa11feb13e299be4e6c5175859893d8`
**Reviewed committed head:** `0e4900b82632e3544d774f5568f869902a4ea0fe`
**Review completed:** `2026-09-04T16:07:34+02:00`
**Committed scope:** 8 commits; 7 files; 243 insertions and 73 deletions
**Additional scope:** pre-existing uncommitted changes in `ASSET_STANDARD.md`, `SETUP.md`, and `docs/EVALUATION.md`
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The reviewed commits close the 2026-09-03 live/recovery classification divergence for the ordinary dust, below-minimum, fee-only, payable, and over-cap branches. They also add finality to reference-based Nexus transfer evidence and move two accounting controls to the immutable startup pair: Solana backing reads use the canonical vault and account reconciliation uses the canonical Nexus treasury. The complete local suite and CI on the exact committed head are green.

Two High recovery/enumeration defects remain. First, a positive Nexus CREDIT amount that is not exactly representable at the configured scale is classified as `invalid`, silently ignored like dust, and can still contribute to a waterline advance. Second, wipeout recovery accepts a transaction when any CREDIT targets the treasury but rebuilds the first CREDIT contract without checking its destination; its fetch helper also collapses page errors and page-budget exhaustion into an ordinary partial list. Those paths can permanently omit a treasury credit or reconstruct an amount the treasury never received. The canonical-identity migration also remains partial, and the required target-chain matrix has not run.

## Severity-ordered findings

### High — inexact positive Nexus credits are skipped while the waterline may advance

The poller's schema pass accepts any CREDIT amount for which `_parse_decimal_amount(...) > 0` (`src/swap_nexus.py:640-657`). `classify_nexus_credit()` then returns `invalid` when `_parse_exact_nexus_units()` cannot represent the amount at the configured Nexus precision (`src/nexus_client.py:374-394,479-490`). The live poller treats `invalid` and `dust` identically and performs no durable write (`src/swap_nexus.py:710-718`). The transaction timestamp still enters `page_ts_candidates`, so an otherwise empty local queue can propose a Nexus waterline from the skipped row (`src/swap_nexus.py:664-672,847-855`).

Executed probe with a 6-decimal configuration and CREDIT amount `1.0000001`:

```text
INVALID_PRECISION={"processed": false, "unprocessed": false, "waterline_calls": ["call(880)"]}
```

This is not fail-closed malformed-evidence handling. Once the waterline passes the row, the user's positive treasury credit can be permanently absent from local liabilities and payout processing.

**Required exit:** distinguish malformed/inexact evidence from deliberate sub-dust policy. Any positive CREDIT that cannot be converted to exact integer base units must make enumeration incomplete, prevent every waterline proposal for that scan, emit an actionable alert, and remain recoverable. Add exact regressions for excess precision, non-finite values, malformed numeric types, and a valid sibling row on the same page.

### High — startup recovery can reconstruct the wrong CREDIT and cannot prove scan completeness

`fetch_deposits_since()` appends an entire transaction when any CREDIT contract targets the configured treasury (`src/nexus_client.py:2002-2024`). `_rebuild_nexus_from_waterline()` then iterates all CREDIT contracts but never checks each contract's `to` address against the treasury (`src/startup_recovery.py:81-142`). It writes `to_address=treasury_addr` regardless of the selected contract and breaks after the first accepted CREDIT. A transaction containing a non-treasury CREDIT before the actual treasury CREDIT is therefore reconstructed from the wrong sender and amount.

Executed probe with a 7-token CREDIT to `OTHER` followed by a 2-token CREDIT to `TREASURY`:

```text
SIBLING_CREDIT={"amount_nexus_units": 7000000, "from": "other-sender", "status": "pending_receival", "to": "TREASURY"}
```

The same recovery boundary has no completeness signal. `fetch_deposits_since()` logs and breaks on a failed page, stops at `max_pages`, and returns whatever rows it accumulated (`src/nexus_client.py:1974-2037`). Recovery consumes that list as complete (`src/startup_recovery.py:50-150`), and startup reports the result without latching exposure on recovery failure (`src/main.py:355-361`). An executed two-page probe returned 100 first-page rows after page 2 timed out:

```text
PARTIAL_FETCH={"has_completeness_metadata": false, "rows_returned": 100}
```

During database-loss recovery this can omit treasury liabilities while allowing the service to continue. The shared amount classifier is useful, but it does not establish recovery parity until contract selection and enumeration completeness are shared too.

**Required exit:** return an explicit complete/incomplete scan result with reason and stable-boundary evidence; validate every selected CREDIT's canonical destination; reject malformed/multi-contract ambiguity; and keep new exposure paused whenever recovery is incomplete. Add tests for sibling CREDIT ordering, multiple treasury CREDITs in one transaction, page failure after data, page-budget exhaustion, unstable ordering, malformed transaction metadata, and restart behavior.

### Medium — canonical asset and custody identity is not yet one enforced trust boundary

The new backing and account-reconciliation call sites correctly consume `SWAP_PAIR.solana.vault_account` and `SWAP_PAIR.nexus.treasury_account`. At import, legacy aliases and the pair object are built from the same validated values, so this review did not demonstrate a normal-startup mismatch.

The migration is nevertheless incomplete: live Nexus polling and recovery still read `NEXUS_USDD_TREASURY_ACCOUNT` (`src/swap_nexus.py:539`, `src/startup_recovery.py:44`); remote mint reconciliation reads `NEXUS_TOKEN_REGISTER_ADDRESS` rather than `SWAP_PAIR.nexus.register_address` (`src/balance_reconciler.py:384-401`); and backing supply is queried by `NEXUS_TOKEN_NAME` with a projection containing only `currentsupply`, so the response is not bound back to the configured register address (`src/nexus_client.py:1627-1659`). The current focused tests prove two call sites ignore patched aliases, not that all admission, supply, recovery, reconciliation, and publication paths share one canonical asset/custody identity.

**Required exit:** route every financial trust decision through the immutable pair, validate Nexus token/register and treasury identity at the external response boundary, and test wrong-token, wrong-treasury, alias disagreement, and multi-asset cases. This remains a production release gate, not a reason to rewrite frozen database/status identifiers.

### Release evidence — target-chain finality, pagination, crash, and recovery matrix is still absent

The new reference lookup requests `confirmations` and returns incomplete below `NEXUS_TRANSFER_MIN_CONFIRMATIONS`; the regression covers the below-threshold case. No evidence in this review establishes the target Nexus node's confirmation type/semantics, complete stable-range pagination, multi-contract behavior, timeout-after-acceptance behavior, or wipeout recovery. The standing Solana devnet/testnet finality and both-direction acceptance matrix also remains unrun.

## Positive controls verified

- Live polling and recovery call one exact-unit classifier for the ordinary dust, below-minimum, fee-only, payable, and over-cap branches.
- Recovery now persists exact Nexus units and fee-ledger rows for below-minimum and fee-only credits, and routes over-cap credits to `refund pending`.
- Reference-only Nexus transfer lookup requests confirmation evidence and holds below the positive configured threshold.
- Backing maintenance reads `SWAP_PAIR.solana.vault_account`; account reconciliation reads `SWAP_PAIR.nexus.treasury_account`.
- The added five regressions pass, the complete local suite passes, and GitHub Actions succeeded on the exact reviewed committed head.
- Automatic Nexus refund/quarantine execution remains disabled; unresolved reference-only outcomes remain held.

## Verification

| Check | Exact result |
|---|---|
| Five tests added by the reviewed commits | **PASS — 5 passed in 0.43s** |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_critical_safety.py` | **PASS — 116 passed, 16 subtests passed in 10.67s** |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | **PASS — 139 passed, 16 subtests passed in 11.85s** |
| `python3 -m compileall -q src *.py tests` | **PASS** |
| `python3 -m pip check` | **PASS — No broken requirements found** |
| `python3 scripts/check_markdown_links.py` | **PASS — Local Markdown links: OK** |
| `python3 scripts/check_token_pair_inventory.py` | **PASS — 648 active lines** |
| `git diff --check d487891^..HEAD` | **PASS** |
| `git diff --check` on the pre-review dirty tree | **PASS** |
| Added-line security-pattern scan over the reviewed committed range plus pre-existing dirty files | **PASS — 607 added lines scanned; 0 hardcoded-secret, shell-injection, eval/exec, pickle, or formatted-SQL matches** |
| GitHub Actions on `0e4900b82632e3544d774f5568f869902a4ea0fe` | **PASS — CI run 33863927243 completed successfully** |
| `ruff`, `pyflakes`, `mypy`, `pip-audit`, `bandit` | **Not available** |
| Target Nexus/Solana live matrix | **Not run** |

## Required repair order

1. Make inexact/malformed positive CREDIT evidence hold the Nexus scan and waterline; add red regressions for every malformed amount class.
2. Make wipeout recovery treasury-contract exact and completeness-aware; pause new exposure on every partial, malformed, bounded, or unstable recovery result.
3. Finish canonical Nexus token/register and treasury identity enforcement across admission, supply/backing, recovery, reconciliation, and publication.
4. Run the target Nexus pagination/finality/multi-contract/crash matrix and Solana devnet/testnet acceptance matrix on the exact candidate commit.
5. Keep automatic Nexus refund/quarantine execution disabled and admit no real funds until all release gates pass.

The pre-existing uncommitted architecture work in `ASSET_STANDARD.md`, `SETUP.md`, and `docs/EVALUATION.md` was reviewed as context and preserved. The committed review adds only this dated evidence document and does not alter application code. `docs/EVALUATION.md` remains an uncommitted architecture draft so its coordinated token-literal inventory update can be handled atomically.
