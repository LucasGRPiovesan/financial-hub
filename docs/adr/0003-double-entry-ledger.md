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

## Addendum (2026-10-10, W2.1 implementation, migration `0001_ledger_core`)

Decisions taken while implementing the ledger core; they refine, not replace, the decision above.

1. **Same idempotency key, different payload → `IdempotencyKeyReused` (HTTP 422)**, not `Conflict`.
   The payload hash covers project, recipe, initial status, `effective_at`, `reverses_entry_id` and
   the multiset of `(account code, currency, direction, amount)`; delivery data
   (`correlation_id`, `causation_id`, `metadata`) is excluded so a redelivered fact replays. The
   check runs inside one batch and against stored entries (under the account locks).
2. **Locking without `UPDATE`**: `SELECT … FOR UPDATE` needs `UPDATE` privilege, which `fh_app`
   must not have on `ledger_accounts`. The lock step (§5.2) therefore calls
   `ledger_lock_accounts(uuid[])`, a `SECURITY DEFINER` SQL function owned by `fh_owner` that only
   locks (`ORDER BY id FOR UPDATE`) and returns the rows. Hardening: fixed
   `search_path = pg_catalog, public, pg_temp`, `REVOKE ALL … FROM PUBLIC`, `EXECUTE` granted only
   to `fh_app`. `ledger_apply(uuid)` is the other `SECURITY DEFINER` function, hardened the same way.
3. **Roles**: a `NOLOGIN` role `fh_owner` owns every ledger object (it is granted to the migration
   login, which runs DDL under `SET ROLE fh_owner`); `fh_app` is `LOGIN NOINHERIT`. Both are created
   idempotently by the migration; its downgrade revokes, `DROP OWNED` and drops them. The `fh_app`
   password comes from `FH_APP_DB_PASSWORD` (env), never from code. Owner credentials never reach
   the runtime containers (`api`, `worker`), even in dev; only `migrate` and the profile-`test`
   service get them (DEVELOPMENT.md, "Database roles").
4. **`fh_app` grants** (exhaustive): `SELECT` on the three tables; column-level `INSERT` on
   `ledger_accounts` (identity and flags, never balances or `version`) and `journal_entries` (never
   `applied_status`); `INSERT` on `ledger_postings`; `UPDATE(status)` on `journal_entries`;
   `EXECUTE` on `ledger_apply`, `ledger_lock_accounts` and `ledger_assert_entry` (called by the
   deferred trigger). Also revoked from `PUBLIC`: `CREATE` on schema `public` and `TEMPORARY` on the
   database.
5. **"Balances changed by exactly the entry's postings" (§2) is implemented as `applied_status`**:
   only `ledger_apply` can write balance columns (privileges) and it records the status it applied
   in `journal_entries.applied_status` (not writable by `fh_app`; a trigger also rejects writes
   outside `ledger_apply`). The deferred check requires `applied_status = status` at commit, so an
   entry inserted or transitioned without `ledger_apply` cannot commit. `ledger_apply` computes the
   deltas itself from the stored postings (`pending/posted` factors per transition), so the applied
   amounts are exact by construction. The `fh.ledger_apply` setting checked by the guard trigger is
   defense in depth only; setting it grants nothing to `fh_app`.
6. **Deferred check, extended**: besides ≥ 2 postings, balance per currency and project/currency
   consistency, it re-checks `no_overdraft` (`available >= 0`) at commit. Postings can only join an
   entry before it is applied; a new entry starts `pending` or `posted`.
7. **One locking write per unit of work**: `LedgerService.post_entries` (and post/void) refuse a
   second ledger write in the same unit of work (`RuntimeError`), because a second lock phase could
   lock accounts out of id order. Callers collect every entry of the UoW and post once.
