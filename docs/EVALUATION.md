# swapService — Current Engineering Evaluation and Remediation Plan

**Date:** 2026-08-26
**Evaluated code:** `e2f83a28177072227d4547087a381fafeff94078`
**Status:** Current issue register and repair priority for `swapService`

This document replaces the old June code-level audit as the current engineering evaluation. Historical findings and their original line references remain available in [`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) and [`RISK_ASSESSMENT.md`](RISK_ASSESSMENT.md). The independent post-change evidence behind this evaluation is in [`POST_CHANGE_REVIEW_2026-08-24.md`](POST_CHANGE_REVIEW_2026-08-24.md).

## 1. Executive verdict

**Do not deploy against real funds.**

The repair work through the evaluated head materially improved the bridge:

- failed, empty, malformed and truncated debit/reference lookups no longer authorize automatic retry or refund;
- receival-asset lookup distinguishes complete absence from lookup failure;
- incomplete receival lookups hold rather than entering the refund path;
- unresolved Solana deposits are deducted from backing and spendable surplus;
- backing and circulating-supply errors fail closed;
- non-idempotent automatic surplus actions are disabled;
- incomplete or malformed Nexus enumeration holds the waterline;
- the processing pass never proposes a Nexus checkpoint;
- every unsafe automatic Nexus refund path now holds and alerts for operator review;
- the heuristic Nexus server-side amount filter has been removed from normal and recovery scans;
- exact integer money math covers unequal-decimal pairs in both directions;
- one composable pytest command and a green GitHub Actions gate now exist.

Those controls are valuable and verified locally and in CI. They do not make the service
production-ready. Automatic Nexus refunds remain disabled because no durable intent protocol
- the new durable reconciliation path still needs target-chain evidence; and the standing
live-chain acceptance matrix has not been run.

### Current severity summary

| Severity | Count | Meaning |
|---|---:|---|
| Critical release gate | 2 | Permanent refund safety and live-boundary evidence remain absent |
| High | 1 | Reconciliation can report healthy from missing or incorrect evidence |
| Medium / operational | 6 | Response, custody, exposure-control, dependency or maintenance gap |
| Low / hygiene | 3 | Documentation and dead-code cleanup |

### Release gates

| Gate | Status |
|---|---|
| No ambiguous state-changing operation is retried blindly | **CONTAINED** — automatic Nexus refunds hold and alert; durable refund protocol remains required |
| No checkpoint advances from incomplete/lossy enumeration | **CONTAINED** — heuristic Nexus amount filter removed in normal and recovery scans; target-node enumeration evidence remains required |
| Exact money math for arbitrary configured decimals | **PASS locally and in CI** — integer-only thresholds, outputs and public terms have exact 6/6, 8/6, 6/8, 9/6 and 0/0 regression coverage; target-chain matrix remains required |
| Durable completed-state data supports reconciliation | **PASS locally** — completed mints retain immutable destination, memo and exact base-unit input/output; target-chain read-back remains required |
| One composable automated test command | **PASS** — `python -m pytest -q` runs the complete suite: 34 tests plus 4 subtests at this head |
| CI enforces tests and static checks | **ENFORCED and green** — run `32967131178` passed on the exact evaluated head |
| Live devnet/testnet matrix | **NOT RUN** |

---

## 2. Critical deployment blockers

### E-001 — Nexus refunds are not crash-safe or idempotent

**Severity:** Critical
**Priority:** P0 — contain immediately, then implement durable protocol

**Current status:** **contained; durable-protocol foundation implemented, not yet released.**
Every automatic Nexus refund branch transitions the source credit to `refund held for operator
review`, records `hold_reason`, emits a Critical alert and leaves the source row in place.
A new append-only `nexus_transfer_intents` ledger now permits exactly one intent per source
credit and persists its destination, exact base units and deterministic unique reference before a
Nexus account debit can be issued. It atomically permits a single execution; parsed remote txids
are retained and timeouts,
interruptions, non-zero exits and unparsed output become `outcome_unknown`. Resolution only
completes an intent after a positive on-chain debit whose unique reference, source account,
destination account and exact base-unit amount all match the immutable intent. For an already
submitted intent, the observed txid must also match the persisted txid. It never retries a debit.

Automatic refunds and quarantine moves remain disabled in the service loop. A separate
`nexus_transfer_operator.py` workflow now requires a named operator, rationale, exact intent
reference confirmation, a one-time execution request and a final exact remote-txid confirmation
before it archives the held source row. Each authorization, requested execution and disposition
is append-only/auditable. Focused fault injection and target-node evidence are still required;
the transfer primitive remains fail closed outside the durable-intent workflow.

**Historical root cause (pre-intent ledger):** `refund_nexus_token()` called
`transfer_nexus_between_accounts()` directly. The operation:

- wrote no durable refund intent before `finance/debit/account`;
- did not persist a returned Nexus transaction id;
- mapped CLI timeout, exception and non-zero result to `False`, even though the node may have accepted the debit;
- performed no on-chain reference lookup before a retry.

The direct transfer function is now a fail-closed legacy shim; only
`execute_nexus_transfer_intent()` can form an account debit, and only from a prepared durable
intent. Automatic callers still do not invoke it.

Before containment, four automatic refund paths called that boolean operation and retried it. A
process crash or timeout after Nexus accepted the refund but before a local completion write
could therefore send the same refund twice. Those paths now only hold and alert.

The prior `is_processed_txid()` check never provided refund idempotency: the source credit
remained in `unprocessed_txids`, while a completed refund was archived elsewhere. The durable
intent row and its reference now carry that identity explicitly.

#### Immediate containment

Disable automatic Nexus refunds. Hold affected rows for operator review and alert with txid, sender, amount, reason and age. This reduces availability but removes the double-refund path while the durable protocol is built.

#### Required permanent repair

1. Persist refund intent before the Nexus debit: source txid, destination, exact base units, unique reference, attempt timestamp and status.
2. Execute the debit using that persisted reference.
3. Treat timeout, process interruption, non-zero exit and unparsed output as `outcome_unknown`.
4. Resolve the reference on-chain before retrying, marking complete or quarantining.
5. Persist the Nexus refund txid before removing the source queue row.
6. Never infer non-execution from a bounded or failed history scan.

#### Exit criteria

- Crash between debit and local completion cannot produce a second refund.
- Timeout after a simulated accepted debit holds and resolves; it never retries blindly.
- Restart recovery reconstructs every refund intent.
- Tests cover accepted/parsed, accepted/unparsed, timeout-before-submit, timeout-after-submit, crash, restart and duplicate invocation.

---

### E-002 — Default heuristic Nexus filtering can hide credits and still advance the waterline

**Severity:** Critical deployment blocker
**Priority:** P0 — remove before any live-fund run

**Current status:** **contained, target-node evidence outstanding.** The setting and both
heuristic `where=contracts.amount...` construction paths have been removed. Normal polling
and recovery now enumerate without a server-side amount predicate and apply dust/minimum
policy locally. Regression tests assert that no `where=` argument is sent even when a legacy
flag is injected into the test configuration.

The remaining release gate is external: the target Nexus build must demonstrate complete,
stable enumeration and pagination under the standing live-node matrix before a waterline is
trusted with real funds.

#### Immediate containment

- ✅ Remove the server-side filter from normal and recovery enumeration.
- ✅ Apply dust/minimum policy locally after complete transaction capture.

#### Exit criteria

- A target-node test creates credits below dust, between dust/minimum, and above minimum; every expected credit is returned by enumeration.
- Unsupported/malformed query behavior produces an explicit incomplete scan and holds the waterline.
- Empty results can advance only after an unfiltered, validated scan.
- Pagination, processing caps and concurrent new transactions cannot move the checkpoint past an unpersisted credit.

---

## 3. High-priority correctness issues

### E-003 — Mixed-decimal thresholds and published terms use the wrong scale

**Severity:** High — **remediated locally and verified in CI; target-chain evidence still required**
**Priority:** P1 — fix before claiming token-pair agnosticism

**Resolution (current branch):** fees are parsed only when exactly representable and are
stored separately in the base units of each chain-side operation. Nexus input thresholds
now derive from the Nexus representation of the Solana-output fee; Solana input thresholds
derive from the Solana representation of the Nexus-output fee. Refunds use their explicit
Solana-scale fee. `format_solana_units()` and `format_nexus_units()` format public terms
with the source-side scale, and both output calculations use integer base units end-to-end.

`tests/legacy_token_pair.py` now has exact assertions for 6/6, 8/6, 6/8, 9/6 and 0/0
decimal pairs. For each case it verifies enforced deposit/minimum/dust thresholds, both
published terms and both 10-token output calculations. The former 8-decimal Solana /
6-decimal Nexus failure now produces the intended `1.0` Nexus minimum, `0.05` dust floor
and `0.2` `min_to_nexus`, rather than `100.0`, `5.0` and `20.0`.

#### Remaining release evidence

- Run the decimal matrix against the configured target Nexus node and Solana devnet/testnet.
- Verify operator fee values are exactly representable in both configured precisions before
  deploying a non-default token pair.

---

### E-004 — Double-mint reconciliation was blind and unit-inconsistent

**Severity:** High — **remediated locally; target-chain evidence still required**
**Priority:** P1 — safety detector must fail closed before deployment

The current reconciler cannot reliably discover completed mint recipients:

- `processed_sigs` stores no Nexus destination or memo (`src/state_db.py:564-592`).
- Confirmation archives the processed row and removes `unprocessed_sigs`, which held the memo (`src/nexus_client.py:386-390`).
- Reconciliation later left-joins to that deleted row to recover the destination (`src/balance_reconciler.py:79-100,146-169,276-297`).
- `run_balance_reconciliation()` may therefore check zero addresses and return no discrepancies;
  `main.py` then prints that all zero addresses match.

Its amount math is also inconsistent:

- token-unit floats are truncated with `int()` (`src/balance_reconciler.py:103-125,182-190`);
- fallback mint math uses the reverse-direction flat fee and returns float despite `-> int` (`src/balance_reconciler.py:66-72`);
- per-account failures are silently skipped (`src/balance_reconciler.py:316-325`).

Executed 10.5-token example:

- actual USDC→Nexus output: 10.3895 tokens;
- reconciler fallback: 9.9895 tokens;
- archived/comparison values truncate to whole tokens.

A green result was not evidence of balance correctness.

#### Resolution (current branch)

- Append-only SQLite migration adds `processed_sigs.amount_usdd_units`,
  `processed_sigs.nexus_destination`, and `processed_sigs.memo`; completed Nexus credits
  likewise retain `processed_txids.amount_usdd_units`.
- Confirmation persists the original memo, destination, integer Solana input and integer
  Nexus output before deleting `unprocessed_sigs`.
- Reconciliation uses only those immutable rows and the production
  `get_nexus_send_amount_units()` function. It never joins a completed row back to the
  transient queue and never converts a token float with `int()`.
- Results include `healthy`, explicit incomplete reasons and account errors. Zero checked
  recipients, missing/malformed evidence, legacy REAL-only relevant history and per-account
  calculation failures are unhealthy. The service emits a distinct critical alert for that
  state as well as one for a confirmed positive surplus.
- Regression coverage proves a completed mint reconciles to zero after its source queue row
  is gone, a seeded extra treasury debit creates an exact positive discrepancy, and missing
  durable evidence cannot produce a green result.

#### Remaining release evidence

- Execute the reconciliation fixture matrix and authoritative transaction-history read-back
  against the configured target Nexus node and Solana devnet/testnet.
- Establish a reviewed backfill/disposition procedure for existing legacy completed rows;
  they intentionally remain incomplete rather than being reconstructed from float data.

---

## 4. Medium and operational issues

### E-005 — Enforceable full-suite test command and CI

**Priority:** P1, before large repair batches

**Status: enforced and green.** The legacy scripts now run as pytest-managed subprocess cases, so
`python -m pytest -q` is the complete local command. GitHub Actions workflow
`.github/workflows/ci.yml` runs on pushes and pull requests and enforces dependency
consistency, byte-compilation, local Markdown-link verification, the complete pytest suite
and whitespace checking. The checked-in link verifier also caught and corrected the stale
Copilot-instructions security-document path.

The exact evaluated head passed GitHub Actions run
[`32967131178`](https://github.com/distordialabs-brutus/swapService/actions/runs/32967131178).
Every later production candidate still needs its own green run plus the separate live-chain
matrix in E-006.

### E-006 — No live-chain acceptance matrix

**Priority:** P1 release evidence

No test has exercised the current service against the target Nexus CLI/node and Solana devnet/testnet. The remaining highest-risk assumptions concern exactly those boundaries: CLI timeout semantics, transaction-reference fields, query/filter behavior, finality, pagination and restart recovery.

Required matrix:

- both swap directions;
- refund and quarantine;
- accepted but unparsed Nexus result;
- timeout before/after chain acceptance;
- process crash and restart at each intent/action boundary;
- pagination and processing caps;
- malformed API bodies;
- Solana finalized/confirmed behavior;
- waterline monotonicity and no skipped deposits.

### E-007 — Ambiguous items can remain held without a complete operator-resolution workflow

**Priority:** P2

The fail-closed lookup changes correctly hold uncertain rows. The dashboard now includes
`debited, awaiting confirmation`, Nexus refund holds, hold reasons, age and safe operator-action
text. What remains is a complete operator lifecycle: evidence display sufficient to resolve
the ambiguity, documented manual commands, a separately authorized disposition, attribution,
and an audit record. Visibility is not yet resolution.

### E-008 — Nexus PIN and session are exposed in process arguments

**Priority:** P2 custody hardening

State-changing Nexus calls pass `pin=` and, in multiuser mode, `session=` through argv. Local users can read them through process inspection. Use a Nexus-supported unlocked session, stdin, protected credential channel or isolated service account. Do not invent a transport the target CLI does not support; verify the mechanism on the actual build.

### E-009 — Exposure controls and alerting are optional by default

**Priority:** P2 operational hardening

Per-swap and daily payout caps default to disabled (`0`), and alert delivery is optional. Before production, require non-zero values appropriate to vault size and require at least one tested alert channel. Startup should refuse production mode when these controls are absent.

### E-013 — Pinned dependencies carry known advisories

**Priority:** P2 compatibility-tested security maintenance

`pip-audit -r requirements.txt` reports three advisories in two pinned packages:

| Package | Current | Advisory | Fixed version |
|---|---:|---|---:|
| `python-dotenv` | 1.0.1 | CVE-2026-28684 | 1.2.2 |
| `requests` | 2.32.3 | CVE-2024-47081 | 2.32.4 |
| `requests` | 2.32.3 | CVE-2026-25645 | 2.33.0 |

The repository does not call the vulnerable `python-dotenv.set_key()`/`unset_key()` or
`requests.utils.extract_zipped_paths()` paths. The Requests `.netrc` credential-disclosure
advisory is potentially relevant to HTTP clients, although service URLs are operator-controlled
or fixed rather than user-provided. Preserve Nexus/Solana compatibility: test targeted upgrades
to `python-dotenv==1.2.2` and `requests==2.33.0` rather than performing a broad dependency refresh.

---

## 5. Low-priority cleanup

### E-010 — Stale/dead configuration and helper paths

`DEBIT_VERIFY_GRACE_SEC` describes an automatic negative-lookup conclusion that no longer occurs. Legacy single-item lookup helpers and disabled fee-conversion code should be removed or clearly isolated so future changes do not accidentally reactivate unsafe behavior.

### E-011 — Documentation relocation and identity drift

- The Copilot-instructions link to the moved security document is fixed.
- Stale moved-document paths remain in `CONFIG.md:173` and `SETUP.md:504,514,517-520`.
- Historical review documents intentionally retain their reviewed heads; this evaluation now
  identifies the current head and is authoritative for current status.
- Current-tree whitespace checks pass.

### E-012 — Dashboard bearer token may appear in URLs

The dashboard accepts `token` from the query string (`src/dashboard.py:444-447`). Query credentials can leak through history, logs and referrers. Prefer the Authorization header and reject query-token authentication when bound beyond loopback.

---

## 6. Architecture assessment

### Sound decisions

- SQLite state is the local source of truth and WAL/atomic reference behavior is tested.
- State-machine strings and on-disk schema are protected by compatibility tests.
- Solana sends use memos/signatures for recovery and idempotency.
- Unresolved liabilities reduce available backing.
- Automatic surplus movements are disabled until an idempotent protocol exists.
- Lookup and waterline changes now prefer a visible hold over an unsafe inferred success.
- Dashboard access is read-only at the SQLite layer.

### Architectural rule to apply everywhere

Every state-changing cross-chain action must follow the same protocol:

```text
persist intent -> execute once -> record returned identity ->
resolve ambiguous outcome against chain -> finalize local state
```

A timeout is not failure, an empty bounded scan is not absence, and a warning is not a safety control. This rule already protects the repaired Solana→Nexus debit path; it must also govern Nexus refunds, quarantine transfers, fee movements and any future automated maintenance action.

---

## 7. Prioritized development plan

The plan is sequenced by expected fund-safety value from the evaluated head. Completed
containment and engineering-gate work stays visible because every later batch depends on it.

### Batch 0 — Immediate containment ✅

1. Disable automatic Nexus refunds; hold and alert instead.
2. Remove heuristic Nexus server filtering from normal and recovery enumeration.
3. Surface every held state with chain references, reason, age and safe operator guidance.

**Exit met:** no ambiguous Nexus refund is retried automatically and no heuristic amount
filter can authorize a checkpoint. This is containment, not permission to deploy.

### Batch 1 — Engineering and exact-money gate ✅

1. Run legacy executable checks in isolated pytest subprocesses.
2. Make `python -m pytest -q` the complete local command.
3. Enforce dependency consistency, compilation, Markdown links, tests and whitespace in CI.
4. Implement exact integer fees, thresholds, outputs and public terms for 6/6, 8/6, 6/8,
   9/6 and 0/0 decimal configurations.

**Exit met:** the current local suite and GitHub Actions run `32967131178` are green. The
mixed-decimal contract still requires target-chain evidence in Batch 4.

### Batch 2 — Durable completed-state model and fail-closed reconciliation ✅ (local)

**Goal:** make a green balance result trustworthy before adding another automated money path.

1. ✅ Add append-only migration for immutable completed-swap destination, original memo and
   exact input/output base units.
2. ✅ Persist evidence before deleting the source queue row.
3. ✅ Reuse the production integer payout function; remove the duplicate float-based fee path.
4. ✅ Return `healthy` plus explicit incomplete reasons and discrepancies.
5. ✅ Treat zero expected recipients, missing context, parse errors and account failures as
   unhealthy, never green.
6. ✅ Alert separately on incomplete evidence and confirmed imbalance.
7. ✅ Add balanced, duplicate-mint, deleted-source-row and malformed-row regression cases.

**Local exit met:** a known balanced completed swap returns zero delta after its queue row is
gone; a seeded duplicate is detected; and zero checked addresses cannot report healthy. The
target-chain integration matrix remains required before deployment.

### Batch 3 — Durable Nexus refund and quarantine protocol **(in progress; automatic execution remains disabled)**

1. ✅ Persist intent, destination, exact units and a deterministic unique reference before every eligible transfer.
2. ✅ Allow exactly one CLI execution from an atomically claimed intent and persist a parsed Nexus txid.
3. ✅ Treat timeout, interruption, non-zero exit and unparsed output as `outcome_unknown`.
4. ✅ Resolve a positive reference match to completed; never retry a debit from the resolver.
5. ✅ Persist and retain all in-flight intents across restart.
6. ✅ Provide an operator-only prepare → reference-confirm → authorize → execute-once →
   resolve → remote-txid-confirmed finalization workflow with an append-only attribution log;
   automatic refunds and quarantine moves remain disabled until focused fault injection and the
   live matrix pass.

**Remaining exit evidence:** crashes at every intent/action/finalization boundary, duplicate
invocation and target-node timeout behavior must prove exactly one remote transfer.

### Batch 4 — Live integration and external-semantics evidence

Run the full matrix on the target Nexus build plus Solana devnet/testnet:

- both swap directions and every configured decimal pair;
- refund, quarantine and manual hold disposition;
- accepted-but-unparsed results and timeout before/after acceptance;
- process crash and restart at every durable boundary;
- pagination, processing caps and concurrent arrivals;
- malformed API bodies, Solana finality and waterline monotonicity.

**Exit:** authoritative chain read-back proves no duplicate payout, skipped deposit or
checkpoint advance from incomplete evidence.

### Batch 5 — Production operational gates

1. Require non-zero per-swap and daily caps appropriate to vault size.
2. Require configured quarantine destinations and at least one tested alert channel.
3. Refuse production mode when mandatory controls are absent.
4. Complete the operator hold-resolution workflow with evidence, authorization and audit.
5. Document incident response, recovery and key rotation; rehearse them before launch.

### Batch 6 — Custody, dependency and maintainability hardening

1. Move Nexus PIN/session values out of argv where the target CLI supports a verified safer
   channel or isolate the service account so process inspection is not a credential boundary.
2. Compatibility-test `python-dotenv==1.2.2` and `requests==2.33.0`; update only after the
   Nexus/Solana suite and live matrix remain green.
3. Remove dead configuration and unsafe dormant helpers or clearly isolate them.
4. Remove query-string dashboard authentication for non-loopback binding.
5. Fix remaining moved-document paths, add structured logging and refresh this evaluation
   against the final reviewed commit.

---

## 8. Verification snapshot

| Check | Current result |
|---|---|
| `tests/legacy_smoke.py` | Enforced as an isolated pytest case |
| `tests/legacy_token_pair.py` | Enforced as an isolated pytest case with exact thresholds, public terms and bidirectional outputs for 6/6, 8/6, 6/8, 9/6 and 0/0 |
| `tests/legacy_session.py` | Enforced as an isolated pytest case |
| `tests/legacy_frozen_names.py` | Enforced as an isolated pytest case |
| `tests/legacy_dashboard.py` | Enforced as an isolated pytest case |
| `python -m pytest -q tests/test_critical_safety.py` | 27 passed, 4 subtests passed |
| Python byte-compilation | Passed |
| Dependency consistency | Passed |
| Local Markdown links | Passed |
| Current-tree whitespace | Passed |
| Full `python -m pytest -q` | 34 passed, 4 subtests passed |
| CI workflow | Passed on `e2f83a2` — [run 32967131178](https://github.com/distordialabs-brutus/swapService/actions/runs/32967131178) |
| `pip-audit -r requirements.txt` | Three advisories in two packages; see E-013 |
| Live integration | Not run |

## 9. Definition of deployment-ready

Deployment may be reconsidered only when:

- E-001 through E-006 are closed with tests and authoritative read-back evidence;
- the complete suite and CI are green from a clean checkout;
- reconciliation cannot report healthy with incomplete evidence;
- exact mixed-decimal public terms match enforcement;
- devnet/testnet restart, timeout, refund and waterline tests pass on the target node build;
- operational caps, quarantine destinations and alert delivery are configured and tested;
- known dependency advisories are fixed or explicitly accepted with documented applicability
  and compensating controls;
- an independent reviewer approves the resulting diff.
