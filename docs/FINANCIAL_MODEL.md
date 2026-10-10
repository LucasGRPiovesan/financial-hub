# Financial Model

> **Normative, v2** (2026-10-10, revised after independent review `docs/reviews/0.1-architecture-review.md`).
> Code in `modules/ledger` (posting rules), `modules/payments`, `modules/billing` MUST implement exactly these recipes.
> Changing a recipe requires an ADR. Related: [ADR-0002 Money](adr/0002-money-representation.md),
> [ADR-0003 Ledger](adr/0003-double-entry-ledger.md), [ADR-0012 Payment state & money-movement protocol](adr/0012-payment-state-and-external-calls.md),
> [ADR-0013 Reconciliation](adr/0013-reconciliation.md).

## 1. Money

- An amount is `(minor_units: int, currency: ISO-4217)`. Stored as `BIGINT` + `CHAR(3)`. Never float.
- Currency exponent from an ISO 4217 table. Multi-currency aware, **no FX** (*Planned*): every entry balances per currency; cross-currency operations are rejected.
- `Decimal` only for **rates and unit prices**. A rated amount is quantized **once per charge line** with `ROUND_HALF_EVEN`; sums are integer sums.
- **Allocation** (`allocate(amount, weights)`) uses the largest remainder method. Participants are always the full list
  **`[payee_1 … payee_n, platform]`** with the platform **last** in tie-break order, so rounding pennies go to the largest
  fractional part and never vanish. The sum of parts always equals the amount.
- Zero-amount postings are never written (legs with 0 are dropped; an entry with no legs is not created).

## 2. Ledger primitives

| Concept | Meaning |
|---|---|
| **Account** | `type` (asset, liability, income, expense, equity) → `normal_balance` (debit for asset/expense, credit otherwise); currency; owner (project + optional customer/payee/provider account); flags. |
| **Journal entry** | Atomic set of postings. `status`: `pending → posted | archived(voided)`. `idempotency_key` unique per project, `reverses_entry_id`, `correlation_id`, `causation_id`, `effective_at`, `recipe` (e.g. `R2`). |
| **Posting** | `(account, direction, amount > 0)`. Immutable. ≥ 2 postings per entry. Account currency = posting currency; account project = entry project. |
| **Invariant** | Per entry and currency: Σ debits = Σ credits (domain + DB constraint trigger, see ADR-0003). |
| **Immutability** | Postings and posted entries never change. Corrections are **reversal entries**. |
| **Balances** | Per account: `posted_dr`, `posted_cr`, `pending_dr`, `pending_cr`. For a credit-normal account `posted = posted_cr − posted_dr` and `available = posted − pending_dr`; for a debit-normal account `posted = posted_dr − posted_cr` and `available = posted − pending_cr`. Pending inflows never increase `available`. |
| **Flags** | `no_overdraft`: posting/pending creation fails if `available` would go below zero (checked under the account row lock). |

## 3. Chart of accounts (per project, per currency)

`{c}` customer, `{p}` payee, `{pa}` provider account.

| Code | Type | Purpose | Flags |
|---|---|---|---|
| `customer_receivable:{c}` | asset | Billed and not yet paid | |
| `customer_credits:{c}` | liability | Prepaid wallet | no_overdraft |
| `customer_unapplied_cash:{c}` | liability | Money received without an open obligation (duplicate/late payment, overpayment) | |
| `sales_clearing` | liability | Billed/consumed value awaiting allocation; must return to zero per settled obligation | |
| `provider_receivable:{pa}` | asset | Collected by the provider, not yet available | |
| `provider_balance:{pa}` | asset | Available balance at the provider | |
| `provider_balance_held:{pa}` | asset | Blocked by the provider (disputes, split divergence) | |
| `payee_payable_pending:{p}` | liability | Owed to a payee, not yet withdrawable | |
| `payee_payable_available:{p}` | liability | Owed to a payee, withdrawable | no_overdraft |
| `payee_payable_held:{p}` | liability | Payee share frozen by a dispute | |
| `payee_receivable:{p}` | asset | Amount the payee owes back (refund/chargeback after payout) | |
| `platform_revenue` | income | The project's earned share | |
| `other_income` | income | Interest/fines collected above the billed amount | |
| `discounts_given` | expense | Discounts granted at payment time (paid below billed amount) | |
| `provider_fees` | expense | PSP fees (payment, transfer, chargeback) | |
| `chargeback_losses` | expense | Lost disputes / unrecoverable amounts written off | |
| `opening_balance` | equity | Opening balances and reconciliation adjustments approved by an operator | |

Accounts are provisioned lazily from templates (upsert-then-lock, ADR-0003). The project is the **platform**; payees are its sellers/professionals.

## 4. Posting recipes

