# Swap Service Audit Findings

Audit of the swap process for inconsistencies, bugs, and documentation issues. Findings are categorized by severity.

---

## Code Bugs

### BUG-1: `check_quarantine_confirmations` missing return statement (MEDIUM)

**File:** `src/solana_client.py`, line ~1124

**Description:** The function `check_quarantine_confirmations()` does not have a `return processed_count` statement at the end. Python returns `None` by default. The caller in `swap_solana.py:77` assigns the result to `confirmed_quar` and prints it, which will print `None` instead of a count when there are no confirmations or an early exit.

**Impact:** The function works correctly for processing but the caller receives `None` instead of `0` when no quarantines were processed. The `if confirmed_quar > 0` guard on line 78 will raise a `TypeError` when comparing `None > 0`.

**Fix:** Add `return processed_count` at the end of the function body.

---

### BUG-2: Exception swallowing in `poll_solana_deposits` (MEDIUM)

**File:** `src/swap_solana.py`, lines 92-94

**Description:** The entire `poll_solana_deposits()` function is wrapped in a try/except that catches all exceptions and silently passes:
```python
except Exception:
    pass
```

**Impact:** Any error during Solana polling — including critical ones like database corruption, configuration errors, or API authentication failures — will be silently ignored. The service will appear to be running normally (heartbeat updates, loop continues) but no deposits will be processed.

**Recommendation:** At minimum, log the exception. Consider re-raising certain exception types (e.g., `SystemExit`, `KeyboardInterrupt`) and logging others.

---

### BUG-3: `send_usdc_to_token_account_with_sig` pollutes `processed_txids` table (LOW)

**File:** `src/solana_client.py`, lines 1499-1508

**Description:** When a USDC send succeeds with a `nexus_txid:` memo, the function inserts a record into the `processed_txids` table using the memo as the txid key (e.g., `nexus_txid:01b88ff8...`) with dummy values (amount=0, empty addresses). This is for idempotency on the USDC send side.

**Impact:** The `processed_txids` table is also used by the USDD→USDC flow to track real Nexus transaction processing. These synthetic entries with dummy data could interfere with reporting, balance reconciliation, or any logic that iterates all processed_txids rows. The idempotency key format (`nexus_txid:01b88ff8...`) is different from real txids (just `01b88ff8...`), which currently prevents collisions, but this is fragile.

**Recommendation:** Use a separate idempotency table (e.g., `usdc_send_idempotency`) or prefix the keys more explicitly to avoid future collisions.

---

## Documentation Inconsistencies

### DOC-1: README still references old `solana:<ADDRESS>` reference pattern (HIGH)

**File:** `README.md`, lines 206-211 ("How It Works > USDD → USDC")

**Description:** The "How It Works" section at the bottom of README.md still describes the old flow:
> "The transaction's reference must be: `solana:<SOLANA_ADDRESS>`"

This contradicts the actual implementation and the upper section of the same file, which correctly documents the asset-mapped approach (`txid_toService` + `receival_account`). The old reference-based approach is not implemented in the current codebase.

**Impact:** Users reading the bottom section will attempt the wrong procedure and their swaps will fail (no asset mapping = timeout and refund).

**Fix:** Remove or update lines 204-211 to match the asset-mapped flow.

---

### DOC-2: README Quick Start uses wrong Nexus command for users (MEDIUM)

**File:** `README.md`, line 56

**Description:** The Quick Start example shows:
```bash
nexus finance/debit/token from=USDD to=<TREASURY_ACCOUNT> amount=10.5 pin=<PIN>
```

Per the Nexus Finance API docs, `finance/debit/token` deducts from the **token supply register itself** — this is an operation reserved for the token creator/owner. Regular users holding USDD in their accounts should use `finance/debit/account`:
```bash
nexus finance/debit/account from=<YOUR_USDD_ACCOUNT> to=<TREASURY_ACCOUNT> amount=10.5 pin=<PIN>
```

**Impact:** Regular users who are not the USDD token creator will get an error when attempting this command.

