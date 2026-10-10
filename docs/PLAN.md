# Financial Hub — Master Plan

> Status: **planning baseline v1** (2026-10-10). Owner: Tech Lead (Claude Opus 5.5).
> Tracker of record: Linear team `PRO`. This file is the *why/what*; Linear is the *who/when/status*.

## 1. Vision

**Products produce business facts. Financial Hub produces financial truth.**

Financial Hub is the financial system of record for N integrated products (e.g. *HealthCheck*, *MyAIHub*).
Products send business facts (a consultation happened, 12k AI tokens were consumed, a plan was purchased).
The Hub turns them — deterministically and auditably — into money movements:

```
Business/Usage Event → Metering → Pricing → Charge → Invoice/Obligation → Payment → Ledger → Settlement → Reconciliation
```

Every number shown in the UI must be derivable from immutable ledger postings and traceable back to the
originating business event (correlation/causation chain).

### Non-goals (explicit)
- Not a real PSP, not a tax/fiscal engine (NF-e), not a general accounting ERP.
- No production payment credentials. Asaas runs in **Sandbox** only.
- AI never moves money and never activates configuration without human approval.

## 2. Scope matrix

Every feature in the demo is labeled with one of four badges. The UI and README must show these labels.

| Label | Meaning |
|---|---|
| **Implemented** | Real code path, tested end-to-end against our own stack. |
| **Sandbox** | Real integration against a third-party sandbox (Asaas only). |
| **Simulated** | Deterministic simulator/fixture standing in for something external (demo products, AI fixture mode). Clearly labeled. |
| **Planned** | Architected and documented (ADR/interface), not implemented. No fake UI. |

| Capability | Label | Notes |
|---|---|---|
| Double-entry ledger, holds, reversals, trial balance | Implemented | Core; DB-enforced invariants |
| Universal customer + per-project identities | Implemented | |
| Projects, API keys, RBAC, audit log | Implemented | |
| Event log / outbox / inbox / scheduler | Implemented | Postgres-only |
| Outbound webhooks (Standard Webhooks, retries, DLQ, replay, rotation) | Implemented | |
| Realtime (SSE + LISTEN/NOTIFY) | Implemented | |
| Pricing: per-unit, package, tiered graduated, volume, percentage, base fee + usage, minimum | Implemented | Pure functions, property-tested |
| Usage ingestion + metering (sum/count/max/unique) | Implemented | Idempotent on `transaction_id` |
| One-off charges, invoices, period close | Implemented | |
| Prepaid wallet / credits (wallet-first) | Implemented | |
| Subscriptions (fixed recurring + usage in arrears) | Implemented | Own engine; no Asaas subscriptions |
| Asaas payments (PIX/BOLETO/CARD), refunds, webhooks | Sandbox | |
| Split: internal split (ledger) | Implemented | |
| Split: provider split (Asaas walletId) | Sandbox | Requires sandbox subaccounts |
| Payouts (Asaas transfers) | Sandbox | |
| Reconciliation (ledger vs provider statement) | Implemented + Sandbox | Matching engine is ours; statement is Asaas |
| Integration Manifest (versioned, approval, rollback) | Implemented | |
| MCP server (onboarding/config/read-only) | Implemented | |
| AI Integration Analyst | Implemented (live key) / Simulated (fixture mode) | |
| Demo products (MyAIHub, HealthCheck) | Simulated | Simulator container |
| Stripe, Mercado Pago providers | Planned | Port exists; adapters "not enabled" |
| Multi-currency FX conversion | Planned | Ledger is currency-aware; no FX |
| Dunning automation, tax | Planned | |
| Postgres RLS | Planned (ADR) | App-level scoping + isolation tests now |

## 3. Architecture summary

Full detail: `docs/ARCHITECTURE.md`, `docs/DOMAIN.md`, `docs/FINANCIAL_MODEL.md`, ADRs in `docs/adr/`.

- **Style:** modular monolith, one deployable API + one worker process (same image, different entrypoint).
- **Backend:** Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2.1 (async, psycopg 3), Alembic, PostgreSQL 18.
- **Frontend:** React 19, TypeScript, Vite, TanStack Query + Router, Tailwind 4 + shadcn/ui, orval-generated client.
- **Async infra:** Postgres only. Event log = outbox (same transaction). Generic dispatcher with
  `FOR UPDATE SKIP LOCKED`, leases, exponential backoff, DLQ, replay. Scheduler loop under advisory lock. No Redis.
