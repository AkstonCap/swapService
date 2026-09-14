# swapService — Configurable Solana ↔ Nexus Token Bridge

A custodial, bidirectional bridge for **one operator-configured Solana SPL token and one Nexus token per deployment**. The tokens are not hardcoded to a particular ticker. `USDC` and `USDD` are the historical deployment and default display names; they also remain in some compatibility settings and stored fields.

The bridge uses a **1:1 whole-token backing/conversion model before fees and conservative decimal rounding**. It is not a market-price exchange, a multi-pair router, or a general cross-chain adapter. The current Solana transfer implementation uses the classic SPL Token program; configurable mint selection does not imply native-SOL or Token-2022 support.

> **Release safety:** local engineering checks do not establish production readiness. Target-chain, custody, migration and crash/recovery acceptance remain required before real funds are admitted. See the [current evaluation](docs/EVALUATION.md), [2026-09-08 development review](docs/DEVELOPMENT_REVIEW_2026-09-08.md), and [2026-09-07 safety repair evidence](docs/POST_CHANGE_REVIEW_2026-09-07.md).

## Documentation

| Audience | Document |
|---|---|
| Swap users | This guide and [user-flow state machines](docs/SWAP_INITIATOR_STATE_MACHINES.md) |
| Operators | [SETUP.md](SETUP.md), [CONFIG.md](CONFIG.md), [`.env.example`](.env.example) |
| Asset/client integrations | [ASSET_STANDARD.md](ASSET_STANDARD.md) |
| Developers | [runtime state machines](docs/STATE_MACHINES.md), [engineering guidance](.github/copilot-instructions.md) |
| Security and release decisions | [SECURITY.md](docs/SECURITY.md), [EVALUATION.md](docs/EVALUATION.md) |
| Current and previous verification | [2026-09-08 development review](docs/DEVELOPMENT_REVIEW_2026-09-08.md), [2026-09-07 repair report](docs/POST_CHANGE_REVIEW_2026-09-07.md), [baseline review](docs/DEVELOPMENT_REVIEW_2026-09-07.md) |

Dated review/audit reports retain their original snapshots, token examples and test counts. They are historical evidence, not a substitute for checking the current code and deployment.

## Which tokens does a deployment bridge?

The operator selects the pair through [`src/config.py`](src/config.py), which builds the immutable `SWAP_PAIR` configuration:

| Setting | Meaning |
|---|---|
| `SOLANA_TOKEN_MINT` | Actual Solana mint identity |
| `SOLANA_VAULT_ACCOUNT` | Service's token account for that mint, not a wallet owner address |
| `SOLANA_TOKEN_SYMBOL` | Display symbol; defaults to `USDC`, not an authorization check |
| `SOLANA_TOKEN_DECIMALS` | Solana-side precision; defaults to `6` and must match the selected mint |
| `NEXUS_TOKEN_REGISTER_ADDRESS` | Immutable Nexus token register identity used for account/transfer validation |
| `NEXUS_TOKEN_NAME` | Configured token name/display label; defaults to `USDD`, not a substitute for register identity |
| `NEXUS_TREASURY_ACCOUNT` | Service's Nexus treasury account for the selected token |
| `NEXUS_TOKEN_DECIMALS` | Nexus-side precision; defaults to `6` and must match the selected token |
| `DEPOSIT_MEMO_PREFIX` | Solana deposit memo prefix; defaults to `nexus:` |

Both precisions are configurable and need not match. Changing a display symbol alone does **not** select another asset. Identity, custody accounts, decimals and fees must all describe the same intended pair.

Supported legacy aliases, such as `USDC_MINT` and `NEXUS_USDD_TREASURY_ACCOUNT`, remain compatible; conflicting canonical/legacy identity, custody, decimal or fee settings are rejected rather than silently selecting a different pair. See [CONFIG.md](CONFIG.md) for the exact alias and precedence rules, including settings that retain legacy-only names.

Do not change a deployed pair over an existing custody database, heartbeat or unresolved liabilities as though it were a label change. Pair migration needs a separately verified custody and recovery plan. The provider-v2 schema in [ASSET_STANDARD.md](ASSET_STANDARD.md) is a **planned extension**, not current multi-pair support.