**Note:** The detailed instructions section (line 96-101) correctly shows both `finance/debit/token` and `finance/debit/account` options, but the prominent Quick Start section only shows the token variant.

---

### DOC-3: `HEARTBEAT_WATERLINE_SAFETY_SEC` default mismatch (LOW)

**File:** `src/config.py` line 78 vs documentation

**Description:** The code sets the default to `"0"`:
```python
HEARTBEAT_WATERLINE_SAFETY_SEC = int(os.getenv("HEARTBEAT_WATERLINE_SAFETY_SEC", "0"))
```

But the README `.env` template (line 355) shows the default as `120`:
```
HEARTBEAT_WATERLINE_SAFETY_SEC=120
```

**Impact:** Without explicitly setting this variable, the safety window is disabled (0 seconds), meaning the service could skip transactions that arrive within the boundary. The `.env` template suggests 120s is the expected default.

**Recommendation:** Either change the code default to `120` or update the documentation to note the actual default is `0`.

---

### DOC-4: Fee naming is cross-directional and confusing (LOW)

**File:** `src/config.py`, lines 84-92

**Description:** The fee variable naming is counterintuitive:
- `FLAT_FEE_USDC` (default 0.5) — Used in the **USDD→USDC** direction (deducted from USDC output)
- `FLAT_FEE_USDD` (default 0.1) — Used in the **USDC→USDD** direction (deducted from swap amount) AND as the USDC refund fee

The variable names suggest "this fee is denominated in USDC/USDD" but they're actually named after the output token direction, not the input. `FLAT_FEE_USDC` is the fee when the output is USDC (i.e., USDD→USDC path), not when the input is USDC.

**Impact:** Configuration errors if operators misunderstand which fee applies to which direction. The CONFIG.md describes them correctly but the variable names are misleading.

**Recommendation:** Add explicit comments in `.env.example` and CONFIG.md clarifying which direction each fee applies to.

---

### DOC-5: SETUP.md lacks API access requirements (MEDIUM)

**File:** `SETUP.md`

**Description:** The setup guide does not clearly specify:
1. **Solana RPC access**: Rate limits on public `api.mainnet-beta.solana.com`, recommendation to use a dedicated RPC provider (Helius, QuickNode, etc.) for production
2. **Helius API key**: Required for the optimized deposit fetching path; without it, the service falls back to N+1 RPC calls which is much slower
3. **Nexus node requirements**: Must have `apiauth=0` or configured `apiuser`/`apipassword`; must have an active session (`sessions/create/local`); CLI must be on PATH or configured via `NEXUS_CLI_PATH`
4. **Nexus session/PIN requirements**: The service uses `pin=` in CLI commands but doesn't handle session creation; the user must ensure a session is active
5. **Solana CLI and SPL Token CLI**: Version requirements, installation links

**Impact:** Operators may fail to set up the service correctly, especially regarding Nexus API authentication and Solana RPC rate limits.

---

## Process Issues

### PROC-1: USDC→USDD debit failure immediately marks for refund (MEDIUM)

**File:** `src/solana_client.py`, lines 680-684

**Description:** When a Nexus USDD debit fails (line 668-669 returns `False`), the deposit is immediately marked `to be refunded` without any retry mechanism at the debit stage:
```python
else:
    state_db.update_unprocessed_sig_status(sig, "to be refunded")
```

The retry mechanism only applies to the refund itself, not the original debit attempt. A transient Nexus CLI error (timeout, temporary network issue) will cause the swap to fail and trigger a refund instead of retrying the debit.

**Recommendation:** Use the attempt tracking system (`should_attempt`/`record_attempt`) for the debit operation, similar to how refunds and USDC sends are handled. Only mark for refund after `MAX_ACTION_ATTEMPTS` debit failures.

---

### PROC-2: USDD→USDC confirmation timeout quarantines instead of retrying (LOW)

**File:** `src/swap_nexus.py`, lines 360-365

**Description:** When a USDC send is in `sig created, awaiting confirmations` state and the confirmation timeout expires, the code moves it directly to `USDD_STATUS_QUARANTINED`:
```python
state_db.update_unprocessed_txid(txid=txid, status=USDD_STATUS_QUARANTINED)
```

