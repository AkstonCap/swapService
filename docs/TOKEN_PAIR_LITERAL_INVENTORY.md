# Batch 7 Token-Pair Literal Inventory

**Status:** Batch 7, item 1 complete locally. This is an inventory and regression
baseline, not a claim that the service is pair-neutral or production-ready.

`docs/EVALUATION.md` requires each active `USDC`/`USDD` literal, legacy
configuration attribute, account label, helper default and public example to be
classified before generic configuration work changes a money path. The checker
reads the **staged candidate commit** (not unrelated worktree edits), then fails
on an unclassified addition or stale marker.

## Scope and exclusions

The scan automatically covers every tracked Python and Markdown surface plus
`.env.example`. It includes runtime modules, root and `scripts/` operator helpers,
active user/operator documents and `.github` developer guidance. This prevents a
new helper or current document from silently bypassing the inventory.

Excluded surfaces are explicit:

- `tests/`: fixture values exercise legacy compatibility rather than deployment
  semantics and remain covered by frozen-name and mixed-decimal regressions.
- `Nexus API docs/`: vendored upstream Nexus documentation.
- dated `DEVELOPMENT_REVIEW_*` / `POST_CHANGE_REVIEW_*` evidence plus
  `AUDIT_FINDINGS.md` and `RISK_ASSESSMENT.md`: rewriting reviewed history would
  corrupt audit evidence.
- this inventory and its checker: their token strings are validation syntax, not
  bridge configuration.

## Classification legend

| Class | Meaning and Batch 7 treatment |
|---|---|
| **Runtime semantics** | Can affect live custody, a money path, validation, lookup or helper default. Replace only through validated canonical configuration and focused tests. |
| **Migration alias** | Existing `USDC_*`/`USDD_*` inputs/attributes that retain deployment compatibility. Keep temporarily behind one conflict-detecting adapter. |
| **Frozen compatibility state** | Existing SQLite column/label or persisted lifecycle surface. Do not rename without an append-only migration and upgrade test. |
| **Display metadata** | Dashboard, log or CLI presentation fallback. Derive from canonical symbols; it must never authorize routing or reconciliation. |
| **Public pair-specific example** | Current USDC↔USDD instructions, terms or developer guidance. Generate selected-pair terms only after the corresponding implementation exists. |
| **Planned/schema example** | A planned v2 or v1-compatibility schema/example. It is not executable pair selection. |

## Classified active surfaces

