# Architecture

> Companion docs: [PLAN](PLAN.md) · [DOMAIN](DOMAIN.md) · [FINANCIAL_MODEL](FINANCIAL_MODEL.md) ·
> [INTEGRATIONS](INTEGRATIONS.md) · [SECURITY](SECURITY.md) · [DECISIONS](DECISIONS.md)

## 1. Context

```
            business facts (HTTP + API key)                 provider events (webhooks)
 ┌───────────────┐   ┌──────────────┐                        ┌──────────────────┐
 │  HealthCheck  │   │   MyAIHub    │  … N products          │  Asaas Sandbox   │
 └──────┬────────┘   └──────┬───────┘                        └────────┬─────────┘
        │  usage / business events, customers               charges, refunds,│
        ▼                                                   transfers, statement
 ┌─────────────────────────────────────────────────────────────────────┴──────────┐
 │                               Financial Hub                                    │
 │  API (FastAPI) ── modules ── PostgreSQL (state + event log + ledger)           │
 │  Worker (dispatcher, scheduler)        MCP server         SSE realtime         │
 └───────┬──────────────────────────────────────┬──────────────────────┬──────────┘
         │ signed outbound webhooks             │ Operator Console     │ AI clients (MCP)
         ▼                                      ▼ (React)              ▼
   product backends                       finance operators      integration engineers
```

## 2. Runtime topology

One image, three entrypoints, one database:

| Process | Role |
|---|---|
| `api` | FastAPI app: REST, SSE, inbound webhooks, MCP (mounted, streamable HTTP) |
| `worker` | Dispatcher loop (event deliveries: internal handlers + outbound webhooks) and scheduler loop (period close, renewals, reconciliation runs) |
| `migrate` | One-shot Alembic upgrade |

PostgreSQL 18 is the only stateful dependency. There is no broker: the **event log is the outbox** (ADR-0005).
Horizontal scale: `api` and `worker` are stateless and can run N replicas (SKIP LOCKED + leases; advisory lock for scheduler leadership; LISTEN/NOTIFY for SSE fan-out).

## 3. Module map (bounded contexts)

| Module | Owns | Rich domain? |
|---|---|---|
| `identity` | operators, sessions, roles, memberships | no |
| `projects` | projects, API keys, provider accounts (encrypted credentials), project settings | no |
| `customers` | FinancialCustomer, CustomerIdentity, provider customer refs | light |
| `ledger` | accounts, journal entries, postings, balances, lock protocol; **`posting_rules` facade** implementing every recipe of FINANCIAL_MODEL §4 (the only code that builds entries) | **yes** |
| `wallets` | wallet views, top-up and consumption use cases (call `ledger.posting_rules` R6/R7) | light |
| `pricing` | price models, price versions, rating | **yes** (pure) |
| `metering` | billable metrics, usage events, aggregation | light |
| `billing` | charges, invoices, credit notes, subscriptions, period close | **yes** |
| `payments` | payment intents, provider payments, provider events, refunds, disputes, payouts; provider port + adapters | **yes** |
| `reconciliation` | runs, statements, matches, breaks, resolutions (ADR-0013) | yes |
| `integrations` | manifests, versions, approvals, mapping runtime, dry run | yes |
| `events` | event log, subscriptions, deliveries, inbox, scheduler | infra |
| `webhooks` | outbound endpoints, secrets, delivery attempts; inbound receipt store | infra |
| `realtime` | SSE streams | infra |
| `audit` | audit records | no |
| `intelligence` | AI Integration Analyst, MCP tools | no (orchestration only) |

### Dependency rules (import-linter)
1. `shared` depends on nothing internal; `platform` depends on `shared`.
2. A module's `domain` depends only on `shared`. `application` may depend on its own domain and on **other modules' `application` public facades** (`modules/<x>/application/api.py`) — never on another module's `domain`, `infrastructure` or `api`.
3. `api` (HTTP) layers depend on their own `application` only.
4. Asynchronous coupling (preferred across contexts) goes through the event log.
5. `intelligence` may only call read facades and integration-manifest commands (enforced by a contract: no imports of `payments`/`ledger` write facades).

## 4. Key flows

### 4.1 Usage → invoice → payment → ledger
1. Product posts usage (`POST /v1/usage-events`, idempotent on `transaction_id`).
2. `metering` stores events; at period close the scheduler emits `billing.period_close_due`.
3. `billing` aggregates, calls `pricing.rate()`, applies wallet-first credits (R7), finalizes invoice (R1), emits `invoice.finalized`.
4. `payments` handler creates a PaymentIntent and an Asaas charge (`externalReference = intent id`).
5. Asaas webhook → `POST /webhooks/inbound/asaas/{provider_account}` → durable receipt (200) → `payments` checks project binding, **refetches** the payment, applies the state transition (ADR-0012) → `payment.confirmed` → ledger R2+R3 in the same transaction, under the payment row lock → `payment.settled` later → R4.
6. Every step writes events; outbound webhook subscriptions notify the product; SSE pushes to the console.

### 4.2 Transaction boundaries
A command handler runs in one DB transaction (Unit of Work) that includes: state change + ledger entry (if any) + event-log rows.
External HTTP calls (Asaas) never happen inside a DB transaction. Money-moving calls follow ADR-0012 §3: commit state + fencing token → call provider → CAS-record the result; unknown outcomes are resolved by lookup, transfers are never auto-retried. Lock order inside a UoW: aggregate row → ledger accounts (ADR-0003 §5).

## 5. Cross-cutting

| Concern | Approach |
|---|---|
| IDs | `prefix_` + UUIDv7 (`cus_`, `inv_`, `pi_`, `je_`, `evt_`, …) (ADR-0001) |
| Errors | RFC 9457 problem+json, stable `type` URIs, `errors[]` with JSON pointers |
| Pagination | cursor (opaque, keyset on `(created_at, id)`) |
| Idempotency | `Idempotency-Key` header; domain keys (ADR-0004) |
| Time | `Clock` port; UTC `timestamptz`; billing periods use project timezone |
| Config | pydantic-settings; secrets as `SecretStr`; per-env `.env` |
| Logging | structlog JSON; redaction processor; correlation id bound per request/delivery |
| Tracing | OTel FastAPI + SQLAlchemy + httpx instrumentation; trace context stored in event envelope and restored by dispatcher |
| Tenancy | Every row with `project_id`; repositories require a `ProjectScope`; isolation test suite (ADR-0011) |

## 6. Frontend

React 19 + Vite SPA served by `web` (nginx in prod image, Vite dev server in dev). TanStack Router (typed routes),
TanStack Query (server state), orval-generated client from the OpenAPI schema, SSE hook that invalidates queries by event type.
Cookie session auth with CSRF header. Every capability displays its scope label (Implemented / Sandbox / Simulated / Planned).
