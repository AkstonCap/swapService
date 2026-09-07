# Batch 7 Token-Pair Literal Inventory

**Status:** Batch 7, item 1 complete locally. This is an inventory and regression
baseline. The current runtime supports one configured Solana/Nexus pair; this is not
a claim that all internal names are generic, provider-v2 exists, or production is approved.

`docs/EVALUATION.md` requires each active `USDC`/`USDD` literal, legacy
configuration attribute, account label, helper default and public example to be
classified when configuration or documentation changes. Canonical single-pair configuration
already exists; the inventory tracks remaining aliases, compatibility state and explicit examples. The checker
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
| **Public pair-specific example** | Explicit USDC↔USDD default/deployment example, not universal bridge instructions. Active prose and public terms must describe the selected pair. |
| **Planned/schema example** | A planned v2 or v1-compatibility schema/example. It is not executable pair selection. |

## Classified active surfaces

| Surface | Classification | Required Batch 7 treatment |
|---|---|---|
<!-- token-pair-inventory: .env.example:3,13,17,28,34,43,45,99,106,110,118,123,131,155,156,157,163 -->
| `.env.example` | Migration alias + Public pair-specific example | Canonical input template with explicitly labelled defaults and legacy-only compatibility settings; do not change persisted contracts or numerical policies as a documentation rename. |
<!-- token-pair-inventory: .github/copilot-instructions.md:5 -->
| `.github/copilot-instructions.md` | Display metadata | Keep money-path and Nexus/Solana safety guidance synchronized with the validated canonical configuration. |
<!-- token-pair-inventory: ASSET_STANDARD.md:15,119,131,136,137,399 -->
| `ASSET_STANDARD.md` | Planned/schema example + Public pair-specific example | Keep v1 distinct from planned v2; Nexus `format=basic` fixes field sets, so never relabel an incomplete v1 asset as v2. |
<!-- token-pair-inventory: CONFIG.md:24,25,28,38,39,40,41,42,43,44,45,46,47,48,49,50,53,59,60,61,62,71,73,81,108,109,110,188,201,202,203,213,233,234 -->
| `CONFIG.md` | Migration alias + Public pair-specific example | Document implemented canonical configuration, exact defaults/alias behavior and currently legacy-only settings; do not advertise future provider-v2 settings as active. |
<!-- token-pair-inventory: README.md:3,30,33,40 -->
| `README.md` | Display metadata + Migration alias | Deployment-neutral instructions describe the configured pair and existing public-record fields; retained token literals identify defaults or compatibility aliases only. |
<!-- token-pair-inventory: SETUP.md:20,135,194 -->
| `SETUP.md` | Public pair-specific example + Migration alias | Document the implemented single-pair runtime with canonical settings and clearly separate planned provider-v2 work; retain exact supported legacy-only settings. |
<!-- token-pair-inventory: create_heartbeat_asset.py:24,26,42,43,44,250,251,253,318,319,328,329,338,339,380,382 -->
| `create_heartbeat_asset.py` | Runtime semantics + Public pair-specific example | Retire default pair/ticker arguments behind a config-derived address-based v2 creation workflow. |
<!-- token-pair-inventory: docs/EVALUATION.md:257,458,506,519,533,663,664,713,749 -->
| `docs/EVALUATION.md` | Planned/schema example | Maintain as evaluated remediation evidence; change only with verified implementation evidence. |
<!-- token-pair-inventory: docs/SECURITY.md:51,58,64,71,95,98 -->
| `docs/SECURITY.md` | Runtime semantics + Migration alias + Frozen compatibility state | Describe controls by chain and preserve actual configuration/compatibility identifiers; defaults are not fixed token identities. |
<!-- token-pair-inventory: docs/STATE_MACHINES.md:7,105,134,175,196,236,237,299 -->
| `docs/STATE_MACHINES.md` | Migration alias + Frozen compatibility state | Preserve current lifecycle terminology; migrate persisted names only with append-only database evidence. |
<!-- token-pair-inventory: docs/SWAP_INITIATOR_STATE_MACHINES.md:150,157,158 -->
| `docs/SWAP_INITIATOR_STATE_MACHINES.md` | Frozen compatibility state | Describe configured-pair flows and effective published terms; any retained fixed-pair values must be explicit examples, not universal minimums or refund guarantees. |
<!-- token-pair-inventory: nexus_transfer_operator.py:49,51,57,59 -->
| `nexus_transfer_operator.py` | Runtime semantics + Frozen compatibility state | Intent/hold reason labels must move only through a tested durable-state migration. |
<!-- token-pair-inventory: quarantine_viewer.py:25,26,27,28 -->
| `quarantine_viewer.py` | Display metadata + Frozen compatibility state | Present canonical symbols while retaining existing state labels until migrated. |
<!-- token-pair-inventory: src/config.py:12,13,15,27,31,33,39,78,79,81,83,84,85,87,88,91,95,96,97,98,104,133,138,148,154,155,157,166,167,168,170,172,173,175,186,187,227,271,272,273,275,278,279,281,287,289,291,292,313,317,319,322,327,331,345,346,348,350,361,362,364,369,372,373,375,380,388,399,400,402,433,434,435,436,439,440,449,452,454 -->
| `src/config.py` | Runtime semantics + Migration alias + Frozen compatibility state | The immutable `SwapPairConfig` exists. Extend remaining consumer coverage without renaming frozen state; preserve the exact conflict/precedence behavior of each supported legacy input. |
<!-- token-pair-inventory: src/dashboard.py:43,44,45,46,460 -->
| `src/dashboard.py` | Display metadata | Dashboard labels/fallbacks must consume canonical display symbols and never control custody or routing. |
<!-- token-pair-inventory: src/main.py:96,98,100,108,109,110,111,309,310,323,326,365,366,368,372,435,450,492 -->
| `src/main.py` | Runtime semantics + Display metadata | Production admission and output must use canonical identities/terms, preserving only compatibility names where migration requires them. |
<!-- token-pair-inventory: src/nexus_client.py:347,352,426,512,990,1726 -->
| `src/nexus_client.py` | Runtime semantics + Display metadata | Require immutable Nexus register identity for authorization/reconciliation; retain token name only where the Nexus API requires it and for presentation. |
<!-- token-pair-inventory: src/solana_client.py:275,365,617,725,747,807,931,1008,1012,1021,1022,1026,1410,1414,1458,1471,1533,1544,1545,1549,1579,1588,1589,1593,1618,1629,1726,1785,1786,1920,1923,1975,2102,2240,2251,2273,2274,2278,2305,2313,2314,2318 -->
| `src/solana_client.py` | Runtime semantics + Frozen compatibility state | Route transfers, payout caps and persisted labels through the canonical pair object without renaming live state prematurely. |

| `src/startup_recovery.py` | Runtime semantics | Recovery must preserve the validated pair/custody identity and exact integer amounts. |
<!-- token-pair-inventory: src/state_db.py:14,2792 -->
| `src/state_db.py` | Frozen compatibility state + Display metadata | Existing SQLite names stay stable until a separately tested append-only migration; new labels derive from canonical metadata. |
<!-- token-pair-inventory: src/swap_nexus.py:90,325,429,828 -->
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
