#!/usr/bin/env python3
"""Guard the boundary between renameable code and frozen on-disk names.

The bridge is token-pair agnostic and its identifiers read `solana`/`nexus`. Three
categories of name are deliberately excluded from that, because they are not code
identifiers at all - they are values that already exist outside this process, written by
an earlier build of the service:

  * state-database column names,
  * persisted key strings that carry a safety property (retry budgets, the debit
    reservation kind, lifecycle status values),
  * environment variables an operator has already set.

Renaming any of them silently breaks an upgrade over an existing database. During the
rename that introduced this file, two such names were changed by accident: a `snap.get()`
column read, and two columns of `fee_summary`. Both would have surfaced only against a
real database with real rows in it - which is exactly the situation where the failure is
most expensive. This test makes the boundary mechanical instead of remembered.
"""
import os
import sqlite3
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


class _PK:
    @staticmethod
    def from_string(s):
        return s

    @staticmethod
    def find_program_address(seeds, pid):
        return ("ATA", 0)


# Only third-party chain/network libraries are stubbed; every module under test is real.
_stub("solana"); _stub("solana.rpc"); _stub("solana.rpc.api", Client=lambda *a, **k: None)
_stub("solders"); _stub("solders.pubkey", Pubkey=_PK); _stub("solders.keypair", Keypair=object)
_stub("solders.signature", Signature=_PK); _stub("solders.hash", Hash=object)
_stub("solders.instruction", Instruction=object, AccountMeta=object)
_stub("solders.transaction", Transaction=object, VersionedTransaction=object)
_stub("solders.message", Message=object)
_stub("requests", post=lambda *a, **k: None, get=lambda *a, **k: None)
_stub("dotenv", load_dotenv=lambda *a, **k: None)
os.environ.update({
    "SOLANA_RPC_URL": "http://x", "VAULT_KEYPAIR": "/k", "VAULT_USDC_ACCOUNT": "V",
    "USDC_MINT": "M", "SOL_MINT": "S", "NEXUS_PIN": "p",
    "NEXUS_USDD_TREASURY_ACCOUNT": "T", "NEXUS_TOKEN_REGISTER_ADDRESS": "TOKEN_REGISTER", "SOL_MAIN_ACCOUNT": "O",
    "NEXUS_CLI_PATH": "/bin/false",
})

DB = tempfile.mktemp(suffix=".db")
os.environ["STATE_DB_PATH"] = DB

from src import state_db  # noqa: E402

state_db.DB_PATH = DB
state_db.init_db()

fails: list[str] = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra and not cond else ''}")
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------- schema
#
# The full expected schema, written out rather than derived, so that a rename applied
# mechanically across the tree cannot quietly update the expectation along with the code.

