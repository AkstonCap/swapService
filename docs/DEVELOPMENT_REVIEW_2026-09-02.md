# Independent Financial State-Machine Review — 2026-09-02

**Review window:** 2026-09-01 16:33:23 +0200 through 2026-09-02 16:13:46 +0200
**Baseline:** `f941d526c44d87a83eaf3002627fe0c5656edbe3`
**Reviewed head:** `8f9a30f8294cfd19fd888a6a60aa0145a782b386`
**Scope:** 2 commits; 5 files; 71 insertions, 18 deletions
**Deployment verdict:** **HARD BLOCKED for production and real funds**

## Executive result

Commit `e810e5d` materially repairs the two defects targeted by the prior review. Completed-mint reconciliation now requires the persisted Nexus `contract_id`, configured token-register source, destination, amount, reference, txid and at least ten confirmations. Direct txid lookup now holds on missing, malformed or below-threshold confirmation evidence under the default configuration. Commit `8f9a30f` only refreshes token-pair inventory line markers.

No new automatic double-payment path was found. Production is not cleared: the confirmation threshold can be configured to zero or a negative value; reference-only intent evidence has no confirmation model; completed-mint validation accepts zero Solana input with positive Nexus output; and target-node pagination/finality/crash behavior remains unverified.

## Severity-ordered findings

### High — a non-positive configured threshold disables finality

`src/config.py:220-221` parses `NEXUS_TRANSFER_MIN_CONFIRMATIONS` but does not require it to be positive. `src/nexus_client.py:596-601` accepts evidence whenever `confirmations >= minimum`. With a value of `0`, a zero-confirmation transaction becomes terminal; a negative setting is weaker still. `src/main.py:89-135` does not reject this configuration in production mode.

**Required exit:** validate the setting as a non-boolean integer greater than zero during configuration/production admission; test zero, negative, boolean-like and malformed values; prove the chosen threshold against the target node's actual confirmation/finality semantics.

### High — zero-input completed mints can reconcile healthy

`src/balance_reconciler.py:79-85` rejects negative Solana units but accepts `amount_usdc_units == 0` while requiring only positive Nexus output. Reconciliation then treats the stored positive output as the expected output. An exact remote zero-input/positive-output mint can therefore satisfy the identity match and produce no discrepancy.

**Required exit:** require strictly positive input and output base units for completed mints and add a regression proving a zero-input/positive-output row returns `healthy=False`.

### High release blocker, currently contained — reference-only outcomes lack finality evidence

`TransferDebitEvidence` does not carry confirmations. Direct txid lookup gates completeness before producing evidence, but reference-history projections do not preserve a confirmation value. Current live reference scans return `complete=False` for actionable non-empty ranges, so ambiguous outcomes remain safely held. A future complete-range or manual-adoption path could terminalize one exact reference match without proving finality if this evidence contract is not extended.

**Required exit:** persist confirmation/finality evidence in the common transfer evidence and completion record, or adopt an independently audited two-person evidence workflow. Do not weaken the current indefinite hold to regain availability.

### High release blocker — external semantics remain unproved

Remote reconciliation intentionally relies on one live-offset history page and returns incomplete when that page cannot prove the boundary. No target Nexus matrix establishes confirmation-field meaning, equal-time ordering, concurrent-head insertion, stable pagination, reorg/drop/void behavior or crash recovery.

### Medium — finality policy is incoherent

The new setting is named for transfers but also affects Solana-to-Nexus mint confirmation through the shared direct lookup. Other paths pass separate minima, while completed-mint reconciliation hard-codes `confirmations >= 10`. One validated finality policy should govern every Nexus terminalization path.

### Medium — durable identity validation remains incomplete

Direct transfer lookup accepts any non-empty txid and non-boolean integer contract ids, including negative values. Malformed identities normally hold later, but can enter durable state and create permanent operational ambiguity. Validate target formats and non-negative contract ids at the first trust boundary.

## Positive controls verified

- Completed mint rows now select, validate and match the durable `contract_id`.
- A wrong configured token-register source or wrong contract identity cannot contribute to a healthy completed-mint match.
- Missing, non-integer and below-default-threshold confirmations hold direct txid resolution.
- Exact source, destination, units, reference, txid and unique contract identity remain required.
- Empty Nexus enumeration continues to hold the waterline.
- The token-pair inventory remains executable and current.

## Verification

| Check | Exact result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider` | **PASS — 115 passed, 14 subtests passed in 10.82s** |
| `python3 scripts/check_token_pair_inventory.py` | **PASS — 651 active lines** |
| `python3 scripts/check_markdown_links.py` | **PASS — Local Markdown links OK** |
| `python3 -m pip check` | **PASS — no broken requirements** |
| `git diff --check` before review edits | **PASS** |
| Live Nexus/Solana matrix | **Not run** |

The pre-existing uncommitted `ASSET_STANDARD.md`, `SETUP.md` and `docs/EVALUATION.md` changes describe planned provider-v2/configuration work. They were read as context but are not part of the reviewed runtime range and must not be mistaken for implemented behavior.

## Required repair order

1. Enforce one positive Nexus finality policy at startup and every terminal transition.
2. Reject zero-input completed mints and add isolated negative reconciliation tests.
3. Extend reference/manual evidence with confirmations before enabling any complete adoption path.
4. Validate txid and contract-id identities at ingress.
5. Execute the target Nexus pagination/finality/crash matrix and both-chain live acceptance suite.
6. Keep automatic Nexus refund/quarantine execution disabled and admit no real funds until these gates pass.
