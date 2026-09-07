# swapService — Current Engineering Evaluation and Remediation Plan

**Date:** 2026-09-07
**Repair baseline:** `6568446d551c785a2b96d16217b468e78a93defd`; current safety repair is an uncommitted working-tree candidate.
**Status:** Current issue register and repair priority for `swapService`
**Architecture-plan update:** 2026-09-07 (source identity, atomic fees and recovery safety repaired locally; provider-v2 remains planned).

This document replaces the old June code-level audit as the current engineering evaluation. Historical findings and their original line references remain available in [`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) and [`RISK_ASSESSMENT.md`](RISK_ASSESSMENT.md). The baseline independent evidence is in [`DEVELOPMENT_REVIEW_2026-09-07.md`](DEVELOPMENT_REVIEW_2026-09-07.md). The [post-change report](POST_CHANGE_REVIEW_2026-09-07.md) controls current repair status and verification; dated sections 10–12 below are historical snapshots, not current open-finding assertions.

## 1. Executive verdict

**Do not deploy against real funds.**

The repair work through the evaluated head materially improved the bridge:

- failed, empty, malformed and truncated debit/reference lookups no longer authorize automatic retry or refund;
- receival-asset lookup distinguishes complete absence from lookup failure;
- incomplete receival lookups hold rather than entering the refund path;
- unresolved Solana deposits are deducted from backing and spendable surplus;
- backing and circulating-supply errors fail closed;
- non-idempotent automatic surplus actions are disabled;
- recognized CLI, parse, page-budget and empty-response failures hold the Nexus waterline;
- the processing pass never proposes a Nexus checkpoint;
- every unsafe automatic Nexus refund path now holds and alerts for operator review;
- the heuristic Nexus server-side amount filter has been removed from normal and recovery scans;
- exact integer money math covers unequal-decimal pairs in both directions;
- startup no longer reports a returned unhealthy reconciliation as green;
- interrupted claimed Nexus transfers restart as durable `outcome_unknown` holds;
- explicit production mode requires positive per-swap/daily caps and an alert route;
- reconciliation errors and unhealthy results latch an exposure pause until an explicit healthy read-back;
- invalid production-mode text is rejected and admission refusal exits non-zero;
- submitted Nexus transfer txids cannot be replaced;
- unsafe dormant Nexus DEX, automatic fee-conversion and direct account-debit paths are removed;
- Nexus/Solana money-path diagnostics are structured and secret-redacted;
- one composable pytest command exists and is green locally.

Those controls are valuable. They do not make the service production-ready.
The 2026-09-07 review verifies that incoming live/recovery admission and the four Nexus lifecycle
queues/archives now preserve `(txid, contract_id)`, including valid sibling CREDITs, and that the
recovery producer rejects malformed qualifying credit identity, amount and finality fields. These
are real local repairs to the 2026-09-05 Critical admission and malformed-scan findings.

Composite source identity now extends through operator intents, references, audit, CLI selection and
atomic sibling-scoped finalization. A held source is revalidated before execution and cannot also
be claimed for a Solana payout. Strict payout memo/evidence reconstruction and an explicit startup
recovery refusal gate replace the old duplicate-payment and log-and-continue paths. Multi-page
mutable offset enumeration holds live checkpoints as well as refusing recovery completeness.
Fees are uniquely attributable per source contract and atomically committed with terminal state;
payout terms are frozen before submission and matched to full successful finalized Solana transfer
evidence before finalization. The submission helper cannot write a sparse terminal source. Legacy/ambiguous
evidence remains held. These are local code repairs, not target-chain production evidence.

### Current release-gate summary

| Gate | Status |
|---|---|
| Exact source identity; sibling disposition isolation | Repaired locally; explicit legacy holds remain |
| No blind retry after an ambiguous financial operation | Local intent/claim and recovery gates enforced; live timeout/crash proof required |
| No checkpoint from incomplete enumeration | Multi-page mutable live polls hold checkpoints and recovery refuses completeness; stable snapshot/cursor protocol remains future work |
| Live payout evidence matches frozen intent | Direct-signature and memo-recovered paths require exact finalized source identity, vault, mint, recipient and output; re-review evidence is recorded in the post-change report |
| Atomic fee and terminal source state | Implemented with rollback, replay and frozen-term fixtures |
| Installed-SDK recovery request construction | Signature-typed transaction lookups and recovery cursor are locally verified through real SDK encoders with a mocked provider; target-chain acceptance remains required |
| Exact mixed-decimal money math | Existing local regression coverage retained; target-chain matrix required |
| Complete local engineering gate | See final command results in the post-change report; do not substitute prior-head CI |
| Exact-candidate CI and live devnet/testnet matrix | Not run for this uncommitted repair |

---

## 2. Critical deployment blockers

### E-014 — Incoming Nexus contract identity is not end to end

**Severity:** Critical
**Priority:** P0 — repair before operator disposition or wipeout recovery can be trusted

**Current status: locally repaired; live and legacy-resolution gates remain.** The new migration
adds exact source identity to transfer intents and fee evidence, preserving outbound contract identity
separately. CLI selection requires the exact source contract; source validation, audit and finalization
preserve siblings and reject conflicting evidence. Legacy intents are retained but cannot authorize,
claim or finalize a fresh debit. Strict composite payout parsing and positive source/output evidence
replace synthetic txid markers during reconstruction. Startup cannot proceed from incomplete recovery.
Per-contract fee classification, terminal state and queue removal are one atomic transaction.

The following exit criteria remain the governing acceptance specification; local fixtures are listed
in the post-change report and do not replace target-node verification.

#### Required exit

1. Add immutable `source_contract_id` to every transfer intent and audit/operator command; derive the
   intent id/reference from both source fields.
2. Read, archive and delete only the exact held source identity in one transaction; never default a
   current-chain terminal row to `-1`.
3. Define and strictly parse a versioned payout memo carrying txid and contract id; retain an explicit
   manual path for legacy txid-only memos.
4. Prove two held siblings can be independently finalized and a wipeout cannot requeue an already-paid
   sibling while still recovering an unpaid sibling.
5. Add source contract identity to fee evidence and make fee classification plus terminal state atomic.

---

### E-001 — Nexus refunds are not crash-safe or idempotent

**Severity:** Critical
**Priority:** P0 — contain immediately, then implement durable protocol

**Current status: automatic execution contained; exact-source durable operator protocol repaired locally.**
Automatic Nexus refunds and quarantine movements remain disabled. The operator path now requires
`--txid` and `--contract-id`; intent references, uniqueness, authorization and finalization bind that
exact held source. Source and terminal/payout conflicts are rechecked under the database transaction
before any single-use execution claim. Finalization preserves sibling rows and requires stored remote
identity. Legacy txid-only intents retain evidence and remain manual holds, blocking a guessed fresh
debit for that txid. Interrupted execution becomes `outcome_unknown`, never a fresh attempt.

Do not enable operational transfers until the target-node crash/finality matrix passes.

**Independent follow-up remediation (2026-08-31):** every resolver now holds when its bounded
reference lookup is incomplete, normalizes Nexus `from`/`to` endpoint objects to immutable register
addresses, compares the configured token register address rather than a display name, and persists
`contract_id` with terminal state. A transfer intent that already has the txid returned by its sole
submitted debit resolves that exact transaction through `ledger/get/transaction`, requiring one
contract that matches its persisted reference, endpoints and integer units; malformed/mismatched
read-back remains held. Reference-only ambiguous outcomes still require complete stable-range
evidence. These are local fail-closed repairs; target-node query/pagination semantics and the live
matrix remain required before enabling the operator protocol.

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
intent. Automatic callers still do not invoke it. The legacy refund and quarantine preparation
wrappers also reject anything other than a positive built-in integer base-unit value before
creating an intent; they do not coerce a float, `Decimal`, boolean or string into a different
Nexus debit amount.

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

**Current status:** **contained locally; target-node evidence outstanding.** The setting and both
heuristic `where=contracts.amount...` construction paths have been removed. Normal polling
and recovery now enumerate without a server-side amount predicate and apply dust/minimum
policy locally. An empty successful poll response now holds rather than proposing `now - safety`,
because it cannot independently prove a complete, snapshot-stable range. Regression tests assert
that no `where=` argument is sent even when a legacy flag is injected into the test configuration
and that an empty page cannot advance the waterline.

The remaining release gate is stable target-node history coverage. Multi-page mutable offset
recovery explicitly returns incomplete; live polling holds its checkpoint after requesting any
nonzero offset, even if a later page is short or empty. Positive credits may still be persisted.
Two identical scans do not establish a snapshot. The preserved repeatability regression is retained.
Enabling multi-page checkpoint advancement or complete recovery requires an
independently verified snapshot/cursor protocol, not removal of this refusal.

#### Immediate containment

- ✅ Remove the server-side filter from normal and recovery enumeration.
- ✅ Apply dust/minimum policy locally after complete transaction capture.

#### Exit criteria

- A target-node test creates credits below dust, between dust/minimum, and above minimum; every expected credit is returned by enumeration.
- Unsupported/malformed query behavior produces an explicit incomplete scan and holds the waterline.
- Empty results hold locally unless a future target-node integration proves an independently stable, complete range.
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
- A confirmation count never terminalizes a submitted Solana→Nexus mint by itself. The service
  reads back the submitted txid's DEBIT contract and archives the mint only when exactly one
  contract matches its persisted reference, token-supply source, memo-derived destination and
  immutable integer Nexus output. Mismatched, missing or duplicate candidates remain held.
- An ambiguous Solana→Nexus debit is likewise resolved only after one remote DEBIT matches the
  persisted reference, token-supply source, memo-derived Nexus destination and exact integer
  Nexus output. A same-reference term collision remains held and cannot attach an unrelated
  Nexus txid to the Solana deposit.
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
any transport control is absent. When `NEXUS_MULTIUSER=true`, it also requires a non-empty
`NEXUS_SESSION` at that same admission gate; otherwise every session-scoped Nexus
`finance/*`/`assets/*` call would fail after the Solana-side poller starts. The form body carries
the Nexus profile PIN and, when `multiuser=1`, session identifier; they are therefore absent from
child-process argv and normal process listings. HTTP response bodies on transport errors are
deliberately discarded so a broken node cannot reflect those fields into logs.

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

### Target deployment configuration (partially implemented; incomplete)

The existing generic token variables are only a partial compatibility layer. Runtime and
operator surfaces still contain USDC/USDD-specific aliases, defaults, labels and account names,
and the fee policy is spread across direction-specific values, micro-amount percentages and an
inert congestion-fee setting. A fork therefore cannot yet be called completely token-pair or
fee-policy configurable.

The current code now has an immutable `SwapPairConfig` foundation, conflict-detecting aliases and
several canonical fee/custody consumers. The target architecture requires that object to be passed
to every money, reconciliation, dashboard and publication path. It must contain, for each
chain side, the network/cluster, canonical token identity (Nexus register address or Solana mint),
display symbol, decimals, custody accounts and quarantine/fee destinations. Symbols are display
metadata only; no authorization, reconciliation or routing decision may compare a ticker such as
`USDD` or `USDC` when an immutable address is available.

The same configuration object must own the complete fee policy:

- independent flat output fee and proportional basis-point fee for each direction;
- configurable minimum, dust and sub-minimum retention policy for each input side;
- explicit refund, quarantine/disposition and Nexus congestion-cost policy (zero is valid);
- fee destination/account and accounting treatment;
- exact base-unit representation, effective timestamp and a terms/configuration version.

Production startup must reject missing token identities, implicit mainnet token defaults,
negative or inexact fees, fees that can consume an otherwise accepted minimum swap, mismatched
mint/account ownership, and unpublished terms. Public terms, payout math, refund math, fee-ledger
entries, dashboard labels and reconciliation must all be derived from the same validated object so
the service cannot advertise one schedule and execute another. Legacy `USDC_*`/`USDD_*` variables
may remain temporarily as migration aliases, but conflicting legacy and canonical values must fail
startup. Existing database column names and persisted lifecycle values remain frozen until an
explicit, tested migration is provided; generic configuration does not justify rewriting in-flight
fund records.

### Provider asset identity and discovery (planned; not implemented)

Using the local asset name as the heartbeat identity is the wrong long-term boundary. A local name
is scoped to a Nexus signature chain and forces name coordination, while a provider may operate
multiple swapService instances or token pairs from that chain. The target provider record therefore
uses the exact immutable attribute `"distordia-type": "swapService"` for type discovery and the
Nexus asset **address** as the canonical runtime identity. The local name becomes an optional human
alias only.

The type attribute is necessary but not sufficient for uniqueness: discovery can legitimately
return several swapService assets. Every record must also carry an immutable `service_id`, provider
owner, schema version and complete pair/custody identity. The running instance is configured with
the selected asset address, then verifies the asset owner, `distordia-type`, `service_id`, token
identities and custody addresses before reading a waterline or publishing a heartbeat. It must
never update the first type match.

One provider asset should give a user or auditor a complete, non-secret overview of the deployment:
provider/contact/source and terms links; software and schema versions; Nexus network, token register,
decimals and treasury; Solana cluster, mint, decimals and vault; enabled directions; memo/mapping
contract; all fee, minimum and cap terms; quarantine/fee destinations; status/pause reason;
liveness timestamps and per-chain safe waterlines. Volatile balances need not be copied into the
asset because the published custody addresses are independently queryable. Secrets, private RPC
URLs, PINs, sessions and key material must never be published. The proposed v2 field contract and
migration are defined in [`../ASSET_STANDARD.md`](../ASSET_STANDARD.md#provider-swapservice-asset-standard-v2-planned).

---

## 7. Prioritized development plan

The plan is sequenced by expected fund-safety value from the evaluated head. Completed
containment and engineering-gate work stays visible because every later batch depends on it.

### Batch 0 — Immediate containment **LOCALLY REPAIRED / EXTERNALLY GATED**

1. ✅ Disable automatic Nexus refunds; hold and alert instead.
2. ✅ Remove heuristic Nexus server filtering from normal and recovery enumeration.
3. ✅ Surface every held state with chain references, reason, age and safe operator guidance.
4. ✅ Bind source identity through treasury admission, recovery, operator intents and exact finalization.

**Local containment implemented:** end-to-end source identity, strict payout reconstruction and
startup refusal are covered by local fixtures. Mutable multi-page recovery is refused. The target-node
account-history, real finality and crash/restart matrix remain release gates.

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
9. ✅ Resolve or terminalize a Solana→Nexus debit only from one exact DEBIT contract: every
   bounded/incomplete reference lookup holds, endpoint objects are normalized to immutable register
   addresses, the source is compared with the configured token register address, and terminal state
   retains `contract_id`. Target-node pagination and transaction-response semantics remain Batch 4
   release evidence.

**Evidence exit met locally:** a known balanced completed swap returns zero delta after its queue
row is gone; local and remote-only duplicates are detected; zero checked addresses return
`healthy=False`; historical completed mints retain their issued integer output across fee changes;
active first-time recipients are scanned and keep the result unhealthy without a false surplus across
a later fee-configuration change or a concurrent completion transition; both consumers refuse green
unless `healthy is True`; and every unhealthy or exceptional reconciliation result pauses new
Solana↔Nexus exposure while already-owed refunds and quarantines continue in paused mode. **Batch
remains partial:** target-node global-uniqueness, single-page boundary/order and transaction-response
semantics are unproven; local code holds whenever those properties cannot be established.

### Batch 3 — Durable Nexus refund and quarantine protocol **LOCALLY REPAIRED / LIVE GATE OPEN**

1. ✅ Persist exact source contract, destination, integer units and deterministic reference before execution.
2. ✅ Revalidate the held source at preparation, authorization and single-use claim; exclude competing payouts.
3. ✅ Retain remote identity; ambiguous results remain `outcome_unknown` without resubmission.
4. ✅ Resolve only attributable positive remote contract evidence, never bounded negative history.
5. ✅ Finalize terminal state, exact source removal and audit atomically without deleting a sibling.
6. ✅ Preserve legacy identities and remote/audit evidence as non-executable manual holds.
7. ✅ Persist payout terms before RPC and fee classification atomically with confirmed terminal state.

**Remaining exit:** run the target-node matrix at every acceptance and crash boundary. Review and
resolve pre-upgrade ambiguity from real chain evidence; do not relabel legacy rows to bypass a hold.

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
3. ✅ Refuse production mode when mandatory controls are absent, including the required `NEXUS_SESSION` when `NEXUS_MULTIUSER=true`.
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
5. ✅ Structured JSON logging covers operator alerts and Nexus/Solana deposit lifecycle
   transitions, with field-level credential redaction. Both chain pollers emit stable
   ingestion/classification summaries and fail-closed enumeration failures (including Nexus page
   and reason context) instead of console prose. The durable Nexus transfer-intent client emits
   redacted, machine-readable submission, ambiguous-outcome, hold and positive-resolution events
   keyed by immutable intent ID/reference and remote txid. All remaining direct console diagnostics
   in the lower-level Nexus and Solana client money paths now emit stable structured events; an AST
   regression rejects new `print()` calls in either client. Helius API-key values are also redacted
   from structured messages. Diagnostics remain best-effort and cannot interrupt durable state
   transitions, so operators can correlate a Nexus debit with the related Solana payout path
   without reopening an ambiguous retry path. The Nexus and Solana poller lifecycle wrappers also
   isolate structured-logging failures, so a JSON logging outage cannot stop custody processing or
   turn a durable Nexus/Solana outcome into a retryable state. Nexus deposit enumeration now uses
   the same `nexus_client._run()` transport wrapper as other Nexus reads: it retains the
   fail-closed timeout/error result handling while routing production reads through the configured
   credential-safe HTTPS POST transport and leaving `register/*` session-free. This is local
   fail-closed behavior; target-node and Solana devnet/testnet acceptance evidence remains required
   before deployment. Refresh this evaluation against the final reviewed commit before a production
   candidate is considered.

### Batch 7 — Complete configurability and provider asset v2 **(in progress; provider v2 remains documentation only)**

**Goal:** make one binary safely deployable for an arbitrary Nexus-token/Solana-mint pair and allow
several independently discoverable swapService instances under one Nexus signature chain.

**Expected implementation surfaces:** `src/config.py`, `src/nexus_client.py`,
`src/solana_client.py`, `src/swap_solana.py`, `src/swap_nexus.py`, `src/fees.py`,
`src/startup_recovery.py`, `src/dashboard.py`, `register_service.py`,
`create_heartbeat_asset.py`, `.env.example`, `CONFIG.md`, `SETUP.md`, `README.md` and new focused
tests under `tests/`. Treat `src/state_db.py` and existing SQLite/status identifiers as frozen
compatibility surfaces unless a separate append-only migration and upgrade test are part of the
same change.

1. ✅ Inventory every USDC/USDD literal, legacy config attribute, account name, database label,
   dashboard label, helper script default and public example. Classify each as runtime semantics,
   display-only metadata, migration alias or frozen persisted compatibility state.
2. Introduce canonical chain-neutral token, custody and fee-policy configuration. Require explicit
   production token identities and fee terms; retain legacy names only through one conflict-detecting
   compatibility adapter.
3. Route payout, refund, micro-amount handling, fee collection, fee accounting, thresholds,
   reconciliation, alerts, dashboard and public terms through the same validated configuration.
   Remove or implement the currently inert congestion-fee setting rather than continuing to publish
   a setting that does not affect execution.
4. Add startup validation for exact representability at both token precisions, non-negative fee
   values, coherent minima/dust/caps, correct mint/account ownership and a deterministic
   configuration/terms fingerprint.
5. Replace name-based provider-record reads and updates with address-based access. On every startup,
   verify asset owner, exact `distordia-type=swapService`, schema version, `service_id`, canonical
   token identities and custody addresses before trusting its waterlines.
6. Update `register_service.py` and retire `create_heartbeat_asset.py` behind a migration path that
   creates the complete v2 record from validated config. Because `format=basic` fixes the field set,
   do not relabel an incomplete v1 heartbeat as v2; create a new asset and record its address.
7. Support a compatibility release that can read the old named v1 heartbeat only when explicitly
   enabled, while writing/advertising the selected v2 address. Remove the name requirement after
   operators and monitors have migrated.
8. Add tests for two or more records owned by one signature chain, multiple token pairs, duplicate
   type matches, wrong-owner/type/service-id records, address/name disagreement, altered on-chain
   terms, every zero/non-zero fee component and legacy/canonical configuration conflicts.
9. Update `.env.example`, `CONFIG.md`, `SETUP.md`, `README.md`, the dashboard and inspection output
   only when the implementation exists; until then, label the v2 contract as planned.

**Required verification:** focused configuration tests must cover every fee component and conflict
case; provider-record tests must cover schema completeness, size budget, address-only updates and
multiple assets per owner; `tests/legacy_token_pair.py` must continue to prove all mixed-decimal
cases; `tests/legacy_frozen_names.py` must prove upgrade compatibility; and the final local gate is
`python -m pytest -q` plus the Batch 4 target-chain matrix.

**Exit:** no runtime financial decision or user-facing label depends on a USDC/USDD literal; a test
matrix proves independently configurable fee components in both directions and exact mixed-decimal
behavior; two services on one signature chain update only their configured asset addresses; and an
external reader can derive the complete current pair, custody, fee, limit and liveness contract from
each v2 provider asset. Target-node acceptance must also prove that Nexus supports the exact
hyphenated `distordia-type` field and address-based read/update calls before v1 is retired.

---

## 8. Verification snapshot

| Check | Current result |
|---|---|
| `tests/legacy_smoke.py` | Enforced as an isolated pytest case |
| `tests/legacy_token_pair.py` | Enforced as an isolated pytest case with exact thresholds, public terms and bidirectional outputs for 6/6, 8/6, 6/8, 9/6 and 0/0 |
| `tests/legacy_session.py` | Enforced as an isolated pytest case |
| `tests/legacy_frozen_names.py` | Enforced as an isolated pytest case |
| `tests/legacy_dashboard.py` | Enforced as an isolated pytest case |
| `python -m pytest -q tests/test_critical_safety.py` focused six-test credit/recovery selection | 5 passed plus 6 subtests; the preserved uncommitted stable-pagination test failed |
| Python byte-compilation | Passed on the working tree with an isolated bytecode cache |
| Dependency consistency | Passed — `python3 -m pip check` reported no broken requirements |
| Local Markdown links | Passed before this documentation update; final check required below |
| Token-pair inventory | 651 active lines at the committed index before this documentation update; regenerate/check against the candidate index |
| Current-tree whitespace | Passed before this documentation update |
| Full `python -m pytest -q` | **Working copy red:** 1 failed, 157 passed, 25 subtests passed in 19.00s; only the uncommitted pagination requirement failed |
| CI workflow | No exact-head CI claim was made by this local review; parent publication must verify the final published SHA |
| `ruff`, `pyflakes`, `mypy`, `pip-audit`, `bandit` | Not installed in the local environment |
| Local mocked safety probes | Reproduced sibling deletion at operator finalization and paid-credit requeue after wipeout reconstruction |
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

## 10. Independent review update — 2026-09-04 21:05 CEST

The historical evidence review at [`DEVELOPMENT_REVIEW_2026-09-04_2105.md`](DEVELOPMENT_REVIEW_2026-09-04_2105.md)
supersedes the prior severity summary. Four P0 repairs precede every other batch:

1. enumerate the canonical treasury account rather than token-register history;
2. persist and process each incoming CREDIT by `(txid, contract_id)`;
3. make heartbeat read/write/recovery use one schema and never clamp a custody waterline forward;
4. pause or exit non-zero on any incomplete startup recovery.

Executed probes demonstrated a two-contract 7,000,000-unit liability becoming one 3,000,000-unit
row, the standard top-level heartbeat schema bypassing both rebuild functions, and a 30-day-old
checkpoint silently omitting 23 days under the default cap. A separate malformed-response probe
returned `complete=True` and no durable row for a treasury credit without a txid. The full suite and
exact-head CI remain green, proving that these fixtures are missing rather than that the release
gate has passed. Medium follow-up also requires crash-atomic fee classification, direct reporting of
unmatched canonical token emissions, strict built-in-integer timestamp/confirmation validation, and
producer-level enumeration regressions rather than only injected `DepositScan` results.

## 11. Independent review update — 2026-09-05 CEST

The historical evidence review at [`DEVELOPMENT_REVIEW_2026-09-05.md`](DEVELOPMENT_REVIEW_2026-09-05.md)
verifies that C-1, C-3 and C-4 are repaired in local code: both admission and recovery query the
canonical treasury account; one strict top-level heartbeat DTO is shared across producers and
consumers; and old custody checkpoints are preserved exactly. The target Nexus node has not yet
validated account-history coverage or stable pagination, so these local repairs do not clear the
external acceptance gate.

C-2 remains Critical. Current txid-only state rejects a transaction with multiple treasury CREDITs
and holds the waterline, preventing silent sibling loss but making the transaction unrepresentable.
H-1 through H-3 also remain open: malformed qualifying evidence can pass producer completeness and
be silently skipped, mutable offset pagination can omit a boundary item, and startup only logs
recovery errors instead of aborting or latching a recovery-specific exposure pause. Production and
real-fund admission remain hard-blocked.

## 12. Independent review update — 2026-09-07 CEST

The historical evidence review at [`DEVELOPMENT_REVIEW_2026-09-07.md`](DEVELOPMENT_REVIEW_2026-09-07.md)
verifies that `594a111` repairs composite identity for live/recovery admission and lifecycle tables,
`5714df3` retains it through ordinary terminal helpers, and `6568446` rejects malformed qualifying
recovery evidence. The migration and two-sibling admission fixtures pass locally.

At that reviewed commit, C-2 was only partially closed. Operator intents/finalization were txid-only;
a local probe finalized one sibling, deleted both queued siblings and archived one `contract_id=-1`
row. Composite payout memo reconstruction could also requeue an already-paid source. Mutable offset
pagination, non-fatal startup recovery failure and non-atomic fee evidence remained blockers.

The subsequent uncommitted repair candidate addresses those code paths and the follow-up live
payout/pagination and installed-SDK defects. Current implementation and executed review evidence are
in [`POST_CHANGE_REVIEW_2026-09-07.md`](POST_CHANGE_REVIEW_2026-09-07.md) and the release-gate table
above; the historical review is not rewritten as if it examined the repaired tree. Production and
real-fund admission remain hard-blocked pending live/operational acceptance. No live financial side
effect was performed.