EXPECTED_SCHEMA = {
    "accounts": ["nickname", "chain", "ticker", "name", "address", "balance", "timestamp"],
    "attempts": ["action_key", "count", "last_timestamp"],
    "counters": ["name", "value"],
    # Explicit additive safety migration: old column names remain unchanged.
    "fee_entries": ["id", "sig", "txid", "kind", "amount_usdc_units", "amount_usdd_units",
                    "contract_id", "timestamp"],
    "fee_summary": ["id", "total_collected_usdc", "total_collected_usdd", "last_updated"],
    "heartbeat": ["name", "last_beat", "wline_sol", "wline_nxs"],
    "metrics_snapshot": ["id", "timestamp", "vault_usdc_units", "circulating_usdd_units",
                         "ratio_bps", "paused", "payouts_24h_units", "fees_usdc_units",
                         "fees_usdd_units"],
    # Every state-changing Nexus transfer must be tied to a durable, attributable
    # operator decision. The event table is append-only and the action field is unique
    # per intent, so repeating a CLI command cannot rewrite past authorization evidence.
    "nexus_transfer_audit_events": ["id", "intent_id", "action", "actor", "rationale",
                                    "evidence", "timestamp"],
    "nexus_transfer_intents": ["id", "kind", "source_txid", "source_contract_id", "from_address", "to_address",
                               "amount_usdd_units", "reference", "status", "remote_txid", "contract_id",
                               "created_timestamp", "last_attempt_timestamp", "resolved_timestamp"],
    "payouts": ["id", "kind", "amount_usdc_units", "reference", "timestamp"],
    # E-015 additive payout-cap journal. It never renames or rewrites an existing
    # payout row; its append-only events retain capacity across unknown outcomes.
    "solana_payout_budget_events": ["id", "obligation_id", "kind", "event",
                                    "amount_usdc_units", "signature", "evidence", "timestamp"],
    # Additive retry journal for cap-refused refund/quarantine obligations. The source
    # lifecycle remains in unprocessed_sigs while exact frozen terms survive restart.
    "solana_payout_capacity_holds": ["source_signature", "kind", "obligation_id",
                                      "needed_units", "used_units", "cap_units",
                                      "first_held_timestamp", "updated_timestamp", "reason",
                                      "intent_evidence", "attempt_count"],
    # Provenance migrations append a durable audit record before converting a
    # pre-schema terminal disposition into an evidence hold.
    "solana_disposition_provenance_migrations": ["id", "kind", "source_signature",
                                                   "payout_signature", "payout_units",
                                                   "reversed_fee_units", "timestamp"],
    # The finalized-history cursor is an additive, durable availability journal. Its
    # events prove page-before-cursor ordering; it does not rewrite financial rows.
    "solana_deposit_scan_cursor": ["vault_account", "mint", "lower_timestamp", "before_signature",
                                    "upper_timestamp", "started_timestamp", "network", "commitment",
                                    "query_identity", "previous_timestamp"],
    "solana_deposit_scan_events": ["id", "vault_account", "mint", "lower_timestamp",
                                    "upper_timestamp", "request_before_signature", "next_before_signature",
                                    "signature_count", "admitted_count", "event", "timestamp"],
    # Trusted Helius uses a provider-native token bound to one immutable bounded query.
    # Seen-signature evidence and durable holds are additive safety journals; no frozen
    # lifecycle row or key is renamed.
    "helius_deposit_scan_cursor": ["vault_account", "network", "mint", "commitment",
                                     "lower_timestamp", "upper_timestamp", "pagination_token",
                                     "previous_timestamp", "query_identity", "started_timestamp"],
    "helius_deposit_scan_events": ["id", "query_identity", "network", "vault_account",
                                     "mint", "commitment", "lower_timestamp", "upper_timestamp",
                                     "request_pagination_token", "next_pagination_token",
                                     "signature_count", "admitted_count", "held_count", "event",
                                     "timestamp"],
    "solana_deposit_scan_seen": ["query_identity", "signature", "block_timestamp"],
    "solana_deposit_holds": ["signature", "block_timestamp", "memo", "from_address",
                              "amount_units", "reason", "evidence_json", "provider",
                              "query_identity", "first_seen_timestamp", "updated_timestamp",
                              "network", "vault_account", "mint", "observed_commitment",
                              "finality_required", "replay_attempts", "last_replay_timestamp"],
    # These append-only fields are intentionally introduced by the E-004 durable
    # reconciliation migration. Existing rows retain their original columns and are
    # treated as incomplete evidence until a separately verified backfill exists.
    "processed_sigs": ["sig", "timestamp", "amount_usdc_units", "txid", "amount_usdd",
                       "amount_usdd_units", "nexus_destination", "memo", "status", "reference",
                       "contract_id"],
    "processed_txids": ["txid", "contract_id", "timestamp", "amount_usdd", "amount_usdd_units", "from_address", "to_address",
                        "owner", "sig", "status", "payout_solana_units", "payout_fee_nexus_units",
                        "payout_receival_account"],
    # E-015 settlement-proof fields freeze the actual token-account recipient and
    # versioned output memo before RPC. E-018 provenance/evidence fields distinguish
    # those pre-submission rows from legacy chain-only recovery output.
    "quarantined_sigs": ["sig", "timestamp", "from_address", "destination_address",
                         "amount_usdc_units", "memo", "payout_memo", "quarantine_sig",
                         "quarantined_units", "status", "intent_provenance", "intent_evidence"],
    "quarantined_txids": ["txid", "contract_id", "timestamp", "amount_usdd", "from_address", "to_address",
                          "owner", "sig", "status"],
    "refunded_sigs": ["sig", "timestamp", "from_address", "destination_address",
                      "amount_usdc_units", "memo", "payout_memo", "refund_sig",
                      "refunded_units", "status", "intent_provenance", "intent_evidence"],
    "refunded_txids": ["txid", "contract_id", "timestamp", "amount_usdd", "from_address", "to_address",
                       "owner_from_address", "confirmations_credit", "status", "sig"],
    # Additive durable publication journal. It never authorizes or retries a payout.
    "swap_receipts": ["source_signature", "receipt_name", "expected_owner", "payload_json",
                      "status", "asset_address", "manual_review_error",
                      "created_timestamp", "updated_timestamp"],
    # E-016 additive receipt NXS-spend journal. Unknown create outcomes retain their
    # reservation, so a restart cannot regain budget by treating timeout as failure.
    "receipt_nxs_budget_events": ["id", "source_signature", "receipt_name", "event",
                                  "expected_cost_nxs_units", "create_txid", "asset_address",
                                  "timestamp"],
    "reservations": ["kind", "key", "timestamp"],
    # Additive input-policy evidence freezes source, decision and exact terms before
    # any debit; pre-existing column names and legacy lifecycle values stay intact.
    "unprocessed_sigs": ["sig", "timestamp", "memo", "from_address", "amount_usdc_units",
                         "amount_usdd_units", "status", "txid", "policy_decision",
                         "policy_evidence", "reference"],
    "unprocessed_txids": ["txid", "contract_id", "timestamp", "amount_usdd", "from_address", "to_address",
                          "owner_from_address", "confirmations_credit", "status",
                          "receival_account", "sig", "amount_usdd_units", "hold_reason",
                          "payout_solana_units", "payout_fee_nexus_units"],
    # Additive total-loss admission latch; initialization never clears it or rewrites
    # custody rows. Existing-schema upgrade is covered by test_empty_database_recovery.
    "recovery_admission_holds": ["id", "reason", "nexus_waterline", "solana_waterline"],
    # Additive replay containment; existing-schema upgrade/reinitialization is covered
    # by test_partial_restore_deposit_recovery without changing any retained rows.
    "solana_recovery_boundary": ["id", "cutoff_timestamp"],
    "waterline_proposals": ["chain", "proposed_timestamp"],
}

