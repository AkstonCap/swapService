# swapService — Current Engineering Evaluation and Remediation Plan

**Repair baseline:** `b91401a907eb5955f2ebebe767ca5dc94058a8f9` plus local changes.
**Status:** reviewed code blockers corrected and offline verification passed; **not approved for real funds**.

This is the current issue register. The complete earlier evaluation and state-machine notes,
including pre-existing staged documentation, are preserved in the
[pre-repair snapshot](POST_CHANGE_REVIEW_2026-09-13_PRE_REPAIR_DOCUMENTATION.md).
Dated reviews describe their own snapshots, not the current working tree.

## Architecture and scope

One process bridges one configured classic-SPL-token/Nexus-token pair. `config.SWAP_PAIR`
provides immutable token/custody identities, independent decimals, display metadata and fee terms.
Gross conversion is 1:1 in whole-token units before fees and rounding; this is not market pricing.
Native SOL, Token-2022, arbitrary-chain routing and simultaneous pairs are not implemented.

Helius is a **trusted primary Solana history provider**, not an untrusted hint requiring a second
attestor. Application validation still requires correct network selection, exact integer amounts,
transaction success/finality and durable continuation. Core RPC is an operational alternative;
one provider's cursor must never be interpreted under another query or network.

Every financial side effect follows:

```text
persist intent → execute once → record remote identity →
resolve uncertain outcome against the chain → finalize atomically
```

A timeout is not failure. A bounded empty lookup is not proof of non-execution. A finalized
signature alone does not prove the intended transfer. Unknown outcomes retain liabilities and
budget capacity. Public recovery waterlines must not pass unresolved obligations that would be
lost with the database.

## Current repair batch

| Area | Implementation / required acceptance |
|---|---|
| Trusted Helius ingestion | Full parsed pages, fixed query identity and atomic provider-token persistence are integrated. Endpoint precedence, known-network conflict rejection and unambiguous API-key network selection are regression-tested. The final review exposed an already-finalized replay bypass; replay now validates the actual provider endpoint before every promotion. Independent closure review passed. |
| Finality and unsupported evidence | Holds freeze provenance and retain principal as a liability; unknown amounts fail accounting closed. Public recovery waterlines remain behind holds. Replay uses ≤256-signature status batches, validates all batches before promotion and fairly reaches beyond 1,000 holds. Liability reads use one SQLite snapshot during concurrent promotion. |
| Classic SPL parsing | Exact vault deltas support ordinary classic-SPL transfers. A matching outer ATA-create/inner-initialization bundle is recognized as one creation; ambiguous sources, duplicate or conflicting evidence remain held. |
| Refund/quarantine recovery — E-015, E-018 | Current versioned dispositions reconstruct exact source/terminal evidence, fees and rolling-cap spend. Recovery rejects active Nexus mint conflicts and normalizes missing memos consistently. Focused recovery and installed-SDK gates pass. |
| Receipt outbox — E-016 | Settlement atomically retains an owner-independent obligation. Builder/payload failures enter explicit manual review. The shared all-string schema rejects overflowing JSON numbers before coercion; publication continues to valid later rows. Authenticated owner binding and budgeted one-shot creation remain separate. Focused independent receipt review/testing passed; its optional hash audit was incomplete. |
| Simplification — E-010 | Shared receipt contract, one authoritative durable ingestion/admission path, one registration lookup per publication batch and completed-query seen-row cleanup reduce duplicate policy and work. Non-durable scanner helpers and the budget-bypassing receipt claim were retired; database compatibility remains supported. |
| Test isolation — E-005, E-017 | Shared offline defaults support standalone modules, real installed-SDK checks and deposit/critical-safety collection in both orders. These gates pass on the current candidate. |

### Verified current candidate

The parent reran these gates against a disposable index containing the actual working candidate,
including new files. The real staged entries were byte-for-byte unchanged by that verification.

| Gate | Actual result |
|---|---|
| Full pytest suite | **405 tests + 71 subtests passed** |
| Helius ingestion + deposit scanner | **70 tests passed** |
| Recovery standalone | **33 tests + 46 subtests passed** |
| Recovery + installed-SDK boundary | **34 tests + 46 subtests passed** |
| Receipt + payout + Nexus fee lifecycle + SDK | **85 tests passed** |
| Deposit then critical safety | **169 tests + 25 subtests passed** |
| Critical safety then deposit | **169 tests + 25 subtests passed** |
| Dependency consistency / compilation / Markdown links / whitespace | Passed |
| Candidate token-literal inventory | Passed; **274 active lines** |

These are offline results, not live-chain acceptance. The final independent ingestion review and
its narrow replay-provider closure review completed; the latter independently passed the same
70-test ingestion/scanner suite and additional core-replay probes. Runtime code was held unchanged
during that review and the final full-suite verification. See the [repair report](POST_CHANGE_REVIEW_2026-09-13_BLOCKERS.md).

### Applied cleanup and deliberately retained boundaries

- Removed `fetch_incoming_deposits_via_helius`, `_fetch_deposits_helius`,
  `_fetch_deposits_core_rpc`, `process_helius_deposits`, `core_get_transactions_for_address`
  and `get_signatures_confirmation`; parser/SDK coverage now exercises authoritative scanners.
- Removed the ingestion `min_units` parameter: all positive principal must enter the liability
  lifecycle, where processing policy may classify it. History filtering cannot discard it.
- Centralized pure receipt schema/name validation in `receipt_contract.py`; independent exact-payout
  validation at the database boundary remains intentional defense, not duplicate code to delete.
