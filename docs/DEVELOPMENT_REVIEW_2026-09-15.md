# swapService Architecture, Development and Financial-Safety Review — 2026-09-15

**Review HEAD:** `6b1f052a2018f0315603e64a440d71cb612e8212` (`main`, matching `origin/main` at review start)

**HEAD tree:** `c8f7743ea096c56bb3dc1fe103f60ea66e12c82a`

**Prior cron-review source:** `d0acd721d4af534c2b1313565ff3677cfb59e73e`

**Latest follow-up baseline:** `b91401a907eb5955f2ebebe767ca5dc94058a8f9`

**Committed delta from the cron source:** eight commits, 29 files, 7,635 insertions and 2,345 deletions

**Dirty implementation candidate:** modified `src/config.py`; untracked `src/service_record.py` and `tests/test_service_record_v2.py`

**Release verdict:** **HARD BLOCKED for production and real funds**

## Scope separation

The committed implementation and the dirty provider-v2 candidate were reviewed separately.

- The committed range is `d0acd721..6b1f052`. Commit `6b1f052` publishes the previously
  reviewed Helius ingestion, disposition-recovery and receipt-outbox repairs together with their
  tests and documentation. The captured raw diff remains local and is not part of review publication.
- The dirty candidate's captured patches remain local under `docs/review_evidence/2026-09-15/`
  and are deliberately excluded from publication to avoid publishing another coder's unfinished
  implementation as documentation. Its runtime files do not match any hash in the 2026-09-12 manifest. No prior report contains either untracked path or their current
  hashes, so they are new provider-v2 work rather than previously reviewed repair code that was
  merely left untracked.
- The real index had no staged entries and started at SHA-256
  `15d4c078b7bfd0a0addbbd6a8041bbadb20a6e12f8f4154b2830ec6ae10cf3f2`.
  Nothing was staged, committed, pushed, deployed or started by this review.

No live Nexus/Solana operation, service startup, key use, payout, refund, quarantine transfer or
Nexus asset create/update was performed. All executable probes used temporary SQLite databases,
non-routable fixture endpoints and mocked external mutation boundaries.

## Severity-ordered findings

### Critical — wipeout recovery can convert an arbitrary disposition shortfall into a terminal fee

**Scope:** committed HEAD; newly identified in this review.

`scan_memos_since_timestamp()` now proves the successful finalized outgoing refund/quarantine,
reads the named incoming deposit and binds source signature, token account, vault, mint, destination
and chronology. That closes the 2026-09-12 fail-open omission. The terminal reconstruction boundary
still has no frozen fee or intended-output evidence after database loss:

- `src/startup_recovery.py:399-422` accepts any positive disposition output no greater than the
  incoming principal;
- `src/state_db.py:3426-3429` applies the same `payout <= source` condition;
- `src/state_db.py:3535-3560` defines the entire unexplained difference as a refund/quarantine flat
  fee and records it as operator revenue.

The current memo, `swapService:v1:<kind>:<source_signature>`, does not carry output units, fee units,
terms version or terms hash. A clean-database probe supplied a 1,000,000-unit incoming deposit and a
successful one-unit refund bearing the exact current memo and recipient. The real reconstruction
helper returned `True`, wrote `refund_confirmed`, and booked 999,999 units as `refund_flat_fee`.
This can erase a user liability and legitimize an accidental or malicious underpayment after local
state loss. Exact chain transfer evidence is not exact intent evidence.

**Coder-ready repair:** do not auto-terminalize a current v1 disposition from chain-only wipeout
evidence. Reconstruct its actual outgoing amount into rolling-cap evidence, but retain the source as
an explicit unresolved/manual-review liability unless a surviving database/backup row proves the
frozen output and fee. Introduce a new memo/evidence version that binds source signature, kind,
integer output, integer fee and immutable terms version/hash before automatic terminal
reconstruction. Do not infer historical policy from current configuration.

**Exit tests:**

1. A source of 1,000,000 and current-v1 payout of 1 is rejected for terminal/fee reconstruction;
   the one-unit outgoing spend still consumes cap and the remaining liability stays visible.
