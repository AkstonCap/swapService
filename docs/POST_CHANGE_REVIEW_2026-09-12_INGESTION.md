# Live Solana ingestion safety repair — 2026-09-12

**Baseline:** `b392059` (recovery containment already committed).
**Scope:** local source/test/documentation changes only; no commit, push, deployment, credentials or live transaction.
**Release verdict:** production and real funds remain blocked.

## Critical issue reproduced

`_fetch_deposits_core_rpc()` returned `[]` after an RPC timeout and silently continued past unreadable
transactions. Both adapters could return a deposit-limited subset without proving scan completion.
The real `poll_solana_deposits()` interpreted a returned list as `fetch_ok=True` and could advance its
heartbeat past missing deposits. A corrected isolated poller regression (all side effects replaced)
failed before the repair: `WATERLINE_ADVANCED` followed an injected `offline RPC failure`.

The enriched parser also ignored `transactionError`. A synthetic failed transaction with a reported
transfer was queued as `ready for processing`; this is not evidence of a real-chain exploit, but proves
the adapter did not enforce successful execution before creating a financial obligation.

## Implementation

- The enriched adapter requires a list response, valid unique signature/timestamp and explicit outcome.
  Failed transactions are excluded. Unknown outcomes/schema, repeated identities, a 100-page budget,
  or reaching the deposit budget before coverage is proven request a fresh core scan, not partial success.
- The core adapter preserves valid empty RPC results but rejects missing/null/error envelopes and invalid
  pages. It requires signature/timestamp/outcome/commitment evidence and matching transaction identity.
  Missing transaction metadata or token-balance lists and per-transaction exceptions fail the scan.
- Core transaction reads explicitly request the configured commitment and transaction version support.
- A full bounded core page without the waterline, or more deposits than the processing budget, raises.
  This is intentional containment rather than an unimplemented claim of complete pagination.
- The existing poller's exception path keeps the waterline held. It receives no partial list from the
  failing scan. Existing queue work is not erased, replayed or financially compensated by this repair.
- Exact successful/empty scans remain supported. No SQLite schema, intent identity, fee policy or
  financial send helper changed.

## Verification record

- Before source edits: baseline full suite **310 passed, 53 subtests passed**.
- New initial regression module: **19 failed, 1 passed**; its poller test initially needed an isolation
  correction. After that correction, the poller separately failed for the intended reason: it actually
  advanced the waterline on the injected RPC timeout.
- After the initial repair: full suite **330 passed, 53 subtests passed**.
- Expanded boundary tests: **27 passed** standalone, including real installed SDK request builders with
  only provider transport replaced, failed/unknown outcomes, malformed RPC data, per-transaction failure,
  saturated pages, exact watermark completion, repeated enriched cursors and real-poller checkpoint hold.
- First independent review rejected source hash `91cf01cd87a32f45a04e49fd8e14b9779b38477b94be55dffac7c51402269c31`:
  an old timestamp could terminate a scan before a newer entry later in the same page. Added tests
  reproduced the issue (**4 failed, 32 passed**). Both scanners now validate the complete page's
  nonincreasing timestamps before using the waterline; enriched pagination validates cross-page order
  too. Unsupported nonmonotonic history holds instead of sorting or skipping it.
- The expanded regression module now passes **42 tests**, including reversed pages, failed second pages,
  fallback failure, transaction RPC errors/signature mismatch, exact configured-vault account-index
  selection despite another same-owner token account, exact classic-SPL transfer/mint/delta matching,
  ambiguous-transfer/memo holds, and a real-poller out-of-order hold.
- Final scoped-candidate gates: full suite **351 tests passed, 53 subtests passed**;
  ingestion→recovery→SDK isolation **65 passed, 28 subtests passed**; and
  receipt→payout→fee→SDK isolation **70 passed**.
- A prior independent re-review covered the original scan-containment candidate. This follow-up adds
  core-only exact-vault evidence and must not be represented as independent approval of the still-open
  enriched-provider normalization path.

## Remaining issues and implementation order

1. **P0 — complete exact deposit normalization.** The core scanner now binds a positive
   base-unit vault delta to the configured vault's unique transaction account index, one classic
   `transferChecked` instruction for the configured mint, its source token account and at most one
   memo; ambiguous/malformed core evidence holds the scan. Enriched `tokenAmount` parsing still guesses
   whether values are base units and falls back to six decimals, so replace that provider-specific path
   with the same authoritative immutable evidence model. Cover account creation/closure, zero-decimal
   pairs and provider schema drift. Audit/revalidate existing queued rows before deployment; this patch
   does not retroactively validate old inputs.
2. **P1 — complete backlog progress.** Implement stable-range/cursor pagination with durable coverage and
   restart tests. The current bounded core scan intentionally refuses a saturated page; a long outage or
   busy vault may remain held until reviewed backfill. Do not override heartbeat checkpoints to clear it.
3. **P1 — recovery availability.** Implement exact refund/quarantine terminal and timestamped cap reconstruction;
   `b392059` currently holds rather than reconstructing those unsupported/ambiguous vault spends.
4. **P1 — receipt and operator evidence.** Preserve an owner-independent publication outbox on provider lookup
   failure; persist disposition cap refusal reasons and make dashboard cap-ledger failure visibly unhealthy.
5. **Release gate — target-provider acceptance.** No live Solana/Nexus tests ran. Validate Helius method/schema
   compatibility, filtered history coverage/order, finality, backlog/cursor behavior and crash/restore with
   non-production profiles before real-fund admission.

Architecture and prioritized coding exits are synchronized in [EVALUATION.md](EVALUATION.md) and
[STATE_MACHINES.md](STATE_MACHINES.md). Historical reviews and existing staging are preserved.
