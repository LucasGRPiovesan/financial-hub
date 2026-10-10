# Financial Patterns Benchmark

Purpose: inform (not dictate) the Financial Hub architecture (Python/FastAPI, PostgreSQL, modular monolith, multi-tenant). Principle: "Products produce business facts; the Hub produces financial truth."

Method and caveats: written from the authors' knowledge of the public documentation of each system; URLs point to official docs or engineering blogs. No live re-fetch was done in this pass. Any claim that could not be pinned to a specific documented statement is marked **UNVERIFIED**. Exact field names, retry schedules and limits change between versions; re-check before copying.

---

## 1. Double-entry ledgers

### 1.1 Modern Treasury Ledgers
Sources: https://docs.moderntreasury.com/ledgers/docs/overview ; https://docs.moderntreasury.com/ledgers/docs/ledger-account-balances ; https://docs.moderntreasury.com/ledgers/docs/ledger-transactions ; https://www.moderntreasury.com/journal (engineering/product posts on ledgering)

- Objects: Ledger (a container) -> Ledger Accounts -> Ledger Transactions -> Ledger Entries. A transaction contains >= 2 entries; sum(debits) == sum(credits) per currency, enforced on write.
- Each ledger account declares a `normal_balance` (debit or credit). Balance = (entries in the normal direction) - (entries in the opposite direction).
- Transaction status: `pending` -> `posted` or `archived`. Pending = intent/hold (funds reserved); posted = final; archived = pending that was discarded. Posted transactions are immutable; corrections are new (reversing) transactions.
- Three balances per account: `pending_balance` (all pending + posted effects), `posted_balance` (posted only), `available_balance` (posted minus pending debits that would reduce spendable funds; formula depends on normal balance). Each also exposes debits/credits sums and amount.
- Balances can be queried "as of" an effective date (`effective_at`), and ledger transactions carry both `effective_at` (business date) and created time (system date) - i.e. bitemporal-ish.
- Concurrency: ledger accounts carry a `lock_version`; a ledger transaction can include a `ledger_account_lock` / account version precondition so a write fails if the account changed (optimistic locking). Also supports balance constraints on entries (e.g. `available_balance` lower bound) to prevent overdrafts. Exact request field names: UNVERIFIED.
- Idempotency: `Idempotency-Key` header on mutating requests; also `external_id` uniqueness on transactions.
- Ledger Account Categories group accounts hierarchically (rollups, e.g. "all customer wallets") and expose aggregate balances. Ledger Account Settlements move a pending/ posted amount between accounts as a unit (payout-style).
- Multi-currency: each account has a single currency + `currency_exponent`; a transaction's entries must balance per currency; cross-currency movement needs explicit FX accounts (UNVERIFIED that no implicit FX exists).
- Balance computation: server-side computed and exposed by API; internally uses materialized running balances (exact implementation UNVERIFIED).

### 1.2 Formance Ledger
Sources: https://docs.formance.com/modules/ledger/introduction ; https://docs.formance.com/modules/numscript/introduction ; https://www.formance.com/blog (engineering posts)

- Model: accounts are free-form addresses (e.g. `users:123:main`), created implicitly; there is no account "type" table. A transaction is a list of **postings** `{source, destination, asset, amount}`; asset strings like `USD/2` carry the exponent.
- `@world` is a special unbounded source account (may go negative) representing the outside of the system; money enters/exits the ledger from/to `@world`. All other accounts are by default non-negative unless overdraft is explicit in the script (`allowing unbounded overdraft`).
- Numscript: DSL to declare transactions (`send [USD/2 100] ( source = @a destination = @b )`, with allotment splits `{ 90% to @x, remaining to @y }`, source fallbacks, max/ceiling caps). Remainder allocation in splits is handled by the language (`remaining`).
- Immutability: the ledger is an append-only log; transactions are never edited; `revert` creates a new transaction with inverted postings. Metadata on transactions/accounts is mutable and itself versioned in the log (v2: UNVERIFIED detail).
- Idempotency: `Idempotency-Key` header (v2) deduplicates API calls; `reference` field can enforce uniqueness per ledger.
- Balances: computed from the log with a materialized volumes table (input/output volumes per account+asset); balance = inputs - outputs. Supports "balance at time" queries (point-in-time volumes). Mechanism detail: UNVERIFIED.
- Multi-ledger / multi-tenant: a "ledger" is a logical bucket within a deployment (separate sequences), useful for tenant isolation.
- Concurrency: v2 serializes writes per ledger with a log-hash chain (each log entry hashes the previous) - tamper evidence. Locking granularity details UNVERIFIED.

### 1.3 TigerBeetle
Sources: https://docs.tigerbeetle.com/ ; https://docs.tigerbeetle.com/coding/data-modeling/ ; https://docs.tigerbeetle.com/coding/two-phase-transfers/ ; https://docs.tigerbeetle.com/reference/account/ ; https://docs.tigerbeetle.com/reference/transfer/

- Only two entity kinds: **Accounts** and **Transfers**. Transfer = single debit account -> single credit account, one amount (u128), one ledger (integer identifying currency/asset class), a `code` (reason), and `user_data_*` fields for correlation to the OLTP DB.
- Amounts are unsigned integers of the smallest unit; asset scale is the application's responsibility (ledger id + convention).
- Account flags: `debits_must_not_exceed_credits`, `credits_must_not_exceed_debits`, `linked` (chain events atomically), `history`, `imported`, `closed`. Balance invariants are checked atomically in the database.
- Two-phase transfers: `pending` transfer reserves amount (increments `debits_pending`/`credits_pending`), later `post_pending_transfer` (full or partial) or `void_pending_transfer`; optional `timeout` auto-voids. This is the canonical hold/capture/release model.
- Immutability: transfers can never be modified or deleted; correction = new transfer in the opposite direction. IDs are client-generated u128 (ULID-like recommended); resubmitting same id is idempotent (returns `exists` if identical, error if different).
- Concurrency: single-writer-per-cluster, deterministic state machine replicated with VSR; batching of up to ~8k transfers per request; no optimistic locking needed because invariants live in the DB.
- Balances: four counters per account (`debits_pending`, `debits_posted`, `credits_pending`, `credits_posted`) maintained materialized; available = posted credits - posted debits - pending debits (for credit-normal). Normal balance expressed by the constraint flag, not a field.
- Multi-currency: each transfer is single-ledger; FX needs two linked transfers via liquidity/"spread" accounts on each ledger (documented recipe in "Currency Exchange").
- Relevance: TigerBeetle is a separate system; adopting it is out of scope for a Postgres modular monolith, but its data model is the cleanest reference.