2. Exact matching frozen evidence reconstructs one terminal row, one fee and one cap lifecycle,
   idempotently across wipeout and backup/WAL restore.
3. A policy change between source and recovery cannot alter the fee or make an old v1 memo
   automatically terminal.
4. Wrong/missing/duplicate output or fee identities hold startup without deleting source evidence.

### High — Solana minimum and micro policy is no longer enforced after safe ingestion

**Scope:** committed HEAD; newly identified in this review.

The Helius repair correctly removed ingestion-time `min_units` filtering so every positive custody
principal enters durable state. No processing classifier replaced that filter. A runtime search
finds `MIN_DEPOSIT_SOLANA_UNITS` only in configuration, startup display and public record builders;
`process_unprocessed_solana_deposits()` at `src/solana_client.py:1020-1143` does not compare it.
It validates the memo/account/cap, calculates a normal Nexus payout and submits whenever net output
is positive.

The real worker probe persisted a 150-unit source while the configured minimum was 200 and the flat
fee was 10. With only the Nexus account lookup and remote debit boundary mocked, the worker called
`debit_nexus_token_with_txid("recipient", 140, 1)` and moved the source to
`debited, awaiting confirmation`. This contradicts the configured/public minimum and the documented
micro policy and can turn dust-sized deposits into repeated Nexus side effects.

**Coder-ready repair:** add one shared Solana-source classifier, analogous to
`classify_nexus_credit`, used by normal processing and startup reconstruction. Every positive amount
must remain accounted. Define one explicit below-minimum disposition (full fee, refund, or durable
operator hold), route it atomically through the corresponding fee/liability state, and derive public
micro terms from that actual behavior. Do not restore history filtering.

**Exit tests:** exercise below, exactly at and above the configured minimum with unequal decimals and
zero/nonzero flat and basis-point fees. The below-minimum case must make no Nexus API call unless the
published policy explicitly authorizes that payout; its complete principal and any fee must remain
reconcilable after restart and wipeout.

### High operational — refund/quarantine cap refusal remains generic and log-only

**Scope:** committed HEAD; previously open on 2026-09-12 and not closed by the follow-up.

At `src/solana_client.py:1287-1298` and `1407-1418`, `prepare_solana_sig_disposition()` returning
false leaves the source in generic `to be refunded` / `to be quarantined` state and emits only a log.
The helper conflates capacity exhaustion, competing lifecycle and evidence conflict. The executed
refund probe filled the cap, confirmed no send and no attempt/cap event, but the durable status
remained exactly `to be refunded` with no needed/used/cap reason.

**Coder-ready repair:** have the transactional preparation boundary return a typed refusal and, for
capacity refusal, atomically persist a distinct retryable cap-held state with obligation, needed,
used and cap units. Expose it in dashboard/API issues and emit the same rate-limited critical alert
class used by primary payout cap holds. Evidence/lifecycle conflicts must use a separate manual hold.

**Exit tests:** cap refusal makes no RPC call and consumes no attempt, remains distinguishable after
restart, reports exact units, alerts once under rate limiting, and can progress only through the same
transactional reservation path after capacity becomes available.

### High candidate blocker — provider-v2 advertises policy the runtime does not execute

**Scope:** dirty candidate only.

`src/service_record.py:351-360` publishes `MICRO_DEPOSIT_FEE_PCT`, `MICRO_CREDIT_FEE_PCT`, the Solana
minimum and a zero dust value as effective terms. `CONFIG.md:114` correctly states that both
percentage settings are parsed but unused and must not be published as configurable policy. The
committed Nexus path retains 100% of below-minimum credit as fee, while the committed Solana path
currently processes positive-net below-minimum deposits as ordinary payouts. The focused dirty test
even sets 99% and 98% and expects those unenforced values in the record.

**Coder-ready repair:** first implement one canonical executable micro/minimum policy for both
chains and recovery, then build the public projection from that immutable policy object. Until then,
publish only truthful fixed behavior or omit unsupported fields; do not treat parsed environment
variables as controls.

