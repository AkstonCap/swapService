# swapService safety repair — 2026-09-07

**Scope:** local implementation candidate on top of `6568446d551c785a2b96d16217b468e78a93defd`.
**Publication:** not committed or pushed. The prior review and its pre-existing pagination regression were preserved.
**Release decision:** real-fund deployment remains blocked pending target-chain and operational acceptance.

This repair supersedes the implementation findings in [the baseline review](DEVELOPMENT_REVIEW_2026-09-07.md), not its historical execution evidence.

## Implemented safety boundaries

### Exact Nexus source identity and operator disposition

- Transfer intents carry immutable `source_contract_id`, separate from the outbound transfer's `contract_id`. Deterministic ids/references and uniqueness bind the exact source pair.
- Operator `prepare` requires `--txid` **and** `--contract-id`. Preparation, authorization and the single-use execution claim require an exact held source with matching units and treasury. Competing payout/disposition paths cannot claim the same source.
- Finalization writes one exact terminal row, deletes only that source, and retains sibling liabilities. Conflicting existing terminal evidence is not replaced. Migration retains old references, remote ids and audit evidence; unknown source identities are explicit legacy holds, not guessed contract zero.

### Payout terms and fee journal

- Before Solana submission, one SQLite transaction freezes output units and retained Nexus fee units and claims the exact ready source. A second worker cannot submit the same source again.
- Submission does **not** book a settled fee or write a terminal source row. The RPC helper no longer creates pseudo-txid/legacy-sentinel zero-amount archives or uses them as payout idempotency keys. Exact queue/intention state owns idempotency. Confirmed finalization journals the per-contract fee, writes terminal source/output/destination evidence and removes the queue row atomically. A failure at any local write rolls the transaction back.
- Below-minimum and fee-only admission use that same atomic terminal/journal boundary. A rejected finalization cannot advance a checkpoint. Replay is idempotent only for matching evidence.
- Missing historical payout terms remain liabilities. Confirmation-time policy changes do not recalculate fees. Both stored signatures and memo-discovered signatures require full successful finalized transaction evidence: source memo identity, transaction signature, vault signer/source, mint, exact recipient and output must match the frozen intent. A finalized status or matching memo alone cannot authorize archival. Unavailable or conflicting proof cannot authorize resubmission, fee booking or source removal.
- Held credits cannot be reopened by ordinary receival lookup; legacy trade-balance lookup keeps the exact contract id. Failed vault-liquidity reads hold instead of submitting. The dormant quarantine helper cannot debit or archive funds.

### Recovery and startup

- One strict memo parser distinguishes `nexus_txid:<txid>:<contract_id>` from legacy txid-only identity. It rejects malformed identifiers rather than normalizing them.
- Recovery uses attributable successful Solana payout evidence plus the exact Nexus source credit, preserving the paid sibling while recovering unpaid siblings. Sparse, legacy or conflicting evidence blocks recovery rather than creating zero-amount completed records.
- Mutable Nexus offset pagination cannot claim multi-page completeness. The live poll also holds its checkpoint once a nonzero offset is requested, including a short or empty later page; positive credits may still be retained. Identical repeat scans are not a substitute for an immutable snapshot. The original recovery-pagination regression remains in the suite alongside live-poller regressions.
- Startup requires affirmative complete recovery before pollers or other exposure-producing loop work. Missing heartbeat, zero checkpoints, bounded fallback, scan failures and reconstruction exceptions are refusal conditions, not warnings that permit service operation.

## Verification

The first independent review returned **SPEC_GAPS**, despite a green baseline of **228 tests and 33 subtests**: the real submission helper still wrote sparse terminal markers, live confirmation discarded actual payout terms, and live polling still authorized multi-page checkpoints. The second repair cycle targets those exact cross-file paths rather than treating the baseline test count as proof.

### Candidate gate before the SDK follow-up (historical)

