# Recovery containment repair — 2026-09-12

## Scope and baseline

Baseline: `50d88bae7a7d33377f002cc9cf8d10c5587f87ce`. This commit was already present when the repair began; it added recognition of current refund/quarantine memos and refused startup when their source/terminal reconstruction is unresolved. The original daily review evaluated the older `d0acd72` source and remains historical evidence.

This follow-up changes only the recovery scanner, regression tests and supporting documentation. It does not send funds, change cap limits, waive startup admission, fabricate terminal rows or implement automatic disposition reconstruction. Existing staged review documents and the real Git index are preserved. The bounded repair was published as `b392059de497bfefa7d8c80f36250ae7a5c4213a`; exact-head CI run `34711667411` passed. No deployment is part of this repair.

## Defect reproduced and fixed

The baseline could still certify a complete scan with empty spending evidence when a vault debit carried an encoded, missing, unknown or newer-version memo. Opaque and inner token operations could also disappear from the scan; an additional unclassified debit could accompany a recognized payout, and one transfer could be attributed to multiple memo identities.

The scanner now:

- requires one recognized top-level money memo for a single observable vault outflow;
- refuses opaque token instructions and opaque programs explicitly touching the vault;
- inspects inner instructions and refuses unsupported inner vault spending;
- rejects multiple vault outflows or multiple memos rather than assigning one transfer to multiple obligations;
- rejects incomplete transaction-error, token-transfer-source and inner-instruction schemas;
- clears accumulated payout, timestamp and disposition maps when completeness fails.

Recognized current dispositions remain held by the baseline's `current_solana_disposition_reconstruction_required` startup gate. Unknown vault spending returns `unclassified_solana_vault_spend`; opaque/inner cases have distinct incomplete reasons. Ordinary parsed incoming transfers and empty history remain accepted; existing primary-payout reconstruction tests remain in the gate.

## Verification

Tests first reproduced twelve failing subcases across unclassified spending, unsupported/ambiguous instructions and malformed evidence. Each group passed after its corresponding narrow implementation change.

A full startup test uses actual `perform_startup_recovery`, the actual scanner, real temporary SQLite databases and only mocked external transport. Both refund and quarantine spending before a newer heartbeat are found by the full rolling-window scan; startup stays incomplete for empty databases and databases with existing reserved exposure. Existing reservations remain unchanged, no synthetic terminal rows appear, and downstream Nexus scanning/reference seeding is not reached.

Final candidate verification used a disposable Git index containing the explicitly named working files; the real staged review/index was not modified. The full suite tests the working candidate, not a new committed or pushed head.

| Check | Result |
|---|---|
| Full suite | **310 passed, 53 subtests passed** |
| Standalone recovery suite | **23 passed, 28 subtests passed** |
| Recovery then real-SDK boundary | **24 passed, 28 subtests passed** |
| Receipt / payout / fee / SDK isolation | **70 passed** |
| Dependency consistency | No broken requirements |
| Byte compilation / local Markdown links / candidate whitespace | Passed |
| Candidate token-pair inventory | Passed; **286 active lines** |
| Real Git index preservation | SHA-256 unchanged: `ba3c44b51fa6ab67b538915c319b4f2612b303c2af75c3188c532332c0c2e568` |

Tested source identities:

```text
18b1a980417ad0decdbe3b122a070e5adad700eb14f9c03af74c0ad6299f1a8e  src/solana_client.py
d56366d1f81ff0ec8a2a2cdc0a586ac31531648c3aa5ca1f7fa4441e161168a5  tests/test_recovery_safety.py
```

Tests used synthetic configuration, disabled dotenv loading, an unavailable keypair and `/bin/false` for the Nexus CLI. External boundaries in the exercised recovery tests were mocked. No chain mutation or production database was used.

## Operational limitation and remaining work

This is a **fail-closed containment repair**, not complete automatic restore. Current or legacy disposition evidence still blocks startup even if some local rows survived. Do not advance waterlines, erase evidence, reset caps or suppress the hold to resume service. Preserve the database/WAL and resolve the exact source, transfer, frozen terms, terminal identity and timestamped cap evidence before implementing a separately reviewed resume protocol.

Next recovery work must reconstruct all disposition kinds from authoritative evidence atomically and idempotently, cover conflicting and incomplete sources, actual backup/WAL restoration, window boundaries, duplicate invocations and crash points, then pass the target-chain matrix. No live Nexus/Solana semantics were established here.

Independent review was attempted but the reviewer provider blocked the request before producing a verdict. **No independent approval is claimed.** The repair remains local and production admission stays blocked.
