# Reconciliation

How the hub proves its books against itself and against the provider (ADR-0013). Code:
`backend/src/financial_hub/modules/reconciliation`; tables from migration `0014_reconciliation`.
Every amount is an integer of minor units; nothing here uses floats.

## 1. Runs

A run (`reconciliation_runs`, `rec_` ids) checks one **window** of calendar days in the
provider's calendar (America/Sao_Paulo, UTC-3), inclusive, at most 31 days, and always **before
today**: the provider's day must be over. Default window: yesterday.

Runs are idempotent by scope `(project, kind, provider account, window)`: starting the same
run again returns the stored run (same id, no duplicate breaks, no second
`reconciliation.run_completed`). A run is `running`, then `completed` or `failed` (provider
unreachable; `error_code` holds the provider's short code, never a body).

| Kind | Compares | Breaks |
|---|---|---|
| `internal` | materialized `ledger_accounts` balances vs. a recomputation from postings; the trial balance per currency (must be 0); clearing residues per payment (`sales_clearing` after R3, `customer_receivable` of fully paid standalone intents) | `ledger_drift`, `clearing_not_zero` |
| `transactions` | the window's hub payments vs. the provider listing (Asaas `GET /payments` by `dateCreated`, every page, `refunds[]`; the fake provider in tests) | `missing_internal`, `missing_external`, `amount_mismatch`, `fee_mismatch`, `status_mismatch` |
| `balance` | provider balance (Asaas `GET /finance/balance`) vs. booked `provider_balance:{pa}` + held | `balance_drift` |

Invoice-backed intents (W5.3 billing) get R1 from billing; the clearing check covers only
standalone intents (`invoice_id IS NULL`).

**Starting a run.** `POST /v1/projects/{p}/reconciliation/runs` (`kind`, optional
`window_start`/`window_end`, `provider_account_id` when the project uses several accounts;
needs `Idempotency-Key`), or the worker's daily events-scheduler schedule
`reconciliation.daily` (handler `reconciliation.scheduled_run`), configured by
`ReconciliationSettings` and off unless enabled (always off in tests).

### Matching (transactions)

1. by provider payment id (stored by the hub at creation);
2. what is left, by `externalReference` = the hub's `pi_` id (a create whose answer was lost).

A hub payment matches at most once. Leftovers are confirmed before a break opens: a provider
payment not in the window's hub payments is looked up by id/reference across all days (the hub
may know it from another day), and a hub payment missing from the listing is fetched by id
(`missing_external` only if the provider says it does not exist).

## 2. Breaks

`reconciliation_breaks` (`rbk_` ids) store both sides as JSON in integer minor units
(`internal`, `external`), the opening run and the last run that saw it, the subject ids
(`payment_intent_id`, `provider_payment_id`, ledger `account_id`, `provider_account_id`) and a
signed `difference` (external minus internal). A break is identified across runs by its
fingerprint (`kind` + subject): a later run that detects it again updates `last_run_id` and does
not open a duplicate.

| Kind | Meaning | Resolution |
|---|---|---|
| `ledger_drift` | a cached balance differs from its postings, or a trial balance is not 0 | operator (a bug: never adjusted away) |
| `clearing_not_zero` | a settled standalone payment left a residue on a clearing account | operator |
| `missing_internal` | the provider has a payment the hub does not know (panel-created, lost create) | operator |
| `missing_external` | the hub has a payment the provider does not know | operator |
| `amount_mismatch` | amount, refunded total or currency differ | operator |
| `fee_mismatch` | both captured, but the provider's net differs from the booked net | **auto** (refresh posts R2a) |
| `status_mismatch` | capture, settlement or dispute status differ (typically a missed webhook) | **auto** (refresh applies the transition) |
| `balance_drift` | provider balance ≠ booked provider balance + held | operator; R12 adjustment if justified, **never automatic** |