The comment (Bug #15 fix) correctly notes that auto-refunding is dangerous because USDC may have been sent. However, quarantining also skips the memo-based signature recovery logic. If the signature was actually created but the confirmation check failed due to RPC issues, the transaction may have succeeded but the service loses track of it.

**Recommendation:** Before quarantining, attempt one final `find_signature_with_memo` lookup to recover the signature. Only quarantine if that also fails.

---

### PROC-3: No validation of memo format before Nexus address extraction (LOW)

**File:** `src/solana_client.py`, lines 632-641

**Description:** The memo parsing `memo.split(":", 1)[1].strip()` extracts the Nexus address but doesn't validate the memo contains only `nexus:` as prefix. A memo like `nexus:some:colon:data` would extract `some:colon:data` as the address, which would fail Nexus validation and trigger a refund. While this is not exploitable (it just wastes a refund cycle), stricter parsing would be cleaner.

---

## API Conformance Notes

### API-1: Nexus `register/list/assets:asset` query with field filters

**File:** `src/nexus_client.py`, lines 396-440

**Description:** The service queries assets using:
```
register/list/assets:asset results.txid_toService=<txid> results.owner=<owner>
```

Per the Nexus Register API docs, `register/list` supports `where` clauses for filtering. The current approach uses URL-parameter-style filtering (`results.field=value`), which works but may behave differently from the documented `where` clause syntax depending on the Nexus node version.

**Status:** Working correctly based on the implementation. The query pattern is consistent with how Nexus CLI processes field-level filters.

---

### API-2: Solana Memo Program ID inconsistency

**File:** `src/solana_client.py`

**Description:** The codebase uses two different Memo program IDs:
- `Memo111111111111111111111111111111111111111` (line 1172, for creating memos)
- `MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr` (line 273, in Helius deposit parsing)

Both are valid Solana Memo programs — `Memo111...` is the newer SPL Memo v1, and `MemoSq4g...` is the legacy Memo program. The Helius enriched data may reference either depending on which the sender used.

**Status:** Not a bug — both are checked in the appropriate contexts. However, the memo creation always uses `Memo111...` while the parsing only checks `MemoSq4g...` in the Helius path. If a deposit uses the newer memo program, the Helius parser may miss it.

**Recommendation:** Check for both memo program IDs when parsing deposits in the Helius path.

---

### API-3: Helius `getTransactionsForAddress` is not a standard Solana RPC method

**File:** `src/solana_client.py`, lines 74-97

**Description:** `getTransactionsForAddress` is a Helius-specific enhanced RPC method, not part of the standard Solana JSON-RPC spec. The fallback to core RPC (`getSignaturesForAddress` + `getTransaction`) is correctly implemented, but this dependency should be documented.

**Status:** Correctly handled with fallback. Documented in the code comments.

---

## Summary

| ID | Severity | Type | Status |
|----|----------|------|--------|
| BUG-1 | MEDIUM | Missing return statement | **Fix applied** |
| BUG-2 | MEDIUM | Silent exception swallowing | **Fix applied** |
| BUG-3 | LOW | Table pollution | Documented |
| DOC-1 | HIGH | Wrong user instructions | **Fix applied** |
| DOC-2 | MEDIUM | Wrong Nexus command | **Fix applied** |
| DOC-3 | LOW | Default value mismatch | **Fix applied** |
| DOC-4 | LOW | Confusing naming | Documented |
| DOC-5 | MEDIUM | Missing API requirements | **Fix applied** (SETUP.md updated) |
| PROC-1 | MEDIUM | No debit retry | Documented |
| PROC-2 | LOW | Quarantine without recovery attempt | Documented |
| PROC-3 | LOW | Loose memo parsing | Documented |
| API-1 | INFO | Query style | Conformant |
| API-2 | LOW | Memo program ID inconsistency | Documented |
| API-3 | INFO | Non-standard RPC method | Correctly handled with fallback |
