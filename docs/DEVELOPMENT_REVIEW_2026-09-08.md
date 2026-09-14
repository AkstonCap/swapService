# swapService Independent Architecture and Development Review — 2026-09-08

**Base/reviewed HEAD:** `917505b74f0b095d7c6ed202f55d4b94fb1378a5` (`main`, matching `origin/main` at review start)
**Prior current documentation:** [`POST_CHANGE_REVIEW_2026-09-07.md`](POST_CHANGE_REVIEW_2026-09-07.md)
**Reviewed delta after that documentation commit:** `bda7782a5fe2ded810bf7cd2a6ba2958f8145b1f` plus inventory refresh `917505b74f0b095d7c6ed202f55d4b94fb1378a5`; 10 files, 924 insertions and 31 deletions
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The 2026-09-07 payout-evidence repair remains materially present. Exact Solana payout finalization still requires full successful finalized transaction evidence, frozen payout terms and exact Nexus `(txid, contract_id)` source identity. The isolated installed-dependency suite is green, the isolated real-SDK boundary test passes, and a direct real-SDK synthetic payout-evidence probe returned the exact expected proof.

The new opt-in Solana→Nexus receipt subsystem adds useful immutable public evidence. The receipt obligation is committed atomically with exact payout/fee finalization; its create boundary is durable and never blindly retried; publication is accepted only after one exact owner-and-payload readback. Default-disabled receipt mode does not alter existing payouts.

It does not clear release. The previously documented daily Solana payout-cap bypass remains executable and was reproduced: the main Nexus→Solana helper submitted without reading the rolling cap or writing the payout ledger. Receipt creation is also incorrectly described in code as non-monetary: each named Nexus asset creation spends NXS, while the implementation has no receipt-spend cap, fee accounting, or production admission control. Receipt create/query/readback behavior remains unverified on the target Nexus build. The tests also remain order-dependent: the official full command passes, but combining independently passing real-dependency modules exposes global SDK/config contamination.

## Severity-ordered findings

### Critical release blocker — the production-required daily payout cap still does not cover the main payout

`send_solana_token()` checks `DAILY_PAYOUT_CAP_SOLANA_UNITS`, reads `state_db.payouts_since()` and records a successful send (`src/solana_client.py:979-1043`). The main Nexus→Solana flow instead calls `send_solana_token_to_account_with_sig()` (`src/swap_nexus.py`, payout submission branch). That helper sends directly (`src/solana_client.py:1568-1607`) without a cap read or payout-ledger write.

The executed offline probe set the cap to one unit and requested 900 units. The helper returned `(True, "signature")`; mocked `payouts_since` and `record_payout` both had zero calls. This confirms the current production-startup requirement for a positive cap is configuration presence, not an enforced service-wide ceiling.

**Required exit:** reserve/check the rolling cap in the same durable pre-submit transaction that claims and freezes the exact payout, retain the reservation across unknown outcomes, and convert it to confirmed spend only from exact finalized evidence. Refund/quarantine and normal payout paths must consume one common cap protocol. Add concurrency, timeout-after-acceptance, crash/restart, duplicate invocation, rolling-window and ledger-write-failure tests.

### High when receipts are enabled — receipt publication spends NXS without a budget or accounting boundary

