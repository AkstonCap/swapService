# swapService 2026-09-21 findings artifact

Reviewed runtime source: `814c0ae8cbe0e65036a3b01d1eb8028e4dcb8ad3`
Previous reviewed source: `91ce0b866a4e155bd690f70b6533124467c7318f`
Publication repository (origin): https://github.com/distordialabs-brutus/swapService
Release verdict: HARD BLOCKED for production and real funds

## Finding F-1 — P0 migration bypass in the recovery repair

`814c0ae` is safe when `reconstruct_confirmed_solana_sig_disposition()` finds no terminal row:
observed spend is reconstructed, the full source is put in an evidence hold, no terminal row or fee is
created, the liability is counted, the dashboard exposes it, and neither send worker selects it.

The repair branches only on terminal-row presence. The pre-repair implementation itself created a
terminal row from chain-only current-v1 evidence. On upgrade/backup restore, current code accepts that
legacy manufactured row as frozen intent although no persisted provenance distinguishes it from an
intent frozen before submission. Exact-HEAD probes for refund and quarantine seed the old terminal
shape with source=10,000,000 and observed output=1; both are accepted, no pending liability is restored,
and a 9,999,999 fee is booked.

Evidence:
- source: `src/state_db.py`, existing-terminal branch after the `terminal is None` hold path;
- default tests: new wipeout test covers no-terminal and a genuine `submitting` backup, not a terminal
  row manufactured by the old recovery;
- probe: `test_legacy_chain_reconstructed_terminal_row_bypasses_new_hold[refund|quarantine]`;
- result: both expected unsafe-behavior reproductions passed.

Required exit: immutable pre-submission provenance plus idempotent migration of in-place, backup/WAL and
restored databases. Missing/legacy/recovery-only provenance becomes a quantified evidence hold with
exact observed cap spend and no inferred fee. Matching genuine provenance can terminalize once.

## Finding F-2 — P1 Solana minimum is still not enforced by the real worker

The live worker validates memo/account/max and positive net output but does not use
`MIN_DEPOSIT_SOLANA_UNITS` before persisting Nexus debit intent and invoking transport. The exact-HEAD
probe sets minimum=1,000,001 for a positive-net 1,000,000-unit source. The real worker invokes the
mocked Nexus debit once and advances to `debited, awaiting confirmation`.

Evidence:
- source: `src/solana_client.py` `process_unprocessed_solana_deposits()`;
- default ingestion test proves below-minimum retention only;
- probe: `test_below_minimum_real_worker_still_crosses_nexus_transport_boundary`;
- result: expected unsafe-behavior reproduction passed.

Required exit: one exact classifier shared by live processing, recovery and public terms; every positive
source remains durable, but only explicitly payable classifications reach transport.

## Finding F-3 — P1 disposition cap refusal remains generic

`prepare_solana_sig_disposition()` returns `False` for cap, source and lifecycle conflicts. Refund and
quarantine retain their generic source state, with no per-obligation needed/used/cap evidence or typed
alert. Exact-HEAD probes fill the cap for both kinds and confirm no own budget event plus unchanged
`to be refunded` / `to be quarantined` status.

Evidence:
- source: `src/state_db.py` `prepare_solana_sig_disposition()` and both workers in
  `src/solana_client.py`;
- probe: `test_disposition_cap_refusal_has_no_typed_durable_reason[refund|quarantine]`;
- result: both diagnostic reproductions passed.

Required exit: typed atomic result, durable retryable cap state with exact units, distinct manual states,
dashboard/API/alert visibility, restart and later-capacity one-send proof.

## Positive control P-1 — fresh `814c0ae` evidence hold

Exact-HEAD refund and quarantine probes each used one-unit observed output against a 10,000,000-unit
source and proved: three exact one-unit cap events, no fee, no terminal row, full-principal unresolved
liability, dashboard issue visibility, and zero calls to either send worker.

Probe: `test_814_chain_only_evidence_is_held_and_not_selected_by_workers[refund|quarantine]`.

## Scope and evidence counts

- exact archived HEAD targeted probes: 7 passed in 2.53s;
- exact archived HEAD collection: 405 tests;
- exact archived HEAD focused recovery/budget/payout suite: 70 passed + 46 subtests in 12.95s;
- shared worktree collection: 434 tests because it includes 29 untracked provider-v2 tests;
- no live-chain mutation or target-chain acceptance;
- final full/staged/CI results are recorded in `verification.log`.

## Ordered repair order

1. F-1 provenance and legacy-terminal migration.
2. F-2 shared input classifier.
3. F-3 typed disposition-cap hold.
4. Independent final-tree review and approved devnet/testnet external-semantics matrix.
5. Optional receipt spending and provider-v2 integration only after 1–4.

Detailed executable acceptance cases are in `docs/DEVELOPMENT_REVIEW_2026-09-21.md` and authoritative
current status is in `docs/EVALUATION.md` / `docs/STATE_MACHINES.md`.
