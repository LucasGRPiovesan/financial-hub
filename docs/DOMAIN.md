# Domain

## 1. Glossary

| Term | Definition |
|---|---|
| **Project** | A tenant: one integrated product (HealthCheck, MyAIHub). Owns API keys, provider accounts, manifests, accounts. Acts as the *platform* in splits. |
| **Operator** | Human user of the Operator Console. Has a role per project. |
| **Business event** | A fact reported by a product (`consultation.completed`, `tokens.consumed`). Input only; carries the product's own id. |
| **Usage event** | A metered quantity derived from a business event or posted directly. Idempotent on `(project, transaction_id)`. |
| **Billable metric** | Defines how usage is aggregated in a period (`sum`, `count`, `max`, `unique_count`) and filtered. |
| **Price / Price version** | A pricing model with parameters, effective from a date. Versions are immutable once used. |
| **Rating** | Pure function: (price version, quantity/amount) → charge lines with breakdown. |
| **Charge** | A priced obligation line (one-off, usage, recurring, credit purchase). |
| **Invoice** | Groups charges for a customer and period. `draft → finalized → paid | void | uncollectible`. Finalized invoices are immutable; corrections via credit notes. |
| **FinancialCustomer** | The universal customer (one legal person across projects). |
| **CustomerIdentity** | `(project, external_ref)` → FinancialCustomer. A product only ever sees its own identities. |
| **Wallet** | The `customer_credits:{c}` account of a customer in a project. Segregated per project. |
| **Payee** | Recipient of split shares (e.g. a doctor). Optionally linked to a provider subaccount (Asaas `walletId`). |
| **PaymentIntent** | Our intention to collect an amount for an invoice. Provider-agnostic. |
| **ProviderPayment** | One attempt at a provider (Asaas payment id). An intent may have several (e.g. method change). |
| **ProviderEvent** | A raw inbound provider webhook, stored verbatim then normalized into a canonical event. |
| **Settlement** | Funds becoming available at the provider (`payment.settled`). |
| **Payout** | Transfer of a payee's available balance out of the provider account. |
| **Reconciliation break** | A discrepancy between our ledger and the provider statement. |
| **Integration Manifest** | Versioned, approved configuration describing how a product's business events map to metering, pricing, splits and webhooks. |
| **Event (event log)** | Envelope `{id, type, version, project_id, occurred_at, correlation_id, causation_id, subject, data}` persisted in the business transaction. |

## 2. State machines

### PaymentIntent / ProviderPayment (ADR-0012)
A `ProviderPayment` carries **orthogonal** state fields instead of one enum:
```
capture_status     pending ──▶ captured            pending ──▶ failed | canceled
settlement_status  none ──▶ settled                (requires captured)
refund_status      none | partial | full           (derived from Refund rows in state done)
dispute_status     none ──▶ open ──▶ won | lost
```
`PaymentIntent.status` shown to products is derived, by precedence:
`dispute(open→disputed, lost→charged_back)` > `refund(full→refunded, partial→partially_refunded)` >
`settled` > `captured→succeeded` > `overdue` (pending past due date) > `failed|canceled` > `processing` > `requires_payment`.
Fields only move forward; a stale provider event is ignored and recorded on its receipt; contradictions trigger a refetch;
unknown statuses park the event. A capture on an intent that is already captured, canceled or failed is unapplied cash (R2b).
Creation of the provider payment is tracked separately on the intent (`creation_status`, ADR-0012 §3): while it is `create_requested` or `unknown` the derived status is `processing`, and a rejected creation is `failed`. A late capture (`failed|canceled → captured`) is accepted and reported to the ledger as late.

### Refund
`requested (pending entry) → submitted → done (post)` · `submitted → denied | cancelled (void)` · `submitted → unknown → (lookup) done | denied`.
Provider-initiated refunds enter directly as `done` (imported from `refunds[]`).

### Invoice
`draft → finalized → paid` · `finalized → void` (only if no payment captured) · `finalized → uncollectible` (*Planned*).

### Outbound webhook delivery
`pending → delivering → succeeded` · `delivering → retry_scheduled → delivering …` · after max attempts `→ dead` (DLQ) · `dead → pending` (replay).
Endpoint: `enabled ⇄ disabled` (auto-disable after sustained failures; manual re-enable).

### Manifest version
`draft → validated → approved → active → superseded` · `validated/approved → rejected` · rollback = activate a previous `superseded` version (creates an audit record and a new activation event, never mutates history).

### Payout
`requested (four-eyes approved; pending ledger entry) → submitted → paid (post)` · `submitted → failed | cancelled | blocked (void)` · `submitted → unknown` (parked for an operator, never auto-retried) · `paid → returned (reversal)`. Withdrawal validation approves only transfers matching a `submitted` payout (ADR-0012).

## 3. Invariants (tested)

1. Every journal entry balances per currency; trial balance per project per currency is zero.
2. Postings and posted entries are immutable.
3. `no_overdraft` accounts never have negative available balance, under concurrency.
4. One usage event per `(project, transaction_id)`; one provider event per `(provider, provider_event_id)`; one journal entry per `(project, idempotency_key)`.
5. A finalized invoice total equals the sum of its lines and the amount posted to `customer_receivable` + wallet consumption (credit-purchase lines are booked only on payment, R6).
9. `sales_clearing` returns to zero for every settled obligation; `customer_receivable` returns to zero for every paid invoice.
10. At most one capture recipe (R2/R6) per PaymentIntent; any further capture is unapplied cash.
11. Ledger postings are derived only from refetched provider data, never from webhook bodies.
6. Split allocations sum exactly to their basis.
7. No query can return data from another project (isolation suite).
8. The AI/MCP layer has no path to create ledger entries, payments or payouts.
