# swapService — Current Engineering Evaluation and Remediation Plan

**Date:** 2026-08-29
**Evaluated code:** `5d92ec0513b8f9be8d6033ecef84f6923cea612a`
**Status:** Current issue register and repair priority for `swapService`

This document replaces the old June code-level audit as the current engineering evaluation. Historical findings and their original line references remain available in [`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) and [`RISK_ASSESSMENT.md`](RISK_ASSESSMENT.md). The current independent evidence is in [`DEVELOPMENT_REVIEW_2026-08-29.md`](DEVELOPMENT_REVIEW_2026-08-29.md); the 2026-08-28 review and earlier containment review remain historical evidence.

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
- startup no longer reports a returned unhealthy reconciliation as green;
- interrupted claimed Nexus transfers restart as durable `outcome_unknown` holds;
- explicit production mode requires positive per-swap/daily caps and an alert route;
- one composable pytest command and an exact-head green GitHub Actions gate exist.

Those controls are valuable and verified locally and in CI. They do not make the service
production-ready. Automatic Nexus refunds remain disabled while the durable intent protocol
awaits crash-boundary and target-node evidence. Reconciliation now requires exact remote Nexus
token-history identity and amount evidence, includes active recipients independently, and fails
closed rather than traversing unsafe live-offset pages. Exceptions or unhealthy results still do
not latch a pause on new exposure; the exact valid first-time-recipient case and target one-page
ordering/boundary semantics remain unproven. The standing live-chain acceptance matrix has not
been run.

### Current severity summary

| Severity | Count | Meaning |
|---|---:|---|
| Critical release gate | 2 | Permanent refund safety and live-boundary evidence remain absent |
| High release blocker | 1 | Reconciliation errors and unhealthy results do not pause new exposure |
| Medium / operational | 6 | Response, custody, configuration, exposure-control, dependency or maintenance gap |
| Low / hygiene | 2 | Documentation and dead-code cleanup |

### Release gates

| Gate | Status |
|---|---|
| No ambiguous state-changing operation is retried blindly | **CONTAINED** — automatic Nexus refunds hold and alert; durable refund protocol remains required |
| No checkpoint advances from incomplete/lossy enumeration | **CONTAINED** — heuristic Nexus amount filter removed in normal and recovery scans; target-node enumeration evidence remains required |
| Exact money math for arbitrary configured decimals | **PASS locally and in CI** — integer-only thresholds, outputs and public terms have exact 6/6, 8/6, 6/8, 9/6 and 0/0 regression coverage; target-chain matrix remains required |
| Durable completed-state data supports reconciliation | **PARTIAL** — local and remote evidence fail closed, but unhealthy results do not pause exposure and target-node boundary semantics remain unproven |
| One composable automated test command | **PASS** — 62 tests plus 4 subtests on `5d92ec0` |
| CI enforces tests and static checks | **ENFORCED and green** — run [`33258188981`](https://github.com/distordialabs-brutus/swapService/actions/runs/33258188981) passed on reconciliation evidence head `de4ae8c` (with implementation `5d92ec0` as its parent) |
| Live devnet/testnet matrix | **NOT RUN** |

---

## 2. Critical deployment blockers

### E-001 — Nexus refunds are not crash-safe or idempotent

**Severity:** Critical
**Priority:** P0 — contain immediately, then implement durable protocol

**Current status:** **contained; durable-protocol foundation implemented, not yet released.**
Every automatic Nexus refund branch transitions the source credit to `refund held for operator
review`, records `hold_reason`, emits a Critical alert and leaves the source row in place.
A new durable, monotonic `nexus_transfer_intents` ledger now permits exactly one intent per source
credit and persists its destination, exact base units and deterministic unique reference before a
Nexus account debit can be issued. It atomically permits a single execution; parsed remote txids
are retained and timeouts,
interruptions, non-zero exits and unparsed output become `outcome_unknown`. Resolution only
completes an intent after a positive on-chain debit whose unique reference, source account,
destination account and exact base-unit amount all match the immutable intent. For an already
submitted intent, the observed txid must also match the persisted txid. It never retries a debit.

Automatic refunds and quarantine moves remain disabled in the service loop. A separate
`nexus_transfer_operator.py` workflow now requires a named operator, rationale, an audited
preparation event tied to the deterministic intent reference, exact intent reference confirmation,
a one-time execution request and a final exact remote-txid confirmation before it archives the
held source row. Each authorization, requested execution and disposition is append-only/auditable. At
startup, any persisted `executing` intent is demoted to the explicit `outcome_unknown` hold
before scans run, so a crash after the durable claim cannot consume its authorization again.
Focused fault injection and target-node evidence are still required; the transfer primitive remains
fail closed outside the durable-intent workflow.

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

**Severity:** High — **partially remediated locally; release gate remains open**
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
- Reconciliation uses only immutable completed/active evidence and exact integer base units.
  It never joins a completed row back to the transient queue, converts a token float with
  `int()`, or recomputes a historical issued output under mutable current fee settings.
- Active debit intents retain the exact Nexus output atomically with their unique reference
  before the CLI invocation. Reconciliation reads completed and active debit evidence in one
  SQLite snapshot, then consumes an exact active remote debit for a first-time recipient using
  that immutable amount. A concurrent confirmation transition or later fee configuration change
  therefore cannot report the in-flight mint as an unrecorded remote surplus. Legacy active rows
  without this evidence remain explicitly incomplete and hold exposure.
- Results include `healthy`, explicit incomplete reasons and account errors. Zero checked
  recipients, missing/malformed evidence, legacy REAL-only relevant history and per-account
  calculation failures are unhealthy. The service emits a distinct critical alert for that
  state as well as one for a confirmed positive surplus.
- Regression coverage proves a completed mint reconciles to zero after its source queue row
  is gone, a seeded extra treasury debit creates an exact positive discrepancy, and missing
  durable evidence cannot produce a green result.

#### 2026-08-28 independent-review correction

- ✅ The startup consumer now requires `healthy is True` before it prints the green balance
  message. Zero checked recipients, malformed evidence and invalid result objects emit the same
  `balance_reconciliation_incomplete` critical alert used by the periodic consumer.
- The reconciliation totals are derived from local completed tables. `include_remote_balance`
  is display-only. A crash-created duplicate remote mint need not create a second local row;
  authoritative Nexus transaction-history identity/amount read-back is still required.

#### 2026-08-29 remote-authority correction

- Commit `5d92ec0` requires one-to-one confirmed remote DEBIT evidence by txid, contract id,
  reference, source, destination and exact integer amount before reconciliation can be green.
- Every remote token-supply DEBIT is retained, including unknown recipients; unmatched emissions
  are reported as surplus, while account-to-account movements are separated by source register.
- Active mint intents are validated independently and keep the result unhealthy until terminal.
- The Nexus scan reads one page only and returns `pagination_snapshot_unavailable` when a full
  page cannot prove the requested boundary. Target short-page, ordering and boundary semantics
  remain unproven.
- SQLite REAL/TEXT money evidence is rejected, per-deposit references are immutable, duplicate
  remote identities must have identical payloads, and missing/non-integer contract ids fail closed.
- Startup and periodic reconciliation exceptions or unhealthy results now latch a fail-closed
  exposure pause. Existing refunds/quarantines continue, but new Solana deposits and Nexus→Solana
  payouts remain blocked until a later reconciliation explicitly returns `healthy=True`.

#### Remaining release evidence

- ✅ Every reconciliation error/unhealthy result latches a fail-closed exposure pause until a
  later explicitly healthy run; existing refunds and quarantines remain processable in paused mode.
- ✅ Completed historical mints remain exact-match reconcilable after a fee-policy change, and
  active first-time recipients are scanned even before any completed recipient anchors the
  token-supply source. The active-mint regressions also prove an active remote debit is held
  (not treated as a green result) without a false surplus when a later fee configuration differs
  or the confirmation worker transitions the row between its active and completed tables after
  reconciliation captures its SQLite snapshot.
- Prove the one-page boundary, short-page and ordering semantics on the target node; retain
  fail-closed behavior whenever the boundary cannot be proven.
- Execute the final reconciliation fixture matrix and authoritative transaction-history read-back
  against the configured target Nexus node and Solana devnet/testnet.
- ✅ Every local consumer, including startup, alerts unless `healthy is True`; caller-level
  regressions cover zero checked and invalid results.
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

The reconciliation implementation and its evaluated documentation evidence head passed GitHub
Actions run [`33258188981`](https://github.com/distordialabs-brutus/swapService/actions/runs/33258188981).
Every later production candidate still needs its own green run plus the separate live-chain matrix
in E-006.

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

### E-007 — Operator-resolution workflow exists locally but is not operationally accepted

**Priority:** P2

The fail-closed lookup changes correctly hold uncertain rows. The dashboard includes held-state
evidence, and `nexus_transfer_operator.py` now implements documented prepare → authorize →
execute-once → positive-reference resolve → exact-txid finalize with named attribution. What
remains is target-node acceptance, crash/restart rehearsal, hold aging/escalation, and a reviewed
two-person production policy. A locally executable workflow is not yet operational acceptance.

### E-008 — Nexus PIN and session are exposed in process arguments

**Priority:** P2 custody hardening — **remediated for production runtime**

The production service now sends every runtime Nexus operation requiring a profile PIN or
multiuser session through the daemon's authenticated HTTPS API rather than invoking the CLI with
`pin=` / `session=` arguments. `NEXUS_API_URL` must be a
credential-free `https` base URL and `NEXUS_API_USER` / `NEXUS_API_PASSWORD` must be set; explicit
production mode refuses startup before SQLite opens or either the Nexus/Solana poller starts when
any transport control is absent. The form body carries the Nexus profile PIN and, when
`multiuser=1`, session identifier; they are therefore absent from child-process argv and normal
process listings. HTTP response bodies on transport errors are deliberately discarded so a broken
node cannot reflect those fields into logs.

The CLI fallback remains only for non-production local development. Operators must configure
`apiauth=1`, `apissl=1`, `apisslrequired=1`, an HTTPS API port, certificate validation, and
local/VPN/firewall network restriction on the target Nexus node. The live-node acceptance matrix
must verify those settings and the target node's POST-form semantics before deployment.

### E-009 — Exposure controls and alerting are optional by default

**Priority:** P2 operational hardening — **partially remediated locally**

`SWAP_PRODUCTION_MODE` is opt-in (so local development and testnet workflows retain their
non-production defaults). Its parser now accepts only explicit true (`1`/`true`/`yes`/`on`) or
false (`0`/`false`/`no`/`off`) spellings; a present invalid value fails configuration loading
rather than silently disabling production controls. When it is enabled, startup refuses before
opening SQLite or polling if one or both per-swap caps, the daily Solana payout cap, or both alert
routes are unset/zero. The rejection emits a `production_controls_missing` critical event naming
every missing control and the process entrypoint exits non-zero so a supervisor cannot mistake the
rejection for a clean stop.

Remaining release evidence: configure values appropriate to the vault, deliver and independently
verify at least one live alert channel, and exercise that configuration in the live acceptance matrix.

### E-013 — Pinned dependencies carried known advisories

**Priority:** P2 compatibility-tested security maintenance — **remediated locally**

The targeted remediation pins `python-dotenv==1.2.2` (CVE-2026-28684) and
`requests==2.33.0` (CVE-2024-47081 and CVE-2026-25645). A clean virtual environment installed
the complete pinned set with `solana==0.36.9` and `solders==0.26.0` unchanged; `pip check`,
`pip-audit -r requirements.txt`, byte-compilation, Markdown-link validation and the full suite
passed. The regression contract asserts the two safe pins so a future dependency edit cannot
silently restore the advisory-bearing versions.

This is deliberately a narrow HTTP/environment-layer update: Nexus HTTPS API wrappers, Solana
JSON-RPC/Jupiter callers, and the pinned Solana SDK pair were not changed. It is local compatibility
evidence only; the target Nexus node and Solana devnet/testnet matrix remains a separate release gate.

---

## 5. Low-priority cleanup

### E-010 — Stale/dead configuration and helper paths

**Remediated locally:** `DEBIT_VERIFY_GRACE_SEC` and the obsolete required `SOL_MINT` setting
have been removed from runtime configuration and current operator/state-machine documentation. The
unsafe legacy Nexus DEX listing/execution, rebalancer and direct mint helpers are also absent from
the runtime surface: no configuration edit can invoke an unaudited `market/execute/order` or
supply debit. An ambiguous Nexus debit remains held unless positive reference evidence is found;
no expiring negative-lookup setting can imply that a bounded scan proved non-execution. Solana→Nexus
decisions use only batch interfaces that expose lookup completeness; backing surplus is alert-only
for named operator review. A stale `None`/`False` result cannot be reintroduced as proof of
non-execution.

### E-011 — Documentation relocation and identity drift

**Remediated locally:** operator documents now use canonical links to the moved `docs/` security,
state-machine and audit documents. `SETUP.md` also states the fail-closed USDD→USDC mapping hold
policy, documents production Nexus API requirements (`apiauth=1`, TLS, no remote exposure), and
labels `apiauth=0` as isolated-development-only. The CI-contract regression test protects these
paths and safety statements from drifting back.
- Historical review documents intentionally retain their reviewed heads; this evaluation now
  identifies the current head and is authoritative for current status.
- Current-tree whitespace checks pass.

### E-012 — Dashboard bearer token may appear in URLs

**Status: remediated locally; verify the proxy configuration before deployment.** The dashboard
accepts `DASHBOARD_TOKEN` only through `Authorization: Bearer <token>` and no longer reads or
propagates a query-string token. This prevents the credential from appearing in URLs, browser
history, access logs and referrers. For any non-loopback bind, inject the header at a TLS reverse
proxy; do not expose the dashboard directly.

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

**Exit met:** the current committed-head local suite and GitHub Actions run `33258188981` are green.
The mixed-decimal contract still requires target-chain evidence in Batch 4.

### Batch 2 — Durable completed-state model and fail-closed reconciliation **PARTIAL**

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
8. ✅ Require exact remote Nexus token-history evidence and detect unrecorded token-supply
   DEBITs without relying on unsafe multi-page live-offset scans.

**Evidence exit met locally:** a known balanced completed swap returns zero delta after its queue
row is gone; local and remote-only duplicates are detected; zero checked addresses return
`healthy=False`; historical completed mints retain their issued integer output across fee changes;
active first-time recipients are scanned and keep the result unhealthy without a false surplus across
a later fee-configuration change or a concurrent completion transition; both consumers refuse green
unless `healthy is True`; and every unhealthy or exceptional reconciliation result pauses new
Solana↔Nexus exposure while already-owed refunds and quarantines continue in paused mode. **Batch
remains partial:** target single-page boundary/order semantics are unproven.

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

**Remaining exit evidence:** the local crash-after-claim/restart regression now proves an
interrupted intent becomes a durable `outcome_unknown` hold and cannot execute twice. Target-node
crashes at every intent/action/finalization boundary, duplicate invocation and timeout behavior
must still prove exactly one remote transfer.

### Batch 4 — Live integration and external-semantics evidence

Run the full matrix on the target Nexus build plus Solana devnet/testnet:

- both swap directions and every configured decimal pair;
- refund, quarantine and manual hold disposition;
- accepted-but-unparsed results and timeout before/after acceptance;
- process crash and restart at every durable boundary;
- pagination, processing caps and concurrent arrivals;
- malformed API bodies, Solana finality and waterline monotonicity.

**Exit:** authoritative chain read-back proves no duplicate payout, skipped deposit or
checkpoint advance from incomplete evidence; startup and periodic consumers both refuse green
unless reconciliation explicitly returns `healthy=True`.

### Batch 5 — Production operational gates

1. ✅ In explicit `SWAP_PRODUCTION_MODE`, require positive per-swap and daily payout caps and at least one configured alert route before startup.
2. ✅ Require configured Solana and Nexus quarantine destinations before production startup; test at least one alert channel operationally.
3. ✅ Refuse production mode when mandatory controls are absent.
4. Complete the operator hold-resolution workflow with evidence, authorization and audit.
5. Document incident response, recovery and key rotation; rehearse them before launch.

**Configuration gap resolved locally:** `SWAP_PRODUCTION_MODE` now accepts only explicit
true/false spellings. An unrecognized present value such as `treu` fails configuration loading,
and a production-control rejection returns `False` to the entrypoint, which exits non-zero for the
supervisor. Production admission also requires both `USDC_QUARANTINE_ACCOUNT` (a self-owned
Solana SPL token account) and `NEXUS_USDD_QUARANTINE_ACCOUNT` (the destination for a separately
authorized durable-intent disposition), preventing failed payout funds from remaining mixed with
live backing. Regression tests cover the invalid switch and both missing destinations. The remaining
Batch 5 work is operational: independently verify alert delivery, and rehearse the documented
hold-resolution, incident-response and key-rotation procedures.

### Batch 6 — Custody, dependency and maintainability hardening

1. ✅ Production uses the verified Nexus HTTPS API POST transport instead of CLI argv for PIN/session; it requires credential-free `NEXUS_API_URL` HTTPS plus API Basic credentials, while the CLI fallback remains development-only. Target-node TLS and POST-form acceptance remain live-matrix evidence.
2. ✅ Compatibility-tested and pinned `python-dotenv==1.2.2` and `requests==2.33.0` in a clean
   environment with the existing Nexus/Solana SDK pins unchanged. The live matrix remains required
   before deployment.
3. ✅ Remove dead configuration and unsafe dormant helpers: `SOL_MINT`, the direct Nexus
   DEX execution/rebalancer helpers and the direct local mint helper are absent from runtime;
   the regression contract prevents their reintroduction.
4. ✅ Remove query-string dashboard authentication; require `Authorization: Bearer` through a TLS reverse proxy for non-loopback access.
5. **In progress:** structured JSON logging now covers operator alerts and Nexus/Solana
   deposit lifecycle transitions, with field-level credential redaction. Migrate the remaining
   money-path console output and refresh this evaluation against the final reviewed commit.

---

## 8. Verification snapshot

| Check | Current result |
|---|---|
| `tests/legacy_smoke.py` | Enforced as an isolated pytest case |
| `tests/legacy_token_pair.py` | Enforced as an isolated pytest case with exact thresholds, public terms and bidirectional outputs for 6/6, 8/6, 6/8, 9/6 and 0/0 |
| `tests/legacy_session.py` | Enforced as an isolated pytest case |
| `tests/legacy_frozen_names.py` | Enforced as an isolated pytest case |
| `tests/legacy_dashboard.py` | Enforced as an isolated pytest case |
| `python -m pytest -q tests/test_critical_safety.py` | 73 passed plus 10 subtests passed on the latest local verification |
| Python byte-compilation | Passed |
| Dependency consistency | Passed |
| Local Markdown links | Passed |
| Current-tree whitespace | Passed |
| Full `python -m pytest -q` | 81 passed, 10 subtests passed in a clean Python 3.11 virtual environment after the targeted dependency remediation |
| CI workflow | Passed on dependency-remediation code head `f9e9406` — [run 33291622439](https://github.com/distordialabs-brutus/swapService/actions/runs/33291622439); earlier reconciliation evidence is [run 33258188981](https://github.com/distordialabs-brutus/swapService/actions/runs/33258188981) on `de4ae8c` |
| `pip-audit -r requirements.txt` | No known vulnerabilities found after the targeted E-013 pins |
| `pyflakes` current tree | Not green; unused/redefinition/f-string diagnostics remain and lint is not enforced in CI |
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
