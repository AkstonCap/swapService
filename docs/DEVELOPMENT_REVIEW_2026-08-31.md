# Independent Financial State-Machine Review — 2026-08-31

**Review window:** 2026-08-29 16:25:39 +0200 through 2026-08-31 09:30:00 +0200
**Baseline:** `5e7d3b853e49b4d3b229b6e7ba8770f158a7edc5`
**Reviewed head:** `cc175cb595ebe7d1fedd8173020e2a133627906a`
**Scope:** 32 commits, 30 changed files
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The range materially improves containment: reconciliation now latches an exposure pause, invalid production booleans fail configuration, startup refusal exits non-zero, interrupted transfers hold, automatic unsafe surplus/DEX paths are removed, transport credentials are hardened, diagnostics are structured, and a submitted Nexus transfer txid is immutable.

Those improvements do not close production admission. Empty “successful” Nexus enumeration can still advance the waterline; exact debit resolution collapses multiple contracts in one txid; a confirmed txid terminalizes a mint without proving its transfer terms; remote reconciliation intentionally cannot pass beyond one page; transfer-intent construction coerces money; production admission omits a required multiuser session; and target-chain crash/pagination/finality evidence remains absent.

## Severity-ordered findings

### Critical — Empty successful Nexus history still advances the checkpoint

`src/swap_nexus.py:583-623,837-859` initializes enumeration as complete and treats an empty parsed page as the end of a valid range. With no page timestamps or pending rows it proposes `time.time() - safety`; `src/main.py:548-554` applies that proposal.

Mocked result with time 10000 and response `[]`:

```text
waterline_proposals= (None, 9880)
NEXUS_WATERLINE_ADVANCED ... new_ts=9880 reason=no_unprocessed_txids
```

An empty bounded response is not proof that the interval was completely enumerated. A stale, filtered, transient or semantically unexpected response can skip deposits permanently. The prior Critical blocker remains open.

### High — “Exact” debit resolution collapses multiple contracts to one txid

Both transfer-intent and unverified-mint resolution reduce exact candidate evidence to a set of remote txids and finalize when that set has length one (`src/nexus_client.py:552-573,818-839,1213-1238`). `TransferDebitEvidence` omits `contract_id`.

Two matching DEBIT contracts in the same transaction are therefore accepted as one exact debit. The unverified-mint resolver also fails to match the expected source account. Mocked two-contract evidence finalized the intent as `completed`.

**Required exit:** preserve `(txid, contract_id)`, require exactly one exact contract, and compare source, destination, amount, reference and submitted txid.

### High — Confirmation count terminalizes a mint without proving transfer terms

`check_unconfirmed_debits()` (`src/nexus_client.py:656-750`) verifies only the stored txid's confirmation count. Once confirmed, it archives locally persisted terms and deletes the queue row without reading back the actual DEBIT contract identity, source, destination, amount or reference.

A deliberately unrelated confirmed txid reproduced:

```text
processed_count= 1
processed_row= ('deposit', 'unrelated-confirmed-tx', 1898000, 'debit_confirmed', 77)
queue_row= None
```

Immutable local intent terms do not prove that the submitted remote txid executed them.

### High release blocker — Remote mint reconciliation cannot scale past one page

`find_nexus_mint_debits_since()` intentionally reads one page and returns `pagination_snapshot_unavailable` when a full page does not cross the requested boundary (`src/nexus_client.py:1055-1078,1168-1171`). This is safe containment, but from a zero/default waterline reconciliation cannot become healthy once history fills that page, so the exposure pause cannot clear.

Target-node short-page, equal-timestamp boundary, ordering, nested-contract and finality behavior remain unverified. This is an open release gate, not a defect to “fix” with unsafe offset pagination.

### Medium — Transfer-intent construction silently truncates non-integer money

`src/state_db.py:472-492` calls `int(amount_usdd_units)` without rejecting bool, float, Decimal or textual values. Reproduction:

```text
input_amount=1.9 persisted_amount= 1
```

An immutable money API must accept an exact runtime integer only.

### Medium — Production admission accepts multiuser mode without a session

The production gate (`src/main.py:89-123`) checks transport and credentials but does not require `NEXUS_SESSION` when `NEXUS_MULTIUSER=True`. `apply_session()` then emits an unscoped command. Reproduction with every enforced production control present:

```text
production_gate_with_multiuser_and_empty_session= True
```

