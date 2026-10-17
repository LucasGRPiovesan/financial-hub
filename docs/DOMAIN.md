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
`draft → finalized → paid` · `draft | finalized → void` (only if no payment captured) · `finalized → uncollectible` (*Planned*). Nothing due at finalize: `finalized → paid` in the same transaction, with no payment intent. `paid` and `void` are final (see section 4).

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

## 4. Billing (as implemented, W5.3)

Modules `pricing` (catalog, rating) and `billing` (plans, subscriptions, invoices). Every amount is an integer of minor
units; quantities are `Decimal`; no `float` anywhere (`tests/unit/pricing/test_no_float.py`).

**Catalog.** A price has a code, a currency and append-only versions (`effective_from`, exclusive `effective_to`). A new
version must start in the future and ends the open one there; history is never rewritten (`price_versions_guard`, an
exclusion constraint against overlaps). Usage prices name the billable metric they rate; fixed prices name none. A plan
lists price codes of its currency (monthly interval only).

**Periods.** A subscription bills on an `anchor_day` (1-31) at UTC midnight. In short months the boundary clamps to the
last day (anchor 31 → Feb 28/29 → Mar 31), without drifting. A trial is `[start, trial_end)` and is never invoiced;
after it the anchor becomes the trial-end day.

**Rating and proration.** Each line is rated by `PricingService.rate` with the price version effective at the period
start; the line pins that version, its model, the metric and the rounding policy (`line:ROUND_HALF_EVEN`). A period
that starts mid-cycle prorates only the fixed recurring amounts (flat fee, base fee, minimum commitment) by
`active_days / period_days`, half-even on integers (4990 x 21/31 → 3380). Usage is never prorated, and neither are
the flat fees of graduated/volume tiers: those are charged by reaching the tier.

**Late usage.** `sum`/`count` metrics are aggregated cumulatively from the billing start minus what earlier invoices
already billed, so usage recorded after a period closed lands on the next invoice and a closed invoice is never
re-rated. `max`/`unique_count` aggregate over the period only (late usage for them is not billed).

**Period close.** The worker's `billing.close_due` schedule (every `FH_BILLING_CLOSE_INTERVAL_SECONDS`, batch
`FH_BILLING_CLOSE_BATCH_SIZE`) closes each ended period oldest first, one transaction per period, idempotent by
`(subscription, period_start)`, at most 24 periods per subscription per run. A closed period becomes a finalized
invoice at once (R1 + one payment intent, `due_date` = close + `days_until_due`).

**Invoice money.** `amount_due = total − credits_applied`; `credits_applied` is 0 until the wallet. Finalize books R1
(`invoice:{id}:finalized`, only if `total > 0`) and opens one payment intent for `amount_due`; nothing due → paid, no
intent. Capture of that intent marks the invoice paid (`payment.status_changed`, idempotent); payments books R2/R3,
never a second R1. Void (R11) reverses R1 and cancels the intent; it is refused once money was captured.

**Cancel.** `at_period_end` bills the current period, then the subscription is `canceled` with `ended_at` = period end.
An immediate cancel closes any ended period first, then bills `[current period start, now)` as a final invoice
(fixed fees prorated by day, usage to date), finalized at once; a trial ends with no invoice.

**Events.** `price.created`, `price.version_added` and `plan.created` are audited; `subscription.created`,
`subscription.canceled`, `invoice.created`, `invoice.finalized`, `invoice.paid` and `invoice.voided` are domain events.

**RBAC (separation of duties).** `billing:read`: every role. `billing:manage` is money movement: owner and finance only;
`admin` reads. The catalog, plans and invoice finalize/void are operator-only, so an API key gets `403` even with
`billing:write`. A key with `billing:write` may create and cancel subscriptions (SECURITY.md s3).
