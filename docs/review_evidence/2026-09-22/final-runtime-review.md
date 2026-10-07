# Final independent closure review — C-Q-1 and A/B/C integration

## Verdict: APPROVED

The reviewed offline working candidate closes C-Q-1 without a scoped regression in the previously accepted A/B/C safety properties. No live-network or production approval is implied.

Repository files were not edited, staged, committed, reset, cleaned, pushed, or published. Tests used temporary databases and mocked Solana transport/address resolution where applicable. No live API was called.

## Closure findings

- `src/state_db.py:3834-3849` defines the closed typed `current_cap_too_low` result and durable impossible-cap reason.
- `src/state_db.py:4234-4251` passes one worker cap snapshot into cap-aware candidate selection. `src/state_db.py:2467-2482` removes already-diagnosed impossible holds before `LIMIT`, orders currently eligible held work first in FIFO order, then new work, then stale holds requiring refreshed diagnosis.
- `src/state_db.py:4677-4867` rechecks source lifecycle, terminal/opposing state, immutable hold identity, global eligible-hold FIFO, rolling usage, and current cap under `BEGIN IMMEDIATE`. Only a committed `PREPARED` result authorizes RPC; hold deletion occurs in the same transaction as reservation, terminal-intent insertion, and source transition.
- The global oldest-hold query excludes payouts individually impossible under the supplied nonzero cap, so an over-cap head no longer starves a younger fitting obligation. Eligible holds remain globally FIFO across refund and quarantine kinds.
- `src/solana_client.py:1265-1608` uses the same cap snapshot for advisory selection and transactional preparation. Held retries load frozen destination, amount, memo, source, fee, and terms; mutable fee/address configuration is not consulted to regenerate intent.
- Unchanged impossible holds are filtered before the worker window, so attempts and alerts do not storm. A changed cap makes them candidates again; a sufficient cap and cap `0` both promote the original frozen intent.
- `src/dashboard.py:210-284` derives the current operator action from frozen needed units and the live cap, including impossible rows not reached by a bounded worker scan.
- Same-kind and cross-kind fairness, bounded-window behavior, cap increase/restart, cap `0`, exact frozen evidence, full old-principal retention, no cap oversubscription, source/terminal conflicts, and bounded alerting are covered and passed.
- Unknown/pre-RPC and submitted outcomes remain non-releasable/non-retryable. Candidate statuses do not include submission-held or awaiting-confirmation states, and preparation rejects existing durable reservation/submission/terminal state.
- No bypass was found in the previously accepted integration controls: A's strict current-v1 disposition provenance remains required; B's below-minimum/nonpositive-output rows remain full-principal non-sendable policy holds; C's atomic cap reservation, frozen held retry, and conflict handling remain intact.
- The original real-worker starvation probe now sends the younger fitting payout exactly once, retains the older impossible hold and all source principal, and reports 30/40 used capacity.

## Executed verification

From `/home/brutus/github/swapService`:

```text
.venv/bin/python -m pytest -q tests/test_solana_capacity_holds.py
31 passed in 4.43s

.venv/bin/python -m pytest -q \
  tests/test_recovery_acceptance.py \
  tests/test_recovery_terminal_admission.py \
  tests/test_recovery_safety.py \
  tests/test_solana_deposit_policy.py \
  tests/test_solana_capacity_holds.py \
  tests/test_payout_budget.py \
  tests/test_payout_review_regressions.py \
  tests/test_critical_safety.py
339 passed, 77 subtests passed in 24.97s

.venv/bin/python -m pytest -q
565 passed, 77 subtests passed in 49.26s

.venv/bin/python -m pytest -q tests/test_legacy_scripts.py tests/legacy_frozen_names.py
5 passed in 1.41s

git diff --check
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
.venv/bin/python scripts/check_markdown_links.py
all passed; pip: No broken requirements found; links: Local Markdown links: OK
```

Pointed closure selection:

```text
.venv/bin/python -m pytest -q \
  'tests/test_solana_capacity_holds.py::test_real_workers_skip_older_currently_impossible_hold_for_younger_fitting_payout' \
  'tests/test_solana_capacity_holds.py::test_restart_reenables_impossible_hold_with_original_frozen_terms_after_cap_change' \
  'tests/test_solana_capacity_holds.py::test_worker_limit_skips_already_diagnosed_impossible_holds_and_admits_fitting_work' \
  'tests/test_solana_capacity_holds.py::test_release_refuses_pre_rpc_disposition_intent_and_submitted_transfer' \
  'tests/test_solana_capacity_holds.py::test_real_worker_capacity_hold_terminal_conflict_retains_full_liability' \
  'tests/test_solana_capacity_holds.py::test_changed_frozen_hold_terms_are_a_source_conflict_not_malformed_evidence'
15 passed in 1.74s
```