## Verify the deployment before sending

Never copy a vault, mint or treasury address from an unrelated deployment. Obtain the operator's actual registration and independently verify:

- Solana network, `solana_vault_mint` and `solana_vault_address`;
- Nexus network, `nexus_token_register_address` and `nexus_treasury_address`;
- `memo_prefix`, current minimums and fees, and the token precision on each side;
- the operator's identity/contact, record `status` and freshness of `last_poll_timestamp`.

An `online` status or recent heartbeat is a liveness signal, **not proof of solvency, completed recovery or release approval**. A displayed ticker alone does not establish token identity.

With the operator tooling configured, a published record can be inspected with:

```bash
python3 register_service.py --inspect <REGISTRATION_ASSET_NAME>
```

Operators can preview their own configured record without publishing it:

```bash
python3 register_service.py --show --json
```

These helpers require the relevant configuration. The public record's `min_to_nexus` is denominated in the **Solana input token**, while `min_to_solana` is denominated in the **Nexus input token**. Flat fees are named for the **output chain**. See [ASSET_STANDARD.md](ASSET_STANDARD.md) for actual field names and schema limits.

## Solana token → Nexus token

1. Verify the deployment and ensure your destination is a Nexus account for the **configured immutable token register**. A matching ticker is insufficient.
2. Use a Solana wallet/tool that can include a memo in the token-transfer transaction.
3. Send the configured SPL token to the verified service vault token account, with an amount meeting the deployment's effective minimum.
4. Include the configured memo prefix immediately followed by your Nexus destination account.

```text
Token:       <CONFIGURED_SOLANA_TOKEN_MINT>
Destination: <VERIFIED_SERVICE_VAULT_TOKEN_ACCOUNT>
Memo:        <DEPLOYMENT_MEMO_PREFIX><YOUR_NEXUS_TOKEN_ACCOUNT>
Amount:      <AMOUNT_IN_SOLANA_TOKEN_UNITS>
```

For a deployment using the default prefix, the memo is `nexus:<YOUR_NEXUS_TOKEN_ACCOUNT>`. The prefix is not universally fixed: use the operator's published value.

The service validates the deposit, finality, memo and Nexus account's token register, computes the net output with the configured fees/precision, and tracks the outbound Nexus transaction through confirmation. Submission alone is not settlement.

Solana-side refund and quarantine mechanisms exist for eligible failed deposits, but **a timeout or incomplete lookup does not guarantee an immediate refund**. Ambiguous outcomes can remain held for authoritative resolution. A refund, when made, returns to the original SPL source token account, not automatically to a wallet-owner address. Do not repeat a transfer merely because processing is delayed.

## Nexus token → Solana token

> **Payout-budget control:** all automated Solana token sends—including Nexus→Solana
> payouts, Solana refunds and quarantine sends—reserve the configured rolling cap in
> SQLite before RPC. Pending, submitted and unknown outcomes continue to consume
> capacity. A timeout or missing returned signature becomes an operator hold, not a
> retry or cap release. Target-chain crash/finality acceptance is still required.

1. Use an **existing token-account address for the configured Solana mint**. The payout path does not resolve a wallet-owner address to its ATA and does not create missing token accounts.
2. Send the configured Nexus token from your token account to the verified service treasury, respecting the deployment's effective minimum. Normal holders use the account-debit flow, not a token-creator supply-debit example.
3. Capture the complete transfer/debit transaction ID and publish or update a Nexus asset owned by the same signature chain that sent the funds.
4. The mapping asset must contain these exact fields:

```json
{
  "txid_toService": "<COMPLETE_DEBIT_TRANSACTION_ID>",
  "receival_account": "<EXISTING_SOLANA_TOKEN_ACCOUNT>"
}
```

See [ASSET_STANDARD.md](ASSET_STANDARD.md) for supported asset creation/update commands. The service matches `txid_toService` **and asset owner**. Mapping is transaction-level; internal Nexus source accounting separately preserves each exact `(txid, contract_id)` credit. Do not invent a new mapping field to replace that existing protocol.