Executed in the installed-dependency Python environment, with a **temporary Git index** containing the current candidate so index-aware checks include the uncommitted runtime, tests and documents. The real index was hash-checked unchanged. Runtime hashes were unchanged during the gate.

| Check | Executed result |
|---|---|
| `python -m pytest -q -p no:cacheprovider` | **245 passed, 33 subtests passed** |
| Focused new payout/poll regressions | **17 passed** |
| Isolated identity and recovery regression modules | **58 passed, 8 subtests passed** |
| `python -m pip check` | No broken requirements found |
| `python -m compileall -q src *.py tests` | Passed |
| `python scripts/check_markdown_links.py` | Local Markdown links: OK |
| `python scripts/check_token_pair_inventory.py` on candidate index | Current; **649 active lines** |
| `ruff check --select F821,F822,F823 src nexus_transfer_operator.py tests` | Passed; this is a scoped undefined-name/local-variable check, not full lint approval |
| Worktree and candidate-index `git diff --check` | Passed |
| Real installed-SDK runtime import smoke | All eight changed runtime/operator modules imported offline |
| Synthetic `GetTransactionResp` decode through direct payout-evidence adapter | Passed with real installed SDK; exact proof returned and finalized commitment requested |

The broad Ruff rule set is **not clean** (style, unused/redefined names and broad-exception findings, plus test import ordering); it is not the repository's CI gate. Do not describe the scoped result above as a full Ruff pass.

The new regression module is `tests/test_payout_review_regressions.py`. It exercises the real submit helper without terminalization; positive direct and crash-recovered finalization; wrong output, recipient, source memo, signer, vault, mint, signature and missing success metadata; duplicate payout ambiguity; no resubmission on recovered mismatch; and live multi-page hold versus valid single-page advancement. Existing fee-policy/rollback/replay/sibling assertions remain, adapted to consume full payout evidence instead of a status-only mock.

**Independent spec re-review: PASS.** The reviewer reproduced the three repaired boundaries, including additional known-error, duplicate-exact-memo and empty-second-page probes; checked operator/payout exclusion, exact sibling deletion and startup admission; and found no blocking specification gap on this working-tree candidate. Its full suite returned **245 passed, 33 subtests passed**, with **87 passed and 8 subtests** in the focused payout/fee/identity/recovery selection. All eight reviewed runtime/operator hashes and two payout-test hashes were identical at review start/end and independently matched by the parent against the current files. **Separate code-quality re-review: REQUEST_CHANGES on that snapshot.** It found that mandatory Solana startup scanning still passed strings to SDK transaction lookups and pagination cursors that require `solders.signature.Signature`. Although inherited from the old helpers, that incompatibility prevents the new mandatory recovery gate from admitting a non-empty valid history. That blocker was subsequently repaired at the SDK boundary, without weakening startup refusal; final follow-up results are recorded below. Tests use temporary databases and mocked network boundaries; installed-dependency import and decoder probes are separate from those mocks. None of these results verifies live transaction acceptance, chain finality, target-node history semantics or production operation. No live financial operation is part of this repair.

## SDK follow-up and final candidate gate

The follow-up converts transaction signatures to `solders.signature.Signature` at all existing raw-string transaction lookup call sites and converts the mandatory scan's `before` cursor. Invalid transaction signatures and cursors return explicit incomplete reasons; cursor failure clears accumulated evidence. It does not change payout amounts, fees, identity semantics or startup admission policy.

`tests/test_solana_sdk_boundary.py` uses a fresh interpreter with the real installed SDK and replaces only the provider transport. It covers a non-empty mandatory scan, successful second-page cursor construction, malformed signature/cursor holds, generic and recent memo discovery, and both inherited core-RPC transaction readers. No endpoint is contacted and no vault keypair is used. This test skips when the real SDK is absent; the installed-dependency gate **executed it successfully, without a skip**.

