# ADR-0005: Event log as transactional outbox, Postgres-only messaging

- Status: Accepted · Date: 2026-10-10 · Revised: 2026-10-10 (review findings F-22, F-23)

## Context
We need reliable internal reactions, outbound webhooks, scheduled jobs, replay and an Event Explorer — without dual-write bugs. Options compared in `docs/research/stack-evaluation.md` §2 (Redis Streams, Dramatiq, Celery, ARQ, Procrastinate, Taskiq, NATS, pure Postgres).

## Decision
- `events` table is both the **event log** (queryable history for the Explorer and SSE replay) and the **outbox**: rows are inserted in the same transaction as the business change.
- `event_subscriptions` (internal handler or webhook endpoint, with type filters) and `event_deliveries` (one per event × subscription) drive work. **Fan-out happens in the business transaction**: subscriptions are read `FOR SHARE` and deliveries inserted together with the event, so a subscription change cannot race an in-flight event.
- **Dispatcher** (worker): claims deliveries with `FOR UPDATE SKIP LOCKED`, sets `lease_until` and a fresh `lease_token`, executes, then completes with a **compare-and-set** `WHERE id = :id AND lease_token = :token`. A worker whose lease expired loses the CAS and discards its result (fencing). Retries use exponential backoff + jitter; after max attempts → `dead` (DLQ). Replay resets a delivery to `pending`.
- A lease protects only *our* rows. Handlers that call external systems follow the money-movement protocol of ADR-0012 (state CAS before the call, lookup before retry); a lease expiry must never cause a second external side effect.
- Internal handlers are idempotent via an **inbox** table (ADR-0004), written in the handler's transaction.
- **Ordering and cursors**: UUIDv7 order is not commit order. Every event stores `txid xid8 DEFAULT pg_current_xact_id()`. Readers (SSE replay, Explorer cursors, polling) only return events with `txid < pg_snapshot_xmin(pg_current_snapshot())` — the visibility horizon — and page by `(txid, id)`. A late-committing transaction can therefore never be skipped.
- **Wake-ups**: `NOTIFY` is a hint, never the source of truth. It is sent by the dispatcher/after-commit hook **outside** the business transaction (a full notification queue must never fail a financial commit); consumers always poll as fallback. Metric + alert on `pg_notification_queue_usage()`.
- **Scheduler**: a loop holding `pg_try_advisory_lock` emits due events (`billing.period_close_due`, `reconciliation.run_due`) based on persisted schedules; idempotent by `(schedule, due_at)`.
- Envelope fields follow CloudEvents naming where sensible (`id`, `type`, `source`, `subject`, `time`, `specversion`-like `version`) plus `correlation_id`, `causation_id`, `project_id`, `traceparent`. Event types come from one **canonical event registry** in code (`modules/events/registry.py`) that docs and the webhook catalog are generated from.
- Procrastinate remains an acceptable swap-in for the job runner (same Postgres); not adopted initially to keep the dispatcher semantics explicit and demonstrable.

## Consequences
+ One datastore, transactional consistency, full replayability, no skipped events, fenced workers. − Throughput bounded by Postgres (thousands/sec — sufficient); events visible to readers with a small delay while long transactions are open; table growth needs partitioning/retention (*Planned*: monthly partitions).

## Alternatives
Redis/NATS broker + outbox relay (more moving parts, still needs outbox); Celery (sync-first, heavy); ARQ (maintenance-only); ordering by a `BIGSERIAL` (same commit-order gap problem).
