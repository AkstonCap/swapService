# Weekly Financial State-Machine Architecture and Code Review — 2026-08-28

**Review window:** 2026-08-24 16:07:50 +0200 through reviewed HEAD
**Baseline:** `492153607224ad9bfc650051177f6b4c2130fbc5`
**Reviewed HEAD:** `f61489773935b5b3b5bc47ebdc91f76e4075961b` (`main`, matching `origin/main`)
**Commits reviewed:** 15
**Method:** every in-window commit and patch, the final current tree, governing documents, the complete local suite, CI-equivalent gates, exact-head GitHub Actions, dependency audit, and current-tree static analysis.

## 1. Deployment verdict

**HARD BLOCKED for real funds.**

The original 2026-08-24 Critical code paths are materially contained or repaired in the local implementation, and the complete automated gate is green. That is not production evidence. Two Critical release gates remain open at the external-system boundary, and the prior High reconciliation finding is not fully closed:

1. the durable Nexus refund/quarantine protocol has strong at-most-once local controls but has not passed crash-boundary and target-node timeout/acceptance tests;
2. Nexus enumeration still advances the checkpoint after an empty unfiltered response, but target-node completeness, pagination and concurrent-arrival semantics have not been proven;
3. reconciliation now rejects missing durable rows locally, but startup still prints a false green for an unhealthy result and the detector does not yet perform authoritative remote transaction-history read-back.

Automatic Nexus refund/quarantine execution and automated surplus actions must remain disabled. Availability loss and manual holds are preferable to double payment or skipped deposits.

### Severity summary

| Severity | Open | Verdict |
|---|---:|---|
| Critical release gate | 2 | No deployment until live authoritative evidence closes E-001/E-002 and E-006 |
| High | 1 | Reconciliation is locally improved but still has a false-green consumer and no authoritative remote ledger proof |
| Medium / operational | 6 | live matrix absent, custody in argv, caps/alerts optional, vulnerable dependencies, static-analysis debt and incident-response maturity |
| Low / hygiene | 3 | historical path/identity drift, dormant/dead paths and remaining cleanup |

## 2. Commit-by-commit review

| Commit | Change reviewed | Financial-safety assessment |
|---|---|---|
| `1e4f20c` | Lookup completeness, liability/backing containment, Nexus waterline repairs, focused safety tests, doc relocation | Closes the original negative-lookup retry/refund path locally; makes unresolved Solana rows liabilities; holds waterline on errors/truncation. It did not close unsafe automatic Nexus refunds, heuristic filtering, mixed decimals, reconciliation or the full-suite gate. |
| `a041475` | Immediate containment | Correctly disables automatic Nexus refunds/quarantine movements, removes heuristic nested amount filtering from normal/recovery enumeration, and exposes held credits. This is containment, not a complete transfer protocol or live enumeration proof. |
| `55990a8` | Composable legacy checks | Runs legacy executable tests as isolated subprocess cases. This closes the import-time `sys.exit()`/shared-module collection failure without pretending those scripts are ordinary in-process unit tests. |
| `82b88e3` | Enforced CI | Adds dependency consistency, byte-compilation, local link, complete pytest and whitespace gates. Appropriate baseline. |
| `a29f613` | CI full history | Adds `fetch-depth: 0`, making the whitespace comparison executable in Actions. |
| `e2f83a2` | Mixed-decimal contract | Separates source-token scales, uses integer base units and exact public formatting, and adds exact equal/unequal/zero-decimal fixtures. The prior H-2 code defect is closed locally. |
| `4d642bb` | Evaluation/development-plan refresh | Documentation-only; correctly keeps deployment blocked and sequences reconciliation, transfer intent and live evidence. |
| `a9231c1` | Durable completed rows and fail-closed reconciliation | Persists destination/memo/exact integer input-output evidence and makes the producer unhealthy on missing/zero/skipped evidence. However, `src/main.py:237-244` still ignores `healthy` during startup and prints green when `discrepancies` is empty, including zero checked recipients. |
| `15ff62d` | Nexus transfer intent ledger | Persists source, destination, exact units and deterministic reference before account debit; atomically claims once; maps timeout/non-zero/unparsed output to `outcome_unknown`; never retries ambiguous intents. Sound intent-first foundation. |
| `9fce88a` | Operator disposition workflow | Separates prepare/authorize/execute/resolve/finalize and records human attribution. Initial reference-only resolution was not sufficient by itself, but later commits add exact debit terms and remote identity checks. |
| `09ea110` | Monotonic intent transitions | Prevents completed chain evidence regressing to an ambiguous state and requires remote txid for submitted/completed. Correct fail-closed strengthening. |
| `8879cd1` | Exact resolution evidence | Requires reference, source, destination and exact amount; submitted intents additionally require the persisted remote txid. This closes the authoritative-identity defect in mocked/local logic, subject to target-node projection semantics. |
| `c106899` | One disposition per source credit | Adds a source-only unique index and fail-closed migration behavior, preventing one credit from authorizing both refund and quarantine. Correct. |
| `2b804cb` | Audited preparation | Prevents direct authorization without a durable preparation event tied to the exact reference. Correct defense in depth. |
| `f614897` | Audited execution request | Prevents direct claim/debit without a durable named execution request. Exact-head local and CI gates are green. |

