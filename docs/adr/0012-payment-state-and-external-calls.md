# ADR-0012: Payment state model and money-movement protocol

- Status: Accepted · Date: 2026-10-10 (review findings F-03, F-04, F-09, F-24, F-25)

## Context
Provider events arrive at-least-once, out of order and sometimes contradictory (PIX `RECEIVED` before `CONFIRMED`, refunds before settlement, chargebacks after payout). A single status enum cannot represent "captured, settled, partially refunded, under dispute" at once. Separately, calls to Asaas that create money movement (`POST /payments`, `POST /payments/{id}/refund`, `POST /transfers`) have no idempotency header, and transfers have no `externalReference`.

## Decision

### 1. Orthogonal state on `ProviderPayment`
| Field | Values | Moves only forward |
|---|---|---|
| `capture_status` | `pending → captured`, or `pending → failed|canceled` | yes |
| `settlement_status` | `none → settled` | yes |
| `refund_status` | derived: `none | partial | full` from `Refund` rows | — |
| `dispute_status` | `none → open → won|lost` | yes (`open` may reopen via `AWAITING_CHARGEBACK_REVERSAL`) |
| `captured_amount`, `net_amount`, `fee_estimated` | integers | updated by R2a only |

`PaymentIntent.status` (shown to products) is **derived** from these fields with a written precedence table (dispute > refund > settlement > capture). Provider events are mapped to *field transitions*, not to a target status; a transition to an earlier value is ignored and recorded as `stale` on the receipt. Unknown or contradictory combinations trigger a refetch.

### 2. Trusting provider data
A webhook is a notification, not an instruction. Before any posting the handler:
1. verifies the receiving provider account belongs to the same project as the intent (`intent.project_id == provider_account.project_id`, else park + security alert);
2. **refetches** the payment (`GET /payments/{id}`) and uses the fetched `value`, `netValue`, `status` and `refunds[]` — never the webhook body — as posting amounts;
3. applies the recipe under the aggregate lock (ADR-0003 §5) with a key derived from the fact. `CONFIRMED` and a synthetic capture from `RECEIVED` both use `payment:{pp}:captured`, so either order yields exactly one R2.

A capture on an intent that is already captured, canceled or failed runs R2b (unapplied cash), never a second R2.

### 3. External money-movement protocol (charges, refunds, transfers)
1. **Commit intent state first** (`create_requested`, with an `attempt_id` fencing token) in its own transaction.
2. Outside any DB transaction, call the provider. Charges and refunds carry `externalReference = intent/refund id`.
3. Record the result with a CAS on `(id, attempt_id, status = '*_requested')`. A worker that lost its lease fails the CAS and its result is reconciled, not applied twice.
4. **Unknown outcome** (timeout, 5xx, crash): status `unknown`. A recovery sweeper resolves it by lookup — charges by `externalReference`, refunds by reading `refunds[]` of the payment, transfers by listing transfers and matching value/destination/time window. Only a confirmed *absence* allows a retry.
5. **Transfers are never auto-retried** after an unknown outcome; they are parked for an operator.
6. **Withdrawal validation** (Asaas webhook for transfer authorization) is the final gate: the hub approves a transfer only if it matches a payout the hub has already committed as `submitted` with the same amount and destination, and rejects everything else (including transfers not created by the hub).

## Consequences
+ Out-of-order and duplicate events are safe by construction; no double charges, refunds or transfers from retries or lease expiry; forged webhook amounts cannot reach the ledger. − One extra provider read per money-affecting event (acceptable at sandbox rate limits; cached per receipt); a parked-items operator queue is required (Ops console, M9).

## Alternatives
Single status enum with transition table (cannot represent concurrent dimensions); trusting webhook bodies (forgeable if the token leaks; amounts may be stale); idempotent retries without lookup (unsafe without provider idempotency keys).
