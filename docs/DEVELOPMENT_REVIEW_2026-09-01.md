# Independent Financial State-Machine Review — 2026-09-01

**Review window:** 2026-08-31 16:25:21 +0200 through 2026-09-01 16:20:23 +0200

**Baseline:** `91719a053379a24fa1701f33d98c9dd1fdb33e91`

**Reviewed head:** `aa710665ead27d0dffa12680931e7aa9aa5197f9`

**Scope:** 13 commits, 15 files, 1,091 insertions, 175 deletions

**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The range closes the previous review's most direct fail-closed defects: incomplete bounded reference scans no longer authorize terminal state; current Nexus endpoint objects are normalized; mint confirmation compares the immutable token-register source; terminal mint/transfer rows retain `contract_id`; malformed transfer txids and inexact transfer units hold; production requires multiuser session and token-register configuration; poller logging is isolated; and Nexus deposit polling uses the common credential-safe transport. Alias conflicts now fail startup, and the token-pair literal inventory is executable.

No new obvious automatic double-payment path was found. The service is still not releasable. Reconciliation ignores the identity fields just added and can report `healthy=True` for a wrong-source/wrong-contract remote debit. The direct transfer-intent resolver does not enforce confirmations before `completed`, reference-only ambiguous outcomes have no realizable complete-range path, and the target-node/Solana finality matrix remains absent.

## Severity-ordered findings

### High — Reconciliation can report healthy without the configured mint source or persisted contract identity

`processed_sigs.contract_id` is now persisted (`src/state_db.py:90-103,405-413,1211-1240`), but `_completed_mint_rows()` does not select it and `_validate_mint_row()` cannot require it (`src/balance_reconciler.py:43-89`). Remote matching uses txid, destination, amount, reference and confirmations, then treats the matched contract's **observed** source as trusted (`src/balance_reconciler.py:375-396`). It never compares `evidence.from_address` with `config.NEXUS_TOKEN_REGISTER_ADDRESS` and never compares the remote id with the stored `contract_id`.

This matters for pre-upgrade terminal rows, whose newly added `contract_id` is NULL, and for corrupted/incomplete local evidence. An executed probe stored `contract_id=7`, returned one matching remote debit with `contract_id=99` and `from_address="WRONG-SOURCE"`, and obtained:

```text
HEALTHY_WITH_WRONG_SOURCE_AND_CONTRACT True
INCOMPLETE_REASONS []
DISCREPANCIES []
```

The current `docs/EVALUATION.md` claim that durable reconciliation uses the complete `(txid, contract_id)` and configured source identity is therefore too strong.

**Required exit:** select and validate the terminal `contract_id`; reject NULL legacy identities as incomplete unless independently backfilled; match `(txid, contract_id, reference, configured token-register source, destination, exact units, confirmations)`; and add negative tests for wrong/null contract id and wrong source that must return `healthy=False`.

### High — Direct Nexus transfer resolution ignores confirmation/finality

`get_nexus_transfer_debits_by_txid()` parses transaction identity and DEBIT contracts but does not parse or require transaction confirmations (`src/nexus_client.py:566-628`). `resolve_nexus_transfer_intents()` marks a submitted transfer `completed` after one exact contract match (`:661-700`), and the operator finalizer trusts that state plus the remote txid (`src/state_db.py:733-795`).

The regression fixture demonstrates the gap: `tests/test_critical_safety.py:2149-2166` supplies a direct transaction with no `confirmations` field and expects the intent to complete. A mempool/unconfirmed debit can therefore be archived as a completed refund/quarantine disposition before target finality is established.

**Required exit:** persist/validate the target transaction confirmation/finality evidence, hold below a configured minimum, and make final operator disposition require the exact confirmed `(txid, contract_id)` rather than txid text alone. Test zero/missing/insufficient confirmations, later confirmation, dropped/voided transactions and restart between each boundary.

### High release blocker — Reference-only unknown outcomes have no complete resolution path

For `outcome_unknown` intents, resolution requires `lookup.complete=True` (`src/nexus_client.py:703-718`). The live-offset reference lookup returns incomplete for a short final page and for page-budget exhaustion (`:1393-1476`); it has no snapshot/cursor condition that can return complete for a non-empty request. This is safe containment—one observed candidate cannot authorize a disposition—but it means a timeout after remote acceptance can remain held forever with no code-supported evidence-adoption path.

