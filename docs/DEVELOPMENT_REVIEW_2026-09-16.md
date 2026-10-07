# swapService Architecture and Development Delta Review — 2026-09-16

**Review HEAD:** `57b2de021fdc67dd83b2fbcf9ac774eb0f5befab` (`main`, matching
`origin/main` at review start)

**HEAD/index tree:** `2a82a82cc6d881fb69b9bfd476a66b8c5e937a1b`

**September 15 runtime baseline:** `6b1f052a2018f0315603e64a440d71cb612e8212`

**Tracked delta:** one documentation commit; no tracked runtime, dependency or workflow change

**Release verdict:** **HARD BLOCKED for production and real funds**

## Scope and candidate separation

The only commit after the source snapshot reviewed on September 15 is `57b2de0`, which publishes that
dated review, its authoritative evaluation/state-machine addenda and concise evidence. The tracked
runtime at HEAD is therefore byte-identical to the prior runtime baseline. The prior ten-path runtime
manifest verifies successfully in the current working tree.

The real Git index contains no staged entries and resolves to the HEAD tree. It excludes the
pre-existing unstaged `src/config.py` provider-v2 additions and untracked `src/service_record.py` /
`tests/test_service_record_v2.py`. Those paths remain a separate proposal, not committed deployable
code. The four old untracked raw diff captures under `docs/review_evidence/2026-09-15/` were preserved
and were not copied into this review.

No service was started. No credential, key, live RPC mutation, token transfer, payout, refund,
quarantine action, Nexus asset mutation or real-fund operation was performed. Tests used temporary
state and their existing mocked/offline boundaries.

## Delta finding

There is **no implementation delta to reassess**. Fresh source hashes and focused execution preserve
the September 15 conclusions; no reviewed blocker was repaired by the documentation-only commit.
The green suite is an execution/isolation result, not a financial-safety approval.

### Critical — current-v1 disposition recovery still infers intent from actual spend

The current memo binds kind and source signature, not frozen output, fee or terms. The fresh committed-
runtime probe again passed by demonstrating that a 1,000,000-unit source plus a one-unit refund can
produce a terminal `refund_confirmed` row and a 999,999-unit operator fee. Chain evidence proves the
spend, not the intended economic terms.

**Required exit:** account for positively proven cap spend, but retain a visible unresolved liability
unless surviving frozen intent or a new evidence version proves exact output, fee and terms. Exercise
wipeout, backup/WAL restore, policy changes, duplicate evidence and idempotent reconstruction.

### High — Solana minimum/micro policy is still absent after safe ingestion

The fresh real-worker probe again persisted a 150-unit source under a 200-unit configured minimum and
observed the Nexus debit boundary called with 140 units after a ten-unit flat fee. Removing the lossy
history filter was correct; a shared live/recovery classifier still has not replaced it.

**Required exit:** retain every positive custody delta, then apply one executable below/boundary/above
minimum policy in live processing and reconstruction. Public terms must derive from that policy and
exact mixed-decimal tests must prove the same fee/output behavior.

### High operational — disposition cap refusal remains generic and log-only

The fresh refund-worker probe again filled the cap, proved that no send occurred, and found only the
generic `to be refunded` state with no cap attempt or durable typed refusal. Capacity exhaustion,
evidence conflict and lifecycle conflict remain operationally indistinguishable.

**Required exit:** atomically persist a retryable cap-held state with obligation/needed/used/cap units,
expose it in operator surfaces and alert it separately from manual evidence conflicts.

### Dirty proposal — provider-v2 blockers remain outside the deployable runtime

The dirty builder remains unimported by tracked registration, heartbeat, startup recovery and
waterline callers. Fresh focused probes again demonstrate that it publishes parsed-but-unenforced
micro percentages and permits a configured secret as a substring of a public contact URL. The module
is absent from the committed index, as is its test file.

**Required exit before merge/default status:** implement address-selected Nexus create/update/readback,
strict owner/type/schema/service/pair/custody validation, positive monotonic terms metadata, explicit
legacy fallback, and public-field secret/URL controls. Map every published economic field to collected
live-worker and recovery tests, then execute target-node multi-asset acceptance.

## Positive controls reverified

- All ten files in the September 15 runtime manifest match their prior SHA-256 values.
- The real index tree equals HEAD and excludes the dirty provider-v2 proposal.
- The full shared-tree suite and all configured CI-focused shards pass in a fresh isolated environment
  containing the pinned dependencies.
- The installed-SDK recovery shard executed rather than skipping.
- Dependency consistency, literal configured compilation, local Markdown links, whitespace checks and
  the index-aware token-pair inventory pass.
- The inventory reads the real index, not the dirty worktree, and reports **274 active lines**.

These controls do not close the three committed-runtime blockers or any target-chain acceptance gate.

## Verification record

The environment was a fresh Python 3.11 virtual environment with `requirements.txt` and pytest.
Repository CI uses Python 3.12; no claim of a local 3.12 rerun is made.