print("\n[1] State database schema matches frozen names and approved additive journals")
conn = sqlite3.connect(DB)
actual = {}
for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    if t.startswith("sqlite_"):
        continue
    actual[t] = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
conn.close()

check("no table added or removed", set(actual) == set(EXPECTED_SCHEMA),
      f"+{sorted(set(actual) - set(EXPECTED_SCHEMA))} -{sorted(set(EXPECTED_SCHEMA) - set(actual))}")
for table in sorted(set(actual) & set(EXPECTED_SCHEMA)):
    got, want = actual[table], EXPECTED_SCHEMA[table]
    check(f"columns of {table}", got == want,
          f"+{[c for c in got if c not in want]} -{[c for c in want if c not in got]}")


# --------------------------------------------------------------------------- keys
print("\n[2] Persisted key strings keep their original values")
check("debit reservation kind", state_db.DEBIT_RESERVATION_KIND == "usdc_to_usdd_debit",
      state_db.DEBIT_RESERVATION_KIND)

EXPECTED_KEYS = {
    state_db.debit_attempt_key: "usdd_debit:SIG",
    state_db.refund_attempt_key: "usdc_refund:SIG",
    state_db.quarantine_send_attempt_key: "usdc_quarantine_send:SIG",
    state_db.quarantine_attempt_key: "usdc_quarantine:SIG",
    state_db.payout_attempt_key: "usdc_send:SIG",
    state_db.nexus_refund_attempt_key: "usdd_refund:SIG",
    state_db.nexus_refund_unresolved_attempt_key: "usdd_refund_unresolved:SIG",
    state_db.nexus_refund_pending_attempt_key: "usdd_refund_pending:SIG",
    state_db.nexus_collect_refund_attempt_key: "usdd_collect_refund:SIG",
}
for fn, expected in EXPECTED_KEYS.items():
    check(f"{fn.__name__}", fn("SIG") == expected, fn("SIG"))


# --------------------------------------------------------------------------- statuses
print("\n[3] Lifecycle status values are unchanged")
# Written into `unprocessed_txids.status`. A rename orphans every row an earlier build
# left mid-flight: the poller would not recognise its own queue after an upgrade.
from src import swap_nexus  # noqa: E402

