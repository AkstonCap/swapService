# Sealed custody admission

## Status and boundary

**Local maintenance status (2026-10-05): green focused offline, but unpublished and not release accepted.**
Local `ebedff7` includes the whole-image/witness protocol and root-entrypoint drift containment;
`89fddc7` binds running Linux interpreter bytes. The current narrow increment also binds on-disk
conventional libpython, C/math and loader files identified through executable Linux mappings,
plus the installed `solders.solders` native transaction-building extension discovered without import,
and the solders initializer plus 25 mandatory flat Python wrappers (seven direct runtime imports
and eighteen additional eager initializer imports).
These in-process checks do not attest code before execution, mapped-memory identity, standard
library/bytecode, nested/optional SDK modules, other shared libraries/extensions, alternate filenames or
complete installed package artifacts.
The custody implementation remains absent from `origin/main`; publishing it is a
separate feature decision. Chain verification proves genesis only; heartbeat validation does not bind
the configured owner/address/pair; and no complete supported bootstrap/restore certificate ceremony exists. See the
[October 2 review](../DEVELOPMENT_REVIEW_2026-10-02.md) and
[current repair plan](../plans/2026-09-25-recovery-admission-and-capacity-fairness.md).

This repository implements an independent sealed-image admission gate in normal service startup and the read-only dashboard, plus a reference witness. It does **not** deploy, provision, or operate an independent witness service. `main.run()` takes the singleton lock and calls `custody_admission.claim()` before `state_db.init_db()`; workers cannot start until recovery succeeds and `Lease.complete()` succeeds. Missing configuration refuses startup in every mode. The dashboard consumes `custody_admission.dashboard_status()`; local SQLite counts never establish complete liabilities.

The control closes one specific restore gap: a runtime may execute only against the exact whole SQLite file most recently approved by an independent witness. The certificate binds every table, index, trigger, schema statement, page, freelist byte, and SQLite header through whole-file SHA-256 and byte size. It also binds a canonical schema digest, deployment configuration, and the currently implemented source manifest.

It does not reconstruct missing funds, validate chain truth, prove solvency, make an unreviewed backup coherent, or independently attest code that executes before the in-process fingerprint.

## Trust and deployment assumptions

The witness history must be on a separately administered server and storage domain with anti-rollback protection, durable backups, access review, monitoring, and an append-only export or equivalent protected audit sink. Copying the witness SQLite file beside the custody database defeats the independence assumption. Root compromise of both domains defeats the control.

Use independent operators for custody runtime access and initial witness approval. Generate two random secrets of at least 32 characters:

- `CUSTODY_WITNESS_TOKEN`: runtime capability; permits `GET head` and `POST claim/complete/seal/hold`.
- `CUSTODY_WITNESS_ADMIN_TOKEN`: distinct administrative capability; permits only first issuance through the CLI.

The reference HTTP server binds loopback only. Place it behind a correctly authenticated TLS reverse proxy. The runtime client requires an `https://` origin, verified TLS, bounded connect/read timeouts and response size, disables environment proxy inheritance, and never follows redirects. Do not put either secret in a URL, command line, log, audit event, exception, or dashboard.

The reference store uses transactional compare-and-swap and hash-linked append-only events. Its local triggers reject audit event update/deletion. Those controls do not protect against a witness host administrator replacing the entire store; deployment anti-rollback storage and independent audit export remain required.

## Certificate

A certificate has exactly these fields and rejects booleans, coercions, unknown fields, uppercase/noncanonical hashes, and duplicate JSON keys:

```json
{
  "deployment_id": "bridge-production",
  "generation": 0,
  "permit_nonce": "64 lowercase hex characters",
  "image_sha256": "64 lowercase hex characters",
  "image_size": 4096,
  "config_sha256": "64 lowercase hex characters",
  "build_sha256": "64 lowercase hex characters",
  "schema_sha256": "64 lowercase hex characters",
  "issued_at": 1800000000,
  "approval_rationale": "reviewed coherent custody backup and current runtime terms"
}
```