- **Realtime:** SSE; fan-out via Postgres `LISTEN/NOTIFY`; `Last-Event-ID` replay from the event log.
- **Observability:** structlog JSON + OpenTelemetry (traces/metrics/logs) → `grafana/otel-lgtm`.
- **Security:** Argon2id operator auth, server-side sessions (cookie) + CSRF, RBAC; API keys
  `fh_<env>_<prefix>_<secret>` stored as HMAC-SHA256 with pepper; secrets AES-GCM with versioned keys;
  RFC 9457 errors; project scoping on every query.

### Repo layout
```
backend/
  src/financial_hub/
    shared/            # Money, ids (TypeID+UUIDv7), errors, clock, pagination
    platform/          # db/uow, settings, logging, telemetry, security primitives, http middleware
    modules/<context>/{domain,application,infrastructure,api}
    workers/           # dispatcher, scheduler entrypoints
    mcp/               # MCP server
  migrations/          # Alembic (single linear history)
  tests/{unit,integration,contract,property}
frontend/              # Vite app
e2e/                   # Playwright
demo/                  # product simulator + seed scenarios
deploy/                # compose files, otel config, grafana dashboards
docs/                  # this file, architecture, ADRs, research
```
Module boundaries enforced by **import-linter** contracts (modules talk via application services or events,
never via each other's infrastructure). Layering is pragmatic: rich domain only in ledger, payments, billing,
pricing; thin CRUD elsewhere.

### Bounded contexts
identity · projects · customers · catalog/pricing · metering · billing · payments · ledger · wallets ·
settlements/payouts · reconciliation · integrations (manifest) · events · webhooks · realtime · audit · intelligence (AI + MCP)

## 4. Core financial decisions (to be ratified as ADRs in M0)

| # | Decision | ADR |
|---|---|---|
| 1 | Money = integer minor units (BIGINT) + ISO 4217 currency; `Decimal` only for rates/unit prices; quantize once per charge line, ROUND_HALF_EVEN; splits by largest remainder | ADR-0002 |
| 2 | Ledger: accounts / journal entries / postings; append-only (DB triggers); debits = credits per currency (deferred constraint trigger); corrections by reversal entries only | ADR-0003 |
| 3 | Balances: cached posted/pending totals on account rows updated in the same tx under the ADR-0003 §5 lock protocol (aggregate row → union of accounts in id order); `available` per normal balance as defined in FINANCIAL_MODEL §2; nightly/on-demand recompute check | ADR-0003 |
| 4 | Two-phase holds via pending entries (post or void) | ADR-0003 |
| 5 | Idempotency everywhere: `Idempotency-Key` on mutating APIs; `idempotency_key` unique per project on journal entries; usage `transaction_id`; inbound webhook event id dedup | ADR-0004 |
| 6 | Event log is the outbox; one dispatcher for internal handlers and outbound webhooks; at-least-once + idempotent consumers (inbox table) | ADR-0005 |
| 7 | Provider-agnostic payment model: PaymentIntent → ProviderPayment(attempt) → normalized ProviderEvent; Asaas has no idempotency header → persist intent first, `externalReference = intent id`, lookup before retry | ADR-0006 |
| 8 | Own billing engine produces one-off provider charges; no provider subscriptions | ADR-0007 |
| 9 | Integration Manifest: versioned JSON schema, draft → validated → approved → active, rollback = activate previous version; runtime is deterministic | ADR-0008 |
| 10 | AI boundary: NL → AI → structured config → schema validation → human approval → versioned config → deterministic runtime. MCP exposes no money-moving tools | ADR-0009 |
| 11 | Realtime via SSE + LISTEN/NOTIFY (not WebSockets) | ADR-0010 |
| 12 | Tenancy: app-level project scoping + isolation test suite; RLS deferred | ADR-0011 |
| 13 | IDs: TypeID-style `prefix_` + UUIDv7; RFC 9457 problem+json; cursor pagination | ADR-0001 |

## 5. Milestones

Each milestone = a Linear parent issue ("epic"). Sub-issues carry acceptance criteria and owner agent/model.

| M | Name | Goal / exit criteria |
|---|---|---|
| M0 | Foundations | Docs+ADRs, repo scaffold, Docker env (`docker compose up` → healthy), shared kernel, CI green |
| M1 | Identity, Projects, Customers | Operators log in, RBAC, projects + API keys, universal customer, audit, idempotency + rate limit |
| M2 | Ledger | Property-tested double-entry core, wallets & chart of accounts, trial balance API |
| M3 | Events & Realtime | Outbox/dispatcher/inbox/scheduler, SSE, Event Explorer API |
| M4 | Payments (Asaas Sandbox) | Intent → Asaas charge → webhook → ledger; refunds; split; payouts |
| M5 | Billing | Pricing engine, metering, charges/invoices, wallet-first credits, subscriptions |
| M6 | Integrations | Manifest v1 + approval/rollback, mapping runtime + dry-run, outbound webhooks, SDK + demo simulator |
| M7 | Intelligence | MCP server, AI Integration Analyst (live + fixture) |
| M8 | Operations | Reconciliation engine, chaos/concurrency suite, dashboards |
| M9 | Frontend | Design system + all operator screens (runs in parallel from M1) |
| M10 | Quality & Portfolio | E2E, security review/threat model, seed & demo scenarios, docs, Portfolio Readiness Review |

### Critical path
```
0.2 → 0.4 → {1.x, 2.1, 3.1} → 4.1 → 4.2 → 4.3 → 4.4 → 8.1 → 10.x
                       5.1 → 5.2 → 5.3 → {5.4, 5.5}
                       5.1 → 6.1 → 6.2 → 7.x
```

## 6. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Free/cheap workers produce subtly wrong financial code | High | Financial core (ledger, payments, pricing, auth) authored or reviewed line-by-line by Opus; property tests; independent review agent |
| Asaas sandbox behaviors not as documented (UNVERIFIED items in R1 §9.3) | Med | Spike in 4.2 against real sandbox; contract tests with recorded fixtures; adapter isolates quirks |
| Bleeding-edge toolchain (Py 3.14, SQLAlchemy 2.1, TS 7, Vite 8) incompatibilities | Med | 0.2 spike validates toolchain first; pin exact versions; fallback TS 5.x/Py 3.13 documented |
| Concurrency bugs in balances/dispatcher | High | Lock ordering, SKIP LOCKED, concurrency tests in 8.2, DB-level invariants as last line of defense |
| Scope creep (billing models × providers × UI) | High | Scope matrix above; "Planned" is a legitimate, honest state |
| Parallel workers create conflicting Alembic heads | Med | Single linear migration chain; Tech Lead merges and re-parents revisions |
| Secrets leakage (Asaas key, AI key) | High | `.env` git-ignored, settings `SecretStr`, log redaction processor, gitleaks in CI |
| Docker Desktop on Windows host performance (bind mounts) | Low | Named volumes for deps; hot reload via watch |

## 7. Strategies

### 7.1 Testing
- **Unit:** domain + pricing pure functions (pytest), Hypothesis property tests for Money/allocation/ledger invariants/pricing.
- **Integration:** real Postgres 18 (compose service or testcontainers), transaction-per-test rollback; API tests via httpx ASGI.
- **Contract:** Asaas adapter vs recorded sandbox fixtures (VCR-style) + JSON schema of inbound webhooks; outbound webhook signature conformance with Standard Webhooks test vectors; OpenAPI snapshot diff.
- **Isolation:** suite asserting project A can never read/write project B (API + repository level).
- **Concurrency/chaos:** parallel postings on same account, duplicate/out-of-order webhooks, worker crash mid-lease, endpoint 5xx storms, DB restart.
- **E2E:** Playwright against the composed stack with seed data (automated regression suite).
- **Manual QA as an end user:** before a milestone is Done, the Tech Lead drives the real UI in a browser through Playwright (MCP browser session: navigate, click, type, read the screen, screenshots) following the user journeys, exactly as an operator would — no API shortcuts. Findings become issues; evidence (screenshots) is attached to the milestone review.
- **Gates:** ruff, pyright strict, import-linter, pytest with coverage floor (domain ≥ 90%, overall ≥ 80%), tsc, eslint, vitest.

### 7.2 Docker
- Host runs **only Docker**. All Python/Node tooling runs in containers (`make`/`just`-free: compose profiles + scripts in `deploy/`).
- Services: `db` (postgres:18), `migrate` (one-shot), `api`, `worker`, `web`, `lgtm` (grafana/otel-lgtm:0.36.0), `simulator` (profile `demo`), `seed` (one-shot).
- Multi-stage images with uv, non-root user, healthchecks, `depends_on: condition: service_healthy`.
- Dev: compose `watch` / bind-mount + reload. Test: `docker compose run --rm api pytest`.

### 7.3 Security
OWASP ASVS L2 as checklist. Argon2id, session fixation protection, CSRF, secure cookies, RBAC (owner/admin/finance/viewer),
API-key scopes (`ingest`, `read`, `mcp`, `admin`), per-key rate limits, SSRF guard on webhook endpoints, AES-GCM secrets
with key versioning, webhook signature verification (inbound shared token, outbound HMAC), audit log for every privileged action,
dependency scanning (pip-audit, npm audit) and gitleaks in CI. Threat model in `docs/SECURITY.md` (STRIDE).

### 7.4 Observability
Correlation id middleware → structlog context → OTel span attributes → event envelope (`correlation_id`, `causation_id`).
Traces across API → event log → dispatcher → webhook delivery. Metrics: postings/sec, dispatcher lag, DLQ size,
webhook success rate, reconciliation breaks. Grafana dashboards provisioned from `deploy/grafana/`.

## 8. Orchestration strategy

Coordinator: Claude Opus 5.5 (this session) in the **local** Orca runtime only (never the company remote environment).

| Work type | Agent / model | Effort |
|---|---|---|
| Architecture, ADRs, financial core (ledger, payment state machine, pricing), security-critical code | Opus (me) or `claude --model opus` worker + my line review | high |
| Well-specified modules (CRUD contexts, adapters, API layers), frontend screens | `claude --model sonnet` | medium/high |
| Mechanical: scaffolds, docs polish, fixtures, renames | `opencode` (dev-worker) or `claude --model haiku` | — / low |
| Research, analysis, root-cause investigation | `opencode2` (reasoning-worker) | — |
| Independent cross-check of plans/diffs | `free-second-opinion` subagent + my own review | — |
| QA of a task | an agent different from its implementer | medium |

Rules:
- Every task spec: Target / Change / Constraints / Ownership / Acceptance.
- `worker_done` is a claim: I read the diff, run acceptance myself, and only then release and mark Done in Linear.
- Parallel workstreams use child worktrees; I merge and fix the migration chain.
- Linear status mirrors reality: In Progress when dispatched, Done only after implemented + tested + reviewed.

## 9. Review gates

Each milestone closes only after the applicable gates pass (recorded as a Linear comment on the epic):

| Gate | Checks |
|---|---|
| Architecture | Module boundaries (import-linter), ADR conformance, no cross-context infra access |
| Domain | Invariants tested, terminology matches DOMAIN.md, state machines complete |
| Security | Authz on every route, project scoping, secrets handling, input validation |
| Data | Migrations reversible, constraints/indexes, money columns BIGINT, no float |
| Testing | Coverage floors, property tests for financial math, flaky-free on 3 reruns |
| Integration | Contract tests green, idempotency verified, webhook dedup |
| UX | Labels (Implemented/Sandbox/Simulated/Planned), empty/error/loading states, a11y basics |
| Portfolio | README clarity, demo runnable in one command, honest claims |

Final: **Portfolio Readiness Review** by four personas — Staff Engineer (architecture), Fintech Domain Expert
(ledger/payments correctness), Security Reviewer, Hiring Manager (clarity & impact).

## 10. Prerequisites from the owner (blocking execution, not planning)

1. Start Docker Desktop.
2. Asaas Sandbox API key → provided via local `.env` only (never in chat logs, commits or Linear).
3. Create Linear project "Financial Hub" in team PRO (CLI cannot create projects); I will attach issues.
4. Decide on a GitHub remote (optional; CI in M0.5 needs it to run).