States: `open → investigating → resolved | accepted`; closed breaks never reopen.
`investigating` and `accepted` need a reason (≤ 1000 chars); every move is appended to
`history` with the actor and time. Routes:
`GET /runs`, `GET /runs/{id}`, `GET /breaks` (filter `kind`, `state`, `run_id`),
`GET /breaks/{id}`, `POST /breaks/{id}/transition`.

**Auto-resolution** runs right after a transactions run, for `fee_mismatch` and
`status_mismatch` only: the payment is refreshed from the provider through the normal payments
transitions (the same code path as a webhook), compared again, and the break is closed as
`resolved` by `system` if the difference is gone. Any failure leaves it open.
A re-run does **not** close other breaks that are no longer detected: an operator closes them.

## 3. Adjustments (R12, four-eyes)

When a `balance_drift` (or another provider-side break) is justified, the books are corrected
with recipe R12: `provider_balance:{pa}` against `opening_balance` (`amount > 0` debits the
provider balance).

1. **Propose** `POST /breaks/{id}/adjustments` (`amount_minor` signed non-zero, `reason`
   required, optional `currency`/`provider_account_id`): stores a `proposed` adjustment
   (`adj_`) with a SHA-256 `payload_hash` over break, account, currency, amount and reason. At
   most one pending proposal per break; the break must be open or investigating.
2. **Approve** `POST /adjustments/{id}/approve` with the `payload_hash` the approver reviewed:
   a **different operator** posts R12 (correlation = adjustment id, causation = break id), the
   adjustment becomes `approved` with the journal entry id, and the break is `resolved`.
   A changed payload is `409 adjustment-payload-mismatch`.
3. **Reject** `POST /adjustments/{id}/reject` with a reason: nothing is posted, the break stays
   as it was.

The proposer can neither approve nor reject their own proposal (`403 four-eyes-required`);
the database enforces it too (`CHECK decided_by <> proposed_by`), and a decided row is final
(trigger). The R12 entry's metadata carries the code `reconciliation.<break kind>`, never the
free-text reason (entry metadata must not hold PII); the reason stays on the adjustment row and
in the audit log.

## 4. Who may do what

| Action | Permission | Roles |
|---|---|---|
| read runs, breaks, adjustments | `reconciliation:read` | owner, admin, finance, viewer |
| start runs, transition breaks | `reconciliation:manage` (moves no money) | owner, admin, finance |
| propose / approve / reject R12 | `ledger:adjust` (money movement) | owner, finance |

Only interactive operator sessions reach reconciliation: API keys have no reconciliation scope
(`403`). There is no password step-up in the hub yet; the gap is recorded in
[SECURITY.md](SECURITY.md) s3.

## 5. Events and metrics

Events: `reconciliation.run_completed` (also for failed runs), `reconciliation.break_opened`,
`reconciliation.break_resolved` (`automatic` true for auto-resolution). Audit actions:
`reconciliation.run_requested`, `.break_transitioned`, `.adjustment_proposed`,
`.adjustment_approved`, `.adjustment_rejected`.

OTel gauges, refreshed after each run and decision: `breaks_open` (by kind) and
`ledger_drift_minor_units` (absolute sum over active `ledger_drift` breaks).

## 6. Shared sandbox caveat

The Asaas Sandbox account is shared by suites, demos and people. A `balance` run against it
compares the whole account balance with one project's books, so `balance_drift` there is
expected noise: accept it with a reason, never adjust it. Transactions runs are scoped by the
hub's own payments and stay meaningful. The live test
(`tests/integration/reconciliation/test_asaas_live_reconciliation.py`, skipped without
`FH_ASAAS_API_KEY`) is read-only and asserts shapes, never counts or amounts.

## 7. Demo data

`seed-demo` records, once, a completed transactions run for 2026-10-01 on the demo project's
fake provider with one open `fee_mismatch` on `demo-pay-02` (provider net 99 minor units lower).
The fake provider keeps no fee history, so that provider side is scripted; nothing is posted.