Once the mapping and destination are valid, the service freezes the payout terms and submits the net Solana transfer. It finalizes only after successful finalized transfer evidence matches the source identity, configured vault/mint, recipient and frozen output. A memo or finalized signature alone is insufficient.

- Missing/invalid mapping, unresolved outcomes or missing destination accounts can require an operator hold.
- `REFUND_TIMEOUT_SEC` is an observation/hold threshold, **not a promise of an automatic Nexus refund**.
- Automatic Nexus refunds and treasury-to-quarantine transfers remain disabled. A separate, exact-source, authorized operator workflow exists; it is not an automatic retry path.
- Do not overwrite a reusable mapping with the next transaction ID while a previous swap still depends on it. Keep the relevant mapping discoverable until that swap is resolved, or use separate assets.

## Fees, minimums and dust

There is no universal minimum expressed in a particular ticker. Check the selected deployment's effective terms before every transfer.

- `FEE_FLAT_TO_NEXUS` governs Nexus output; `FEE_FLAT_TO_SOLANA` governs Solana output.
- `FEE_BPS` applies a percentage fee to the input of each direction.
- `FEE_REFUND_SOLANA` is the separate Solana-side refund fee. It is not automatically a Nexus-output fee on a different unit scale.
- `MIN_DEPOSIT_SOLANA_TOKEN` and `MIN_CREDIT_NEXUS_TOKEN` are input-side minimum settings. Effective minimums have a fee-derived floor; a lower configured value does not necessarily lower the enforced minimum.
- Accepted sub-minimum amounts are retained as fees with **no output**. The legacy micro-fee percentage settings are parsed but do not implement a partial-return policy. Nexus credits below `DUST_CREDIT_NEXUS_TOKEN` are ignored rather than recorded as swaps.
- Amounts are accounted in integer base units; unequal precisions can leave a conservative rounding remainder.

[CONFIG.md](CONFIG.md) documents defaults, fallback rules and units. The operator's `.env` may override them; sample fees for the historical token pair are not quotes for every configurable token.

## Processing, recovery and monitoring

Processing time depends on chain finality, polling, asset discovery, RPC availability and hold conditions. There is no guaranteed number of blocks or automatic timeout refund.

The current service requires valid custody checkpoints and affirmative complete startup recovery before exposure-producing loop work. Missing/zero waterlines, incompatible heartbeat data, incomplete scans and recovery errors refuse startup. Creating a heartbeat asset does not by itself establish a safe bootstrap checkpoint. Never set waterlines to the current time to bypass recovery.

Mutable multi-page Nexus offset scans cannot authorize checkpoint advancement. Previously discovered positive credits can be retained while coverage remains incomplete. Refer to [STATE_MACHINES.md](docs/STATE_MACHINES.md) for live processing and recovery invariants.

### Read-only operator dashboard

```bash
python3 dashboard.py
# Default: http://127.0.0.1:8787
```

The dashboard is separate from the service and exposes no retry/refund controls. It shows selected-token labels, backing/liabilities, pending and held work, fee accounting, payout-cap use and heartbeat age. Historic column names are compatibility fields, not fixed token selection.

Keep it local or follow the authentication/TLS requirements in [SETUP.md](SETUP.md). The dashboard's read-only design is not an instruction to expose custody credentials or the service database publicly.

## Operator and developer entry points

- [Installation, custody setup, registration and startup](SETUP.md)
- [Configuration and legacy aliases](CONFIG.md)
- [Security controls and deployment restrictions](docs/SECURITY.md)
- [Current engineering evaluation and remaining release gates](docs/EVALUATION.md)
- [Nexus API reference material](Nexus%20API%20docs/) — check it against the intended node version; vendored documentation is not proof of live API semantics.

Run local automated verification in an environment with the declared dependencies installed:

```bash
python -m pytest -q
python -m compileall -q src tests
python -m pip check
python scripts/check_markdown_links.py
python scripts/check_token_pair_inventory.py
```

The literal-inventory check reads the **Git index**. An unchanged-index pass does not verify unstaged documentation; stage an intended candidate or use a disposable index without altering other work. Real-SDK regression coverage requires the installed Solana dependencies. Local tests do not replace isolated target-chain acceptance.

## License

This project is provided as-is. Use at your own risk.
