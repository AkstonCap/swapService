# swapService development review — 2026-10-02

## Verdict

**Release blocked.** Four published recovery containments since
`ed73c513ee22f9626502273aa0d8e42a4c238b7a` are accepted for their narrow offline scope.
The staged sealed-custody candidate materially improves restore continuity, startup truth and
read-only monitoring, but its build certificate does not bind the executable root entrypoint and its
chain/heartbeat admission still proves neither the configured service identity nor node freshness.
No live-chain or production acceptance was performed.

```text
review base:       ed73c513ee22f9626502273aa0d8e42a4c238b7a
source HEAD:       ee10b6e20dfe85f15347386adecb9dc99db55bb5
source index tree: a73785b8653e3ad03c9216072b7366999ba1e854
remote main:       7b2d1c4e3c9d3b2f006a083f9372cfadf80830fc
```

`HEAD` is one local documentation commit ahead of `origin/main`. The sealed-custody runtime and tests
are staged but uncommitted; maintained documentation and this report are unstaged. This review made
documentation changes only and preserved all pre-existing staged, unstaged and untracked work.

## Changes since the September 28 baseline

Published runtime changes extend the startup transaction in
`state_db.record_solana_recovery_boundary()`:

1. `17a9f17` holds every retained ordinary refund/quarantine state before a worker can derive a new
   fee, output or destination from current configuration.
2. `1b267f2` holds a ready source with any retained Nexus debit transaction ID, reference or frozen
   output, including malformed/zero values.
3. `b3d5dbb` holds a ready source with any retained Solana disposition-capacity sibling.
4. `7b2d1c4` holds a ready source with any processed/refunded/quarantined terminal sibling rather than
   allowing idempotency cleanup to discard disputed principal.
5. `ee10b6e` changes only the Markdown-link inventory contract so the informative untracked root
   `vision.md` is not treated as a publication document.

The staged candidate adds:

- a separately stored witness with one-use `ready → claimed → running → ready(next generation)`
  permits and permanent holds;
- whole-file SQLite SHA-256/size, schema, effective configuration and source-manifest certificate
  checks before `state_db.init_db()`;
- pinned Solana and Nexus genesis checks before database migration/recovery;
- recovery, session and heartbeat checks before the witness enters `running`;
- per-cycle witness/read-receipt checks, tracked timeout workers and quiescent WAL checkpoint/sealing;
- dashboard health derived from a matching external running lease plus local live-process receipt, with
  one read-only SQLite snapshot and no missing-path database creation.

This is a continuity gate, not a proof that an initially approved image is economically complete or
that either node is synchronized and fresh. Approval of a bad/pre-fix image would faithfully preserve
bad state.

## Findings

### P0 — the approved build fingerprint omits the executable entrypoint

`custody_admission.build_fingerprint()` hashes `src/*.py` and `requirements.txt`. The documented and
systemd-installed process executes root `swapService.py` first. A scratch regression copied the exact
candidate, changed only `swapService.py` to perform a pre-admission side effect, and observed the same
build fingerprint. The declared requirements file also does not attest the installed interpreter or
package artifacts.

The current certificate therefore cannot support the claim that the exact executable build was
approved. Accidental or malicious root-entrypoint drift can execute before `custody_admission.claim()`
while the in-process hash still matches.

**Required coding/deployment contract:** define one externally enforced deployment artifact identity.
At minimum include every executed root script and transitively loaded runtime module; for adversarial
integrity use a read-only image/package digest verified by a trusted launcher before repository code
runs. Bind the interpreter and installed dependency artifacts or a reproducible immutable image, not
only `requirements.txt`. Mutation of every covered byte must refuse before database, witness claim or
chain access; an exact artifact must still boot.

### High — service-record identity and chain freshness are not admitted

`custody_chain.verify()` makes exactly two identity reads: Solana `getGenesisHash` and Nexus
`ledger/get/blockhash height=0`. It does not require Solana health/root freshness or Nexus
`synchronized=true`, `syncing=false`, expected mode/network and fresh tip. The offline probe showed
exact genesis values alone pass.

`nexus_client.validate_heartbeat_asset()` checks only that a name-resolved object is readable and has
three parseable fields. A focused probe supplied a different address, owner, provider, token register
and vault with those fields; validation returned true. The staged main loop now treats a false result
as fatal, which closes the former alert-and-continue path, but it does not strengthen the evidence being
validated. Session/heartbeat checks also occur after migration and recovery scans, not before mutable
startup.

**Required contract:** before mutation, bind the exact configured heartbeat address, owner, schema,
pair/custody identities and economic terms. Require target-version Nexus readiness fields and fresh tip,
plus Solana health and root/slot freshness from the exact configured endpoint. Wrong/missing/stale or
wrong-typed evidence and query exceptions must consume/hold no new work and start no worker. Validate
these semantics on explicitly approved target nodes.

### High operability — the witness has no complete supported bootstrap/restore workflow

The runtime requires witness URL/token/deployment and both genesis pins in every mode, but `CONFIG.md`,
`.env.example` and the normal setup sequence do not define them. The maintenance note describes the
protocol and initial issue command, but no supported read-only command constructs the externally
reviewed certificate or proves a new database/restore is financially coherent. `main.run()` cannot
initialize an uncertified new database because admission correctly precedes `init_db()`.

