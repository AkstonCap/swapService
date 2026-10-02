# swapService operational / release-architecture re-evaluation

**Reviewed commit:** `85030c890fa6f3bb7db97e068e5cf80827d21b28` (`main`, `origin/main` at review start)
**Scope:** runtime callers, provider registration/heartbeat/waterline flow, production admission, wrong-network/sync semantics, and operator resolution of current hold classes. Documentation claims were not treated as implementation evidence. Parent-authored working-tree documentation changes that appeared during review were excluded.

## Verdict

**Not ready for live acceptance or real funds.** The committed core fails closed in many financial-state transitions and the complete offline suite is green, but provider-v2 is a unit-tested island rather than the runtime contract; startup can continue into pollers after an invalid registration check; neither chain is admitted by authoritative network/sync evidence; and several newly created Solana hold classes have no audited operator resolution path.

This review did **not** establish a current remote exploit in the provider-v2 code: it has no production caller. Findings 1, 3, and most of 4 are release/operability gates. Finding 2 is a current deployment-safety defect: a misconfigured or wrong/stale endpoint/registration is not itself an attacker exploit, but startup does not reliably prevent financial activity after that condition.

## Findings, strongest first

### 1. P0 release blocker — provider-v2 is committed but completely unwired; the legacy opt-in switch is inert

`src/service_record.py` defines the v2 builder/validator, but an AST import scan found its only direct importer is `tests/test_service_record_v2.py`. Actual callers remain on the old v1 contract:

- `src/nexus_client.py:1787-1801,1861-1904` defines and builds v1 (`distordiaType=nexusBridgeHeartbeat`).
- `src/nexus_client.py:1912-1965` publishes and reads v1 by `NEXUS_HEARTBEAT_ASSET_NAME`.
- `src/main.py:370-379,620-626`, `src/swap_solana.py:214-220`, and `src/startup_recovery.py:637-662` consume that v1 heartbeat/waterline path.
- `register_service.py:24-35,80-151` and `create_heartbeat_asset.py:67-142` create legacy records, not v2.
- `ALLOW_LEGACY_PROVIDER_V1` is parsed only in `src/config.py:473-475` and tested, but has no runtime consumer. Its default `False` therefore does not disable v1.
- `NEXUS_SERVICE_ASSET_ADDRESS`, expected owner, service ID, and `NEXUS_NETWORK` are consumed only by the dead v2 module/tests.

An offline production-admission probe set every v2 identity field empty while satisfying/mocking the existing production controls; `main.validate_production_controls()` returned `True`.

**Impact:** the new owner/address/service/pair/terms-hash validation provides no protection to startup, publication, inspection, recovery, or receipt registration. Operators can believe v2 is the default because config says so while the process continues to trust/publish v1.

**Measurable exit:** one integration test must start the real runtime path with v2 required and prove: (a) missing/mismatched address, owner, service ID, pair, custody address, terms hash, or schema aborts before DB mutation/pollers; (b) v1 is accepted only when `ALLOW_LEGACY_PROVIDER_V1=true`; (c) registration create/read/update and recovery all address the same configured immutable asset; and (d) no production caller references the v1 field constants when legacy mode is false.

### 2. P0 operational safety — invalid registration is alert-only, and chain network/sync identity is not an admission gate

`main.run()` aborts on incomplete recovery (`src/main.py:296-317`) but merely alerts when `validate_heartbeat_asset()` returns false or raises (`src/main.py:370-379`), then proceeds to reconciliation and the poll loop. An isolated mocked probe returned:

- `run_result_with_invalid_heartbeat=True`
- `heartbeat_alerted=True`
- `poller_entry_reached=True`

The existing heartbeat validator itself checks only readability, field presence and parseable waterlines (`src/nexus_client.py:2037-2062`), not v2 owner/address/pair/terms identity.

Network handling is incomplete as an admission protocol:

- Solana official hostnames are checked against the configured label and query/cursor provenance is bound (`src/solana_client.py:446-470,584-612,743-750`). This is useful drift containment.
- A custom/proxied Solana endpoint is represented by its URL or trusted configured label; there is no `getGenesisHash`, `getHealth`, rooted-slot freshness, or node-sync gate before work.
- `NEXUS_NETWORK` is only published by the unwired v2 builder (`src/service_record.py:293-306`). No runtime Nexus network identity, tip age, or synchronization check was found.
- `validate_production_controls()` (`src/main.py:89-148`) requires caps, alerts, quarantine accounts, token register identity, transport and session controls, but not a valid provider-v2 registration or either chain's verified network/sync state.

**Impact:** a wrong custom RPC label, stale/unsynced node, or registration identity mismatch can survive production admission. Existing recovery/backing checks may catch some consequences, but they do not prove chain identity or freshness.

**Measurable exit:** add one pre-state startup admission object containing authoritative Solana genesis/network + health/root freshness and Nexus network/genesis + sync/tip freshness; fail startup on unavailable/mismatch/stale evidence. Tests must assert wrong network, stale tip, unsynced node, invalid provider record, and validator exception all return non-zero before `state_db.init_db`, chain writes, or any poller. Repeat on the target nodes, not only mocked responses.

### 3. P1 release architecture — the v2 record does not fit the repository's declared basic-asset budget and has no wire acceptance path

`src/service_record.py` requires 52 fields. Key names plus separators and one-byte values already have a **1010-byte lower bound**. The realistic fixture in `tests/test_service_record_v2.py` measures **1448 bytes** with `nexus_client.service_record_size()`, versus the repository's declared `SERVICE_RECORD_MAX_BYTES = 1024` (`src/nexus_client.py:1800-1801`). The v2 module has no size calculation/gate, and v2 tests contain no create/update/readback case. The only registration tool with a size guard builds v1 (`register_service.py:90-109`).

