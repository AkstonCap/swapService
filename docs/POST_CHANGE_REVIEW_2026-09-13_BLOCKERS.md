# Blocker repair acceptance — 2026-09-13

**Candidate:** `b91401a907eb5955f2ebebe767ca5dc94058a8f9` plus the local repair tree.
**Verdict:** concrete reviewed code blockers closed; offline verification passed.
**Release status:** not approved for real funds. No staging of the repair candidate, commit, push,
deployment, dependency upgrade or live financial transaction was performed by this repair.
Pre-existing staging remains separate and preserved.

## Repairs accepted

### Trusted Helius ingestion

- The live poller uses full parsed Helius transaction history when configured, not an unused adapter.
- Explicit Helius URL takes precedence, then a Helius-owned core URL, then an API key on the
  identified mainnet/devnet network. Known core/Helius network contradictions and ambiguous key-only
  network selection fail closed. Helius is trusted; no second attestor is required.
- Bound query identity, provider continuation, admitted rows and recoverable holds commit atomically.
  Provider switching cannot reinterpret a saved continuation. Legacy core cursors re-enumerate
  conservatively; completed-query seen rows are removed atomically.
- Exact classic-SPL parsing recognizes ordinary transfers and a real outer ATA-create/inner-init
  bundle. Unsupported but proven vault deltas retain principal rather than disappear from liabilities.
- Holds freeze network, vault, mint, observed commitment and finality requirement. Fresh status proof
  is required for unfinalized/unknown evidence. All status batches validate before promotion, each
  request stays within 256 signatures, and replay fairly reaches beyond 1,000 held rows.
- Every replay validates its actual provider endpoint even when stored evidence is already finalized.
  Missing/conflicting providers retain the hold and liability; repairing the endpoint permits one
  promotion without a redundant finality request. Core and Helius routing were independently checked.
- Public checkpoints stay behind unresolved holds. A real poll → temporary DB wipe → rediscovery
  regression verifies this boundary. Hold and pending liability totals share one SQLite read snapshot,
  preventing concurrent promotion from temporarily disappearing between separate sums.

### Recovery and receipt obligations

- Current refund/quarantine memo versions reconstruct exact source/terminal identity, fees and
  timestamped rolling-cap usage. Active Nexus mint conflicts hold reconstruction; missing memos use
  the same canonical representation as normal persistence.
- Exact payout completion atomically retains an owner-independent receipt obligation. Registration
  lookup happens later; payload construction/validation failures retain explicit manual-review state.
- Shared all-string receipt validation rejects overflowing JSON numbers before integer coercion.
  A malformed row cannot abort the batch or prevent a later valid receipt from progressing.
- Immutable owner binding, one-shot create identity, unknown-outcome retention and NXS budget controls
  remain. Optional production receipt publication remains disabled pending separate live acceptance.

## Simplification and legacy removal

The four-angle review covered reuse, quality, efficiency and whether fixes addressed the shared
mechanism rather than only one caller. Applied changes:

- Removed `fetch_incoming_deposits_via_helius`, `_fetch_deposits_helius`,
  `_fetch_deposits_core_rpc`, `process_helius_deposits`, `core_get_transactions_for_address`
  and `get_signatures_confirmation`; migrated useful parser and SDK coverage to authoritative paths.
- Removed ingestion `min_units`: all positive principal belongs in the durable processing lifecycle.
- Centralized pure receipt schema/name logic and removed the budget-bypassing receipt claim.
- Reused registration lookup per receipt batch and bounded completed-query bookkeeping.

Retained migration readers, persisted legacy names, exact database-boundary validation and explicit
uncertainty states. Broader typed-evidence refactors and merging recovery-history projections are
separate higher-risk work, not prerequisites for this repair.

## Executed verification

| Gate | Final result |
|---|---|
| Full pytest suite | **405 passed, 71 subtests passed** |
| Helius ingestion + deposit scanner | **70 passed** |
| Recovery alone | **33 passed, 46 subtests passed** |
| Recovery + installed-SDK boundary | **34 passed, 46 subtests passed** |
| Receipts + payout regressions + Nexus fee lifecycle + SDK | **85 passed** |
| Deposit → critical-safety module order | **169 passed, 25 subtests passed** |
| Critical-safety → deposit module order | **169 passed, 25 subtests passed** |
| `pip check`, compilation, local Markdown links, whitespace | Passed |
| Actual-candidate token-literal inventory | Passed, **274 active lines** |

Index-aware checks used a disposable `GIT_INDEX_FILE` containing the scoped working candidate,
including new files. Real staged entries were compared before/after and unchanged. This is not a
claim of broad lint cleanliness or a new dependency vulnerability audit.

Independent lifecycle review found the overflowing-number issue; its correction passed the receipt
suite, including independent execution of all 44 receipt tests. That review's optional hash audit
was incomplete. The final ingestion review found the replay-provider shortcut; its correction
received a PASS with no logic/security findings, independently reran 70 ingestion/scanner tests,
and exercised additional core replay and unknown-provenance probes. Runtime source stayed frozen
during the final closure review and full-suite verification; no complete signed hash manifest is claimed.

## Remaining acceptance, not established offline

Before production, execute the explicitly authorized Solana devnet/Nexus test matrix: both bridge
directions, target API shapes and pagination, finality, accepted-but-unparsed/time-out outcomes,
crash boundaries, backup/WAL restore and database-loss recovery. Rehearse operational holds, alerts,
TLS/proxy setup, incident response and custody controls. Optional receipts additionally require
actual NXS cost, indexing/readback, schema and registration-migration evidence.

Current authoritative guidance: [evaluation and development plan](EVALUATION.md),
[state machines](STATE_MACHINES.md), [configuration](../CONFIG.md).