No commit in the window supplied live Nexus/Solana evidence. The last five commits harden mocked/local protocol transitions but do not satisfy the crash/restart and external-semantics acceptance matrix.

## 3. Critical fund-loss / skipped-deposit release gates

### C-2026-08-28-1 — Durable Nexus refund/quarantine execution is not live-proven

**Status:** contained; local protocol foundation implemented; release gate open.

Verified positive controls:

- `nexus_transfer_intents` persists one immutable intent per source credit before any debit (`src/state_db.py:232-266`, `469-530`).
- Claim is atomic and requires prior audited preparation, authorization and execution request (`src/state_db.py:548-674`).
- The CLI is invoked at most once from an authorized intent; timeout, exception, non-zero exit and unparsed output become `outcome_unknown` (`src/nexus_client.py:291-343`).
- Resolution requires a positive reference plus exact source, destination and integer amount; a submitted intent additionally requires the persisted txid (`src/nexus_client.py:346-378`, `853-923`).
- Finalization requires `completed`, exact remote txid, exact held source status/amount/treasury, and matching refund destination before archive/delete (`src/state_db.py:691-758`).
- The legacy raw transfer entry point fails closed and the service loop does not automatically execute the operator workflow (`src/nexus_client.py:613-679`).

Why the release gate remains open:

- no process-kill test covers every instruction boundary, especially claim-before-call, accepted-before-response, response-before-persistence and persistence-before-finalization;
- no target Nexus run proves that account debits appear in the selected token-history projection with `contracts.reference/from/to/amount` exactly as assumed;
- no target run proves timeout-before-acceptance versus timeout-after-acceptance behavior;
- negative live offset scans intentionally remain non-authoritative, so an `executing` intent can hold indefinitely rather than risk a second debit. That is safe containment but not completed recovery.

**Required exit:** on the target build, fault injection and restart tests must demonstrate one and only one remote transfer for every boundary, with authoritative reference/txid read-back. Until then automatic execution remains disabled.

### C-2026-08-28-2 — Empty Nexus enumeration can still advance the waterline without target proof

**Status:** heuristic filter removed; error/truncation handling repaired; live semantics unproven.

The poller correctly marks CLI/API/schema errors incomplete and holds on page/processing exhaustion (`src/swap_nexus.py:583-663`, `831-842`). It no longer sends a nested server-side amount filter (`src/swap_nexus.py:540-552`; `src/nexus_client.py:1589-1679`).