The final installed-dependency candidate gate returned **246 passed, 33 subtests passed**. Compilation, dependency consistency, scoped F821/F822/F823 lint, Markdown links, candidate-index inventory and whitespace checks passed. Runtime hashes and the real Git index were unchanged during the run. Independent parent probes additionally exercised the real public `Client` request encoders and typed SDK responses with synthetic positive Nexus payout evidence: both single-page and multi-page recovery returned complete coverage and the exact payout proof, with socket connections forbidden.

**Final supplemental spec review: PASS. Final code-quality re-review: APPROVED.** The spec reviewer reran the real-SDK boundary test, the 17 payout/poll regressions and both positive SDK reconstruction probes. All 11 reviewed runtime/test hashes matched the final manifest before and after review. A separate quality reviewer examined the exact SDK delta, all current transaction lookup call sites and complete new regression module, confirmed the installed SDK parameter types, and reran the 18 focused tests successfully. Its verdict had empty security-concern and logic-error lists, with the changed source and test hashes identical before/after review. The earlier stalled reviewer was cancelled and was not counted as approval. These reviews target the post-SDK-fix files, not the earlier 245-test snapshot; approval covers this local repair, not production readiness.

<details>
<summary>Final reviewed runtime and regression-test SHA-256 snapshot</summary>

```json
{
  "nexus_transfer_operator.py": "50b7723eda834cabd7319790a0effa05cea14019e1f1e048cedac7604b75e1c6",
  "src/main.py": "2adaecd9a6cb9f97b2ec0f97b9822c4a2d894c346ffdccc8b458a1b2bb430eb7",
  "src/nexus_client.py": "fb2f84e73c2b3223d6989b9558b4a23062428c9e60a640a6f6d98627bbc13a08",
  "src/nexus_memo.py": "329c5649001475e10e87be4b1687c8cfc9e4a9426e605d52c014bc4bae20f8c3",
  "src/solana_client.py": "354e4ecdfbb95f3ae81fd5004f9cf7ab1af9ca4453f90c30f7245d697838e196",
  "src/startup_recovery.py": "85823a06c0f14035929512ccf670f4ea45e2c39e7f4576413c8e02ce8658995a",
  "src/state_db.py": "69c3b2e135205cd0eb8d43963a9fbd65870b938b28fa56032f2fbcc4085ce96d",
  "src/swap_nexus.py": "bb190a8a287ec8da5db8ba39953561c6fa1e52212d4c5bfa2cf6ab4a8b181076",
  "tests/test_nexus_fee_lifecycle.py": "c7f51deb6ca808ad281e87d46a20f7c0ed8a973aa55b4dc611fbb206264570d8",
  "tests/test_payout_review_regressions.py": "ad28dfd804bd77b5794031cb7297e8634818c2b7dd4fc7ab0c3939869d2140ff",
  "tests/test_solana_sdk_boundary.py": "6a5532bbb7ed9de68388cafd14ccead403eaba934a406a31ba76c63617a5ffdd"
}
```

</details>

## Upgrade and acceptance instructions

1. Stop the service and take a consistent database/WAL backup before testing migration. Rehearse migration and repeated initialization on a disposable copy; do not operate old code against the upgraded schema.
2. Inventory legacy `source_contract_id=-1`, fee-journal identity gaps, pending payouts without frozen terms and legacy/sparse payout memos. Do not edit them into ready state or invent source ids. Retain liabilities and obtain attributable chain/operator evidence.
3. Use the operator's explicit contract selector. Inspect the returned intent and reference before authorizing one execution. An unknown result remains held; a failed lookup never permits another debit.
4. Run the full local gate, then the target Nexus/Solana isolated-network matrix: real account-history projection, ordering/equal timestamps, stable range coverage, finality, RPC/CLI response shapes, before/after-acceptance timeout, crash/restart and two-party credit/claim behavior.
5. Missing/zero custody checkpoints require a separately reviewed bootstrap or recovery disposition. Never set waterlines to the current time merely to make startup pass.
6. Keep automatic Nexus refunds/quarantine and surplus movements disabled. These fixes do not authorize real funds, production startup, deployment, or a trade.