- Purged completed-query `scan_seen` rows atomically and reused provider registration within a receipt
  batch. Kept immutable hold provenance, explicit unknown-outcome states and frozen database names.
- Deferred broader typed-evidence/DB-layer restructuring and combined recovery-history projections:
  these change safety-critical interfaces and are not justified as cosmetic cleanup.

## Existing controls retained

- **E-014:** Nexus source identity is `(txid, contract_id)` throughout admission, payout memos,
  operator intents and terminalization; an unpaid sibling remains independent. Legacy identity
  sentinels cannot authorize a guessed fresh transfer.
- **E-001 / E-007:** automatic Nexus refunds/quarantine remain disabled. The separate operator
  workflow prepares, authorizes, executes once, resolves positive evidence and finalizes one exact
  source. Live operational acceptance remains required.
- **E-002:** no heuristic amount filter hides Nexus credits. Mutable multi-page offset scans hold
  checkpoints; processing-only passes cannot claim enumeration completeness.
- **E-003 / E-004:** exact mixed-decimal money calculations and durable completed-payout evidence
  support fail-closed reconciliation. Errors/unhealthy results latch an exposure pause.
- **E-008 / E-009 / E-012:** explicit production admission requires configured pair/fee terms,
  positive exposure caps, alerting, quarantine destinations and authenticated Nexus HTTPS transport.
  The dashboard uses bearer-header authentication, not query tokens. Live TLS/proxy/alert verification
  remains an operator gate.
- **E-013:** earlier compatibility-tested dependency remediation is retained. This repair does not
  upgrade dependencies; a dependency-consistency check is not a fresh vulnerability audit.
- **E-011:** current documentation is separated from historical evidence. Persisted legacy names
  are migration contracts, not proof that the bridge only supports its original token pair.

## Remaining release gates

### Local candidate gate — completed for this working tree

- Full suite, focused financial regressions and installed-SDK/config-order shards passed.
- Dependency consistency, byte compilation, local Markdown links and whitespace passed.
- Token-literal inventory passed against the actual candidate in a disposable index; real staged
  entries were unchanged. The working candidate has not been staged or committed.
- Independent reviews identified concrete blockers, each corrected and regression-tested; the last
  closure review passed with no logic/security findings. Runtime was frozen for that in-place review.
  The earlier optional receipt hash audit did not complete; no signed/full hash attestation is claimed.

Any subsequent runtime edit requires renewed affected-path review and test verification.

### Target-chain acceptance — E-006

These are **not established by offline fixtures**:

- Helius target-network history, real nested transaction shapes, continuation/restart, confirmed
  versus finalized behavior and concurrent arrivals over bounded ranges.
- Both bridge directions, mixed decimals, refund/quarantine, and exact authoritative readback.
- Accepted-but-unparsed responses, timeout before/after acceptance, crashes at durable boundaries,
  backup/WAL restore and database-loss reconstruction without skipped deposits or double payout.
- Nexus reference/contract fields, pagination/completeness, TLS and POST semantics on the intended build.

No live financial transaction is authorized by this repair. Run the matrix on explicitly approved
Solana devnet and Nexus test infrastructure before any production decision.

### Optional receipts — E-016

Receipt assets and names spend operator NXS. Keep `NEXUS_SWAP_RECEIPTS_ENABLED=false` for production;
the admission gate still rejects an explicit enablement. Required separate acceptance covers the
actual creation/name cost, budget adequacy, timeout-after-acceptance, indexing delay, exact owner and
payload readback, duplicate detection, and receipt-capable registration migration. A fixed-field v1
asset cannot gain `receipt_schema` through a heartbeat update.

### Operations — E-007 / E-009

Rehearse hold resolution, alert delivery, incident response, backups and key rotation. Maintain
independent authorization for ambiguous financial dispositions. Unknown/legacy evidence remains
held, not silently rewritten or released by an upgrade.

## Prioritized development plan

1. **Completed:** repair and independently review this local candidate; preserve safety controls and compatibility.
2. Execute the target-chain matrix and operator rehearsals with recorded authoritative evidence.
3. Enable optional receipts only after their separate cost/schema gate; otherwise leave them disabled.
4. Pursue provider-v2 and remaining configuration consolidation as a separate versioned migration.

### Batch 7 — Complete configurability and provider asset v2 **(in progress; provider v2 remains documentation only)**

The implemented canonical pair object is not the planned provider-v2 contract. Remaining work:

- Consolidate network, custody, complete fee/minimum/dust terms and a deterministic terms fingerprint.
- Replace named-v1 heartbeat identity with an explicitly selected immutable asset address; validate
  owner, `distordia-type=swapService`, schema, `service_id` and complete pair/custody identity.
- Create a new complete fixed-field registration rather than relabelling v1, with an explicit
  compatibility release and migration evidence.
- Test multiple services/assets per signature chain, address/name disagreement, altered terms and
  every configured fee/decimal/legacy-alias conflict. Never select the first type match.
- Publish only non-secret identity, terms, custody, status and liveness data; never private RPC URLs,
  PINs, sessions or keys.

See the [planned standard](../ASSET_STANDARD.md#provider-swapservice-asset-standard-v2-planned),
[configuration reference](../CONFIG.md), [state machines](STATE_MACHINES.md), and
[token-literal inventory](TOKEN_PAIR_LITERAL_INVENTORY.md). Provider-v2 is not a prerequisite to
correctly repairing the existing single-pair bridge, and is not implemented by this batch.
