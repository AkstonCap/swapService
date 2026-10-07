# swapService development review — 2026-09-28

## Verdict

**Release blocked. Three Solana recovery containments are accepted within their narrow scope; coherent
partial-restore admission is still open and a retained non-ready disposition can use current terms.**

```text
review base:  9f12211811331bae741702757e9d8259a16d55ff
source HEAD:  6769f7a1bb68dd2a975f4b39aa910f2405d38d42
source tree:  54f06ad253326f21673d5ca0536921bc323e04b6
remote main:  6769f7a1bb68dd2a975f4b39aa910f2405d38d42
```

Reviewed commits:

```text
c3d36c9 fix(recovery): hold unseen pre-startup Solana deposits
70c572d fix(recovery): hold retained sources without frozen policy
6769f7a fix(recovery): hold invalid retained ready-row policy
```

This review changed documentation only. It did not edit runtime/tests/dependencies, stage, commit, push,
access production credentials, call a live chain or move funds. The untracked vision was read as strategic
context and is not linked or proposed for publication.

## Assessment of the three changes

### 1. Unseen pre-startup Solana deposits — accepted as conservative containment

Startup writes a monotonic `solana_recovery_boundary` before either chain rebuild. Both core and Helius
page committers compare a previously unseen source timestamp to that boundary in the same transaction as
source/hold/cursor persistence. At or before the boundary, exact principal and custody/query provenance
enter `solana_deposit_holds` with reason `historical_solana_authorization_missing`; the row is included in
liabilities and checkpoint pinning, excluded before the replay limit and cannot be promoted automatically.

This prevents the September 25 source-only replay from obtaining first-time current policy. It is not a
restore manifest or authoritative chain-clock proof. Offline or clock-ambiguous deposits may remain held,
and unseen sources outside complete enumeration are not solved.

### 2. Retained ready sources without policy — accepted as conservative containment

In the boundary transaction, startup audits every retained `ready for processing` row without a worker
limit or timestamp exemption. A row with absent policy fields becomes the historical-authorization status
before reconstruction. Source data, principal, references, reservations and capacity evidence are not
rewritten or released. The ordinary deposit, refund and quarantine workers do not select the held status.

This intentionally also holds a legitimate crash between source admission and first policy freeze. No
audited resolution exists yet.

### 3. Invalid retained ready-row policy — accepted as conservative containment

The same audit parses frozen evidence using the strict policy parser and requires an exact match to source
signature, timestamp, memo, sender and principal plus a `payable` decision. Partial fields, malformed JSON,
source mismatch, decision/output conflict and internally valid nonpayable policy become the same hold.
Current configuration never repairs the evidence. The boundary and all status changes commit atomically.

The 49-test two-module shard and 23-case direct actual-worker selection passed. Positive cases preserved a
valid original policy through terms drift and admitted genuinely new post-boundary work.

## Severity-ordered findings

### High — non-ready restored dispositions still bypass historical-policy admission

`state_db.record_solana_recovery_boundary()` audits only `status = 'ready for processing'`. The ordinary
refund and quarantine workers accept `to be refunded` / `to be quarantined` without requiring frozen input
policy or a pre-existing capacity intent. In that branch they calculate output from the current refund fee
and resolve the current destination before freezing a new disposition intent.

Fresh offline actual-worker probes retained one unrelated terminal row, inserted a 1,100-unit non-ready
source with both policy fields null, and ran real startup followed by each real disposition worker. Recovery
reported complete. Refund and quarantine each called the mocked Solana send boundary once for 1,090 units
using the current 10-unit fee and current resolved destination. No real transport ran. Full principal
remained locally liable pending confirmation, so the proven defect is replacement authorization and one
attempted external send, not realized loss or duplicate settlement.

**Required exit:** before workers start, transactionally audit every nonterminal source status against its
required immutable policy, disposition, capacity, reservation and submission evidence. Missing, malformed
or conflicting evidence becomes a quantified non-sendable recovery state without deleting any row or
inventing current terms. Exercise real deposit/refund/quarantine workers over every status and partial
component; require zero transport, full liability and no inferred fee.

### High operability — malformed oldest capacity evidence still starves valid work

