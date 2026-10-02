# Re-evaluation evidence — 2026-09-22

Reviewed runtime: `85030c890fa6f3bb7db97e068e5cf80827d21b28`.
The [current evaluation](../../EVALUATION.md) owns the consolidated verdict and priorities.

## Preserved independent artifacts

- [Financial review](reevaluation-financial.md): SHA-256
  `d6b41133704c738bb21cfa7f0df4cd5f8197cda176301fa8b605bca5f6551965`.
- [Operational review](reevaluation-operations.md): stored-copy SHA-256
  `7fd3343e61706fd92db424ccab6c617912199187eec4f961c6a627867a7e7116`.
  Original reviewer artifact: `2db531ef48fec3310bd430548d5e7a5dae609a24e06992698b5d944ccf8830c2`;
  the stored copy removes only the two trailing Markdown hard-break spaces on line 3.
- [Startup/waterline/route-loss probe](swap-reeval-db-loss-probe.py): SHA-256
  `84712e6af0b0b4d9bfcdb813dbf0ce402cace84a4d867c78a94f2bf58b2cb136`.
- [Frozen refund-term loss probe](swap-reeval-cap-wipe-probe.py): SHA-256
  `559dccaa089d9079e8cb586fdb116f9eed1089a313bc6fa43d823c3e6b5b4a87`.

Except for the explicitly recorded two-space formatting change above, reports and probes are
exact copies; their original `/tmp/` references are historical local
locations, not required execution paths. Parent verification checked the artifact hashes and all
15 runtime/test hashes recorded across the independent reports. Reviewer P0 labels for v2/operator
readiness are not a claim of a proven current v2 exploit; the consolidated evaluation distinguishes
runtime defects from optional-migration and operability gates.

## Parent reproduction

Both financial probes were rerun with synthetic fixture configuration, dotenv disabled, socket
connections blocked and temporary SQLite databases. They reached mocked send boundaries only.
The first returned complete startup recovery and one Nexus debit of 1,100 after losing an original
1,090-unit refund intent. The second retained the refund route but changed the mocked destination,
payout from 1,090 to 1,080, and fee from 10 to 20 after DB loss and configuration drift.

From the repository root, with the existing test environment installed, run each probe in its own
process. Keep scratch under the active profile, not a deployment database:

```bash
TMPDIR=/home/brutus/.hermes/profiles/principal-dev/cache/scratch \
.venv/bin/python -c 'import runpy,socket,sys; import dotenv; dotenv.load_dotenv=lambda *a,**k:False; runpy.run_path("tests/conftest.py"); socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(RuntimeError("network forbidden")); runpy.run_path(sys.argv[1],run_name="__main__")' \
  docs/review_evidence/2026-09-22/swap-reeval-db-loss-probe.py
```

Replace the final argument with `docs/review_evidence/2026-09-22/swap-reeval-cap-wipe-probe.py`
for the second case. These are diagnostic reproductions, **not default-collected acceptance tests**.
A successful command currently demonstrates the defect, not a safe recovery result.

The parent also repeated the provider-v2 fixture size measurement with `_configure_v2()` from
`tests/test_service_record_v2.py` and `build_record(last_poll=0)`: the repository estimator returns
1,448 against its declared 1,024-byte budget. The default current-time poll field instead measures
1,457; neither value establishes target-node wire-size semantics.

## Execution and publication scope

Full suite: 565 passed, 77 subtests passed. Parent standalone recovery, recovery+SDK,
receipt/payout/Nexus-fee/SDK and policy/cap/recovery-acceptance shards passed as recorded in the
current evaluation. No live-chain acceptance was attempted.

Exact reviewed-head [CI](https://github.com/distordialabs-brutus/swapService/actions/runs/35755684698)
is red on committed historical `.diff` whitespace: 194 findings in `committed-since-sept12.diff`
and 2 in `dirty-config.diff`. Runtime-test steps passed. A clean documentation working diff is
not evidence that this earlier published run passed.