### 1.4 Stripe balance transactions
Sources: https://docs.stripe.com/api/balance_transactions ; https://docs.stripe.com/reports/balance ; https://docs.stripe.com/payouts

- Not a general ledger: one **BalanceTransaction** per movement on the Stripe account balance (charge, refund, payout, adjustment, fee...). Fields: `amount`, `fee`, `fee_details`, `net`, `currency`, `type`, `reporting_category`, `source` (the originating object), `available_on`, `status` (`pending`/`available`), `created`.
- Immutable: refunds, disputes, fee adjustments each create **new** balance transactions; originals are never modified.
- Balance has `available` and `pending` buckets per currency (plus `connect_reserved`, `instant_available` - UNVERIFIED completeness). `available_on` is the date a pending transaction becomes available - a deferred-availability model.
- Idempotency: `Idempotency-Key` header, keys retained ~24h (documented), same key + different params returns error.
- Each currency has an independent balance; conversions appear as separate balance transactions with `exchange_rate`.
- Fees are first-class on the transaction (gross, fee, net) rather than separate postings.

### 1.5 Square Books
Sources: https://developer.squareup.com/blog/books-an-immutable-double-entry-accounting-database-service/ (Square engineering blog "Books, an immutable double-entry accounting database service"); https://developer.squareup.com/blog/ (related posts)

- Square's internal service: immutable double-entry journal; every money-moving operation across Square products posts to it. Entries are append-only; mistakes fixed by compensating entries.
- Key properties described: immutability, auditability, strong consistency, idempotent posting (client-supplied unique keys), and balances derived from the journal. The post also discusses "accounts are cheap" and balance snapshots for read performance. Exact schema and storage engine (reported as MySQL-based/sharded): UNVERIFIED.
- Design lesson stated: products emit what happened; Books is the single place where balances are authoritative - very close to the Hub principle.

### 1.6 Cross-cutting comparison

| Concern | MT Ledgers | Formance | TigerBeetle | Stripe | Square Books |
|---|---|---|---|---|---|
| Immutability | posted immutable | append-only log + hash chain | transfers immutable | immutable | append-only |
| Correction | reverse/new txn | `revert` txn | opposite transfer | new BT | compensating entry |
| Idempotency | header + external_id | header + reference | client u128 id | header, ~24h | unique key |
| Concurrency | `lock_version` + constraints | per-ledger serialization | DB-enforced invariants | n/a (provider side) | UNVERIFIED |
| Hold model | pending/posted/archived | UNVERIFIED (holds via metadata/accounts) | pending/post/void | pending -> available | UNVERIFIED |
| Balance | materialized, 3 kinds | materialized volumes | 4 counters | pending/available | derived + snapshots |
| Currency | per account | asset `USD/2` | per ledger id | per currency balance | UNVERIFIED |

Normal balance rule of thumb (all systems): assets/expenses are debit-normal; liabilities/equity/revenue are credit-normal. Customer wallet balances held by the platform are **liabilities** (credit-normal) from the platform's perspective.

### Takeaway for Financial Hub
- Model: `account` (with currency, normal_balance, tenant_id) / `journal_entry` (header, status, effective_at, idempotency key) / `posting` lines. DB constraint (deferred trigger or write-path check) enforcing sum(debit)=sum(credit) per currency per entry.
- Append-only: no UPDATE/DELETE on posted entries (revoke privileges + trigger). Corrections = reversal entry referencing `reverses_entry_id`. Status machine `pending -> posted | voided`, copied from MT/TigerBeetle two-phase.
- Idempotency key unique per (tenant, source_product, key); same key + different payload hash -> hard error, not silent success.
- Concurrency: Postgres is enough: `SELECT ... FOR UPDATE` on affected account rows (ordered by id to avoid deadlock) or a `version` column with CAS for accounts that have non-negative constraints; no lock needed for accounts without constraints.
- Balances: maintain a materialized `account_balance` (pending/posted/available) updated in the same transaction as postings, plus a periodic recompute-from-postings integrity check; expose "as of effective_at" via postings query.
- Single currency per account; FX = explicit paired entries through FX position accounts. Normal balance stored on account; API returns signed "natural" balance.

---

## 2. Money representation