| Gate | Exact result |
|---|---|
| `/tmp/swapservice-review-20260916-venv/bin/python -m pip check` | **PASS** — `No broken requirements found.` |
| `python -m compileall -q src *.py tests` with external bytecode cache | **PASS** — exit 0 |
| `python scripts/check_markdown_links.py` | **PASS** — `Local Markdown links: OK` |
| `python -m pytest -q` on the final shared tree | **PASS** — **434 passed, 71 subtests passed in 128.97s** |
| `python -m pytest -q tests/test_recovery_safety.py` | **PASS** — **33 passed, 46 subtests passed in 8.31s** |
| Recovery plus `tests/test_solana_sdk_boundary.py` | **PASS** — **34 passed, 46 subtests passed in 8.94s** |
| Receipt + payout + Nexus-fee + SDK CI shard | **PASS** — **85 passed in 25.06s** |
| `git diff --check HEAD~1 HEAD` | **PASS** |
| `git diff --check` and `git diff --cached --check` | **PASS** |
| `python scripts/check_token_pair_inventory.py` against the real index and docs-only disposable candidate index | **PASS** — **274 active lines** in each scope |
| Focused committed-runtime blocker probes | **PASS** — **3 passed, 2 deselected in 1.37s**; passing reproduces unsafe behavior |
| Focused dirty-v2 blocker probes | **PASS** — **2 passed, 3 deselected in 0.07s**; passing reproduces unsafe behavior |
| Combined focused blocker probes | **PASS** — **5 passed in 0.78s** |
| Target Helius/Solana/Nexus matrix | **NOT RUN** |

The final post-edit configured-gate log is
[`review_evidence/2026-09-16/final-doc-candidate-gate.log`](review_evidence/2026-09-16/final-doc-candidate-gate.log),
SHA-256 `fe64e2fc6079730a269cb6034f22965abfda7acd35e71ef34ee1335d6c636072`.
The earlier pre-edit configured-gate log is
[`review_evidence/2026-09-16/full-configured-gate.log`](review_evidence/2026-09-16/full-configured-gate.log).
Its two earlier exploratory compile invocations explicitly named nonexistent root files and printed
`Can't list ...` while returning zero; the later literal CI command `python -m compileall -q src *.py
tests` is the compile result reported above. Focused evidence is in
[`focused-probes.log`](review_evidence/2026-09-16/focused-probes.log).

The full suite necessarily exercised the shared tree, including the unchanged dirty provider-v2
proposal. An isolated exact-HEAD materialization was denied by unattended approval policy and was not
retried or rerouted. Therefore this review does **not** represent the shared-tree count as a clean-
checkout exact-HEAD result or remote CI result.

## Runtime identities and hashes

**Real index SHA-256 at evidence capture:**
`ce49450bfc066087e24af9cca5b001b325fa758806eec8f9a867bac7c1d0280a`

| Path | Committed-index SHA-256 | Current worktree SHA-256 | Scope |
|---|---|---|---|
| `src/config.py` | `f8e4e295aacbc0236dc332f33917c78e413a7611790e48f52646cdd2a570d868` | `e6b7f28d60e4d26c6fed1fea0ab869aaa727fb7be3672dd6d002ba1f658f52eb` | dirty proposal differs from deployable HEAD |
| `src/nexus_client.py` | `20ed25032231735972f2565049c6014eca060964c98fed373282600a27cf19d6` | same | tracked runtime |
| `src/receipt_contract.py` | `b6afe7a8abb0f4027cbf9a78a2052b5a87920ba872d0088322092c9a95e8533b` | same | tracked runtime |
| `src/service_record.py` | absent | `1487382a259b373f08f3ec667075e88d1485bb99042dccb4d1469b16bcf8c6ec` | untracked proposal |
| `src/solana_client.py` | `bc2c4108f65fd4585cfe3166a9503c6065c83f3067eb4794ce19c96bf19e2bd6` | same | tracked runtime |
| `src/startup_recovery.py` | `25a7528fff6c80e3bb9737e6494fa29f7cc295e1e8d35e21184a29fed9dcbd48` | same | tracked runtime |
| `src/state_db.py` | `4ae733f36aac1c5b85124832eae9df8150d6ac5910c16b33dbb15da3243578cb` | same | tracked runtime |
| `src/swap_nexus.py` | `1374281b0bf62cc7a86c4af945b9d7d7bf14674372f8b68ca24aea7bebc7f4d2` | same | tracked runtime |
| `src/swap_receipts.py` | `745b894cbc2c39789036d07579c40a31a906e26bd3742b8e24d1e5b6318ff81f` | same | tracked runtime |
| `src/swap_solana.py` | `3cf6cdaa8e5170992fdc384cc25fffaf6508ec42ab50eb0cca34e9e17065f8e5` | same | tracked runtime |
| `tests/test_service_record_v2.py` | absent | `9039c042f660dfeddf0eaaaa0c36a1f35969e777f2cb9f8345c34a7f81055497` | untracked proposal test |

The complete machine output is
[`runtime-hashes.log`](review_evidence/2026-09-16/runtime-hashes.log). At capture time the configured-
gate log SHA-256 was `ea08d0c593d36d904a1b85f822cfd41cef1fcd5e82750fbf750d702d540f0eaa`
and the focused-probe log SHA-256 was
`e9c15931e47f576e9e99a7db0cda8cf17f1496b9d072ffee12601e99c7d02e8e`.

## Repair order

1. **P0:** make current-v1 chain-only disposition recovery retain unproven liabilities while counting
   actual spend; introduce intent-binding evidence for automatic terminal reconstruction.
2. **P1:** implement one shared Solana minimum/micro classifier for live processing and recovery.
3. **P1:** add typed durable disposition cap refusal, operator visibility and alerting.
4. **P1 before provider-v2 merge:** integrate address-selected runtime/recovery behavior, enforce every
   published term and close public-field secret leakage.
5. Re-run independent final-tree review and the complete local gate, then execute the target-chain,
   crash/restart, backup/WAL restore and operator rehearsal matrix on the exact candidate.

No file was staged, committed or pushed by this review. Production and real-fund admission remain
hard-blocked.