However, when the endpoint returns an empty successful response, there are no unprocessed rows and no page timestamps, `src/swap_nexus.py:853-859` proposes `now - safety`. That is safe only if the target endpoint guarantees the unfiltered empty response is a complete view of the scanned range. The service uses live offset pagination, not a snapshot. No repository live test establishes that guarantee under below-dust/boundary/above-minimum credits, concurrent arrivals or pagination.

**Impact:** an accepted-but-lossy empty response can move the checkpoint past a real treasury credit, orphaning user funds.

**Required exit:** run the target-node enumeration matrix and either prove empty/range completeness or change the architecture so no empty live scan can advance a destructive checkpoint without an authoritative stable-range token.

## 4. High money-contract / reconciliation finding

### H-2026-08-28-1 — Reconciliation remains only partially closed

**Status:** producer repaired locally; startup consumer false-green; authoritative remote evidence absent.

Positive local repair:

- completed mints retain exact integer input/output, destination and memo before the transient queue row is deleted (`src/state_db.py:388-408`; `src/nexus_client.py:509-549`);
- the producer rejects missing identity/context, malformed/non-integer values, fee-math mismatch, zero checked recipients and per-account failures (`src/balance_reconciler.py:36-92`, `240-301`);
- local fixtures prove a balanced durable row returns zero and a deliberately seeded extra DB debit returns a positive discrepancy.

Remaining defects/gaps:

1. **Concrete false green:** startup checks only `discrepancies`; if `healthy=False` with no discrepancy, it prints `✓ Balance check: All 0 ... match expected balances` (`src/main.py:234-245`). The periodic consumer correctly checks `healthy` (`src/main.py:312-329`), so the two operator surfaces disagree.
2. **No authoritative remote ledger read-back:** `run_balance_reconciliation()` derives transaction totals from local `processed_sigs`/`processed_txids`. `include_remote_balance` is display-only (`src/balance_reconciler.py:175-203`). An unrecorded duplicate remote mint—the actual crash hazard—need not create a second local row, so the seeded local duplicate fixture is not proof the deployed detector sees remote duplication.
3. **Legacy history:** old completed rows intentionally remain incomplete; no reviewed backfill/disposition procedure exists.

**Required exit:** fix every consumer to refuse green unless `healthy is True`, add a caller-level regression, reconcile against authoritative Nexus transaction identities/amounts, prove balanced and duplicate remote cases on the target node, and document legacy-row disposition.

## 5. Prior 2026-08-24 Critical/High closure matrix

| Prior finding | Current determination | Evidence |
|---|---|---|
| C-1 failed/empty/bounded Nexus lookup triggers retry/refund | **Closed in local code; live projection semantics still gated** | Batch lookups carry completeness; only positive identity matches transition; negative/error/truncated scans hold. |
| C-2 surplus excludes unresolved refund/quarantine liabilities | **Closed locally** | Every `unprocessed_sigs` row is summed gross; backing/surplus reads propagate failure; automated surplus actions return disabled. |
| C-3 waterline advances after enumeration failure | **Closed for explicit error/malformed/truncated/capped paths** | Those paths hold. Empty successful response remains part of C-2026-08-28-2 until target completeness is proven. |
| H-1 backing reads fail open | **Closed locally** | supply read raises; backing exceptions pause. |
| H-2 mixed decimals | **Closed locally and in CI** | exact integer 6/6, 8/6, 6/8, 9/6 and 0/0 tests; public terms use source scale. Live non-default-pair evidence remains absent. |
| H-3 completed-state/reconciliation false green | **Partially closed; remains High** | durable producer data is repaired, but startup false-green and authoritative remote read-back remain open. |
| Post-review P-1 automatic Nexus refund double-send | **Contained; permanent/live closure not met** | automatic execution disabled; durable operator workflow exists; crash/target matrix absent. |
| Post-review P-2 lossy heuristic server filter | **Code containment closed; release gate open** | filter removed in normal/recovery scans; target empty/pagination semantics unproven. |
| M-1 no composable full gate/CI | **Closed** | full pytest and exact-head Actions pass. |