**Required exit:** use an authoritative direct lookup once a txid can be proven, establish a target-proven cursor/snapshot-stable reference query, or implement an audited two-person/manual evidence adoption that records exact confirmed `(txid, contract_id)` and immutable terms. Never weaken the current hold to regain availability.

### High release blocker — Remote history remains intentionally one-page and live-offset

`find_nexus_mint_debits_since()` reads one page and returns `pagination_snapshot_unavailable` unless that page crosses the requested boundary (`src/nexus_client.py:1277-1390`). This avoids unsafe multi-page inference, but a zero/old waterline becomes permanently unhealthy after history exceeds one page. Equal timestamps, concurrent head inserts, boundary crossing, nested/malformed contracts and target finality have not been exercised on the deployed Nexus build.

### Medium — Remote identity validation is incomplete

- Returned and supplied txids are accepted as any non-empty string (`src/nexus_client.py:536-555,575-595`), while Nexus transaction hashes have a fixed target format.
- Contract ids reject bool/non-int but accept negative integers (`src/nexus_client.py:607-611,1364-1367,1450-1452`; `src/state_db.py:845-855`).
- Production startup requires a non-empty token-register string but does not validate address format or resolve it against the configured token on the trusted node (`src/main.py:112-127`).

These paths generally fail safe later, but malformed identities can enter durable state and create permanent holds. Validate at the first trust boundary and add captured target-node fixtures.

### Documentation boundary — Planned provider-v2 work is not runtime

The pre-existing uncommitted changes in `ASSET_STANDARD.md`, `SETUP.md` and `docs/EVALUATION.md` are clearly labeled planned/documentation-only and correctly avoid claiming provider v2 is active. They were reviewed but deliberately not staged or included in this review commit.

## Positive controls verified

- Every consumer holds when bounded reference evidence is incomplete.
- A known submitted txid is read through `ledger/get/transaction` and matched on reference, source, destination, exact units and unique contract id.
- Current nested and legacy-flat Nexus endpoint forms are normalized without stringifying arbitrary objects.
- Solana→Nexus terminal rows retain destination, memo, exact units and contract id.
- Transfer intents retain immutable txid and contract id; completed state cannot regress.
- Inexact/non-integer transfer amounts and non-string/blank returned txids hold.
- Empty successful Nexus enumeration holds the waterline.
- Nexus polling uses the shared `_run()` transport; production requires HTTPS API transport and Basic credentials.
- Production admission requires caps, quarantine destinations, an alert route, token-register identity, and `NEXUS_SESSION` when multiuser mode is enabled.
- Structured-logging failures cannot interrupt the reviewed poller/client state transitions.
- Conflicting canonical/legacy token-pair aliases fail startup.
- Token-pair inventory drift is checked by code and CI.

## Verification

No live RPC, Nexus CLI credentials or fund-moving operation was invoked.

| Check | Exact result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | **PASS — 114 passed, 14 subtests passed in 11.02s** |
| Independent focused safety cases | **PASS — 9 passed in 1.14s** |
| `python3 scripts/check_token_pair_inventory.py` | **PASS — 651 active lines current** |
| `python3 scripts/check_markdown_links.py` | **PASS — Local Markdown links OK** |
| `python3 -m compileall -q src tests` | **PASS — exit 0** |
| `python3 -m pip check` | **PASS — No broken requirements found** |
| `git diff --check 91719a0..aa71066` | **PASS** |
| Exact-head GitHub Actions CI | **PASS — run 33506807712** |
| Wrong-source/wrong-contract reconciliation probe | **FAIL — returned `healthy=True`** |
| Live Nexus/Solana crash, pagination and finality matrix | **Not run** |

## Required repair and release order

1. Make completed-mint reconciliation consume and require stored `contract_id` plus the configured token-register source; fail closed on legacy NULL identities.
2. Require confirmed/final direct transaction evidence before completing or finalizing Nexus transfer intents.
3. Provide a safe, auditable resolution path for reference-only `outcome_unknown` intents without treating bounded history as absence or uniqueness.
4. Establish a target-proven stable pagination/boundary protocol for mint reconciliation and Nexus waterlines.
5. Validate txid, contract-id and token-register formats/ownership at their first durable boundary.
6. Run the full Nexus plus Solana devnet/testnet crash, timeout, duplicate, equal-time, pagination, concurrent-arrival, malformed-body, finality and mixed-decimal matrix.
7. Independently review the exact deployment tree, alert route and operator runbook before any real-fund admission.

A green offline suite and CI run are not permission to move real funds.