Notation `Dr account amount / Cr account amount`. `B` amount billed on the charge (invoice amount due after wallet), `G` gross
collected (`value` received), `F` provider fee, `N = G − F` (`netValue`), `u` wallet credits applied.
Every recipe runs **inside the state transition** of its aggregate (payment, refund, payout, invoice) in one transaction
under that aggregate's row lock (ADR-0012), so recipes for one payment can never interleave or run out of order.

### R1 — Invoice finalized
Sales lines (`B_sales`) and credit-purchase lines (`A`) are booked differently:
```
Dr customer_receivable:{c}  B_sales − u          Cr sales_clearing  B_sales − u
```
Credit-purchase lines: **no entry at finalization** (see R6). If amount due = 0 no PaymentIntent is created.

### R2 — Payment captured (`payment.confirmed`, first capture of the intent only)
```
Dr provider_receivable:{pa}  G
Cr customer_receivable:{c}   B
Cr other_income              G − B     (if G > B: interest/fine)
Dr discounts_given           B − G     (if G < B: discount)
Dr provider_fees             F
Cr provider_receivable:{pa}  F
```
`F = value − netValue` as reported at capture (estimated); adjusted by R2a. A second capture for an intent that is already
captured (or a late capture on a canceled/failed intent) does **not** run R2: it runs **R2b**.

**R2a — Fee adjustment.** When `netValue` changes later (at `RECEIVED` or from the statement): post the delta
`Dr provider_fees / Cr provider_receivable` (or the reverse). Idempotency key `payment:{pp}:fee:{netValue}`.

**R2b — Unapplied cash.** `Dr provider_receivable G / Cr customer_unapplied_cash:{c} G` (+ fee legs as in R2).
An operator (four-eyes) either refunds it (R8 variant debiting `customer_unapplied_cash`) or applies it to an open invoice.

### R3 — Split allocation (same transaction as R2; sales lines only, never credit-purchase lines)
Basis by project policy `fee_bearer`: `proportional` → `X = N` (mirrors Asaas split over netValue); `platform` → `X = B`.
`[s_1 … s_n, s_platform] = allocate(X, weights)`.
```
Dr sales_clearing                B
Cr payee_payable_pending:{p_i}   s_i
Cr platform_revenue              B − Σ s_i
```
For **provider split** (Asaas `walletId`), the hub computes `s_i` and sends them as **`fixedValue`** on the charge, so Asaas
and the ledger use identical numbers (no `percentualValue` rounding mismatch). Configurations with `Σ s_i > N` are rejected.

### R4 — Settlement (`payment.settled`, Asaas `RECEIVED`)
```
Dr provider_balance:{pa}           N
Cr provider_receivable:{pa}        N
Dr payee_payable_pending:{p_i}     s_i      (internal split only)
Cr payee_payable_available:{p_i}   s_i
```
For PIX, `CONFIRMED` and `RECEIVED` may arrive in any order or together. A `RECEIVED` seen first applies R2+R3 then R4 in one
transaction; a later `CONFIRMED` hits the same idempotency key `payment:{pp}:captured` and is a no-op.

### R5 — Provider-executed split (`split.done`)
Requires R4 applied for the payment (if not, the handler refetches the payment and applies R4 first).
```
Dr payee_payable_pending/available:{p_i}  s_i   (whichever holds it; pending→available is skipped for provider split)
Cr provider_balance:{pa}                  s_i
```
`split.blocked` (divergence): `Dr provider_balance_held / Cr provider_balance` for the blocked amount; reversed when unblocked.

### R6 — Wallet top-up (credit purchase `A`)
Credits become spendable only when paid. On capture of a top-up intent (instead of R2/R3):
```
Dr provider_receivable:{pa}  G              Cr customer_credits:{c}  A
Dr provider_fees             F              Cr provider_receivable:{pa}  F
(+ other_income / discounts_given for G ≠ A as in R2)
```
Settlement follows R4 without payee legs. The platform bears top-up fees.

### R7 — Wallet consumption (wallet-first)
At invoice finalization, before external payment (same transaction as R1):
```
Dr customer_credits:{c}           u      (no_overdraft)
Cr sales_clearing                 u
Dr sales_clearing                 u
Cr payee_payable_available:{p_i}  s_i    (funds already settled → directly available)
Cr platform_revenue               u − Σ s_i
```
Basis `X = u`, `F = 0`.

### R8 — Refund (two-phase; amount `R` of payment `pp`)
Refunds are our `Refund` entity (`rf_` id = idempotency key). Provider-initiated refunds are imported by computing the delta
between Σ `DONE` refunds at the provider and what is already booked.
1. **Request / `REFUND_IN_PROGRESS`** → *pending* entry (reduces available balances, so refund money cannot be paid out).
2. **`DONE`** → post. **`DENIED`/`CANCELLED`** → void.

