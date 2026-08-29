# Independent Financial State-Machine Review — 2026-08-29

**Review window:** 2026-08-28 16:25:36 +0200 through 2026-08-29 16:08:08 +0200
**Baseline:** `787a401db4a74c6d9062c537534cf16ad3a94356`
**Reviewed committed head:** `5e7d3b853e49b4d3b229b6e7ba8770f158a7edc5`
**Additional staged tree reviewed:** `src/balance_reconciler.py`,
`src/nexus_client.py`, `src/state_db.py`, `tests/test_critical_safety.py`
**Deployment verdict:** **HARD BLOCKED for real funds**
**Staged-change verdict:** **NOT COMMIT-READY**

## Executive result

The three committed repairs materially improve containment:

- startup refuses to label a returned unhealthy reconciliation as green;
- a claimed Nexus transfer interrupted by process restart becomes a durable
  `outcome_unknown` hold and cannot consume its execution authorization again;
- explicit production mode requires positive per-swap and rolling-daily payout
  caps plus at least one alert route.

These controls do not close production acceptance. Target-node crash, timeout,
pagination, finality and identity semantics remain unproven. Reconciliation
failure does not pause exposure-producing pollers. During this review the staged
remote reconciliation proposal was revised: it now loads active recipients
independently and refuses multi-page ambiguity. It remains not commit-ready
because the exposure pause is absent, the exact valid first-time-recipient case
is untested, and target one-page boundary/order semantics are unproven.

## Commit review

| Commit | Claim | Independent result |
|---|---|---|
| `178adbc` | Fail-closed startup reconciliation | **Materially improved.** A returned invalid/unhealthy result alerts and does not print green. Exceptions and unhealthy results still do not latch a safety pause. |
| `6009dae` | Hold interrupted Nexus transfer intents on restart | **Passes the local at-most-once containment slice.** Persisted `executing` intents become `outcome_unknown` before recovery; target-node crash evidence remains required. |
| `5e7d3b8` | Gate production startup on exposure controls | **Partial operational gate.** Positive caps and alert routing are required in explicit production mode. Invalid boolean text silently disables the mode, and refusal returns normally. |

## Severity-ordered findings

### Resolved during review — Active recipients are now included independently

The first staged snapshot derived its account set from completed mints and could
skip an active mint to a first-time recipient. The final reviewed index loads
active intents independently (`src/balance_reconciler.py:255-302`) and keeps
every valid active intent incomplete until terminal reconciliation (lines
351-367). The remote scan retains debits for destinations outside the completed
set (`src/nexus_client.py:882-885`).

**Remaining evidence:** add the exact valid scenario with a completed mint to
Alice and an active first-time mint to Bob. The current staged test covers a
malformed active destination, not this boundary.

### Resolved during review by containment — Multi-page ambiguity fails closed

The first staged snapshot attempted timestamp-only live offset pagination. The
final reviewed index reads exactly one page (`src/nexus_client.py:892-904`) and
returns `complete=False` with `pagination_snapshot_unavailable` when a full page
does not cross the requested boundary (lines 981-984). The staged regression
exercises that full-page incomplete result.

This removes the cross-page false-green path by sacrificing availability: from
a zero waterline, a history of at least one full page cannot reconcile healthy.
Target-node proof of short-page, ordering and boundary semantics is still a
release requirement.

### High — Reconciliation remains advisory rather than fail-closed

Startup ignores the reporter's boolean result and catches exceptions with a
print (`src/main.py:301-307`). Periodic reconciliation alerts on unhealthy
results but also only prints exceptions (`src/main.py:373-392`). Exposure-
producing pollers continue based on the separate backing pause
(`src/main.py:443-476`).

A database/schema/read failure can therefore bypass the health result, while an
explicit unhealthy result or positive discrepancy does not latch a pause on new
mints/payouts.

**Required exit:** feed every result and exception into one fail-closed consumer,
emit a Critical alert, and latch a reconciliation exposure pause until a later
explicitly healthy run. Ambiguity resolution and liability-reducing operations
may continue under a narrower policy.

### Medium — Malformed production-mode text disables the gate

`src/config.py:315-318` parses every unrecognized value as false. A typo such as
`SWAP_PRODUCTION_MODE=treu` silently restores development defaults. In addition,
`main.run()` returns normally when production controls are rejected
(`src/main.py:208-212`), allowing a supervisor to observe exit status zero.

**Required exit:** distinguish unset from invalid; reject present unrecognized
booleans during configuration validation and exit non-zero when a production
start is refused.

## Prior blocker disposition

| Prior blocker | 2026-08-29 status |
|---|---|
| Failed/empty/bounded lookup authorizes retry/refund | **Contained locally; target projection semantics unproven.** |
| Unresolved liabilities omitted from backing | **Closed locally.** |
| Explicit failed/malformed/truncated Nexus scan advances waterline | **Closed locally.** |
| Empty successful Nexus enumeration proves a stable range | **Critical release gate remains open.** |
| Nexus refund/quarantine may double-send | **Contained.** Automatic actions remain disabled; restart-after-claim is now a hold. |
| Mixed-decimal money contract | **Closed locally and in CI; live pair matrix absent.** |
| Startup prints `All 0` on returned unhealthy result | **Closed.** Exception and exposure-pause paths remain High. |
| Authoritative remote mint reconciliation | **Not closed.** Earlier recipient/pagination false-green paths were contained during review; exposure-pause, exact-boundary regression and live target evidence remain. |
| Production exposure controls | **Partial.** Explicit-mode caps/alert route exist; malformed-mode and operational acceptance gaps remain. |

## Verification

No live RPC, Nexus CLI, credentials or fund-moving operation was invoked.

| Tree/check | Exact result |
|---|---|
| Committed `5e7d3b8`: `python3 -m pytest -q -p no:cacheprovider` | **51 passed, 4 subtests passed in 5.65s** |
| Active staged tree: same pytest command | **58 passed, 4 subtests passed** |
| `python3 -m pip check` | **No broken requirements found** |
| `python3 scripts/check_markdown_links.py` | **Local Markdown links: OK** |
| Commit-range and staged `git diff --check` | **PASS** |
| Exact committed-head GitHub Actions | **PASS — run 33254976148** |
| Staged code CI | **None — no commit identity** |
| Live Nexus/Solana acceptance | **Not run** |

A green mock-based suite cannot establish target pagination, finality, accepted-
but-unparsed behavior or exactly-once transfer semantics.

## Repair order and release exits

1. Keep automated Nexus refund/quarantine execution disabled.
2. Latch reconciliation error/unhealthy states into the production exposure
   pause before adding remote history complexity.
3. Add the exact completed-recipient plus valid active first-time-recipient
   regression to verify every unresolved destination remains incomplete.
4. Preserve one-page fail-closed behavior and prove its short-page,
   ordering and boundary semantics on the target node.
5. Reject malformed production-mode values and propagate startup refusal as a
   non-zero process exit.
6. Re-run the final exact-tree offline gate and independent review.
7. Run the target Nexus plus Solana devnet/testnet matrix: crash at every durable
   boundary, timeout before/after acceptance, duplicate invocation, equal-time
   pagination, concurrent arrival, malformed bodies, finality and exact decimal
   pairs.

Production readiness requires all seven exits plus tested alert delivery,
quarantine destinations, incident response and custody controls. Focused local
passes are not permission to move real funds.