| Surface | Classification | Required Batch 7 treatment |
|---|---|---|
<!-- token-pair-inventory: .env.example:1,12,15,17,40,41,42,43,44,45,46,47,49,83,86,124,126,127,128,134,135,137,138,140,142,144,154,155,161,173,174,185,186,187,188,204,205 -->
| `.env.example` | Migration alias + Public pair-specific example | Replace with canonical input names and documented alias-conflict policy only when validated configuration exists. |
<!-- token-pair-inventory: .github/copilot-instructions.md:4,10,11,18,19,34,37,49,50,58,137,140,154,156,160,170,171,174,175,176,177,179,180,181,197,202 -->
| `.github/copilot-instructions.md` | Public pair-specific example + Runtime semantics | Keep money-path and Nexus/Solana safety guidance synchronized with the validated canonical configuration. |
<!-- token-pair-inventory: ASSET_STANDARD.md:5,10,41,42,59,60,96,97,133,141,144,145,161,255,305,306,333,377,378,379,380,381,389,390,396,402,413,414,415,416,417,465,467,469 -->
| `ASSET_STANDARD.md` | Planned/schema example + Public pair-specific example | Keep v1 distinct from planned v2; Nexus `format=basic` fixes field sets, so never relabel an incomplete v1 asset as v2. |
<!-- token-pair-inventory: CONFIG.md:16,17,19,25,26,27,28,29,31,40,41,52,53,54,55,65,72,73,83,85,94,95,97,98,99,102,118,119,120,124,125,137,156,161,162,176 -->
| `CONFIG.md` | Migration alias + Public pair-specific example | Derive operator configuration reference and terms from the canonical object after implementation. |
<!-- token-pair-inventory: README.md:1,3,31,32,36,37,38,42,44,46,53,55,56,58,62,64,68,76,80,82,85,94,97,99,100,106,110,111,117,119,121,122,123,126,128,130,141,150,151,161,163,170,171,172,175,176,177,178,184,189,196,198,199,200,203,205,207,208,215,216,218,219,220,223,226,227,228,230,231,234,235,237,238,248,249,275,317,348,351,353 -->
| `README.md` | Public pair-specific example | Current fixed deployment instructions stay truthful until selected-pair public terms are generated from validated configuration. |
<!-- token-pair-inventory: SETUP.md:3,22,30,31,33,36,37,40,41,60,138,139,140,141,142,144,177,178,181,187,188,189,197,204,210,213,215,220,224,234,236,254,259,262,266,268,269,270,275,306,324,325,326,327,330,331,333,334,366,381,413,433,436,465,468,524,534,535,537,538,539,541,545,582 -->
| `SETUP.md` | Public pair-specific example + Migration alias | Update only after runtime validation and terms generation are shipped, so documentation never overstates generic support. |
<!-- token-pair-inventory: create_heartbeat_asset.py:24,26,42,43,44,250,251,253,318,319,328,329,338,339,380,382 -->
| `create_heartbeat_asset.py` | Runtime semantics + Public pair-specific example | Retire default pair/ticker arguments behind a config-derived address-based v2 creation workflow. |
<!-- token-pair-inventory: docs/EVALUATION.md:239,440,584,585 -->
| `docs/EVALUATION.md` | Planned/schema example | Maintain as evaluated remediation evidence; change only with verified implementation evidence. |
<!-- token-pair-inventory: docs/SECURITY.md:35,42,55,56,59,73,100 -->
| `docs/SECURITY.md` | Public pair-specific example | Derive risk/control naming from the selected validated pair once behavior changes. |
<!-- token-pair-inventory: docs/STATE_MACHINES.md:3,7,73,77,80,95,99,106,118,121,127,133,137,139,140,142,145,156,178,182,183,186,188,197,199,207,217,218,223,224,225,236,237,238,239,240,245,246,256,257,262,271,277,279,323,325,327,328,329,330,338,339,340,341,342,343,344,345,346,347,348,351,370,374 -->
| `docs/STATE_MACHINES.md` | Public pair-specific example + Frozen compatibility state | Preserve current lifecycle terminology; migrate persisted names only with append-only database evidence. |
<!-- token-pair-inventory: docs/SWAP_INITIATOR_STATE_MACHINES.md:7,9,13,15,16,25,26,27,28,31,34,41,44,47,50,51,60,65,66,68,71,75,77,78,80,87,88,89,90,91,95,97,99,100,101,107,108,114,120,121,122,123,124,128,130,134,136,139,140,154,162,164,173,174,176,179,182,183,192,199,200,202,203,206,210,211,212,213,218,225,226,227,228,229,238,245,248,251,272,274,277,279,280,282,292,293,294,295,296,298,302,304,312,313,314,316,318,320,341,342,348,349,351,354,355,357,359 -->
| `docs/SWAP_INITIATOR_STATE_MACHINES.md` | Public pair-specific example | Keep user flows, thresholds and fees specific until generated selected-pair terms exist. |
<!-- token-pair-inventory: nexus_transfer_operator.py:44,46,52,54 -->
| `nexus_transfer_operator.py` | Runtime semantics + Frozen compatibility state | Intent/hold reason labels must move only through a tested durable-state migration. |
<!-- token-pair-inventory: quarantine_viewer.py:25,26,27,28 -->
| `quarantine_viewer.py` | Display metadata + Frozen compatibility state | Present canonical symbols while retaining existing state labels until migrated. |
<!-- token-pair-inventory: src/balance_reconciler.py:156,158 -->
| `src/balance_reconciler.py` | Runtime semantics | Reconciliation must consume canonical identities and scales, never a token ticker literal. |
<!-- token-pair-inventory: src/config.py:12,13,15,27,31,33,39,78,79,81,83,84,85,87,88,91,95,96,97,98,104,133,138,148,154,155,157,166,167,168,170,172,173,175,186,187,227,271,272,273,275,278,279,281,284,285,297,299,300,302,304,314,318,319,321,323,334,335,337,342,345,346,348,353,361,372,373,375,406,407,408,409,412,413,422,425,427 -->
| `src/config.py` | Runtime semantics + Migration alias + Frozen compatibility state | Build one immutable `SwapPairConfig`; legacy aliases remain conflict-detecting inputs and database labels remain frozen absent migration. |
<!-- token-pair-inventory: src/dashboard.py:43,44,45,46,460 -->
| `src/dashboard.py` | Display metadata | Dashboard labels/fallbacks must consume canonical display symbols and never control custody or routing. |
<!-- token-pair-inventory: src/fees.py:143 -->
| `src/fees.py` | Runtime semantics | Route every flat, proportional and micro fee through the exact pair fee policy. |
<!-- token-pair-inventory: src/main.py:96,98,100,108,109,110,111,286,287,300,303,342,343,345,349,414,429,471 -->
| `src/main.py` | Runtime semantics + Display metadata | Production admission and output must use canonical identities/terms, preserving only compatibility names where migration requires them. |
<!-- token-pair-inventory: src/nexus_client.py:264,320,325,367,453,931,1081,1082,1115,1639,1696,1697,1698,1699 -->
| `src/nexus_client.py` | Runtime semantics + Display metadata | Require immutable Nexus register identity for authorization/reconciliation; retain token name only where the Nexus API requires it and for presentation. |
<!-- token-pair-inventory: src/solana_client.py:273,363,614,722,744,804,928,1005,1009,1018,1019,1023,1407,1411,1455,1468,1530,1541,1542,1546,1584,1593,1594,1598,1612,1638,1649,1666,1667,1771,1876,2003,2014,2036,2037,2041,2068,2076,2077,2081 -->
| `src/solana_client.py` | Runtime semantics + Frozen compatibility state | Route transfers, payout caps and persisted labels through the canonical pair object without renaming live state prematurely. |
<!-- token-pair-inventory: src/startup_recovery.py:44,105,110,143 -->
| `src/startup_recovery.py` | Runtime semantics | Recovery must preserve the validated pair/custody identity and exact integer amounts. |
<!-- token-pair-inventory: src/state_db.py:14,2184 -->
| `src/state_db.py` | Frozen compatibility state + Display metadata | Existing SQLite names stay stable until a separately tested append-only migration; new labels derive from canonical metadata. |
<!-- token-pair-inventory: src/swap_nexus.py:90,168,332,424,539,715,723,726,756,768,772,779,816 -->
| `src/swap_nexus.py` | Runtime semantics | Nexus-to-Solana processing must use canonical token/custody/fee configuration and durable intent rules. |
<!-- token-pair-inventory: src/swap_solana.py:126,193 -->
| `src/swap_solana.py` | Runtime semantics | Solana-to-Nexus processing must use canonical token/custody/fee configuration and immutable Nexus identity checks. |

## Verification

```bash
python scripts/check_token_pair_inventory.py
git add docs/TOKEN_PAIR_LITERAL_INVENTORY.md scripts/check_token_pair_inventory.py
git diff --cached --check
python -m pytest -q tests/test_token_pair_inventory.py
```

Run `python scripts/check_token_pair_inventory.py --list` after an intentional
legacy-literal change, then update its classified table row and rationale in the
same candidate commit.