**Required contract:** add an offline inspection/export tool that never issues, mutates or signs current
state; define audited new-deployment and restore ceremonies; list all required settings; deploy the
reference witness behind authenticated TLS on independent anti-rollback storage; and rehearse claim,
completion, crash-held recovery, clean sealing and independently approved next-generation recovery.
Do not add a development bypass to the money path.

### High operability — malformed capacity evidence can still starve eligible work

No operator-action scheduler state or eligible-query rewrite was added. The existing capacity table and
loader still retain malformed/source-conflicting rows in the global oldest-hold ordering. The prior
actual-worker reproduction remains applicable: refusal conserves liability and makes no send, but a
later valid fitting frozen intent can remain blocked indefinitely.

**Required contract:** transactionally classify non-retryable evidence outside automatic FIFO while
preserving source, principal, raw evidence, cap use and diagnostics. Real refund and quarantine workers
must advance younger same-kind and cross-kind valid work exactly once across restart and beyond worker
limits, without making the blocked row sendable.

### Separate release gates remain open

- The external witness controls byte continuity; it does not independently establish historical source
  completeness, solvency or chain truth for the approved image.
- Non-capacity Solana holds still lack an evidence-bound resolution protocol.
- Provider-v2 and receipt publication remain separately disabled/unaccepted migrations.
- Nexus pagination/reference/TLS semantics and Solana pagination/finality/unknown-outcome behavior are
  not accepted on intended infrastructure.
- No crash/restore/total-loss/operator rehearsal ran against live devnet/testnet nodes.

## Acceptance matrix

| Contract | Offline evidence | Decision |
|---|---|---|
| Retained ordinary, debit-metadata, capacity-sibling and terminal-sibling conflicts preserve principal and call no transport | Four retained-source modules included in **324 passed** focused new-change run; complete suite green | **Accepted narrowly** |
| Exact certified SQLite image required before schema mutation | Production-schema tests dynamically remove every discovered table; claim/complete/hold/seal tests pass | **Implemented offline, not release accepted** |
| Dashboard is read-only, snapshot-consistent and cannot show healthy without matching running witness | Missing-path, concurrent transition, copied receipt and snapshot tests pass | **Accepted for offline candidate scope** |
| Certificate binds exact executable/dependency build | Scratch mutation of `swapService.py` leaves `build_fingerprint` unchanged | **Failed / P0** |
| Configured heartbeat owner/address/pair is exact | Scratch mismatched identity fixture is accepted when three fields parse | **Failed / High** |
| Both nodes are healthy, synchronized and fresh | Genesis-only probe passes without health/sync/freshness reads | **Failed / High** |
| Invalid capacity rows cannot starve eligible frozen work | No new scheduler state/acceptance regression | **Open** |
| Target-chain semantics and operational ceremony | No live activity authorized or executed | **Open** |

## Executed verification

All tests and probes were offline. Chain/RPC boundaries were mocked or pointed at unreachable local
fixtures; no credentials, production state or funds were used.

| Gate | Result |
|---|---|
| Initial shared-tree complete suite | **947 passed, 77 subtests passed in 128.61s** |
| Final documentation worktree complete suite | **947 passed, 77 subtests passed in 147.43s** |
| Final disposable-index candidate complete suite | **947 passed, 77 subtests passed** |
| Focused retained-source + sealed-custody modules | **324 passed in 59.73s** |
| Recovery standalone | **35 passed, 52 subtests passed in 2.86s** |
| Recovery + installed SDK | **36 passed, 52 subtests passed in 3.23s** |
| Receipt/payout/Nexus-fee/SDK shard | **85 passed in 6.83s** |
| Dependency consistency, compilation, Markdown links | Passed |
| Index-aware token-pair inventory | Passed; **274 active lines** |
| Baseline-to-worktree and staged whitespace | Passed |
| Review probes | **3 passed**: reproduced unbound entrypoint, genesis-only admission and unbound heartbeat identity |

There is no CI run for local `HEAD` `ee10b6e` and no CI can cover the staged/uncommitted sealed-custody
candidate. Exact remote source `7b2d1c4` has successful CI run `36857060922`; that run predates
`ee10b6e` and excludes all staged candidate files.

## Ordered coding batches

1. **P0 artifact identity:** make pre-execution artifact attestation complete and externally enforceable.
2. **P1 service/chain admission:** exact heartbeat owner/address/schema/pair/terms plus Solana and Nexus
   health/sync/freshness, before mutable startup.
3. **P1 witness operations:** supported offline certificate evidence, bootstrap/restore ceremony,
   independent witness deployment and crash/seal rehearsal.
4. **P1 capacity progress:** separate operator-action evidence from eligible automatic FIFO.
5. **P1 operator resolution:** evidence-bound Solana hold disposition with exact authoritative readback.
6. **P1 live acceptance:** both directions, pagination, finality, unknown outcomes, crash boundaries,
   coherent backup/WAL restore and total-loss behavior on approved devnet/testnet infrastructure.
7. Re-run the exact final artifact gate, independent review and exact-head CI before a separate release
   decision. Production and real funds remain blocked.