**Exit tests:** for each published percentage/minimum/dust field, map one record value to a collected
live-worker test and a recovery test. Exact below/boundary/above cases must produce the advertised
integer fee/output and the same terms hash under unequal decimal pairs.

### High candidate blocker — provider-v2 is a library, not the default runtime contract

**Scope:** dirty candidate only.

The dirty `src/config.py` says provider-v2 is the default and adds an opt-in legacy flag, but no
tracked runtime module imports `src.service_record`. Existing `build_service_record()`, named-v1
reads/updates, registration tooling, heartbeat validation and startup recovery remain unchanged.
`ALLOW_LEGACY_PROVIDER_V1`, `NEXUS_SERVICE_ASSET_ADDRESS`, expected owner and terms revision are not
consumed by those callers. `ASSET_STANDARD.md:194-200` correctly still labels v2 planned and warns
not to configure it as the waterline source.

The builder also permits zero terms version/effective timestamp by default and contains no
monotonic update/readback protocol. Its unit tests prove an isolated DTO, not address-based Nexus
creation, selection, update, restart or multi-instance isolation.

**Coder-ready repair:** keep the new module explicitly experimental until the entire address-based
reader/writer/recovery path exists. Then require configured address, expected owner, service ID,
network and positive/monotonic terms metadata in production; validate built-in owner/address from
Nexus response metadata; update only the configured address; exact-read back the full immutable
record before trusting waterlines; and make legacy-v1 fallback a tested explicit compatibility
branch rather than an unused boolean.

**Exit tests:** two v2 assets under one owner cannot cross-read/update; wrong owner/type/schema/service
ID/pair/custody/terms hold startup; duplicate type discovery never selects a writable record; update
failure cannot advance local/public waterlines; v1 fallback is impossible unless explicitly enabled;
and exact target-node create/update/readback proves field and size semantics.

### High candidate security — secret substring can be published irreversibly

**Scope:** dirty candidate only.

`_assert_no_secret_collision()` at `src/service_record.py:220-225` rejects only when a whole public
field exactly equals a configured secret. The executed builder probe set the API password to
`leaked-password` and contact to `https://public.invalid/?token=leaked-password`; the record built
successfully and contained the password. This contradicts the module and asset-standard guarantee
that credentials/private endpoints are never published.

**Coder-ready repair:** validate public URL fields structurally; reject userinfo, secret-like query or
fragment material and private RPC endpoints. Compare every nontrivial configured secret as a
substring of every public value after canonical decoding, and consider fail-closed entropy/token
checks. Keep an explicit allow-list of publishable configuration sources rather than inspecting a
configuration dictionary.

**Exit tests:** each configured PIN/session/API password/API user/Helius key/keypair path embedded as
an exact value, URL component, query value or percent-encoded substring is rejected before any Nexus
create/update call. Ordinary public HTTPS source/terms/contact URLs remain accepted.

### External and operational gates remain open

No target Helius/Solana devnet or Nexus test-node matrix ran. The review does not establish live
continuation semantics, finality, accepted-but-unparsed outcomes, crash timing, actual backup/WAL
restore, Nexus POST/TLS/query behavior, alert delivery, key rotation or two-person dispositions.
Helius remains the configured trusted provider; no second attestor is required. Application-level
exactness, durable cursor binding and external acceptance still are required. Optional receipt
publication remains production-disabled pending actual NXS cost, create/index/readback and
registration-migration acceptance.

## Closed or positively verified controls

The following prior findings are closed **locally** in committed code, subject to the new recovery
intent defect and the external matrix above:

- Helius is the actual primary ingestion path when configured; provider/network/query identity and
  continuation are durable and cannot be reinterpreted through core RPC.
- Complete page/order/schema validation, principal-retaining holds, fair bounded replay, provider
  revalidation and waterline pinning are present. Helius/core ingestion regressions passed 70 tests.
- Current refund/quarantine memos are discovered, source transactions are read, active Nexus-mint
  conflicts hold, and terminal/cap/fee writes are atomic and idempotent for accepted evidence.