`src/swap_receipts.py:127-149` invokes `assets/create/asset` with a deterministic `name`. The current upstream Nexus API documentation pinned at core commit
[`1185145534a20ed4d2288e4513c505f271be536d`](https://github.com/Nexusoft/LLL-TAO/blob/1185145534a20ed4d2288e4513c505f271be536d/docs/API/COMMANDS/ASSETS.MD)
states that an asset costs 1 NXS and its optional name costs another 1 NXS (`ASSETS.MD`, create-asset optional/note section). The module and main loop call this a “non-money” or “non-monetary” side effect (`src/swap_receipts.py:1-4`, `src/main.py:571-572`), but each accepted create consumes operator NXS.

The durable `pending → creating → verifying → published` protocol prevents blind duplicate creation after an ambiguous outcome, which bounds one receipt obligation to at most one create attempt. It does not impose a daily/total NXS spend budget, record actual create txid/fee, reserve expected cost, alert on budget exhaustion, or require a funded/capped receipt policy in production admission. A stream of otherwise valid swaps can therefore trigger an unbounded series of named asset costs while the feature is enabled.

**Containment:** `NEXUS_SWAP_RECEIPTS_ENABLED` defaults false. Keep it false in production until the following exit is met.

**Required exit:** classify receipt creation as a financial side effect; define an NXS budget and accounting ledger; persist expected cost and create identity; decide whether the extra named-register cost is necessary; hold before create on budget/read failures; and exercise accepted, rejected, timeout-after-acceptance, delayed-indexing, duplicate and restart cases on the target node.

### High receipt-mode acceptance gap — target Nexus semantics and registration migration are unproved

Local tests validate command construction, but no target node executed the JSON asset create or the global filtered readback:

- create: `assets/create/asset format=JSON json=... name=...`;
- readback: `register/list/assets:asset/... where=results.source_signature=... limit=100`.

The implementation correctly rejects a full 100-row page and requires exactly one matching owner/payload. It still relies on target-node Query DSL equality, global coverage, projection shape, index visibility and owner/address response shape. A short response is not authoritative completeness until those semantics are verified.

Enabling receipts on an existing v1 `format=basic` registration also cannot add the immutable `receipt_schema` field. `build_service_record()` emits it when enabled, but runtime publication updates only mutable fields and startup heartbeat validation does not require it (`src/nexus_client.py:1784-1797`, `:1896-1897`, `:1923-1930`, `:2031-2056`). The service can therefore create receipts while its existing registration does not advertise the extension. In addition, receipt mode resolves the provider owner during payout confirmation and holds local finalization if that read fails (`src/nexus_client.py`, `check_unconfirmed_debits`), so the supposedly separate publication feature becomes an availability dependency after the Nexus payout already exists.

**Required exit:** use an isolated target node to prove create, owner derivation, exact query filtering/projection, delayed indexing, duplicate detection and readback. Require a receipt-capable registration/schema and provider owner at startup when enabled, document/rehearse recreation of the fixed-field v1 asset, and keep receipt publication failure separate from already-proven financial settlement without fabricating historical obligations.

### Engineering gate — test results depend on module order and process-global SDK stubs

`tests/test_critical_safety.py:22-54` replaces `solana`, `solders`, `requests` and `dotenv` in `sys.modules` at import time. Several other modules use process-global `os.environ.setdefault`. Consequently:

- The isolated installed-dependency test command passed **271 tests and 33 subtests**;
- `tests/test_payout_review_regressions.py` alone passed **17** and `tests/test_nexus_fee_lifecycle.py` alone passed **12**;
- one combined installed-dependency selection of receipt, payout, fee and SDK modules failed **3** and passed **51** because receipt tests loaded real config first while payout fixtures expected the stub identities;
- a second combined recovery/SDK selection failed **2**, passed **14**, and passed **8 subtests** for the same isolation class;
- the isolated real-SDK boundary module passed, and a separate direct real-SDK payout-evidence probe passed after explicitly freezing the synthetic vault/mint/authority identities.

This is not evidence that production payout parsing is broken. It is evidence that the test gate can hide or create failures based on collection order, contrary to its isolation requirement.

**Required exit:** move SDK fakes to fixture scope or subprocesses, stop replacing dependency modules during collection, set/restore environment explicitly, and add a CI shard that runs the payout/recovery/receipt modules in at least two independent orders under the installed SDK.

## Receipt and payout controls positively verified

- Receipt mode is strictly opt-in and defaults disabled (`src/config.py:428-437`).
- Exact confirmed Solana→Nexus payout, fee entry, queue deletion and optional receipt obligation commit in one SQLite transaction; source/terminal conflicts hold (`src/state_db.py`, `finalize_confirmed_solana_payout`).
- Receipt wire data is 11 immutable string fields binding the full Solana source signature, configured mint/vault, Nexus token/destination, exact output txid/contract/base units and reference (`src/swap_receipts.py:14-54`).
- The deterministic receipt name is derived from the full source signature, not the sequential reference (`src/swap_receipts.py:57-59`).
- A create is claimed durably before the external call. Timeout/unknown status remains `creating` and is resolved only by readback; it is never submitted again (`src/swap_receipts.py:176-196`).
- Readback requires one exact provider owner, all frozen fields, canonical integer strings and a nonempty asset address; absent, malformed, duplicate, full-page or wrong-owner evidence remains unpublished (`src/swap_receipts.py:71-124`).
- The 24 isolated receipt tests passed with the real installed dependency set.
- The 17 payout-evidence regressions and 12 fee-lifecycle regressions passed independently. The real-SDK signature-boundary test passed without a skip.
- The current exact committed HEAD has successful fork GitHub Actions run [34158353970](https://github.com/distordialabs-brutus/swapService/actions/runs/34158353970).

## Verification executed

| Check | Result |
|---|---|
| Isolated Python 3.11 review venv, pinned requirements + pytest | Dependencies available; install/check completed successfully |
| `PYTHONDONTWRITEBYTECODE=1 ... python -m pytest -q -p no:cacheprovider` | **271 passed, 33 subtests passed** in 29.54s |
| Default interpreter full suite | **270 passed, 1 SDK test skipped, 33 subtests passed**; default interpreter lacked `solana`/`solders` |
| Isolated `tests/test_swap_receipts.py` | **24 passed** |
| Isolated `tests/test_payout_review_regressions.py` | **17 passed** |
| Isolated `tests/test_nexus_fee_lifecycle.py` | **12 passed** |
| Isolated `tests/test_solana_sdk_boundary.py` | **1 passed**, no skip |
| Combined receipt/payout/fee/SDK selection | **3 failed, 51 passed** — order-dependent fixture/config contamination described above |
| Combined recovery/SDK selection | **2 failed, 14 passed, 8 subtests passed** — same isolation class |
| Direct real-SDK synthetic payout-evidence probe | **PASS** — exact evidence returned, network forbidden by mocks |
| Daily payout-cap bypass probe | **REPRODUCED** — send succeeded with no cap read or ledger write |
| `python -m pip check` in disposable venv | **PASS** — no broken requirements |
| `python -m compileall -q src *.py tests` | **PASS** |
| `ruff check --select F821,F822,F823 src nexus_transfer_operator.py tests` | **PASS**; scoped undefined-name/local-variable check only |
| Local Markdown links, token-pair inventory, whitespace | **PASS on final staged review candidate** — local links OK, inventory current (273 active lines), `git diff --cached --check` clean; parent reran the full suite (271 tests, 33 subtests), compilation and dependency consistency successfully |
| Target Nexus/Solana live matrix | **Not run** |

No live RPC transaction, Nexus asset creation, token transfer, refund, quarantine or other financial mutation was performed. All focused probes used temporary SQLite state and mocked transport/signing boundaries.

## Release order

1. **P0:** enforce one durable rolling Solana payout-cap protocol on the actual Nexus→Solana payout helper and every refund/quarantine send.
2. **P1 for receipt mode:** keep receipts disabled until NXS spend is budgeted/accounted and target-node create/query/readback behavior is verified.
3. **P1:** make receipt-enabled startup require a receipt-capable provider registration and authoritative owner; document fixed-field v1 migration.
4. **P1:** remove test collection-order dependence and run installed-SDK shards in isolated processes.
5. **P1:** execute the existing target Nexus/Solana finality, pagination, timeout-after-acceptance, crash/restart, migration and reconciliation matrix on the exact release candidate.
6. **P2:** rehearse alert delivery, hold escalation, two-person operator disposition, database/WAL backup/restore and key rotation.

The repairs in the 2026-09-07 post-change review should not be reopened or weakened to solve these gates. In particular, missing/ambiguous evidence must continue to retain liabilities and must never authorize resubmission.