`config_sha256` binds all effective public uppercase configuration settings, including production mode, limits/caps, minimums/dust, finality, fees, heartbeat and service/query owners, retry budgets and timeouts, as well as the configured pair and independently pinned immutable chain identities. Credentials/transient session values are excluded explicitly; private locations are hashed, never published. Changing safety settings, endpoints, custody identities or the build requires separate approval, not reuse of an existing permit. The current local `build_sha256` implementation binds `src/*.py`, root `swapService.py`, `requirements.txt`, and domain-separated evidence for the running Linux executable's bytes (`/proc/self/exe`) plus the on-disk files backing conventional executable libpython/libc/libm/loader mappings (`/proc/self/maps`). A further domain-separated component hashes the installed `solders.solders` native extension selected by `PathFinder` without executing its package initializer or library. It requires an absolute, recognized extension filename and exact loader/origin agreement, performs bounded nonblocking reads with descriptor/path metadata continuity, and repeats discovery before accepting evidence. This includes the native transaction-building artifact, not complete dependency closure. A separate domain-separated source component hashes the conventional installed `solders/__init__.py`, `hash.py`, `instruction.py`, `keypair.py`, `message.py`, `pubkey.py`, `signature.py` and `transaction.py`. Discovery uses `PathFinder` and exact `SourceFileLoader`/origin agreement without execution; bounded nonblocking reads recheck descriptor/path metadata, earlier files and the complete discovery result. The manifest additionally includes the eighteen mandatory flat wrappers eagerly imported by the pinned initializer: `account.py`, `account_decoder.py`, `address_lookup_table_account.py`, `clock.py`, `commitment_config.py`, `compute_budget.py`, `epoch_info.py`, `epoch_rewards.py`, `epoch_schedule.py`, `errors.py`, `null_signer.py`, `presigner.py`, `rent.py`, `slot_history.py`, `stake_history.py`, `system_program.py`, `sysvar.py` and `transaction_status.py`. Nested `token`/`rpc` packages, optional `litesvm`/`transaction_metadata`, other SDK dependencies and executed bytecode remain outside this source manifest. Both native manifests are independent of normal service/dashboard extension import differences. It recognizes conventional glibc/musl and `ld`/`ld64` names; it does not discover every native dependency or attest alternate filenames. Selected file bytes are streamed with device/inode, mode, size and timestamp continuity checks and a repeated selected-mapping snapshot. Missing/unreadable/non-regular/empty/changing or ambiguous evidence refuses admission before permit consumption. These file hashes do **not** bind mapped-memory contents, standard library/bytecode, other shared libraries/extensions or all installed dependency artifacts, and are not pre-execution attestation. Batch 0 must externally enforce complete artifact identity. This manifest change invalidates prior build approvals; a binary/library upgrade requires independently reviewed new approval, never an automatic certificate rewrite.

`main.run()` verifies Solana `getGenesisHash` and Nexus `ledger/get/blockhash height=0` against independently approved pins before any database migration, scanner or recovery/reference lookup. This verifies the immutable chain identity reported by the approved trusted endpoints; it is not a cryptographic proof that an untrusted endpoint is honest and does not establish health, synchronization or tip/root freshness. Nexus session and heartbeat validation failures are fatal before lease completion, but the current heartbeat validator checks only name-resolution plus required fields—not exact address, owner, schema, pair, custody or terms. Complete service identity and node readiness remain release gates.

Fees are current runtime policy, not historical authorization for individual liabilities. If fees change, an independent reviewer must explicitly approve the current runtime configuration in the certificate rationale. A new certificate cannot repair missing frozen intent in a restored row.

## State machine

A generation follows only:

```text
ready(g, permit) -> claimed(g, claim nonce) -> running(g, claim nonce)
                                           -> held(g)
running(g) -> ready(g+1, new permit)
running(g) -> held(g)
```

A permit is one-use. `claim` is idempotent only for the same random 64-hex claim nonce so an ambiguous network response can be read back. Stale generations, changed nonces, and consumed permits fail. `complete`, `seal`, and `hold` accept only the owning generation and claim nonce. `held` is permanent: there is no runtime restore, clear, or reissue route.

Initial issuance is intentionally separate. The CLI consumes an externally prepared, independently reviewed certificate plus an explicit actor and rationale. It has no command that hashes or signs the current custody database:

```bash
export CUSTODY_WITNESS_TOKEN='<runtime capability>'
export CUSTODY_WITNESS_ADMIN_TOKEN='<separate admin capability>'
python -m src.custody_witness issue \
  --store /protected/witness/custody-witness.db \
  --certificate /reviewed/offline/certificate.json \
  --actor independent-reviewer \
  --rationale 'review board approval reference ...'
unset CUSTODY_WITNESS_ADMIN_TOKEN
```

Start the loopback runtime API behind a TLS proxy. The serving process deliberately does not load or retain the administrative capability:

```bash
export CUSTODY_WITNESS_TOKEN='<runtime capability>'
python -m src.custody_witness serve \
  --store /protected/witness/custody-witness.db \
  --host 127.0.0.1 --port 8790
```

Runtime endpoints are `GET /v1/deployments/<id>/head` and `POST .../claim`, `.../complete`, `.../seal`, and `.../hold`. Requests use bearer authentication and exact JSON field sets. Audit events contain transition identifiers and rationale, never credentials.

## Startup ordering