- The previously dropped receipt obligation is retained independently of owner lookup; malformed
  payloads enter manual review and do not stop later rows. Receipt/payout/fee/SDK regressions passed.
- Composite Nexus `(txid, contract_id)`, primary payout exact proof, intent-first sends, integer money
  math and test isolation remain present in the exercised suite.

These controls do not make the service production-ready.

## Verification record

| Scope | Result | Evidence |
|---|---|---|
| Final shared-tree full pytest, committed runtime plus dirty v2 candidate and review docs | **434 passed, 71 subtests passed** | [`final-candidate-full-gate-pytest.log`](review_evidence/2026-09-15/final-candidate-full-gate-pytest.log) |
| Helius ingestion + deposit scanner | **70 passed** | [`ingestion-focused.log`](review_evidence/2026-09-15/ingestion-focused.log) |
| Recovery + installed SDK boundary | **34 passed, 46 subtests passed** | [`recovery-sdk-focused.log`](review_evidence/2026-09-15/recovery-sdk-focused.log) |
| Receipt + payout + Nexus fee + SDK | **85 passed** | [`receipt-payout-fee-sdk-focused.log`](review_evidence/2026-09-15/receipt-payout-fee-sdk-focused.log) |
| Dirty provider-v2 module | **29 passed** | [`dirty-v2-focused.log`](review_evidence/2026-09-15/dirty-v2-focused.log) |
| Review probes asserting reproduced unsafe behavior | **5 passed** | [`targeted-review-probes.log`](review_evidence/2026-09-15/targeted-review-probes.log), [`source`](review_evidence/2026-09-15/targeted_review_probes.txt) |
| Disposable-index inventory/whitespace plus dependency consistency, Python compilation, Markdown links and unchanged real index | Passed | [`final-candidate-static-index-gate.log`](review_evidence/2026-09-15/final-candidate-static-index-gate.log) |
| Target chain / live funds | **Not run** | none |

The first custom-probe run had one fixture error: it passed integer `50` to an API that parses Nexus
whole-token amounts, yielding a payable amount. The corrected probe uses canonical `"0.00005"`
for 50 base units and passed. The initial fixture-failure log is retained locally as
`docs/review_evidence/2026-09-15/targeted-review-probes-initial-failed.log` and is not part of publication.

Publication is documentation-only: the temporary candidate-index inventory coordinates were restored
to the committed-runtime coordinates before staging. The dirty `src/config.py` and untracked provider-v2
implementation/test remain outside the commit. Candidate results above retain that explicit wider scope;
the staged inventory gate and exact documentation-head CI must be checked separately.

A separate exact-HEAD clone/full-gate command was denied by unattended approval policy and was not
retried or rerouted. The September 13 follow-up records the prior exact-candidate 405-test gate; the
fresh final 434-test shared-tree run above exercises that committed runtime plus the isolated dirty v2
module but is not represented as a clean-checkout exact-HEAD CI run. No fresh dependency advisory
scan or exact-head remote CI claim is made.

The content-addressed inventory of runtime files inspected for this review is
[`review_evidence/2026-09-15/runtime-files.sha256`](review_evidence/2026-09-15/runtime-files.sha256).

## Repair order

1. **P0:** stop chain-only v1 disposition evidence from terminalizing an unproven payout/fee intent;
   count actual spend while retaining the unresolved liability.
2. **P1:** restore one explicit, shared Solana minimum/micro classifier after durable ingestion.
3. **P1:** persist typed actionable refund/quarantine cap-refusal states and alerts.
4. **P1 before merging provider-v2:** derive every advertised term from enforced runtime policy,
   prevent substring/URL secret publication, and keep v2 non-default until address-based
   reader/writer/recovery integration is complete.
5. **Release gate:** execute the target Helius/Solana/Nexus finality, pagination, timeout, crash,
   restore, reconciliation and operational matrix on the exact published candidate.

Production and real-fund admission remain hard-blocked. This documentation review authorizes no
financial operation or deployment.