**Scope:** this is measured against the repository's own approximate budget, not proof of the target node's exact hard limit. It is nevertheless enough to block a naive v2 cutover.

**Measurable exit:** decide and implement a target-valid storage layout (compact one-record schema only if it truly fits, or explicitly versioned/signed split records), enforce exact encoded size before spending NXS, then capture target-node create/read/update fixtures. Acceptance must prove all immutable fields survive creation, only the intended mutable fields update atomically, owner/address come from authoritative response metadata, delayed readback is handled, and an over-limit payload is rejected before create.

### 4. P0 operability gate — safe resolution exists for one Nexus hold family, but not for the new Solana policy/evidence/conflict holds

Positive control: `nexus_transfer_operator.py` implements a durable flow for exact Nexus rows in `refund held for operator review`: prepare → authorize → single execute → positive chain-reference resolve → finalize (`:29-121,130-172`). `state_db` rechecks exact source identity and competing terminal/fee/payout evidence transactionally (`src/state_db.py:1180-1212,1237-1323,1343-1458`). Tests cover sibling contract IDs, conflicts, ambiguous outcomes and finalization.

The scope is narrow:

- `_held_credit()` accepts only `refund held for operator review` (`nexus_transfer_operator.py:29-43`). Legacy/manual intent rows remain deliberately non-authorizable.
- Solana `policy held, non-sendable`, `refund/quarantine evidence held`, submission holds, and capacity `SOURCE_CONFLICT` / `MALFORMED_EVIDENCE` are visible in the dashboard (`src/dashboard.py:51-84`) and workers correctly refuse unsafe retries (`src/solana_client.py:1220-1262,1289-1308`; `src/state_db.py:4158-4214`).
- No operator command was found that accepts independent chain evidence, binds an actor/rationale, validates the frozen source/intent, and atomically resolves any of those Solana states. The only `*operator*` file is the Nexus tool.
- `quarantine_viewer.py` is read/export-only, yet its epilog advises direct Solana/Nexus CLI transfers and marking the DB resolved (`:398-418`). That bypasses the durable intent/cap/evidence protocols and is not a safe workflow.
- Capacity-only holds are operational: they auto-retry oldest-first when the rolling/current cap admits the frozen intent. The gap is policy, provenance/evidence, malformed, conflict and unknown-submission resolution.
- The Nexus CLI records actor strings but does not enforce distinct preparer/authorizer/executor/finalizer identities; therefore it must not be described as an enforced two-person control.

**Impact:** these paths are fail-safe against automatic duplicate sends, but can strand user principal indefinitely or push operators toward direct-chain/manual-SQL actions that the state machine cannot reconcile.

**Measurable exit:** provide one audited Solana disposition/resolution CLI (or explicitly choose permanent retention) with immutable evidence, actor/rationale, exact chain readback, competing-state checks, rolling-cap accounting, and atomic source/terminal transition. End-to-end tests must cover every policy hold, recovery evidence hold, malformed evidence, lifecycle/source conflict, unknown submission, conflicting terminal row, same-command replay, crash at each boundary, and rejection of direct retry. If two-person approval is required, tests must reject the same actor across approval roles.

## Verification evidence

- Full offline suite at reviewed HEAD: **565 passed, 77 subtests passed in 40.64s**.
- Focused provider-v2 / Nexus operator / Solana policy+capacity set: **131 passed in 6.56s**.
- Focused Helius network/provenance + recovery + critical safety set: **212 passed, 77 subtests passed in 20.88s**.
- `git diff --check`: clean for the observed tree.
- Initial review state was clean at HEAD. Parent-authored working-tree docs appeared during review (`docs/RECOVERY_INPUT_CAP_ACCEPTANCE.md`, `docs/STATE_MACHINES.md`, and untracked `docs/POST_CHANGE_REVIEW_2026-09-22_PRE_REEVALUATION.md`); they were not read as implementation evidence or modified.

### Reviewed file hashes (SHA-256)

- `src/service_record.py`: `1487382a259b373f08f3ec667075e88d1485bb99042dccb4d1469b16bcf8c6ec`
- `src/config.py`: `e6b7f28d60e4d26c6fed1fea0ab869aaa727fb7be3672dd6d002ba1f658f52eb`
- `src/nexus_client.py`: `7e392979aba8ac6cd06c005104fcc2861190add028ffadb44d2caced0cff29f3`
- `src/main.py`: `3f109caa95e1a005ead47cd797a97633b192548bcf6a2055c981c9fd7429e2bf`
- `nexus_transfer_operator.py`: `50b7723eda834cabd7319790a0effa05cea14019e1f1e048cedac7604b75e1c6`
- `quarantine_viewer.py`: `e1af0c467a9df56d9f699dd70353df61e7076f757252096755d14a1afd09bdf2`
- `tests/test_service_record_v2.py`: `9039c042f660dfeddf0eaaaa0c36a1f35969e777f2cb9f8345c34a7f81055497`

## Release order

1. Make network/sync/provider identity a fail-closed pre-state startup gate.
2. Choose a target-valid v2 storage/publication design and wire every registration/heartbeat/recovery caller behind the explicit legacy flag.
3. Implement and rehearse audited resolution for non-capacity Solana holds; remove direct-CLI/manual-DB advice.
4. Run target-node create/update/readback, wrong-network/sync, finality/pagination, crash/unknown-outcome and operator-rehearsal matrices on the exact candidate hash.
