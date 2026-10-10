# ADR-0004: Idempotency strategy

- Status: Accepted · Date: 2026-10-10

## Context
Products retry, providers redeliver webhooks at-least-once, workers crash mid-delivery. Every money-affecting path must be safe to repeat.

## Decision
| Layer | Mechanism |
|---|---|
| HTTP API (mutations by API key) | `Idempotency-Key` header required on POST that creates financial objects. Table `idempotency_records(project, key, request_hash, status, response, expires_at)`. In-flight duplicate → 409; same key different payload → 422; completed → stored response replayed. TTL 24h. |
| Usage ingestion | `UNIQUE(project_id, transaction_id)`; duplicates acknowledged as `duplicate`. |
| Inbound webhooks | `UNIQUE(provider, provider_event_id)` on receipts; processing state per receipt. |
| Internal event handlers | Inbox table `UNIQUE(handler, event_id)` written in the handler's transaction. |
| Ledger | `UNIQUE(project_id, idempotency_key)` on journal entries, keys derived deterministically (e.g. `payment:{pp_id}:confirmed`). |
| Provider calls (Asaas has no idempotency header) | Money-movement protocol of ADR-0012 §3: commit state + fencing token first, `externalReference = intent/refund id`, unknown outcome resolved by lookup before any retry; transfers never auto-retried and gated by withdrawal validation. |

## Consequences
+ End-to-end exactly-once *effects* over at-least-once delivery. − Extra tables and key-derivation discipline (enforced in code review and tests).

## Alternatives
Client-only retries without server dedup (unsafe); distributed locks (do not survive crashes).