EXPECTED_STATUSES = {
    "NEXUS_STATUS_PENDING": "pending_receival",
    "NEXUS_STATUS_READY": "ready for processing",
    "NEXUS_STATUS_PAYOUT_CAP_HOLD": "payout cap held",
    "NEXUS_STATUS_SENDING": "sending",
    "NEXUS_STATUS_AWAITING": "sig created, awaiting confirmations",
    "NEXUS_STATUS_REFUNDED": "refunded",
    "NEXUS_STATUS_PROCESSED": "processed",
    "NEXUS_STATUS_FEES": "processed as fees",
    "NEXUS_STATUS_REFUND_PENDING": "refund pending",
    "NEXUS_STATUS_REFUND_HOLD": "refund held for operator review",
    "NEXUS_STATUS_QUARANTINED": "quarantined",
    "NEXUS_STATUS_TRADE_BAL_CHECK": "trade balance to be checked",
    "NEXUS_STATUS_COLLECTING_REFUND": "collecting refund",
}
for attr, expected in EXPECTED_STATUSES.items():
    check(attr, getattr(swap_nexus, attr, None) == expected, str(getattr(swap_nexus, attr, None)))

# The dashboard filters issues by these exact strings; a drift on either side hides work
# from the operator rather than raising an error.
from src import dashboard  # noqa: E402

for s in dashboard.SIG_ISSUE_STATUSES:
    check(f"sig issue status in use: {s!r}", isinstance(s, str) and s != "")
    check(f"sig issue status has operator action: {s!r}", s in dashboard.SIG_OPERATOR_ACTIONS)
for s in dashboard.TXID_ISSUE_STATUSES:
    check(f"txid issue status matches a lifecycle value: {s!r}",
          s in EXPECTED_STATUSES.values())
    check(f"txid issue status has operator action: {s!r}", s in dashboard.TXID_OPERATOR_ACTIONS)


# --------------------------------------------------------------------------- env vars
print("\n[4] Every documented environment variable is still read")
# Operators already have these in their .env; the generic spellings are aliases, not
# replacements. Config is read as source rather than imported, because importing it
# requires a full chain environment.
#
# The expected set is derived from `.env.example` rather than written out here. A rename
# applied mechanically across the tree would otherwise rewrite the code AND this test's
# expectation in the same pass and still pass - which is exactly what happened once while
# this file was being written. Cross-checking against a non-Python file closes that hole:
# the substitution would have to corrupt both a .py and a .env to go unnoticed.
import glob
sources = sorted(glob.glob(os.path.join(ROOT, "src", "*.py")) +
                 glob.glob(os.path.join(ROOT, "*.py")))
cfg_src = open(os.path.join(ROOT, "src", "config.py")).read()
extra_sources = "".join(open(f).read() for f in sources)

env_example = os.path.join(ROOT, ".env.example")
documented = []
if os.path.exists(env_example):
    for raw in open(env_example):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        documented.append(line.split("=", 1)[0].strip().lstrip("# "))
check(".env.example parsed", len(documented) > 10, f"{len(documented)} names")

haystack = cfg_src + extra_sources
missing = [n for n in documented if f'"{n}"' not in haystack]
check("every documented variable is read somewhere", not missing, ", ".join(missing))


# --------------------------------------------------------------------------- round trip
print("\n[5] A row written through the frozen names reads back")
state_db.add_unprocessed_sig("SIG_FROZEN", 1_700_000_000, "nexus:acct", "sender",
                             1_500_000, "to be refunded", None)
check("sig round-trips", state_db.is_unprocessed_sig("SIG_FROZEN"))
state_db.record_payout("solana_send", 2_000_000, "SIG_FROZEN")
check("payout total reads back", state_db.payouts_since(86400) == 2_000_000,
      str(state_db.payouts_since(86400)))
check("reservation taken", state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, "SIG_FROZEN"))
check("reservation is exclusive",
      not state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, "SIG_FROZEN"))
state_db.release_reservation(state_db.DEBIT_RESERVATION_KIND, "SIG_FROZEN")
check("reservation released",
      state_db.reserve_action(state_db.DEBIT_RESERVATION_KIND, "SIG_FROZEN"))

for p in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(p):
        os.remove(p)

print()
if fails:
    print(f"❌ {len(fails)} frozen name(s) drifted:")
    for f in fails:
        print(f"   - {f}")
    print("\nIf a change here is intentional, it needs a migration that rewrites the "
          "existing rows, not just an updated expectation in this file.")
    sys.exit(1)
print("✅ frozen on-disk names intact; renames stayed on the code side of the boundary")