This likely fails closed at the node, but makes the production-admission verdict false and disables finance operations after startup.

### Medium — Structured logging faults can silently terminate poller work

Money-path `_log()` wrappers do not isolate logging exceptions (`swap_nexus.py:37-39`, `swap_solana.py:8-10`). `_run_with_watchdog()` records worker exceptions but does not report or rethrow them (`main.py:202-209`). Injected logging failures raised out of both pollers. Diagnostics must not silently abort money-path work.

### Medium — Operational documents contradicted current behavior

Before this review, `STATE_MACHINES.md` still claimed reconciliation did not pause exposure and showed an awaiting Nexus debit refunding when a txid did not appear. Current code holds ambiguous/missing evidence. `EVALUATION.md` also described the pre-pause state. These documents are corrected alongside this review.

### Low — Existing range fails the whitespace gate

`git diff --check 5e7d3b8..cc175cb` reports trailing whitespace at `src/main.py:451`. No application code was changed during this architecture review.

### Low — Nexus deposit polling bypasses the common transport wrapper

`swap_nexus.py:535,590-597` invokes subprocess directly. This is a public `register/*` read and does not expose PIN/session credentials, but it is an architectural exception to common timeout/error behavior and should be explicit and tested.

## Positive controls verified

- Reconciliation exposure pause is latched and cleared only by an explicit healthy read-back.
- Invalid production-mode text is rejected; admission refusal exits non-zero.
- Local completed-mint evidence has exact SQLite integer, destination, reference, output, txid and confirmation checks.
- Submitted transfer txid replacement is refused.
- Automatic unsafe Nexus refund, quarantine, surplus and dormant DEX paths remain disabled/removed.
- Dashboard query-token authentication is removed.
- Structured JSON logging and secret-key redaction are present.
- Negative, failed or bounded history does not authorize retry/refund.

## Prior-blocker disposition

| Prior blocker | Current status |
|---|---|
| Reconciliation does not pause exposure | **Closed locally** |
| Malformed production boolean / zero exit | **Closed locally** |
| Production admission | **Partial** — session prerequisite omitted |
| Explicit failed/malformed/truncated enumeration advances waterline | **Closed locally** |
| Empty successful enumeration advances waterline | **OPEN — Critical** |
| Remote history pagination/ordering semantics | **Contained; release gate open** |
| Direct debit/transfer exactness | **OPEN** — contract multiplicity and txid-only confirmation |
| Immutable intent fields | **Mostly closed** — constructor still coerces units |
| Submitted transfer txid preservation | **Closed locally** |
| Refund/quarantine duplicate execution | **Contained locally; live crash evidence absent** |
| Authoritative remote mint reconciliation | **Local fail-closed implementation; target evidence open** |
| Structured diagnostics | **Implemented; logger/watchdog isolation incomplete** |
| Unresolved liabilities / mixed-decimal math | **Closed locally** |
| Live Nexus/Solana matrix | **NOT RUN / OPEN** |

## Verification

No live RPC, Nexus CLI, credentials or fund-moving operation was invoked.

| Check | Exact result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | **PASS — 94 passed, 10 subtests passed in 8.43s** |
| `python3 scripts/check_markdown_links.py` | **PASS — Local Markdown links: OK** |
| `python3 -m pip check` | **PASS — No broken requirements found** |
| `git diff --check 5e7d3b8..cc175cb` | **FAIL — trailing whitespace at `src/main.py:451`** |
| Ruff | **Not installed** |
| Live acceptance | **Not run** |

## Required repair and release order

1. Never advance a Nexus checkpoint from an empty response without an independently proven stable range.
2. Require exactly one full contract identity for transfer and mint resolution; verify every immutable term.
3. Read back submitted Nexus mint txids and match the exact DEBIT contract before terminalization.
4. Establish a stable target-node pagination/boundary contract that can reconcile beyond one page without false green.
5. Reject non-integer transfer units at the state boundary.
6. Add multiuser session prerequisites to production admission.
7. Make money-path event logging non-throwing or independently surface watchdog failures.
8. Run the complete target Nexus plus Solana devnet/testnet crash, timeout, duplicate, equal-time, pagination, concurrent-arrival, malformed-body, finality and decimal-pair matrix.
9. Independently re-review the exact deployment tree and operational runbook.

A green offline suite is not permission to move real funds.