## 6. Complete verification evidence

### Green required gates

| Command/evidence | Exact result |
|---|---|
| `python -m pytest -q` | final rerun: `45 passed, 4 subtests passed in 8.18s` |
| `python -m pip check` | `No broken requirements found.` |
| `python -m compileall -q src *.py tests` | exit 0, no output |
| `python scripts/check_markdown_links.py` | `Local Markdown links: OK` |
| exact-head GitHub Actions | run [`33169510993`](https://github.com/distordialabs-brutus/swapService/actions/runs/33169510993), head `f61489773935b5b3b5bc47ebdc91f76e4075961b`, job `Test and static checks`: success; every step success |

### Non-green quality evidence

- Review-window `git diff --check 4921536..f614897` found two trailing-whitespace lines in the historical 2026-08-24 review. This review removes them; the final docs-only diff must be rechecked.
- `pip-audit -r requirements.txt` found three advisories in two pinned packages:
  - `python-dotenv 1.0.1` — `PYSEC-2026-2270`, fixed in `1.2.2`;
  - `requests 2.32.3` — `PYSEC-2026-1872`, fixed in `2.32.4`;
  - `requests 2.32.3` — `PYSEC-2026-2275`, fixed in `2.33.0`.
- Current-tree `pyflakes` is not green. Diagnostics include unused imports/locals, repeated `timeout`/`nexus_client` definitions, and placeholder-free f-strings. CI does not enforce a Python linter.
- No `.env`, devnet/testnet harness, or live test file is present in the checkout. Repository tests explicitly point `NEXUS_CLI_PATH` at `/bin/false` or mock `_run`/`subprocess.run`.
- No live Nexus or Solana transaction was issued during this review.

## 7. Operational/custody hardening still required

1. `NEXUS_PIN` and multiuser session are still passed through process argv.
2. Per-swap and daily payout caps default to disabled (`0`).
3. Alert delivery remains optional; stdout-only failure is accepted at startup.
4. Dependency advisories require compatibility-tested upgrades or an explicit applicability/acceptance record.
5. Current-tree static-analysis debt should be baselined and enforced without conflating pre-existing warnings with new regressions.
6. Incident response, key rotation, manual hold aging/escalation and two-person disposition policy need a rehearsed production runbook.

## 8. Repair order and executable exit criteria

1. **Fix reconciliation consumption and evidence:** startup must alert/hold on `healthy=False`; add caller-level test; add authoritative remote txid/reference/amount read-back and live balanced/duplicate fixtures.
2. **Run durable-transfer fault injection:** every crash boundary, duplicate invocation, timeout before/after acceptance and restart must produce exactly one remote transfer or a durable, explicitly unresolved hold—never a retry.
3. **Prove Nexus enumeration/waterline semantics:** below dust, boundary, above minimum, empty range, multiple pages, concurrent arrivals, malformed bodies and processing caps; no checkpoint may pass an unpersisted credit.
4. **Run both directions on Solana devnet/testnet and the target Nexus build:** finality, accepted-but-unparsed responses, payout confirmation ambiguity, refund, quarantine and manual disposition.
5. **Enforce production controls:** non-zero caps, configured quarantine destinations, tested alert delivery, custody isolation, dependency decision and incident rehearsal.
6. **Re-run the complete suite and exact-head CI, then obtain an independent final-tree review.**

## 9. Final conclusion

The development window shows disciplined incremental containment: the original negative-lookup, liability, explicit enumeration-failure, backing and mixed-decimal defects are closed in local logic; composable tests and CI are real improvements; and the Nexus transfer ledger is substantially safer than the prior boolean refund call.

The supportable claim is nevertheless **“locally fail-closed under the tested mocks and fixtures,” not “production-safe.”** Real-fund deployment remains blocked by unverified external enumeration/identity semantics, an unproven crash matrix, and reconciliation that can still present a startup false green and does not yet establish remote ledger completeness.
