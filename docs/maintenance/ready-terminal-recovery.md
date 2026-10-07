# R-1 containment: ready source with competing terminal evidence

Startup now holds a retained `ready for processing` Solana source when the same
signature exists in `processed_sigs`, `refunded_sigs` or `quarantined_sigs`.
A terminal-table marker alone is not exact settlement proof. A partial restore
can combine an older ready source with incomplete or newer terminal evidence;
the deposit worker previously deleted the ready source during idempotency cleanup,
removing its full principal from unresolved liabilities without verifying settlement.
The initial collected regression reproduced this for all three sibling tables.

The status-only update runs in `record_solana_recovery_boundary()` in the same
`BEGIN IMMEDIATE` transaction as the monotonic replay boundary and existing startup
audits, before chain reconstruction. Any matching sibling holds the ready row,
regardless of sibling status, completeness, timestamp or payable input policy.
The source becomes `historical_solana_authorization_missing`. Source principal,
raw policy, sibling records, reservations, fees and cap events are not rewritten
or released by this audit. Failed persistence rolls back the boundary and all
status changes and refuses reconstruction.

The existing dashboard historical-authorization warning applies. Do not clear
the hold, delete a sibling or send manually. Even independently valid terminal
provenance does not authorize discarding an inconsistent retained ready component;
resolving that conflict requires an audited protocol. Coherent terminal records
without a ready sibling, valid unrelated ready work and existing in-flight states
retain their previous behavior. The earlier all-history terminal provenance gate
still refuses unproven finalized refund/quarantine records before this audit.

Focused offline coverage:

```bash
python -m pytest -q tests/test_retained_ready_terminal_recovery.py
```

The module covers all three tables; minimal and populated malformed/nonterminal
markers; absent, expired and active reservations; repeated startup/initialization;
both page committers refusing the retained lifecycle conflict; full liability,
dashboard warnings and evidence preservation; atomic rollback with absent/existing
boundaries; timestamps outside scan ranges and beyond the local clock; more held
rows than the worker limit ahead of one original-term debit; unchanged in-flight
rows; and proven terminal dispositions with/without a stale ready source. Chain
and transport boundaries are offline/mocked; no real funds or credentials are used.

This is a narrow increment of [R-1](../EVALUATION.md), **not closure** or a coherent
restore certificate. It does not audit all lifecycle states, orphan terminal rows,
budget/fee-only conflicts, missing components, Nexus recovery or restore identity.
R-1b capacity fairness, R-1c durable startup visibility, R-1d dashboard read-only
behavior and audited hold resolution remain open. Production remains blocked.