Original blocking probe after repair:

```text
PYTHONPATH=. .venv/bin/python /tmp/swap-cap-review-probe.py
{'worker_results': [1, 0], 'send_calls': 1,
 'holds': [('old-impossible', 50, 30, 40,
            'frozen Solana payout exceeds current nonzero cap', 2)],
 'sources': [('old-impossible', 60, 'refund capacity held'),
             ('young-fits', 40, 'refund sent, awaiting confirmation')],
 'liability': 100}
```

## Exact reviewed runtime/test hashes

SHA-256 values were captured before execution and recaptured after all verification; they were identical.

```text
6e893eaff0a38b8988c6c16ebbf54b3e6b054630aff9ff8bd0e39dbda054a4f4  src/alerts.py
e6b7f28d60e4d26c6fed1fea0ab869aaa727fb7be3672dd6d002ba1f658f52eb  src/config.py
eb79683a5aaacf7d47732deac063d166cff987486dc9c8e717c7b42f68d95d89  src/dashboard.py
9e5d778865402765014b27f250430bfdfc758dc3d1dd2593fc3a3bbd8940f2c4  src/fees.py
7e392979aba8ac6cd06c005104fcc2861190add028ffadb44d2caced0cff29f3  src/nexus_client.py
a607bb0594be61c0a9e2429f178df8c8711e1b17572dcad9d2ba32baabed84f3  src/solana_client.py
184dea5d83396a74667988e3b300b892152b1c23f5f385d6293282ed6f95c84e  src/solana_deposit_policy.py
919e1127a5b914d360ac45c33d97d787e30500fc0f43bd5a3d0e1541806aae8b  src/startup_recovery.py
a226b450fe689fed1b121c76ece8903ea05dbf68f67f7bdc61cdc8d973fc97c1  src/state_db.py
3cf6cdaa8e5170992fdc384cc25fffaf6508ec42ab50eb0cca34e9e17065f8e5  src/swap_solana.py
18ef51e6ec09ae8df904951463107826280e05a2fd4fda3db2099eb51bac7a5f  tests/legacy_frozen_names.py
3cee9e50620c85ebccd33dcea9e92d407d914716373af3684ffe7c85a7364eb9  tests/test_ci_contract.py
87a964b654b43a09773d3ed52b3a36c2ac7bcb54ba04adb39c3e0253be4af622  tests/test_critical_safety.py
ca35221f15d857907923c209e699423a3efaf128495bf4ca6da2a953bc3dc66e  tests/test_payout_budget.py
8b593069092e7a765051c039afb26644b1094f8cb27e6eeb971fe9f84451d321  tests/test_payout_review_regressions.py
437593ca7eaa3a6a5cb276a95af8f51d51eabd9b6c286835af28ac161a3b8119  tests/test_recovery_acceptance.py
5f8ea97c578fe995abb565a8df75366bdc0a4e1e851b5a750a723c3cf746112b  tests/test_recovery_safety.py
8b1965b9267b7bd0a1045dc6157e964ebd4e7249fba41143eda108fea9de9e7f  tests/test_recovery_terminal_admission.py
a0afaa85d20a80222f05bff5c61bc4d353163f22f00a4e43219921b9b984bbb3  tests/test_solana_capacity_holds.py
889cef2aa6f062ffce16ee1cba064707202e3cefe8c45ecf4f94c60c95d9bdad  tests/test_solana_deposit_policy.py
5a64f7ab5878b986ad24a6adef3842ae897abf8b6c1ab9043cd5f43057f0f713  tests/test_solana_sdk_boundary.py
```

Repository/index identity after verification:

```text
HEAD:          a1f19b109681da693331583e30fa192ccee17d2d
index tree:    93f17eb238bac1d898a331996ae673f7d4654381
cached diff:   empty
```

Documentation/inventory files may continue to move under the parent task and were not treated as a runtime blocker. This verdict is limited to the requested offline C-Q-1 repair and A/B/C runtime/test integration; it does not approve live Solana/Nexus/provider behavior or unrelated inherited expansion.
