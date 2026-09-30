# R-1 containment: ready source with retained capacity intent

Startup now holds a retained `ready for processing` Solana source when a matching
`solana_payout_capacity_holds.source_signature` survives. Valid matching payable
input policy does not authorize a new Nexus debit alongside a frozen refund or
quarantine intent. A partial restore can combine an older ready source component
with newer capacity evidence; the actual deposit worker previously attempted a
mocked 1,090-unit debit from a 1,100-unit source in this case.

The status-only update runs in `record_solana_recovery_boundary()` under the same
`BEGIN IMMEDIATE` transaction as the boundary and existing startup audits, before
reconstruction. Any matching capacity row is conflicting evidence, including corrupt
intent JSON, mismatched disposition kind or obligation identity. No current terms
repair it. The source becomes `historical_solana_authorization_missing`; original
policy, principal, capacity diagnostics, raw intent and reservations remain unchanged.
Persistence failure rolls back all startup changes and refuses reconstruction.

The existing dashboard historical-authorization warning applies and must not suggest
an automatic capacity retry. Repeated startup, initialization and either page
committer cannot promote the row. Do not clear this hold, delete its capacity sibling
or send manually. A source coherently labelled `refund capacity held` or
`quarantine capacity held` is unaffected and retains its original-term retry behavior.

Focused offline coverage:

```bash
python -m pytest -q tests/test_retained_ready_capacity_recovery.py
```

The collected regressions exercise real startup and deposit/refund/quarantine workers
with external transport mocked: valid/corrupt/source-conflicting capacity evidence,
absent/expired/active reservations, repeated initialization and both page committers,
full liability and evidence preservation, rollback with absent/existing boundaries,
timestamps outside scan ranges and beyond the local clock, more conflicts than the
ready-worker limit ahead of valid work, and coherent original-intent retry controls.
No live transaction or production credential is used.

This is one narrow increment of [R-1](../EVALUATION.md), **not its closure** or proof
of coherent restore admission. It does not audit all source states, competing
terminal/budget/fee-only components, orphan capacity evidence, Nexus-side recovery,
missing lifecycle components, or restore identity/completeness. The separate malformed
capacity FIFO issue (R-1b) is unchanged: retained capacity evidence is not deleted or
reclassified by this repair and may still block other capacity retries. Durable general
startup visibility, read-only dashboard APIs and audited hold resolution also remain
open. Production and real funds remain blocked.