Shares: allocate the **cumulative** refunded amount over the original R3 parts and post the difference from what was
already refunded (no drift across partial refunds; cumulative refunds capped at `G`). Source of funds: `provider_balance`
if settled, else `provider_receivable`. Fee is non-refundable by default (`refund_fee_policy`).

*Internal split / no split:*
```
Dr platform_revenue                 r_platform
Dr payee_payable_*:{p_i}            r_i        (available, then pending, then held)
Dr payee_receivable:{p_i}           shortfall  (payee already paid out)
Cr provider_balance|provider_receivable:{pa}   R
```
*Provider split:* Asaas reverts `refundedSplits` from the subaccounts. Only the platform part leaves our account:
```
Dr platform_revenue                       R − Σ refundedSplits(done=true).value
Cr provider_balance:{pa}                  R − Σ refundedSplits(done=true).value
```
`refundedSplits` with `done=false` (Asaas debited our account instead): `Dr payee_receivable:{p_i} / Cr provider_balance`.

*Top-up refund:* `Dr customer_credits:{c}` (shortfall → `Dr customer_receivable:{c}`) `/ Cr provider_balance`.
*Unapplied cash refund:* `Dr customer_unapplied_cash:{c} / Cr provider_balance`.

### R9 — Dispute (chargeback; amount `D` = `chargeback.value`)
- **Opened:** `Dr provider_balance_held D / Cr provider_balance D`; freeze payee shares
  `Dr payee_payable_available|pending:{p_i} d_i / Cr payee_payable_held:{p_i} d_i` (`d_i` = allocate(D) over original parts).
- **Won:** reverse both legs.
- **Lost:** `Dr platform_revenue d_platform`, `Dr payee_payable_held:{p_i} d_i` `/ Cr provider_balance_held D`; chargeback fee
  `Dr provider_fees / Cr provider_balance`. Amounts not recoverable from payees → `payee_receivable`, written off to
  `chargeback_losses` only by an approved write-off (R13).

### R10 — Payout to payee (`P`, two-phase)
1. Request (four-eyes approved) → pending `Dr payee_payable_available:{p} P / Cr provider_balance:{pa} P` (no_overdraft).
2. `payout.paid` → post; transfer fee `Dr provider_fees / Cr provider_balance`.
3. `payout.failed` / `cancelled` / rejected by our withdrawal validation → void.
4. Returned after paid → reversal entry.

Transfers are **never auto-retried** when the outcome is unknown; they are parked for an operator (ADR-0012). The Asaas
withdrawal-validation webhook is the final idempotency gate: we approve only a transfer matching a pending payout.

### R11 — Invoice void (`finalized → void`, only if nothing captured)
Reversal entries of R1, and of R7 (credits restored, payee/platform shares reversed). Asaas `PAYMENT_DELETED` on an open invoice → intent canceled; invoice stays `finalized` (re-collect) unless an operator voids it.

### R12 — Reconciliation adjustment
Approved resolution of a break (ADR-0013): e.g. fee difference → R2a; unexplained provider balance difference →
`Dr|Cr provider_balance / Cr|Dr opening_balance` with a mandatory reason. Opening balances use `opening_balance`.

### R13 — Write-off
`Dr chargeback_losses / Cr payee_receivable:{p}` (or `customer_receivable`), four-eyes approved.

### Not supported
`RECEIVED_IN_CASH` (payment marked as received outside the provider): rejected — the event is parked with an alert; no recipe moves money that never reached the provider.

## 5. Worked example (HealthCheck consultation, 75/25 split, internal split)

Consultation R$ 200.00 by PIX; Asaas fee R$ 1.99 (illustrative); `fee_bearer = proportional`; weights doctor 75, platform 25.

| Step | Entries (BRL cents) |
|---|---|
| R1 | Dr customer_receivable 20000 / Cr sales_clearing 20000 |
| R2 | Dr provider_receivable 20000 / Cr customer_receivable 20000; Dr provider_fees 199 / Cr provider_receivable 199 → N = 19801 |
| R3 | allocate(19801, [75, 25]) = exact [14850.75, 4950.25] → floors [14850, 4950], 1 cent to the largest fraction → **[14851, 4950]**. Dr sales_clearing 20000 / Cr payee_payable_pending:doctor 14851 / Cr platform_revenue 5149 |
| R4 | Dr provider_balance 19801 / Cr provider_receivable 19801; Dr payee_payable_pending 14851 / Cr payee_payable_available 14851 |

Platform net = 5149 − 199 = 4950 = its allocated part. `sales_clearing`, `customer_receivable`, `provider_receivable` all return to 0.
Trial balance is zero after every step.

## 6. Intentionally simplified

Revenue is recognized at allocation (operational subledger, not GAAP/IFRS). No FX, tax lines, deferred revenue schedules,
recovery workflows for `payee_receivable` beyond write-off (*Planned*).