The fresh September 25 actual-worker probe reproduced unchanged: after capacity release, two refund-worker
passes sent nothing; the malformed older 60-unit hold remained first and a valid younger 60-unit hold
remained `waiting behind older Solana payout capacity hold`. Liability stayed 120 units. Safe refusal is
intact, but eligible FIFO progress is not.

**Required exit:** atomically classify malformed/conflicting/unknown-submission rows outside automatic FIFO
while retaining principal and raw evidence. Prove younger same-kind and cross-kind work proceeds exactly
once beyond worker limits and restart.

### High operability — startup refusal can still look healthy on the dashboard

A heartbeat-missing recovery again returned incomplete while summary reported `not_held`, retained ratio
20,000 bps and zero recovery issues. Main remains fail-closed; this is operator misinformation rather than
a transport bypass.

**Required exit:** durable startup-owned pending/held/complete admission across every phase, with healthy
metrics exposed only from the same read-only snapshot as a valid complete record.

### Medium — dashboard summary still creates a missing database

`api_summary()` again created the configured SQLite file when it did not exist. Its admission read is
read-only, but later state helpers use ordinary connections.

**Required exit:** one read-only transaction or dedicated read-only API per endpoint; assert no DB/WAL/SHM
creation or byte change and snapshot-consistent admission/metrics/counts.

### Existing separate release blockers — unchanged

Registration validation remains alert-only after recovery; authoritative custom-endpoint network/sync/tip
admission is incomplete; non-capacity Solana holds lack an audited evidence-bound resolution protocol;
provider-v2 is unwired; optional receipts remain separately disabled/unaccepted; and no live provider,
finality, pagination, TLS, Nexus completeness, accepted-but-unparsed, crash or operator rehearsal ran.

## Verification

The exact source ran from a detached disposable worktree, excluding all pre-existing dirty/untracked
paths. External network/send boundaries in probes were blocked or mocked.

| Command/shard | Result |
|---|---|
| `.venv/bin/python -m pip check` | No broken requirements |
| `.venv/bin/python -m compileall -q src *.py tests` | Passed |
| `.venv/bin/python scripts/check_markdown_links.py` | Passed |
| `.venv/bin/python scripts/check_token_pair_inventory.py` (exact source index) | Passed; 274 active lines |
| `.venv/bin/python -m pytest -q` (exact source) | **641 passed, 77 subtests passed in 81.69s** |
| `.venv/bin/python -m pytest -q` (documentation candidate copy) | **641 passed, 77 subtests passed** |
| `pytest -q tests/test_recovery_safety.py` | **35 passed, 52 subtests passed in 2.59s** |
| recovery safety + installed SDK | **36 passed, 52 subtests passed in 2.97s** |
| receipt/payout/Nexus-fee/SDK isolation shard | **85 passed in 6.23s** |
| two new recovery modules | **49 passed in 6.47s** |
| three focused actual-worker test families | **23 passed in 3.68s** |
| `git diff --check 9f122118..HEAD` and `git diff --check HEAD~1 HEAD` | Passed |
| residual non-ready actual-worker probe | Reproduced both refund and quarantine transport calls |
| three unchanged-risk probes | Reproduced malformed FIFO, stale dashboard and DB creation |

Exact-source CI [run 36316328344](https://github.com/distordialabs-brutus/swapService/actions/runs/36316328344)
is green. It does not verify this documentation candidate or provide live-chain acceptance.

## Ordered development and publication gates

1. Complete Batch 1 across every nonterminal Solana lifecycle, beginning with non-ready refund/quarantine
   rows lacking exact policy/capacity evidence. Keep all three accepted controls.
2. Persist truthful startup admission for every failure and read it with metrics from one read-only snapshot.
3. Move invalid capacity rows outside eligible FIFO without weakening non-sendability or liability.
4. Close registration/network admission and audited hold resolution; keep provider-v2/receipts disabled.
5. Run explicitly authorized target-infrastructure acceptance against the exact candidate, then make a
   separate release decision.
6. For this documentation-only publication, stage only the named review files, run index-aware inventory,
   Markdown, whitespace, compile, dependency and full-suite gates, commit/push, read back remote `main`, and
   verify CI for the exact publication SHA.

The maintained executable plan is
[the recovery admission and capacity-fairness plan](plans/2026-09-25-recovery-admission-and-capacity-fairness.md).
