# ADR-0013: Reconciliation

- Status: Accepted · Date: 2026-10-10 (review finding F-28)

## Context
The ledger records what the hub believes happened; the provider holds the money. They diverge through missed webhooks, fee changes, manual actions in the provider panel and bugs. A financial system without systematic reconciliation cannot claim to produce financial truth.

## Decision
1. **Three runs**, scheduled by the ADR-0005 scheduler and on demand:
   - **Internal**: balances recomputed from postings = materialized balances; clearing accounts (`sales_clearing`, `customer_receivable` of fully paid invoices) return to zero; trial balance = 0.
   - **Transactions**: provider payments, refunds and transfers in a window (`GET /payments`, `/financialTransactions` statement) matched 1:1 against `ProviderPayment`/`Refund`/`Payout` by provider id, then by `externalReference`.
   - **Balance**: provider `GET /finance/balance` vs. `provider_balance:{pa}` (+ held) at a cut-off time.
2. **Break taxonomy** (`reconciliation_breaks.kind`): `missing_internal`, `missing_external`, `amount_mismatch`, `fee_mismatch`, `status_mismatch`, `balance_drift`, `ledger_drift`, `clearing_not_zero`. Each break stores both sides, the run id, and a state `open → investigating → resolved|accepted`.
3. **Auto-resolution only for non-money-creating cases**: missing webhook → replay the canonical event from a refetch (normal recipes, idempotent keys); fee mismatch → R2a. Everything else requires an operator.
4. **Adjustments** are journal entries with recipe R12 (FINANCIAL_MODEL §4), a mandatory reason, the break id in metadata and four-eyes approval. Opening balances for a provider account use the `opening_balance` equity account.
5. Runs are idempotent by `(provider_account, window)`; results feed metrics (`breaks_open`, `ledger_drift_minor_units`) and the Ops console.

## Consequences
+ Every divergence becomes visible, typed and auditable; the demo can show a seeded break and its resolution. − Statement endpoints and their pagination are sandbox-dependent (UNVERIFIED items in `docs/research/asaas-sandbox.md`, confirmed in task 4.2).

## Alternatives
Ad-hoc balance comparison only (hides transaction-level causes); auto-adjusting all differences (would let the system invent money).
