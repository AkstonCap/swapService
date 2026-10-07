# Financial recovery and capacity-retry follow-up plan — 2026-09-23

> **Historical plan.** The empty-custody latch and dashboard containment described by later work are
> implemented at `17f65a3e3b45281162c1604cd0a695a36dc55991`. The remaining partial/stale-restore,
> complete admission visibility, malformed-oldest fairness and read-only dashboard work is ordered in
> the [2026-09-25 recovery admission and capacity-fairness plan](2026-09-25-recovery-admission-and-capacity-fairness.md).
> Preserve the acceptance detail below as the pre-containment repair record.

## Decision and scope

Source under review: `85030c890fa6f3bb7db97e068e5cf80827d21b28`, compared with
`da79e0928c2dc7c39648734d9ad329637c87eae6`.

The committed provenance migration, deposit policy and typed capacity-hold protocol are valid local
safety improvements. Keep them. This plan closes two broader boundaries that their passing tests do
not establish:

1. exact historical authorization after complete SQLite/DB/WAL loss;
2. automatic progress when the oldest capacity row is malformed, conflicting or otherwise requires
   operator action.

No task in this plan authorizes production credentials, live funds, optional receipts, provider-v2
cutover or a dependency upgrade.

## Batch 1 — fail closed on total database loss

### Required runtime contract

A nonzero existing-deployment waterline plus an empty/recreated database is not proof that
rediscovered incoming principal is new work. Before any deposit worker can classify it under current
configuration, startup/replay must do one of the following:

- restore and validate the exact historical policy/cap intent from an independent durable source; or
- retain the rediscovered source as a quantified, visible, non-sendable recovery hold.

The hold must bind source signature, source timestamp/account/memo, exact integer principal,
network/vault/mint/finality provenance and the reason historical authorization is unavailable. It
must count in backing liabilities and pin public recovery progress. Current fees, minimums, maximums,
destinations or account resolution must not manufacture replacement authorization.

### Collected RED acceptance

Use real `perform_startup_recovery`, scan-page admission, deposit processing, refund and quarantine
workers with only chain/address/send boundaries stubbed. Add default-collected cases for:

1. below-minimum and nonpositive-output policy holds through DB/WAL deletion and terms drift;
2. oversized and invalid-destination refund/quarantine capacity holds through DB/WAL deletion,
   changed maximum/refund fee/destination/cap and source re-enumeration;
3. a pinned source older than one page and more sources than the worker limit;
4. verified SQLite online backup and copied DB+WAL restore as the positive path;
5. missing, partial, corrupt or identity-conflicting external authorization evidence;
6. restart and duplicate replay after the new recovery hold is written.

For every unsafe/no-evidence case assert:

- startup remains incomplete **or** the source enters the explicit recovery hold;
- Nexus debit and Solana send call counts remain zero;
- full integer principal remains in unresolved liabilities;
- no fee, payout reservation, submission or terminal row is invented;
- dashboard/API exposes a non-secret actionable issue;
- later current-config changes do not alter the retained source contract.

For a verified backup restore assert the original decision, destination, output, fee, payout memo and
terms evidence are reused exactly once after legitimate capacity release.

### Implementation constraints

- Do not infer historical intent from source rediscovery, current configuration or source minus an
  observed transfer.
- Keep A's conservative terminal-provenance migration and observed-spend cap reconstruction.
- Keep B's strict integer policy as the first economic classifier for genuinely new work.
- Keep C's intent-first reservation/submission protocol for authorized work.
- Make recovery mode explicit and durable; do not rely on an in-memory startup flag that disappears
  before later ingestion.

## Batch 2 — separate automatic FIFO from operator-action capacity rows

### Required runtime contract

Only a capacity hold whose complete frozen source/intent evidence validates transactionally may
participate in automatic eligible FIFO. Malformed evidence, source/lifecycle conflict, database
failure and unknown/submitted outcomes remain non-sendable and liability-bearing, but must be
atomically classified outside the automatic retry queue so they cannot starve later valid work.

### Collected RED acceptance

Exercise both real disposition workers and both ordering directions:

1. oldest malformed refund hold followed by a valid fitting refund;
2. oldest malformed quarantine hold followed by a valid fitting refund, and the reverse;
3. source conflict, opposing terminal conflict and missing-source hold ahead of valid work;
4. more operator-action rows than the worker limit;
5. restart, repeated scheduling and alert deduplication;
6. reviewed repair/resolution of the blocked row without releasing it blindly;
7. concurrent workers at the exact remaining-cap boundary.

Require the younger valid original intent to submit exactly once, while the blocked source, frozen
blob, full principal and diagnostics remain unchanged. No scheduler transition may delete source or
hold evidence, release an ambiguous reservation, or convert malformed evidence into sendable terms.

### Implementation direction

Prefer an explicit durable scheduler class/status and transactional transition over query-only
filtering. The operator-action class must remain visible in dashboard/API liability views and have a
reviewed resolution protocol. Keep global FIFO among **eligible validated** holds and retain the
existing special handling for individually impossible payouts.

## Batch 3 — independent acceptance and release separation

After each batch:

1. independently review the final runtime functions and shared dependencies;
2. run the focused collected module before the complete suite;
3. run recovery standalone, recovery plus installed SDK, receipt/payout/Nexus-fee/SDK isolation,
   dependency consistency, compilation, Markdown links, literal inventory and whitespace checks;
4. record exact source SHA, runtime/test SHA-256 hashes, commands and results;
5. verify the real Git index and unrelated concurrent work are unchanged.

Only after offline closure may an explicitly authorized target-infrastructure matrix test Solana
provider pagination/finality/network identity, Nexus completeness/reference/TLS semantics, both
bridge directions, accepted-but-unparsed outcomes, durable-boundary crashes and operator recovery.
Live acceptance and exact-head publication/CI remain separate release gates.
