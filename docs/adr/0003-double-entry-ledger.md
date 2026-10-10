# ADR-0003: Double-entry ledger design

- Status: Accepted · Date: 2026-10-10 · Revised: 2026-10-10 (review findings F-20, F-21)

## Context
The ledger is the financial source of truth. It must be immutable, balanced, safe under concurrency, fast to read balances, and auditable back to the causing event. Benchmarked designs: Modern Treasury Ledgers, Formance, TigerBeetle, Square Books (see `docs/research/`).

## Decision
1. Tables: `ledger_accounts`, `journal_entries`, `ledger_postings` (direction + positive amount). Postings reference the account with a composite FK `(account_id, project_id, currency)` and the entry with `(entry_id, project_id)`, so a posting can never cross projects or currencies.
2. **Integrity in the database, not only in the domain** — a `DEFERRABLE INITIALLY DEFERRED` constraint trigger fires once per touched **entry** at commit and checks: ≥ 2 postings, Σ debits = Σ credits per currency, posting currency = account currency, posting project = entry project, and that the account's balance columns changed by exactly the entry's postings.
3. **Append-only**: triggers reject `UPDATE`/`DELETE` on postings; entries may only transition `pending → posted|archived`. Corrections are reversal entries (`reverses_entry_id`).
4. **Balances materialized** on the account row (`posted_dr`, `posted_cr`, `pending_dr`, `pending_cr`, `version`), updated by a `ledger_apply` SQL function in the same transaction. The application DB role (`fh_app`) has `INSERT`/`SELECT` on ledger tables and `EXECUTE` on `ledger_apply` only — no `UPDATE`/`DELETE`, no `ALTER`/`DISABLE TRIGGER`. Migrations run as `fh_owner`.
5. **Lock protocol** (one unit of work = one transaction):
   1. Lock the business aggregate first (`payment_intents`/`refunds`/`payouts`/`invoices` row `FOR UPDATE`). This serializes every recipe for that aggregate and fixes the order of recipes (R4 can never run before R2).
   2. Compute **all** postings of **all** entries in the UoW, take the **union** of their accounts, provision missing ones with `INSERT … ON CONFLICT DO NOTHING` (upsert-then-lock), then lock the union **once** `FOR UPDATE` ordered by account id.
   3. Check `no_overdraft` against `available` (definition in FINANCIAL_MODEL §2) under the lock, write entries and postings, apply balances.
   Hierarchy: aggregate rows → ledger accounts → never the reverse. No code path locks an aggregate after an account.
6. **Two-phase** (holds): pending entries affect pending columns; post/void moves or releases them.
7. **Idempotency**: `UNIQUE(project_id, idempotency_key)` on entries; a replay returns the existing entry if the payload hash matches, else conflict. Keys are derived from the business fact, never from the delivery (`payment:{pp}:captured`, `refund:{rf}:posted`, `payout:{pay}:pending`).
8. Every entry stores `recipe`, `correlation_id`, `causation_id` (event id), `effective_at`, `metadata` (no PII).
9. A verification job recomputes balances from postings and alerts on drift (metric + reconciliation break, ADR-0013).

## Consequences
+ O(1) balance reads, invariants enforced by the database even against buggy application code, deadlock-free lock order. − Hot accounts (e.g. `platform_revenue`) serialize writers; acceptable at portfolio scale; documented mitigation: sharded sub-accounts or deferred aggregation (*Planned*). − Some ledger logic lives in SQL (`ledger_apply`, triggers); covered by integration tests against real Postgres.

## Alternatives
Computed balances via `SUM` (simple, slow at scale); TigerBeetle/Formance as external ledger (strong, but adds infra and hides the modeling we want to demonstrate); event-sourced balances only (complex reads).