Configure every environment, including development and recovery runs:

```text
CUSTODY_WITNESS_URL=https://independent-witness.example
CUSTODY_WITNESS_TOKEN=<runtime capability>
CUSTODY_DEPLOYMENT_ID=<stable deployment identity>
CUSTODY_SOLANA_GENESIS_HASH=<independently approved canonical base58 hash>
CUSTODY_NEXUS_GENESIS_HASH=<independently approved height-zero hash: 256 lowercase hex>
```

There is no development/default opt-out. Missing witness configuration blocks admission.

Obtain chain pins from independently verified deployment records, never by automatically trusting the unverified runtime endpoint. The Nexus height-zero block hash is not a user's sigchain genesis. The Nexus node must support `ledger/get/blockhash height=0` with its `-indexheight` index enabled; unavailable indexing/query results refuse admission rather than falling back to a profile identity. The custody host/filesystem must enforce exclusive trusted writing: no concurrent live restore or unauthorized in-place edit is supported; restoring requires a stopped service and the singleton lock. Host/root compromise is outside this control.

Required startup order:

1. Load configuration and take the singleton lock without creating or migrating the custody database.
2. Call `lease = custody_admission.claim()`.
3. Admission reads the external `ready` head and compares deployment, build, configuration, whole-file SHA-256, and size.
4. Admission consumes the permit with a fresh random claim nonce.
5. It retains an open descriptor and captures the original device/inode before hashing, rehashes the file, requires `-wal`, `-journal`, and `-shm` to be absent, runs read-only `PRAGMA integrity_check`, validates the schema digest and rechecks the original file identity. A replacement cannot become the trusted identity after hashing.
6. Verify observed immutable chain identities, revalidate the retained image immediately before database initialization/migrations, and run existing recovery. Recheck file continuity after initialization and before completion.
7. After recovery and mandatory session/heartbeat validations succeed, call `lease.complete()`. It fsyncs a local `claimed` receipt, completes the remote lease, then fsyncs the matching `running` receipt. No worker may start before this succeeds.
8. The main loop calls `lease.assert_running()` before each cycle. All nested timeout/RPC threads are tracked; timed-out threads are not assumed stopped.

A local receipt is diagnostic continuity, not authority. It lives at `<STATE_DB_PATH>.admission.json`, is atomically replaced and fsynced, and cannot grant startup when copied. Healthy dashboard state requires an exact external `running` head and matching local `running` receipt for the deployment, generation, permit, claim, build, and configuration. The receipt additionally binds the database path/device/inode and the live writer's Linux boot ID, PID and process-start identity; a copied image/receipt or dead/reused PID cannot present a healthy live database. Linux `/proc` identity must be readable or admission fails closed. All other cases report `unknown` or `held`, with `liabilities_complete=false`.

The dashboard collects custody totals/counts in one read-only transaction snapshot and rechecks the exact witness generation/permit/claim after collection. An intervening transition or changed running lease suppresses healthy totals; a nonmatching held head reports unknown.

Before claim, rejection does not write the custody database or a receipt and does not consume a permit. If failure occurs after a successful claim, the generation is held whenever that can be established. For an ambiguous compare-and-swap, only an exact readback is accepted; otherwise startup fails and operator review is required.

## Graceful shutdown and sealing

The parent owns quiescence. It must stop admission of new work, signal every worker, join every worker, and prove no worker or callback can still access SQLite **before** calling `lease.seal()`. Never hold a seal permit while a worker might execute. If any thread is unresolved, call `lease.hold()` rather than seal.

`seal()` opens the existing database only, performs a truncate checkpoint, obtains an exclusive transaction, checks integrity, closes its connection, fsyncs the database and directory, rejects all ambiguous sidecars, computes the next exact certificate, and compare-and-swaps owned `running(g)` to `ready(g+1)`. There must be no database write after sealing. It finally writes a local `sealed` receipt.

A crash or unknown outcome after claim consumes the permit and blocks automatic restart. Completion/seal uncertainty is accepted only when exact remote readback proves the intended state; otherwise the lease is held. Operators must not delete receipts, edit witness state, copy an old witness store, seed a database, or issue a replacement certificate to bypass a hold.

## Verification scope

Focused offline tests cover strict certificate and JSON types, one-time permits, stale/replayed claims, permanent holds, hash-linked audit events, authenticated bounded HTTP behavior, exact normal images, online-backup clones, mutation of dynamically discovered user tables, missing images, ambiguous SQLite sidecars, uncertified existing databases, claim/complete uncertainty, repeated permit consumption, clean next-generation sealing, copied receipts, and missing configuration. They use local stores and injected HTTP sessions; there is no insecure runtime transport switch and no live-chain activity.
