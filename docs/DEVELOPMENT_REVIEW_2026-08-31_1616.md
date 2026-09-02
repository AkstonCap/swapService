# Independent Financial State-Machine Follow-up — 2026-08-31 16:16 +0200

**Review window:** 2026-08-31 09:39:53 +0200 through 2026-08-31 16:16:19 +0200

**Baseline:** `38d2696c11eb48259e55e092e5b5253665c19ac0`

**Reviewed head:** `368b0643ae5b7f81894825785198aecc8f3cfe54`

**Scope:** 4 commits, 5 changed files, 273 insertions, 60 deletions

**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

The range closes two concrete local defects: an empty successful Nexus deposit enumeration no longer advances the checkpoint, and transfer-intent construction now rejects every non-`int` base-unit value. It also improves the in-memory evidence model by preserving `(txid, contract_id)` and attempts exact DEBIT-term read-back before terminalizing a confirmed mint.

The exact-contract claim is not release-ready. The reference lookup can act on one candidate from an explicitly incomplete bounded history, so it does not prove global uniqueness. More fundamentally, the parser and tests model `contracts.from`/`contracts.to` as flat strings and compare the source to the configured token name, while current LLL-TAO emits address objects for those fields and the DEBIT source is a register address. The new path therefore cannot establish its claimed exact match against the target API contract. Contract identity is also discarded when durable terminal state is written.

## Severity-ordered findings

### Critical release gate — Bounded evidence is treated as proof of uniqueness

`find_nexus_transfer_debits_by_references()` scans at most `NEXUS_LOOKUP_MAX_PAGES`, returns `complete=False` for short-page negative or page-budget outcomes, and has no snapshot/cursor guarantee (`src/nexus_client.py:1217-1301`). Its consumers nevertheless act whenever the returned candidate list has length one:

- transfer intents complete at `src/nexus_client.py:552-580`;
- unverified mints attach a txid at `src/nexus_client.py:844-880`;
- confirmed-mint read-back terminalizes at `src/nexus_client.py:746-792`.

A second matching contract outside the observed pages is invisible. The commits detect two contracts only when both happen to occur in the returned bounded response. That is not authoritative uniqueness and can archive an overpaid/double-executed disposition as complete.

**Required exit:** use a direct authoritative transaction lookup when a txid exists; for reference-only ambiguity, use a target-proven complete/cursor-stable query or remain held. Never interpret `len(candidates) == 1` from `complete=False` as “exactly one exists.”

### High — Exact endpoint matching does not implement the target Nexus JSON contract

The new parser reads `contract.get("from")` and `contract.get("to")`, converts each value with `str(...)`, and stores those strings (`src/nexus_client.py:1271-1285`; the same flat parsing exists at `1175-1201`). Tests supply flat values such as `"from": config.NEXUS_TOKEN_NAME`.

Current upstream LLL-TAO `ContractToJSON()` emits DEBIT endpoints through `AddressToJSON()`: `from` and `to` are objects containing at least an `address`, optionally a reverse name/type. The DEBIT source is the register address serialized from the contract, not the configured token-name string. Comparing the parsed source to `config.NEXUS_TOKEN_NAME` at `src/nexus_client.py:749` and `858` therefore does not prove the submitted source account and will normally fail to match a real response.

This fails safe by holding, but the new confirmation/read-back path is not operational and `docs/EVALUATION.md` overclaimed it as implemented.

**Required exit:** normalize and validate the exact target response schema, resolve the configured source name to its immutable register address before intent creation, persist that address, and test captured target-node fixtures for nested and malformed endpoint objects.

### High — Contract identity is not retained in terminal durable state

`TransferDebitEvidence` now carries `contract_id`, but terminal writes retain only `remote_txid`:

- completed transfer intent: `src/nexus_client.py:577-580`;
- processed mint archival: `src/nexus_client.py:777-792`.

The completed records therefore cannot identify which contract in a multi-contract transaction was accepted. A later audit/reconciliation must infer the identity again from mutable/bounded enumeration, weakening the exact `(txid, contract_id)` model introduced by this range.

**Required exit:** persist contract id with every terminal Nexus debit record and reconcile using the complete remote identity plus immutable source, destination, amount and reference.

### High release blocker — Remote history still cannot establish a scalable stable range

`find_nexus_mint_debits_since()` remains intentionally one-page bounded and returns `pagination_snapshot_unavailable` when that page does not cross the boundary. This is safe containment, but a zero/default waterline cannot become healthy once history exceeds the page. No target-node ordering, equal-timestamp, concurrent-arrival, nested-contract or finality matrix has been run.

### Medium — Other prior operational blockers remain

- Production admission still omits the required session when multiuser mode is enabled.
- Money-path logging exceptions can still terminate poller work without an independently surfaced worker failure.
- Nexus polling still bypasses the common transport wrapper.
- Live Nexus/Solana crash, timeout, duplicate, pagination and finality evidence remains absent.

## Commit disposition

| Commit | Disposition |
|---|---|
| `2084b9f` | **PASS locally.** Empty successful enumeration now holds the waterline. |
| `3167aa6` | **PARTIAL / BLOCKED.** Preserves contract ids inside returned evidence and detects same-response duplicates, but bounded lookup does not prove uniqueness and endpoint parsing does not match target schema. |
| `e233518` | **PARTIAL / BLOCKED.** Confirmation now attempts term read-back and holds mismatches, but exact target-source matching is not implemented and terminal state drops contract id. |
| `368b064` | **PASS locally.** Durable transfer intent accepts only exact positive runtime `int` units; bool, float, `Decimal` and text are rejected. |

## Positive controls verified

- Empty Nexus enumeration cannot propose a checkpoint.
- Explicit failed, malformed and truncated enumeration remains held.
- Same-response duplicate DEBIT contracts retain distinct ids and are held.
- A confirmed txid with mismatched mocked terms remains held.
- Submitted transfer txid immutability remains enforced.
- Transfer-intent construction rejects non-integer money without coercion.
- Automatic unsafe Nexus refunds and surplus actions remain disabled/contained.

## Verification

No live RPC, Nexus CLI credentials or fund-moving action was invoked.

| Check | Exact result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | **PASS — 99 passed, 14 subtests passed in 9.32s** |
| `python3 scripts/check_markdown_links.py` | **PASS — Local Markdown links: OK** |
| `python3 -m pip check` | **PASS — No broken requirements found** |
| `python3 -m compileall -q src tests` | **PASS — exit 0** |
| `git diff --check 38d2696..368b064` | **PASS** |
| Exact-head GitHub Actions CI | **PASS — run 33400416736** |
| Upstream LLL-TAO source inspection | **DEBIT `from`/`to` are address objects; contract `id` is the transaction contract index** |
| Live target-chain acceptance | **Not run** |

## Required repair and release order

1. Stop treating candidates from incomplete bounded reference scans as globally unique.
2. Parse captured target-node endpoint objects and persist/compare immutable source register addresses, not token-name labels.
3. Persist `contract_id` in completed transfer and mint records; reconcile by `(txid, contract_id)` and all immutable terms.
4. Establish a target-proven stable pagination/boundary protocol that can reconcile beyond one page.
5. Add the multiuser session prerequisite and isolate/surface logging/watchdog failures.
6. Run the complete Nexus plus Solana devnet/testnet crash, timeout, duplicate, equal-time, pagination, concurrent-arrival, malformed-body, finality and decimal-pair matrix.
7. Independently review the exact deployment tree and operational runbook.

A green offline suite and CI run are not permission to move real funds.
