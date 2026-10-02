# swapService Vision

## Place within Distordia

Cross-chain settlement is one part of Distordia's coordination and accountability infrastructure. As software and execution become abundant, the scarce value is trustworthy identity, verification, settlement evidence, accountability, and risk information. Distordia should make those properties legible without becoming the permanent custodian, market maker, or underwriter.

The intended destination is open settlement infrastructure in which parties can verify exact assets, terms, authorization, and outcomes; conditional contracts or peer-to-peer escrow hold value where the target chains permit it; and Distordia supplies standards, indexing, attestations, recovery tooling, and risk analytics. Execution should remain open and independently verifiable.

This repository is a **bounded transition system**, not that destination. Its current single-pair custodial bridge is useful for learning and proving settlement invariants across Solana and Nexus, but expanding operator custody is not the strategic goal. New work should reduce custodial discretion and create a credible migration path toward open, non-custodial or conditional settlement. Distordia does not take balance-sheet risk or promise to absorb settlement losses.

## Settlement principles

1. **Identity precedes value movement.** Authorize by immutable network, mint/register, custody endpoint, source, destination, and contract identity—not by ticker, display name, or operator convention.
2. **Settlement is proven, not asserted.** Submission, a returned transaction identifier, a heartbeat, or a provider's `online` status is not final settlement. Completion requires authoritative success/finality evidence matching the frozen intent.
3. **Accounting is exact and durable.** Use integer base units, conservative conversion, explicit fees, and complete liability accounting. Unknown principal remains a liability; absence of local rows is not evidence of zero obligations.
4. **Intent precedes side effects.** Persist immutable terms, authorization, idempotency identity, and capacity before transport. Ambiguous outcomes are held for evidence or audited disposition; they are never blindly retried, refunded, or reinterpreted under new configuration.
5. **Recovery governs admission.** A service that cannot reconstruct exact obligations and authorization must not create new exposure. Crash, restart, stale or partial backup, and total state-loss behavior are part of the settlement protocol, not operational afterthoughts.
6. **Public records are claims, not endorsements.** Provider records and attestations must expose their issuer, scope, freshness, evidence, and limits. Publication by Distordia or observation on-chain does not prove solvency, correctness, or release approval.
7. **Custody is transitional and minimized.** Do not add custody merely for convenience. Prefer atomic or conditional settlement, user-controlled escrow, bounded authority, and independently executable recovery whenever the chains support them.
8. **Safety claims follow evidence.** Local tests establish only their tested boundaries. Production readiness requires target-chain, provider, custody, migration, crash/recovery, and operator acceptance against the exact release candidate.

## Authority and document hierarchy

When sources disagree, use this order:

1. **Canonical Distordia strategy:** [Business Thesis and Strategy](../../projects/Distordia/Distordia_Labs_Business_Thesis_and_Strategy_v2.docx), [Customer Problem Atlas](../../projects/Distordia/Distordia_Customer_Problem_Atlas_v2.docx), [Staked Accountability Rails](../../projects/Distordia/staked-accountability-rails.md), and [Infrastructure Buildout](../../projects/Distordia/infrastructure-buildout.md).
2. **This vision:** the durable strategic role and direction of cross-chain settlement within Distordia.
3. **Repository architecture, security, evaluation, and development plans:** including [state machines](docs/STATE_MACHINES.md), [security boundaries](docs/SECURITY.md), and the [current evaluation](docs/EVALUATION.md). These define the intended and currently accepted engineering boundary, not portfolio strategy.
4. **Code, tests, and target-chain evidence:** code and tests establish implemented local behavior; target-chain evidence establishes external behavior. Neither silently changes the strategy or broadens a release claim.

Dated reviews are evidence snapshots. They do not override newer verified behavior, the current evaluation, this vision, or canonical strategy.

## Development grounding checklist

Before merging or describing a settlement change, answer each item explicitly:

- **Strategic fit:** Which scarce primitive—identity, verification, settlement, accountability, or risk—does it strengthen? Does it reduce or expand Distordia's custody or underwriting role?
- **Scope:** Which chains, token programs, exact asset identities, pair, direction, and lifecycle states are implemented? What remains unsupported?
- **Authorization** — Freeze the immutable source, destination, owner, amount, fee, terms version, and idempotency identity before the first side effect.
- **Evidence:** What authoritative evidence proves enumeration, success, finality, and exact intent match? Which provider inputs are trusted claims, and how are incompleteness and staleness represented?
- **Accounting:** Are principal, fees, rounding, reservations, caps, pending work, held work, and sibling source identities conserved in integer base units through every transition?
- **Uncertainty:** Do timeout, malformed data, missing lookup, conflict, and unknown submission fail closed without blind retry, duplicate settlement, inferred fee, or erased liability?
- **Recovery:** What happens after crash, restart, configuration drift, partial restore, DB/WAL loss, chain reorganization, and incomplete pagination? Can admission refuse safely before exposure?
- **Operations:** Are every hold and unknown outcome visible, quantified, actionable, and resolvable only through an evidence-bound audited protocol?
- **Migration:** Does the design preserve compatibility while moving toward open standards and non-custodial or conditional settlement? If custody remains, is its necessity and exit path documented?
- **Validation:** Do default-collected tests cover positive, negative, boundary, concurrency, and recovery cases? Has the exact candidate passed repository gates and authorized target-chain acceptance?
- **Claims:** Do documentation and public records distinguish implemented behavior, planned behavior, local verification, target-chain evidence, and release approval?

## Current boundary

The implemented service remains a custodial bridge for one operator-configured classic SPL token and one Nexus token per process. Its safety work is valuable only within the boundaries established by the current code and evidence. It must not be presented as a general cross-chain protocol, a solvency guarantee, or production-ready infrastructure until the open gates in the [current evaluation](docs/EVALUATION.md) are independently closed.