Sources: ISO 4217 list (https://www.iso.org/iso-4217-currency-codes.html, maintenance at https://www.six-group.com/en/products-services/financial-information/data-standards.html); Stripe zero-decimal currencies https://docs.stripe.com/currencies#zero-decimal ; Martin Fowler "Money" pattern https://martinfowler.com/eaaCatalog/money.html ; Fowler "Patterns of Enterprise Application Architecture" allocation algorithm; Python `decimal` https://docs.python.org/3/library/decimal.html ; PostgreSQL numeric https://www.postgresql.org/docs/current/datatype-numeric.html ; Modern Treasury/Formance/TigerBeetle docs above (all use integer minor units); Dinero.js https://dinerojs.com/ (allocation helpers).

- **Integer minor units** (cents) are the norm for ledgers: MT (`amount` + `currency_exponent`), Stripe (smallest currency unit), Formance (`USD/2`), TigerBeetle (u128). Benefits: exact, no float, cheap indexed sums.
- ISO 4217 minor unit exponent varies: 0 (JPY, KRW), 2 (USD, BRL, EUR), 3 (KWD, BHD, JOD), 4 (CLF), plus special cases. Stripe documents "zero-decimal" and "three-decimal" currencies (three-decimal ones must round to a multiple of 10 on Stripe - UNVERIFIED current rule). Store the exponent with the amount or derive from a currency table, never hard-code 2.
- **NUMERIC** (Postgres) is exact; fine for storage when scale is bounded (`NUMERIC(38,0)` as integer, or `NUMERIC(38,18)` for crypto/rates). Python side: `decimal.Decimal`, never `float`.
- Sub-minor precision is needed for usage-based unit prices (e.g. US$0.00003/token): pricing stage uses decimal (Lago, Orb support high-precision unit amounts - see §3); only the **final line amount** is rounded to minor units. Keep the rounding point explicit and single.
- Rounding: `ROUND_HALF_EVEN` (banker's) minimizes bias in aggregated rounding and is the IEEE 754 default; `ROUND_HALF_UP` is the common legal/consumer expectation in many jurisdictions (e.g. tax). Brazilian NF-e/ICMS-like rules: UNVERIFIED which mode applies per tax; must be a per-rule configuration. Stripe rounds tax/proration with half-up-like behavior (UNVERIFIED).
- **Allocation** (splits): never compute each share by rounding independently. Largest-remainder (Hamilton) method: floor each share = floor(total * ratio_i), then distribute leftover minor units one at a time to items with the largest fractional remainders (tie-break deterministically, e.g. by index/id). Fowler's `allocate` gives leftovers to the first shares; Dinero.js `allocate` is similar. Invariant: sum(parts) == total exactly. Formance Numscript `remaining` is the explicit "rest" recipient.
- Percentages/ratios: represent as integer basis points or exact decimal/rational, not float.
- Currency mixing: arithmetic across currencies must be a type error.

### Takeaway for Financial Hub
- Canonical type: `(amount_minor: int64/NUMERIC(38,0), currency: ISO-4217 code)`; currency table with exponent in the Hub; Python wrapper class forbids float and cross-currency add.
- Unit prices and rates: `Decimal` with fixed precision (e.g. NUMERIC(30,12)); a single named rounding step produces minor units; rounding mode (`HALF_EVEN` default, `HALF_UP` configurable per rule) stored on the price/tax rule version.
- Splits use largest-remainder with deterministic tie-break; property test: sum(parts)==total for random inputs.
- Ratios in basis points (integer) or Decimal; no float in the money path (lint rule).
- Record rounding residue explicitly (a "rounding" account) if any step cannot allocate exactly.

---

## 3. Usage-based billing

Sources:
- Lago: https://doc.getlago.com/docs/guide/billable-metrics/aggregation-types ; https://doc.getlago.com/docs/guide/plans/charge-models ; https://doc.getlago.com/docs/guide/events/ingesting-usage ; https://doc.getlago.com/docs/guide/wallet-and-credits/ ; https://doc.getlago.com/docs/guide/invoicing/
- Orb: https://docs.withorb.com/ (events: https://docs.withorb.com/events-and-metrics/event-ingestion ; billable metrics; price models: https://docs.withorb.com/price-configuration/overview ; plan versioning https://docs.withorb.com/product-catalog/plan-versions ; credits https://docs.withorb.com/product-catalog/prepurchase)
- Metronome: https://docs.metronome.com/ (events: https://docs.metronome.com/connecting-metronome/events ; rate cards/contracts https://docs.metronome.com/pricing-packaging/ ; commits & credits https://docs.metronome.com/invoice-customers/)
- Stripe Billing meters: https://docs.stripe.com/billing/subscriptions/usage-based ; https://docs.stripe.com/api/billing/meter ; https://docs.stripe.com/api/billing/meter-event

**Events / ingestion**
- All four: event = {customer id, event name/type, timestamp, properties, unique id}. Dedup key: Lago `transaction_id`, Orb `idempotency_key`, Metronome `transaction_id`, Stripe meter event `identifier` (dedup window documented ~24h on Stripe; others: UNVERIFIED window). Duplicate = ignored, not an error.
- Events are accepted asynchronously (queue) and aggregated later; Orb and Metronome emphasize a ClickHouse/OLAP-style store (engineering detail UNVERIFIED), Lago uses ClickHouse in "high volume" mode (documented).
- Late events: Orb allows backfill within a grace period and amendments/deprecation of events; Metronome supports late/backdated events and re-computes invoices in draft; once invoice is finalized, late usage typically rolls to the next invoice or triggers an adjustment/credit note (behavior per vendor: UNVERIFIED in detail). Stripe: events outside the allowed past window (~35 days) rejected (UNVERIFIED exact value) and meter events after invoice finalization are not added to it.

**Meters / billable metrics / aggregation**
- Aggregations: `count`, `sum` (of a property), `max`, `unique count` (distinct property), `latest`, `weighted sum` (time-weighted, Lago), `custom` (Lago SQL-ish expression). Stripe meter `formula`: `sum`, `count`, `last` (UNVERIFIED completeness). Orb adds `matrix`/`bulk` groupings and SQL-defined metrics.
- Recurring (persistent) vs. non-recurring metrics (Lago: "recurring" metrics carry over, e.g. seats/storage).
- Filters / dimensions on properties (e.g. region, model) allow different prices per dimension.

**Pricing models (charge models)**
- Standard/per-unit; **graduated** tiers (each unit priced at its tier's rate); **volume** (total quantity picks one rate applied to all units); **package** (price per N units, rounded up); **percentage** (rate on a transaction amount + optional fixed fee per event, free units, min/max per transaction - Lago); **matrix** (dimension-based, Orb); **bulk/tiered with flat fee** variants; **dynamic** (price from event property - Lago).
- Billing in advance (fixed/subscription fees) vs. in arrears (usage); usage charges are in arrears at period end.

**Plans / versioning**
- Orb: explicit plan versions; changes create a new version; subscriptions pin a version and can be migrated on a date. Lago: plan changes; overrides per customer ("plan overrides"). Metronome: rate cards with effective dates, contract-level overrides. Stripe: Prices are immutable (you create a new price, archive old).
- Proration for mid-period changes (recurring fees), with usage kept per-period.

**Invoices**
- States: draft -> finalized (-> paid / voided / payment_failed). Grace period before auto-finalize lets late events land (Lago `invoice_grace_period`, Orb `invoice_grace_period`, Stripe draft invoices ~1h before finalization). Finalized invoices are immutable; corrections via credit notes (Lago, Stripe) or reissue/void.
- Invoice lines reference the charge, the units, the unit price(s) and the period.

**Credits / wallets / commitments**
- Lago: prepaid credits wallet with rate (1 credit = X currency), top-ups (manual / recurring / threshold), consumption order priority vs coupons; Orb: prepaid credit blocks with expiry, cost basis, drawdown ordering (expiry first); Metronome: commits (prepaid or postpaid) + credits as ledger entries with scopes (specific products), priority, rollover; Stripe: Billing Credits / credit grants with applicability scope and expiry (UNVERIFIED completeness).
- Minimum commitment/true-up: Lago "minimum commitment" billed if usage < commit; Metronome "commit" with overage at list rate; Orb "minimums".

**Notes on correctness**
- Billing is recomputed deterministically from (events, price version, period); keep events immutable and the aggregation reproducible - makes invoice disputes auditable.

### Takeaway for Financial Hub
- Keep metering **out** of the Hub core unless required: products send aggregated or raw usage facts; the Hub only needs (customer, meter, quantity, period, idempotency id) and a rating step. Decide this boundary explicitly.
- Event ingestion contract: `(tenant, source_product, event_id)` unique; duplicates acknowledged idempotently; store raw events immutably; late events accepted until the period's invoice is finalized, afterwards go to an adjustment entry or next period (policy per tenant).
- Rating function pure and versioned: `rate(events, price_version) -> lines` so any invoice can be recomputed; support per-unit, graduated, volume, package, percentage as enum with Decimal parameters.
- Invoice lifecycle `draft -> finalized -> (paid|void)`; finalized immutable; adjustments via credit-note-like reversing entries into the ledger.
- Prepaid wallet = ledger liability account; credit grants as entries with expiry and drawdown order; commit/minimum = scheduled true-up entry.

---

## 4. Payments orchestration & state machines

Sources:
- Stripe PaymentIntents: https://docs.stripe.com/payments/payment-intents ; lifecycle https://docs.stripe.com/payments/paymentintents/lifecycle ; Charges https://docs.stripe.com/api/charges
- Adyen: https://docs.adyen.com/online-payments/payment-lifecycle (UNVERIFIED path) and https://docs.adyen.com/development-resources/webhooks/
- Hyperswitch (Juspay, open-source orchestrator): https://docs.hyperswitch.io/ ; https://github.com/juspay/hyperswitch ; routing https://docs.hyperswitch.io/explore-hyperswitch/payment-orchestration/smart-router (UNVERIFIED path)
- Spreedly: https://docs.spreedly.com/ (payment method vaulting, gateway-agnostic transaction API) ; https://docs.spreedly.com/guides/
- Primer/others: UNVERIFIED, not reviewed.

**Concept separation**
- **Payment intent** (Stripe) = merchant's intention to collect an amount; long-lived, tracks the whole attempt sequence; statuses: `requires_payment_method`, `requires_confirmation`, `requires_action`, `processing`, `requires_capture`, `succeeded`, `canceled`.
- **Charge / payment attempt** = one try against a payment method/provider; a PaymentIntent has many Charges (retries, different methods). Charges carry the provider outcome, failure code, `balance_transaction`.
- Hyperswitch: `payment` -> `payment_attempt` (one per try; stores connector id, connector_transaction_id, error codes), plus `refund`, `dispute`, `payout` objects. Attempts cycle across connectors on retry/smart routing.
- Auth vs capture: `requires_capture` (manual capture), partial capture, cancel/void of authorization, expiry of auth (~7 days cards; scheme-dependent).
- Async methods (Pix, boleto, bank redirects, 3DS): final state arrives via webhook; polling as fallback. The platform must treat webhooks as hints and verify via provider API (Stripe recommends re-fetch; Adyen sends notifications and expects `[accepted]` ack).

**Multi-provider abstraction**
- Spreedly: provider-agnostic API (`purchase`, `authorize`, `capture`, `void`, `credit`) over many gateways, with a vault of tokenized payment methods so you can move between providers - separates *credential custody* from *processing*.
- Hyperswitch: connector trait/interface per provider; unified request/response model; routing rules (cost, success-rate, volume split), retries/failover across connectors, unified webhook ingestion that maps connector events into a canonical event.
- Normalization of statuses: map each provider's statuses/events/error codes into a **canonical** set (e.g. `requires_action`, `authorized`, `captured`, `failed`, `canceled`, `partially_refunded`, `refunded`, `disputed`) and keep the raw provider payload + raw status alongside (never discard). Canonical error taxonomy: `declined (soft/hard)`, `insufficient_funds`, `fraud`, `provider_unavailable`, `invalid_request` - soft vs hard declines drive retry logic.
- State machines are monotonic with a transition table; out-of-order webhooks are common (e.g. `captured` arrives before `authorized`), so transitions must be idempotent and tolerant (apply only if it advances state, otherwise store as event).
- Refund = separate object with its own state (`pending`, `succeeded`, `failed`), many per payment, sum <= captured.

### Takeaway for Financial Hub
- Decide whether the Hub **executes** payments (orchestrator) or only **records** facts reported by products/providers. Either way keep the 3 layers: `payment` (intent) -> `payment_attempt` -> provider `transaction_event` (raw).
- Canonical status enum + explicit transition table; unknown/out-of-order provider events are persisted and acknowledged but do not regress state.
- Provider adapter interface (Python Protocol): `authorize/capture/void/refund/parse_webhook/fetch_status/map_status`; store `provider`, `provider_object_id`, raw payload JSONB, canonical mapping version.
- Treat webhook as a notification: verify signature, dedup by provider event id, then re-fetch authoritative state when the amount/status is financially material.
- Ledger posting happens on canonical state transitions (authorized -> pending entry; captured -> posted), never directly on provider webhooks.

---

## 5. Marketplace splits & settlement

Sources: https://docs.stripe.com/connect ; https://docs.stripe.com/connect/charges ; https://docs.stripe.com/connect/destination-charges ; https://docs.stripe.com/connect/separate-charges-and-transfers ; https://docs.stripe.com/connect/marketplace/tasks/refunds-disputes ; https://docs.stripe.com/connect/account-balances ; https://docs.stripe.com/connect/manage-payout-schedule ; https://docs.stripe.com/connect/instant-payouts ; https://docs.stripe.com/connect/charges-transfers (UNVERIFIED path)

- **Charge types**: *Direct charges* (charge on the connected account; platform takes `application_fee_amount`), *Destination charges* (charge on the platform; `transfer_data.destination` sends funds to the connected account; `application_fee_amount` or `transfer_data.amount` keeps platform fee; platform is merchant of record, bears refund/dispute liability), *Separate charges and transfers* (charge on the platform, later explicit `Transfer`s to one or many accounts - the multi-party split case; `source_transaction` ties a transfer to a charge so funds availability follows the charge).
- Application fee: recorded as `ApplicationFee` object (refundable via `ApplicationFeeRefund`), appears as a balance transaction on the platform.
- **Balances**: every account (platform and connected) has `pending` and `available` buckets; funds from a charge are pending until `available_on` (payout timing/country-dependent), then available for payout. A transfer from platform to connected account increases the connected account's available (or pending when `source_transaction` used) balance.
- **Payouts**: balance -> bank. Schedule per connected account (`daily/weekly/monthly/manual`, delay_days); payout is itself a balance transaction (negative); failed payouts return funds and generate a `payout.failed` event; instant payouts have a fee. Each payout is reconcilable: `balance_transaction` list filtered by `payout` id yields exactly what it contains (documented "payout reconciliation report").
- **Refunds**: for destination charges, refund does not automatically reverse the transfer unless `reverse_transfer=true`; `refund_application_fee=true` returns the platform fee. Partial refunds reverse proportionally (documented behavior: UNVERIFIED for rounding). Transfer reversal can drive the connected balance negative; the platform can be liable (negative balance -> debit from bank, or reserves).
- **Disputes/chargebacks**: dispute amount + dispute fee debited from the account that owns the charge (platform for destination charges; connected account for direct); funds withdrawn immediately (`charge.dispute.funds_withdrawn`) and reinstated if won (`funds_reinstated`). Platform may reverse transfers to recoup.
- Negative balance handling, reserves, rolling holds and KYC-gated payouts are separate risk controls; details UNVERIFIED for non-US.
- Other references: Adyen for Platforms (balance accounts, sweeps) https://docs.adyen.com/platforms/ ; Mangopay wallets https://docs.mangopay.com/ ; both UNVERIFIED in detail, same pattern (wallet/balance account per seller + payout).

### Takeaway for Financial Hub
- Model split as a **plan** computed at capture time (largest-remainder across parties, fee rules by price version) and posted as one balanced journal entry: debit `provider_clearing`, credit `seller_payable:{seller}`, credit `platform_fee_revenue`, credit `tax_payable` etc.
- Keep two-phase availability: seller liability posted in `pending` sub-balance, moved to `available` on a settlement event/date (`available_on`), payouts only from available.
- Refund/chargeback = reversing entries that mirror the original split proportionally (reference `original_entry_id`, rounding via largest remainder on the reversed amount, tracked so cumulative reversals never exceed original); explicit flag whether platform fee is returned.
- Allow seller balance to go negative only via an explicit policy account; track reserves.
- Payout = batch with items; reconcilable by id to the provider payout statement (see §6).

---

## 6. Reconciliation

Sources: Stripe reconciliation docs https://docs.stripe.com/reports/payout-reconciliation ; https://docs.stripe.com/reports/report-types ; Adyen settlement detail report https://docs.adyen.com/reporting/settlement-reconciliation/ ; Modern Treasury reconciliation https://docs.moderntreasury.com/payments/docs/reconciliation (UNVERIFIED path) ; Square engineering blog (Books, §1.5) ; Brazil: BACEN Pix statement / ADICON (UNVERIFIED); Formance Reconciliation module https://docs.formance.com/modules/reconciliation/introduction (UNVERIFIED path); Moov/Plaid-style bank statement matching (UNVERIFIED).

- **Three-way idea**: (a) internal ledger/facts, (b) provider event stream (webhooks/API), (c) provider settlement/statement report (daily CSV/API: Stripe balance + payout reports, Adyen Settlement Details, bank statements/CNAB/OFX in Brazil - UNVERIFIED specifics).
- Matching keys, in order: provider transaction id (exact) -> merchant reference/idempotency key -> amount + currency + date window + counterparty (fuzzy). Many-to-one cases: one payout contains many transactions; one bank credit = one payout.
- **Break types**: missing internally (provider has it, we don't); missing at provider (we have it, they don't); amount mismatch (fees, FX, rounding, partial); status mismatch (we say captured, provider says failed/refunded); timing difference (cutoff/timezone/settlement T+n); duplicate; currency mismatch; fee difference; unexpected adjustment (chargeback fee, interchange true-up).
- Resolution: **auto** for deterministic matches and known tolerances (within +/- N minor units for rounding, known fee patterns, timing within cutoff window); **auto-correct** by re-fetching provider state and posting an adjustment/reversal entry; **manual** queue for unmatched/ambiguous with reasons, assignee, notes, approval for write-offs; every resolution leaves an audit record and (if financial) a ledger adjustment entry, never an edit.
- Reconciliation produces **matched sets** (n:m) with status `matched | partial | unmatched | disputed | written_off`, plus aging. Continuous (event-level) vs. batch (daily statement) both needed; the statement is the source of truth for cash.
- Provider reports are themselves immutable, versioned inputs: store raw file + hash + import run id; reconciliation runs are idempotent and re-runnable.

### Takeaway for Financial Hub
- Tables: `statement_import` (raw file/hash/provider/period), `statement_line`, `reconciliation_match` (n:m link to ledger entries/payments), `recon_break` (type, amount delta, status, owner, resolution_entry_id).
- Hierarchical matcher: exact id -> reference -> fuzzy; tolerance rules as versioned config; unknown stays open (never force-match).
- Settlement cash accounts per provider: `provider_clearing` (what the provider owes us) vs `bank` (what arrived); reconciliation proves clearing -> 0 per settled period.
- Every fix is an adjusting ledger entry linked to the break; dashboards for break aging and totals by type.
- Run recon idempotently per (provider, period); re-imports must not create duplicate matches.

---

## 7. Reliable messaging

Sources: Transactional outbox https://microservices.io/patterns/data/transactional-outbox.html ; Polling publisher https://microservices.io/patterns/data/polling-publisher.html ; Debezium outbox https://debezium.io/documentation/reference/transformations/outbox-event-router.html ; Idempotent consumer https://microservices.io/patterns/communication-style/idempotent-consumer.html ; CloudEvents spec https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md and https://cloudevents.io/ ; Enterprise Integration Patterns https://www.enterpriseintegrationpatterns.com/patterns/messaging/IdempotentReceiver.html ; Stripe event versioning https://docs.stripe.com/api/versioning ; Confluent schema evolution https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html ; PostgreSQL `SKIP LOCKED` https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE

- **Transactional outbox**: write business state and an `outbox` row in the same DB transaction; a relay (poller using `FOR UPDATE SKIP LOCKED`, or CDC like Debezium/logical replication) publishes and marks/deletes. Gives at-least-once publication, ordering per aggregate if the relay preserves `aggregate_id` order (partition/key by aggregate).
- **At-least-once + idempotent consumer**: duplicates are normal. Consumer keeps an **inbox**/`processed_message` table keyed by `(consumer, message_id)` with unique constraint, inserted in the same transaction as the side effects. Effects must be idempotent or protected by that insert.
- Exactly-once is an illusion across boundaries; achieve "effectively-once" = at-least-once delivery + dedup.
- In a **modular monolith**, the same pattern applies between modules (in-process bus writes to outbox in the caller's transaction, dispatcher runs handlers with inbox dedup), which preserves module boundaries and makes later extraction trivial.
- Ordering: only guaranteed per key; consumers must handle out-of-order (version number/`sequence` on aggregate; ignore stale).
- **CloudEvents 1.0 required attributes**: `id`, `source`, `specversion` ("1.0"), `type`; optional: `datacontenttype`, `dataschema`, `subject`, `time`, `data`, `data_base64`; extension attributes lowercase alphanumerics (e.g. `traceparent`, `partitionkey`, `sequence`). `source`+`id` unique identifies an event; `type` reverse-DNS style e.g. `com.example.invoice.finalized`.
- **Versioning**: put version in `type` (`...v1`) or `dataschema`; additive-only changes within a version (new optional fields); breaking change = new type/version published in parallel; consumers ignore unknown fields (tolerant reader). Stripe pins API version per account/endpoint so the event payload shape is stable per consumer; schema registries (Avro/JSON Schema/Protobuf) enforce compatibility modes (backward/forward/full).
- Event content: *thin* (ids only; consumer fetches) vs *fat* (state snapshot). Financial facts favor immutable fat events with amounts/currency/reference so consumers need not call back; ids still included.
- Dead-letter queue + retry with backoff for poison messages; metrics on outbox lag.

### Takeaway for Financial Hub
- `outbox_event` table (id UUIDv7, tenant, aggregate_type/id, type, version, payload JSONB, occurred_at, published_at) written in the same tx as ledger postings; relay with `SKIP LOCKED` batches first, upgrade to logical replication later.
- `inbox_message` unique `(consumer, event_id)`; handlers idempotent anyway.
- Envelope = CloudEvents-compatible (`id`, `source`=`/hub/{module}`, `type`=`hub.invoice.finalized.v1`, `time`, `subject`, `dataschema`, extensions `tenantid`, `sequence`, `traceparent`).
- Events about money carry amount_minor + currency + ledger entry id; additive evolution only; breaking = new `vN` type with dual-publish window.
- Inbound product events (business facts) use the same envelope and idempotency key; the Hub acknowledges only after the fact is durably persisted.

---

## 8. Outbound webhooks

Sources: Standard Webhooks spec https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md and https://www.standardwebhooks.com/ ; Svix docs https://docs.svix.com/ ; retry schedule https://docs.svix.com/retries ; endpoint disable https://docs.svix.com/incoming-webhooks (UNVERIFIED) and https://docs.svix.com/retries#disabling-failing-endpoints (UNVERIFIED anchor) ; replay https://docs.svix.com/receiving/replay (UNVERIFIED path); Svix blog https://www.svix.com/blog/ ; Stripe signatures https://docs.stripe.com/webhooks#verify-official-libraries and https://docs.stripe.com/webhooks/signature ; Stripe retries https://docs.stripe.com/webhooks#automatic-retries

**Standard Webhooks**
- Headers: `webhook-id` (unique message id, stable across retries - the dedup key), `webhook-timestamp` (integer seconds since epoch, set at **each** attempt), `webhook-signature` (space-delimited list of `version,signature` pairs, e.g. `v1,<base64>`).
- Signed content: `"{webhook-id}.{webhook-timestamp}.{raw body}"`.
- Symmetric scheme `v1`: HMAC-SHA256, base64 signature; secret format `whsec_<base64>` (the part after the prefix is base64-decoded to get the key bytes). Asymmetric scheme `v1a`: Ed25519, key prefix `whpk_` (public) / `whsk_` (secret).
- Key rotation: sender may include **multiple signatures** (space separated, e.g. old and new secret) so receivers verify against any one that matches; receiver should use constant-time comparison.
- Replay protection: receivers reject timestamps outside a tolerance (spec suggests ~5 min).
- Payload recommendations: JSON with `type` (event type, dot-separated), `timestamp`, `data`; response: any 2xx = success; spec recommends receivers respond quickly and process async; retries on non-2xx and timeouts; spec recommends retry with exponential backoff and disabling failing endpoints (guidance level, details are implementation-defined).

**Svix delivery semantics** (docs https://docs.svix.com/retries)
- Success: HTTP 2xx within timeout (documented ~15s - UNVERIFIED current value). 3xx are not followed/treated as failure (UNVERIFIED).
- Retry schedule (documented): immediately, then 5 seconds, 5 minutes, 30 minutes, 2 hours, 5 hours, 10 hours, 10 hours (additional) - roughly 8 attempts over ~1 day; exact list UNVERIFIED, re-check.
- Failing endpoints: if all attempts fail for ~5 consecutive days, the endpoint is **disabled** automatically and the owner is notified (operational webhooks `endpoint.disabled`); can be re-enabled. Manual **recovery/replay**: "Recover failed messages since <date>" for an endpoint, and resend of individual messages from the dashboard/API.
- Other features: per-endpoint event-type filter, per-endpoint rate limit, message ordering not guaranteed, idempotency key on message creation (`idempotency-key` header) to avoid duplicates, endpoint secret rotation with 24h overlap (documented ~24h: UNVERIFIED), static source IPs, event catalog with JSON Schema, consumer portal (embedded UI), attempt logs with response bodies.

**Stripe webhook signing**
- Header `Stripe-Signature: t=<unix ts>,v1=<hex hmac>[,v1=...]`; signed payload = `"{t}.{raw body}"` with HMAC-SHA256 using endpoint secret (`whsec_...`); hex-encoded (differs from Standard Webhooks' base64 and headers). Multiple `v1` values appear during secret roll (expiry configurable up to 24h). Default tolerance 5 minutes. Retries: live mode up to 3 days with exponential backoff; test mode a few attempts over hours (documented). Events are not delivered in order and may duplicate -> dedup by `event.id`.
- Stripe includes `api_version` of the endpoint so payload shape is predictable.

### Takeaway for Financial Hub
- Adopt Standard Webhooks wholesale for outbound webhooks (headers, signing string, `whsec_` secrets, v1 HMAC-SHA256) so tenants can use off-the-shelf verification libraries; do not invent a scheme.
- Delivery model: `webhook_endpoint` (tenant, url, secrets[] with status active/rotating/expired, event filters, state active/disabled), `webhook_delivery` per (event, endpoint) with `webhook-id` = event id stable across retries, attempts table with status code/latency/response snippet.
- Retry exponential schedule with jitter (start from Svix-like: immediate, 5s, 5m, 30m, 2h, 5h, 10h...), cap ~3 days; auto-disable endpoint after N days of continuous failure with notification; manual replay (single message and "since timestamp").
- Rotation: store 2 active secrets; sign with all active -> multiple signatures in header; expire the old one after a configurable overlap.
- Outbound webhooks are produced from the outbox (§7), never directly in request handlers; payloads are CloudEvents-like data wrapped in the Standard Webhooks body; SSRF protection on endpoint URLs (block private IP ranges) and per-tenant rate limits.
- Make clear: webhooks are at-least-once and unordered; include `sequence`/version per aggregate and document dedup by `webhook-id`.

---

## 9. Audit trail & configuration versioning for financial rules

Sources: Stripe Prices immutability https://docs.stripe.com/products-prices/manage-prices ; Orb plan versions https://docs.withorb.com/product-catalog/plan-versions ; Metronome rate cards https://docs.metronome.com/pricing-packaging/ (effective dates) ; Lago plans https://doc.getlago.com/docs/guide/plans/overview ; Martin Fowler "Bitemporal History" https://martinfowler.com/articles/bitemporal-history.html and "Effective Dates" / "Temporal Property" https://martinfowler.com/eaaDev/TemporalProperty.html ; SQL:2011 temporal tables overview https://en.wikipedia.org/wiki/Temporal_database ; PostgreSQL range types and exclusion constraints https://www.postgresql.org/docs/current/rangetypes.html ; `btree_gist` https://www.postgresql.org/docs/current/btree-gist.html ; MT ledger `effective_at` (see §1.1); Event sourcing https://martinfowler.com/eaaDev/EventSourcing.html ; pgaudit https://github.com/pgaudit/pgaudit ; SOX/PCI guidance: UNVERIFIED not reviewed.

- **Price/plan versions**: Stripe Prices cannot change amount after creation (new Price, archive old); Orb: plan versions with subscription pinned to a version and scheduled migration; Metronome: rate cards with `starting_at` entries (effective-dated rate changes); Lago: plan updates affect subscriptions unless overridden (UNVERIFIED exact semantics). Commonality: **historic invoices always re-explain from the version that applied**.
- **Effective dating**: each version has `effective_from` (inclusive) and `effective_to` (exclusive, nullable); at most one version per key active at any instant (Postgres `EXCLUDE USING gist (key WITH =, tstzrange(effective_from, effective_to) WITH &&)`). Distinguish *valid time* (when the rule applies in the real world) from *transaction/recorded time* (when we learned/entered it) - bitemporal: allows retroactive correction with an audit trail of what we believed at invoice time.
- Resolution rule: a calculation records **which version ids** it used (price_version_id, tax_rule_version_id, fee_rule_version_id, rounding mode) - so recomputation uses the pinned version, not "latest".
- Config changes are themselves audit events: who (actor + tenant + auth context), what (before/after diff), when, why (reason/ticket), approval (four-eyes for rule changes affecting money). Write-once audit log (append-only table; optional hash chaining like Formance's log or periodic anchoring - hash chain is a Formance feature, usage for audit is a design choice).
- Ledger-level audit: every entry has `created_by`, source product, source reference, idempotency key, causation/correlation ids (CloudEvents `traceparent`), and links `reverses`/`adjusts`. Nothing is deleted; "soft delete" is a status transition recorded as an event.
- Retention and access: financial records commonly need multi-year retention (jurisdiction-specific; Brazil fiscal retention ~5 years - UNVERIFIED, check with counsel); PII separated so erasure (LGPD/GDPR) can nullify PII without breaking ledger integrity (store pseudonymous party ids in ledger, PII in a separate table).
- Tooling: row-level triggers writing to `audit_log`, or application-level domain events as the audit record (preferred for business semantics); pgaudit for DB-level access auditing.

### Takeaway for Financial Hub
- `price_version`, `fee_rule_version`, `tax_rule_version`, `plan_version` tables: immutable rows, `effective_from/to` ranges with exclusion constraints, `recorded_at`, `created_by`; never UPDATE amounts - insert a new version.
- Every computed monetary result (invoice line, split, fee) stores the version ids + inputs snapshot used; recomputation is a pure function over those.
- Separate `effective_at` (business date) from `recorded_at` on ledger entries; late-arriving facts post with past `effective_at` but are blocked once the period is closed (period-close table; after close, post to current period with reference).
- Append-only `audit_log` for config/admin actions with actor, before/after, reason; optional hash chain; admin changes to money rules require a second approver.
- Keep PII outside the ledger; use opaque party ids so retention and erasure policies do not collide.

---

## Top 10 decisions this suggests

1. **Ledger core = double-entry with immutable, append-only journal entries** (header + postings), enforced by DB privileges/triggers; corrections only via reversal entries linked to the original.
2. **Money = integer minor units + ISO-4217 currency (exponent from a table); Decimal only for unit prices/rates, with one explicit, versioned rounding step** (default HALF_EVEN, configurable HALF_UP) and **largest-remainder allocation** for every split.
3. **Two-phase postings: `pending -> posted | voided`**, with three balances (pending/posted/available) maintained materialized in the same transaction and verified by a periodic recompute job.
4. **Idempotency everywhere**: unique `(tenant, source_product, idempotency_key)` with payload-hash comparison; same key + different payload is an error.
5. **Concurrency via Postgres row locks / version CAS on accounts with constraints**, ordered locking to avoid deadlocks; no distributed locking; multi-tenant isolation via `tenant_id` on every table (+ RLS).
6. **Hub only posts to the ledger from canonical state transitions** (payment attempt authorized/captured, invoice finalized, payout paid) - never directly from raw provider webhooks; provider statuses are normalized through a mapping layer that retains raw payloads.
7. **Settlement model for marketplaces: split plan computed once, posted as one balanced entry** (clearing -> seller payable + platform fee + taxes), seller funds move pending -> available on settlement date, refunds/chargebacks mirror the original split proportionally.
8. **Reconciliation as a first-class module**: immutable statement imports, n:m matches, typed breaks, tolerance rules as versioned config, every resolution = adjusting entry + audit record; clearing accounts must tie to zero per settled period.
9. **Transactional outbox + inbox dedup + CloudEvents-compatible envelope with versioned `type`** between modules and toward products; **outbound webhooks follow Standard Webhooks** (whsec_ secrets, multi-signature rotation, jittered exponential retries, auto-disable, replay).
10. **Everything financial is versioned and effective-dated** (prices, fees, taxes, rounding, plans) with exclusion constraints and bitemporal fields (`effective_at` vs `recorded_at`); every calculation pins the version ids it used so any invoice or split can be re-derived and audited; usage rating is a pure function over immutable events and a pinned price version.

---

### Appendix: UNVERIFIED register (re-check before relying)
- MT Ledgers: exact request fields for account version/lock preconditions; FX handling; internal balance implementation.
- Formance: hold/pending mechanism; per-ledger locking granularity; v2 metadata versioning.
- Square Books: storage engine, schema, concurrency approach.
- Stripe: three-decimal currency rounding rule; proration/tax rounding mode; meter event backdating window; Billing Credits scope list.
- Lago/Orb/Metronome: dedup retention windows; exact late-event/finalized-invoice behavior; Lago plan-update semantics.
- Svix: exact retry schedule, request timeout, auto-disable threshold, secret-overlap duration, docs URL anchors.
- Hyperswitch / Adyen / Spreedly: specific doc paths; Adyen for Platforms and Mangopay details.
- Brazil-specific: rounding mode per tax, fiscal retention period, bank statement formats (CNAB/OFX), Pix reconciliation artifacts.
