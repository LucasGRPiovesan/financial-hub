# Financial Hub — Stack Evaluation & Infra Options (Task R3)

Date of research: 2026-10-10. Author: dispatched worker (task_e4661f0cb91f). Scope: read-only research, one deliverable.

## 0. Method, evidence levels, caveats

- **Versions**: queried directly from the registries on 2026-10-10 — PyPI JSON API (`https://pypi.org/pypi/<pkg>/json`), npm registry (`https://registry.npmjs.org/<pkg>`), GitHub releases API, Docker Hub tags API, endoflife.date. These are primary sources and are marked **[V]** (verified).
- **Maintenance/behavioural claims**: taken from upstream READMEs / docs on `raw.githubusercontent.com` (marked **[V-doc]** with the repo), or from general knowledge, marked **UNVERIFIED**.
- WebSearch results were thin and partly contradictory (e.g. aggregator pages disagreed on FastAPI/MCP latest versions); where they conflicted with registry data, **registry data wins** and the aggregator claim is discarded.
- Context7 MCP was loaded but, to keep this read-only pass economical, library facts were taken from upstream READMEs/changelogs fetched directly. Anything needing API-level confirmation before coding is flagged "confirm via Context7 at implementation time".
- No code in this document beyond tiny config snippets. Pinning policy: pin exact versions in `pyproject.toml`/`uv.lock` and `package.json`/lockfile; the table is a *snapshot*, not a promise.

---

## 1. Stack versions (as of 2026-10-10)

### 1.1 Backend / tooling

| Component | Latest stable | Released | Requires | Source | Notes |
|---|---|---|---|---|---|
| CPython | **3.15.0** (new, 2026-10-09); **3.14.8** (2026-09-30); 3.13.16 (2026-09-30) | — | — | [V] endoflife.date/api/python.json | 3.15.0 landed yesterday → too new for libs. 3.14 EOL 2030-10; 3.13 EOL 2029-10 |
| FastAPI | **0.143.0** | 2026-10-08 | py>=3.10 | [V] PyPI, GitHub releases | Still 0.x → minor bumps may break; pin `~=0.143`. Classifiers 3.12–3.14 |
| Starlette | 1.7.0 | 2026-09-23 | py>=3.10 | [V] PyPI | Starlette is now 1.x (FastAPI's base) |
| Uvicorn | 0.54.0 | 2026-09-25 | py>=3.10 | [V] PyPI | 3.12–3.14 classifiers |
| Pydantic | **2.14.0** | 2026-10-08 | py>=3.10 | [V] PyPI | Classifiers up to 3.15; pydantic-core has 3.12–3.14 wheels |
| SQLAlchemy | **2.1.4** | 2026-10-07 | **py>=3.11** | [V] PyPI | **2.1 is now the stable line** (not 2.0.x). Needs Python >= 3.11. Classifiers to 3.15. Review 2.0→2.1 migration notes before adopting (UNVERIFIED which APIs changed) |
| Alembic | **1.20.0** | 2026-09-11 | py>=3.10 | [V] PyPI | Confirm SQLAlchemy 2.1 compatibility in its changelog at implementation time |
| psycopg (v3) | **3.3.6** | 2026-09-18 | py>=3.10 | [V] PyPI | Classifiers to 3.15; native sync+async in one package |
| asyncpg | **0.32.0** | 2026-10-06 | py>=3.9 | [V] PyPI | 3.14 support since 0.31.0 per search snippet (releasebot); classifiers 3.12–3.14 |
| uv | **0.13.0** | 2026-10-09 | — | [V] PyPI/GitHub | Docker docs pin to specific tag (see 1.3) |
| Ruff | **0.17.0** | 2026-10-09 | — | [V] PyPI | 0.x; pin exact |
| mypy | **2.4.0** | 2026-10-01 | py>=3.10 | [V] PyPI | mypy is 2.x now |
| pyright | **1.1.414** (PyPI wrapper) | 2026-09-10 | — | [V] PyPI | PyPI package wraps the Node tool |
| pytest | 9.1.1 | 2026-06-19 | py>=3.10 | [V] PyPI | |
| pytest-asyncio | 1.4.0 | 2026-05-26 | py>=3.10 | [V] PyPI | 1.x line (post-0.2x API cleanup); set `asyncio_mode = "auto"` |
| testcontainers (Python) | 4.15.0 | 2026-07-24 | py>=3.10 | [V] PyPI | Needs Docker socket — see 1.5 |
| structlog | 26.1.0 | 2026-06-06 | py>=3.10 | [V] PyPI | CalVer; classifiers to 3.15 |
| opentelemetry-api / sdk | **1.45.1** | 2026-10-06 | py>=3.10 | [V] PyPI | |
| opentelemetry-instrumentation-fastapi / -sqlalchemy | **0.66b1** | 2026-10-06 | py>=3.10 | [V] PyPI | Instrumentation packages are still **beta (`b`)** by version scheme. Must be version-aligned with API/SDK 1.45.x |
| mcp (official MCP Python SDK) | **2.3.0** | 2026-10-02 | py>=3.10 | [V] PyPI, GitHub releases | **v2.x is current stable.** Package name: `mcp`. README: `uv add "mcp[cli]"`; "speaks stdio, Streamable HTTP, and SSE" [V-doc modelcontextprotocol/python-sdk README]. v1.x is reportedly security-fix-only (search snippet, UNVERIFIED on GitHub) |
| anthropic (Python SDK) | **1.13.0** | 2026-10-09 | py>=3.10 | [V] PyPI | 1.x |
| argon2-cffi | 25.1.0 | 2025-06-03 | py>=3.8 | [V] PyPI | Stale release date but stable/low-churn library |
| Authlib | 1.8.0 | 2026-08-30 | py>=3.10 | [V] PyPI | |
| fastapi-users | 15.0.5 | 2026-03-27 | py>=3.10 | [V] PyPI | **Maintenance mode** [V-doc README] — see §5 |
| PostgreSQL | **18.6** (supported until 2030-11-14); 17.11 | — | — | [V] endoflife.date, Docker Hub `postgres:18` | Use `postgres:18` (or pin `18.6`) |

### 1.2 Python 3.13 vs 3.14 — recommendation

Evidence ([V] PyPI classifiers, 2026-10-10): FastAPI, Pydantic, pydantic-core, SQLAlchemy, psycopg, asyncpg, Uvicorn, mcp, anthropic, Dramatiq, ARQ, Taskiq, structlog, OpenTelemetry API, uv, Ruff all declare **3.12, 3.13 and 3.14**. **Celery declares only 3.12–3.13** (its 5.6 "what's new" lists CPython 3.9–3.13 [V-doc celery docs]) — a mild argument against Celery on 3.14. Procrastinate declares no version classifiers (UNVERIFIED; requires >=3.10).

Python 3.15.0 was released 2026-10-09 [V endoflife.date]; only Pydantic/SQLAlchemy/psycopg/structlog/uv/Ruff declare 3.15 so far. **Do not use 3.15** for the foundation.

**Recommendation: Python 3.14 (`python:3.14-slim` image, pinned to patch or digest)**, since every core dependency declares 3.14 and it is supported until 2030-10. 3.13 is an acceptable fallback if a transitive dependency fails to build; both are within EOL. Free-threaded (`3.14t`) builds: **not recommended** (C-extension wheel compat is the known blocker; UNVERIFIED for individual libs). Enforce with `requires-python = ">=3.14,<3.15"` and run CI on 3.14 only (optionally 3.13 as canary).

### 1.3 uv + Docker pattern

Source: uv docs `docs/guides/integration/docker.md` [V-doc astral-sh/uv]:
- Copy the binary from the official image, **pinned to a specific version**, e.g. `COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/` (docs state pinning is best practice; SHA256 digest pinning for reproducibility).
- Base `python:3.x-slim-trixie` is the pattern used in the docs.
- Standard recommended structure (from uv docs, general knowledge, UNVERIFIED line-by-line): multi-stage build; `uv sync --locked --no-dev` in a layer that copies only `pyproject.toml` + `uv.lock` first (dependency layer cached), then copy source and sync again; `--mount=type=cache,target=/root/.cache/uv`; `UV_COMPILE_BYTECODE=1`; `UV_LINK_MODE=copy`; final stage copies `/app/.venv` and runs as non-root.
- Host has no Python → all tooling (`uv run pytest`, `alembic`, `ruff`) runs through `docker compose run --rm api ...`. Provide a `tools`/`dev` image target with dev dependency group.

### 1.4 mypy vs pyright (strict)

- Both current: mypy 2.4.0, pyright 1.1.414 [V PyPI].
- Pydantic ships a mypy plugin; SQLAlchemy 2.x is natively typed (`Mapped[...]`) and no longer needs a mypy plugin (SQLAlchemy docs; UNVERIFIED for 2.1 specifics).
- pyright strict is generally faster and has better inference/narrowing; mypy has plugin ecosystem (pydantic). Both support strict mode (`strict = true` / `typeCheckingMode = "strict"`).
- **Recommendation: pyright strict in CI** (fast, stricter on unannotated returns, good SQLAlchemy `Mapped` support) **+ Ruff for lint/format**. Keep mypy out unless the Pydantic mypy plugin features are needed. (Judgement; no benchmark run. UNVERIFIED performance claims.) Ruff 0.17 handles lint + `ruff format`; drop Black/isort/flake8.
- Alternative acceptable: mypy strict + pydantic plugin if the team already knows it. Do not run both.

### 1.5 Testing notes

- `pytest 9.1.1` + `pytest-asyncio 1.4.0` + `testcontainers 4.15.0` [V].
- testcontainers inside a Docker Compose-only workflow needs the Docker socket mounted in the test container (`/var/run/docker.sock`), or run tests on the host runtime. **Simpler for this project**: use the Compose `postgres` service for tests (separate database per test run, `CREATE DATABASE` + template DB), use testcontainers only for integration tests that need throwaway infra. Recommendation: **Compose Postgres service as default; testcontainers optional** (docker-in-docker friction on Windows hosts; UNVERIFIED but a common pitfall).
- Per-test isolation pattern for SQLAlchemy async: connection-level transaction + SAVEPOINT rollback per test (well-known pattern; confirm against SQLAlchemy 2.1 docs via Context7 at implementation time).

### 1.6 SQLAlchemy async driver: psycopg 3 vs asyncpg

| | psycopg 3 (3.3.6) | asyncpg (0.32.0) |
|---|---|---|
| SQLAlchemy dialect | `postgresql+psycopg` (sync **and** async) | `postgresql+asyncpg` |
| Sync + async in one driver | Yes | No (async only) |
| LISTEN/NOTIFY, COPY, pipeline mode | Yes (native) | Yes (own API) |
| Alembic | Same driver for migrations (sync engine) | Alembic usually needs an extra sync driver (or async-run migrations pattern) |
| Used by Procrastinate | **Yes** (`PsycopgConnector`, psycopg3 — [V-doc procrastinate]) | No |
| Raw speed | Good | Historically faster in micro-benchmarks (UNVERIFIED for 2026) |
| 3.14 classifier | Yes (to 3.15) | Yes (3.12–3.14) |

**Recommendation: psycopg 3** — one driver for API (async), Alembic (sync), the outbox worker, Procrastinate (if chosen), and `LISTEN/NOTIFY`. Throughput difference is irrelevant at this scale.

### 1.7 MCP server notes

- Package `mcp` 2.3.0 [V]; transports per README: stdio, Streamable HTTP, SSE [V-doc]. Use stdio for local agent clients, **Streamable HTTP** for remote (SSE is the legacy transport; UNVERIFIED deprecation status — treat as legacy).
- Auth: the MCP spec's HTTP auth is OAuth 2.1-based (Resource-Server role, token verification). Search snippets mention OAuth issuer validation and Streamable HTTP session expiry/cap changes in 2.2 (UNVERIFIED — single aggregator source). **Confirm exact token-verifier/auth-settings API via Context7 at implementation time** (the SDK has had auth API churn between 1.x and 2.x).
- Design implication: MCP server should be a thin layer **over the same service layer/API** as the REST API, authenticating with the project API keys (§5) or OAuth tokens, scoped to a tenant. Can be mounted into the FastAPI ASGI app (Streamable HTTP) or run as separate container. **UNVERIFIED** mount specifics.
- Pin `mcp>=2.3,<3`; 1.x → 2.x migration is non-trivial, do not copy 1.x-era tutorials.

### 1.8 Anthropic SDK

`anthropic 1.13.0` [V PyPI, 2026-10-09], `AsyncAnthropic` for async usage. Model IDs in this environment's context: `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` (from the harness; not independently verified). Keep model IDs in config, not code.

### 1.9 Frontend

| Component | Latest stable | Released | Source | Notes |
|---|---|---|---|---|
| React | **19.3.0** | 2026-09-09 | [V] npm | |
| Vite | **8.3.4** | 2026-10-08 | [V] npm | Major 8 — verify plugin compat (`@vitejs/plugin-react`, Vitest) |
| TypeScript | **7.0.2** | 2026-07-08 | [V] npm | Major 7 — UNVERIFIED what changed (rumoured native-compiler port; do not rely on it). Check `typescript-eslint`/tooling compat before pinning; fall back to latest 6.x if a tool blocks |
| TanStack Query | **5.104.1** | 2026-10-02 | [V] npm | v5 line |
| TanStack Router | 1.170.42 | 2026-10-10 | [V] npm | Very high patch cadence (daily) |
| React Router | **8.4.0** | 2026-09-15 | [V] npm | v8 (v7 merged Remix) |
| Tailwind CSS | **4.3.3** | 2026-07-16 | [V] npm | v4 (CSS-first config, `@tailwindcss/vite` plugin) |
| shadcn (CLI) | 4.21.4 | 2026-10-07 | [V] npm | CLI copies components into repo; `npx shadcn@latest init` (Tailwind v4 supported — UNVERIFIED version-specific details; confirm via Context7) |
| Playwright | `@playwright/test` **1.64.0** | 2026-10-07 | [V] npm | Use official `mcr.microsoft.com/playwright` image matching the exact version |
| Vitest | **5.0.3** | 2026-09-30 | [V] npm | Align with Vite 8 (check peer deps) |
| orval | 8.41.0 | 2026-10-08 | [V] npm | Generates TanStack Query hooks from OpenAPI |
| openapi-typescript | 7.13.0 | 2026-02-11 | [V] npm | Types only (+ `openapi-fetch`); last release 8 months ago |
| @hey-api/openapi-ts | 0.99.0 | 2026-06-22 | [V] npm | Pre-1.0 |

**Router choice — TanStack Router vs React Router 8**: For a Vite SPA dashboard (no SSR needed) both work. TanStack Router: end-to-end type-safe routes/search params, pairs naturally with TanStack Query (loaders prefetch), file-based route generation; cost: very fast release cadence (patch daily), smaller ecosystem. React Router 8: stable, ubiquitous, but type-safety is weaker in library mode. **Recommendation: TanStack Router** (type-safe search params matter for filter-heavy financial tables). Fallback: React Router 8 if churn becomes a problem. (Judgement.)

**API client generation — orval vs openapi-typescript**: FastAPI emits OpenAPI 3.1. **Recommendation: orval 8 (react-query client)** — generates typed TanStack Query hooks plus types, 1 tool instead of 2; it is actively released (2026-10-08). Alternative: `openapi-typescript` + `openapi-fetch` (lighter, more manual, release cadence slow). Generate the client in a Docker `node` stage from the exported `openapi.json` and commit the output or diff-check in CI.

---

## 2. Async job / event infrastructure

### 2.1 Requirements for this use case

1. **Transactional outbox**: domain write + event row commit atomically in Postgres; relay must deliver **at-least-once** (consumers idempotent).
2. Retries with exponential backoff, dead-letter/inspectable failures.
3. Scheduled/periodic jobs (invoice finalization, reconciliation).
4. Webhook delivery workers (per-endpoint ordering optional, retries, signing, timeouts).
5. Realtime fan-out to dashboard clients (see §3).
6. Fit with SQLAlchemy async; low ops footprint in Docker Compose; demo-able; multi-tenant.

### 2.2 Candidate snapshot (registry data [V] unless noted)

| Candidate | Latest | Last release | Maintenance status 2026 | Source |
|---|---|---|---|---|
| Redis Streams (raw, `redis` py 8.1.0) | redis-py 8.1.0 | 2026-07-30 | Core Redis feature; you write consumers yourself | [V] PyPI |
| Dramatiq (+Redis/RabbitMQ) | 2.2.1 | 2026-09-02 | Active (pushed 2026-10-05) | [V] PyPI/GitHub |
| Celery | 5.6.3 | 2026-03-26 | Active but slow cadence; 3.12–3.13 classifiers only | [V] PyPI, docs |
| ARQ | 0.28.0 | 2026-04-16 | **"In maintenance only mode"** per README | [V-doc python-arq/arq] |
| Procrastinate (Postgres) | **3.10.0** | 2026-09-23 | Active (pushed 2026-10-10), 1.4k stars | [V] PyPI/GitHub |
| Taskiq | 0.13.0 | 2026-09-26 | Active, pre-1.0 | [V] PyPI/GitHub |
| NATS JetStream (`nats-server` 2.15.1, `nats-py` 2.16.0) | server 2.15.1 | 2026-10-09 | Very active (CNCF-hosted, UNVERIFIED) | [V] GitHub, PyPI |
| Pure Postgres (`FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`) | PG 18.6 | — | PG 18 supported to 2030-11 | [V] endoflife.date |

### 2.3 Comparison

Legend: ✔ good, ~ partial/with effort, ✘ weak.

| Criterion | Redis Streams raw | Dramatiq | Celery | ARQ | Procrastinate | Taskiq | NATS JetStream | Pure Postgres |
|---|---|---|---|---|---|---|---|---|
| **asyncio** | ✔ (redis.asyncio) | ~ async actors supported (changelog mentions async actors) [V-doc]; threads-first model | ✘ prefork/threads; no native async tasks (UNVERIFIED for 5.6) | ✔ asyncio-native | ✔ async + sync [V-doc README] | ✔ async-native [V-doc README] | ✔ (`nats-py` async) | ✔ (psycopg async) |
| **Delivery** | at-least-once w/ consumer groups + XACK/XAUTOCLAIM | at-least-once (acks late) (UNVERIFIED default) | at-least-once with `acks_late`; default early ack (UNVERIFIED) | at-least-once-ish; job loss risks on crash (UNVERIFIED) | at-least-once; job row in same DB → **enqueue in the same transaction as business write** | broker-dependent | at-least-once (JetStream ack/redelivery) | at-least-once; exactly what you design |
| **Retries/backoff** | you build | ✔ built-in (Retries middleware, exponential) | ✔ built-in | ✔ basic | ✔ `RetryStrategy` constant/linear/exponential, exception filters [V-doc] | ✔ middleware | ✔ (`max_deliver`, backoff list) | you build (simple: `attempts`, `run_after`) |
| **Scheduling/periodic** | you build | via `periodiq`/APScheduler add-ons (UNVERIFIED) | ✔ beat | ✔ cron jobs | ✔ periodic tasks built-in [V-doc] + `defer` with schedule | ✔ scheduler component | ~ (no cron natively; external) | `pg_cron` ext or loop in worker |
| **Observability** | roll own | Prometheus middleware (UNVERIFIED), OTel hand-wired | Flower, OTel instrumentation exists (UNVERIFIED) | minimal | **jobs/events tables are SQL-queryable** — dashboards trivial | OTel middleware (UNVERIFIED) | NATS monitoring endpoint, `nats` CLI | SQL-queryable |
| **Compose footprint** | +Redis | +Redis/RabbitMQ +worker | +broker +worker +beat (+Flower) | +Redis +worker | **+worker only** (Postgres already there) | +broker +worker (+scheduler) | +nats container | **+worker only** |
| **Transactional with PG writes** | ✘ dual-write → needs outbox relay | ✘ same | ✘ same | ✘ same | ✔ job insert joins your transaction (shared Postgres, same connection if you pass it — confirm API) | ✘ | ✘ (outbox relay) | ✔ native |
| **SQLAlchemy async fit** | neutral | neutral | poor (sync-oriented) | OK | Uses psycopg3; own connection pool, **not** SQLAlchemy engine; a shared transaction with SQLAlchemy session needs care (UNVERIFIED) | OK | neutral | ✔ same engine/session |
| **Realtime fan-out** | ✔ pub/sub | ✘ | ✘ | ✘ | `LISTEN/NOTIFY` internal | ✘ | ✔ native pub/sub | ✔ `NOTIFY` |
| **Risk** | build effort, DLQ/ordering DIY | OK | heavyweight, ops complexity | maintenance-only | 3.x major; single-maintainer-ish (UNVERIFIED), young ecosystem | pre-1.0 | new tech in stack | you own the correctness |

Key facts behind the table:
- Procrastinate stores task definitions in Postgres, uses row locks to dispatch and `LISTEN` to be notified when a task is available; supports retries (constant/linear/exponential), periodic tasks, locks; requires PostgreSQL 13+ [V-doc procrastinate README + discussions.md]. It also uses a dedicated LISTEN connection outside the pool [V-doc].
- ARQ README: "arq is in maintenance only mode" [V-doc].
- fastapi-users, similarly, is maintenance mode (see §5).

### 2.4 Analysis for the outbox pattern

The outbox needs two pieces: (a) an `outbox_events` table written in the **same transaction** as the business change, and (b) a **relay** that claims rows and delivers them. Only Postgres-native options make (a)+(b) one consistency domain. With Redis/NATS/Celery you still need a Postgres outbox + a relay publishing into the broker — i.e. the broker is **additional** moving parts and gives no extra guarantee for the outbox (the relay-to-broker step is again at-least-once).

Pure-Postgres relay (standard pattern, general knowledge — **UNVERIFIED against any one doc**, validate in a spike):
- Claim: `SELECT ... FROM outbox WHERE status='pending' AND available_at <= now() ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED`, mark `processing`/lease or process inside the same txn; on failure set `attempts+1`, `available_at = now() + backoff`, `last_error`; after N attempts → `dead` status (visible in dashboard).
- Wake-up: `NOTIFY outbox` after commit (trigger or app) so workers don't busy-poll; **always keep a poll fallback** (NOTIFY is not durable; lost during reconnect).
- Ordering: per-aggregate ordering needs partitioning key or advisory locks; global strict order is incompatible with parallel workers — decide whether per-tenant/per-aggregate ordering is required.
- Idempotency: every event has stable `event_id`; consumers/webhook receivers dedupe on it (Stripe-style `Idempotency-Key` / `webhook-id`).
- Table bloat: partition or periodically delete delivered rows; watch vacuum.

Procrastinate vs hand-rolled SKIP LOCKED worker:
- Procrastinate gives retries, periodic scheduling, worker lifecycle, queues, locks, CLI/inspection, migrations — **already written**. But it models *jobs*, not an event log; the outbox table can still be your own (domain events), with a relay job that fans out into Procrastinate jobs (webhook-delivery jobs per subscriber), keeping webhooks retried with its `RetryStrategy`.
- A hand-rolled worker means writing retry/backoff/scheduling/leases/graceful shutdown yourself — ~300–500 lines and the classic bug surface (lease expiry, stuck `processing` rows).

### 2.5 Ranked recommendation

1. **Postgres outbox table (own, simple) + Procrastinate (psycopg3) as the job runner/scheduler — no Redis, no broker.**
   - Fewest moving parts: Compose adds **one worker container** (+ optional second for periodic). Everything transactional and inspectable via SQL (demo-friendly: show retries/dead jobs in the dashboard from tables).
   - Covers: retries with exponential backoff [V-doc], periodic tasks for invoice finalization/reconciliation [V-doc], webhook-delivery jobs (one job per delivery with lock = endpoint id for ordered delivery, [V-doc locks]), async-capable [V-doc].
   - Risks to validate in a 1–2 day spike: Procrastinate 3.x API for deferring inside an existing transaction/connection when the app uses SQLAlchemy's own engine (may need a second psycopg connection/pool; weigh whether job insert must be atomic with business write — if yes, the outbox row carries the atomicity and the relay defers jobs, which sidesteps it); Alembic coexistence with Procrastinate's own schema migrations (`procrastinate schema --apply` vs Alembic — UNVERIFIED; run Procrastinate schema in a separate Postgres schema or include its SQL in an Alembic revision); LISTEN connection through pgbouncer (transaction pooling breaks LISTEN — don't use pgbouncer in transaction mode for the worker).
2. **Pure Postgres (SKIP LOCKED + LISTEN/NOTIFY) hand-rolled.** Same architecture as #1 without a dependency. Choose this if the spike shows Procrastinate friction. More code to own and test, but fully understood; best "no framework magic" demo.
3. **Taskiq (+ Postgres or Redis broker) — only if broker flexibility is needed.** Async-native, FastAPI integration [V-doc]; but pre-1.0 (0.13.0) and a separate broker would again need an outbox relay.
4. **Dramatiq + Redis.** Mature, simple, retry/backoff built in; but adds Redis and dual-write relay; async via actors is secondary.
5. **NATS JetStream.** Excellent semantics and fan-out, but a new technology/container/ops surface for a transactional-DB-centric app; justified only if multi-service event streaming or very high fan-out becomes a requirement.
6. **Celery.** Most feature-rich, heaviest ops (broker + worker + beat), poor asyncio fit, 3.14 not declared.
7. **Redis Streams raw.** Requires building retries/DLQ/scheduling yourself on top of a broker you don't otherwise need.
8. **ARQ.** Maintenance-only per its own README [V-doc]; avoid for a new system.

Decision rule: **start with #1; keep the outbox/relay interface (`publish(event)`, `handler(event)`) independent of the runner** so #2 or a broker can replace it without touching domain code.

---

## 3. Realtime

### 3.1 Options

| | FastAPI/Starlette WebSockets | SSE (Server-Sent Events) | Centrifugo |
|---|---|---|---|
| Latest | Starlette 1.7.0 [V] | via `sse-starlette` or `StreamingResponse` (version UNVERIFIED) | **v6.9.7** (2026-09-29, Docker `centrifugo/centrifugo:v6.9.7`) [V GitHub/Docker Hub] |
| Direction | bi-directional | server→client only | bi-directional, pub/sub channels |
| Reconnect/resume | you build | **built-in `EventSource` reconnect + `Last-Event-ID`** | built-in recovery, presence, history |
| Auth | token in query/first message/cookie (headers not settable from browser WS API) | cookie or fetch-based SSE with `Authorization` header | JWT connection tokens + channel permissions |
| Proxies / HTTP | WS upgrade; some proxies unfriendly | plain HTTP, simplest through proxies/CDN | WS/SSE/HTTP-streaming transports |
| Ops | none extra | none extra | **+1 container (+ Redis for multi-node)** |
| Fan-out across replicas | must build | must build | built-in (broker: Redis/NATS) |

### 3.2 Fan-out across multiple API replicas

An API replica holds only its own connections; an event produced by replica A (or by the worker) must reach clients on B.
- **Postgres LISTEN/NOTIFY**: each replica opens one dedicated listener connection (psycopg async `notifies()`); payload ≤ 8000 bytes (documented PostgreSQL limit, general knowledge) → send only `{tenant_id, event_id, type}` and let clients refetch via TanStack Query invalidation. Not durable; no replay (use `Last-Event-ID` against the outbox table to catch up). Zero new infra. Does not work through transaction-mode pgbouncer.
- **Redis pub/sub**: lowest latency, scales higher, but introduces Redis solely for this; also at-most-once.
- For this product (dashboard of a financial system-of-record, tens–hundreds of concurrent viewers, events are "something changed, refresh"), LISTEN/NOTIFY is sufficient.

### 3.3 Recommendation

**SSE, with Postgres LISTEN/NOTIFY fan-out, backed by the outbox table for replay.**
- One-way flow matches the need (server pushes "invoice finalized", "reconciliation done"); user actions go through normal REST calls.
- Native browser reconnect + `Last-Event-ID` ⇒ gap-free resume by querying `outbox_events WHERE id > last_id AND tenant_id = ...`.
- Each SSE stream is tenant-scoped, authorized at connect time (cookie session), with a heartbeat comment every ~15 s.
- Escape hatches: if bi-directional or presence is needed → FastAPI WebSockets; if realtime becomes a product feature at scale → Centrifugo v6 (`centrifugo/centrifugo:v6.9.7`) with Redis/NATS broker. Not needed now.
- Frontend: `EventSource` event → `queryClient.invalidateQueries([...])` (TanStack Query) — no state sync logic needed.
- Caveat to test: SSE through the Vite dev proxy and any reverse proxy (disable buffering, `X-Accel-Buffering: no`; HTTP/1.1 6-connection-per-origin limit unless HTTP/2 — **use one multiplexed stream per tab**).

---

## 4. Observability stack for Docker Compose

### 4.1 Options

| | A. `grafana/otel-lgtm` (single image) | B. OTel Collector + Jaeger + Prometheus + Loki (+ Grafana) |
|---|---|---|
| Components | Grafana + Loki + Tempo + Mimir/Prometheus + OTel Collector in one container [V-doc grafana/docker-otel-lgtm README: "OpenTelemetry backend in a Docker image"] | each a separate service |
| Image/tag | `grafana/otel-lgtm:0.36.0` (Docker Hub tag exists [V]; GitHub release v0.36.0 2026-10-09 [V]) | `otel/opentelemetry-collector-contrib:0.162.0` [V Docker Hub]; `jaegertracing/jaeger:2.22.0` [V Docker Hub]; Prometheus v3.15.0 [V GitHub]; Loki v3.7.8 [V GitHub]; Grafana (tag UNVERIFIED) |
| Compose services | **1** | 5–6 + 4 config files |
| Ports | OTLP gRPC 4317, OTLP HTTP 4318, Grafana 3000 (UNVERIFIED in this run; standard for the image — README snippet fetched did not print the table) | many |
| Pre-wired datasources/dashboards | yes | manual provisioning |
| Production use | **Dev/demo/test only** — README positions it that way (UNVERIFIED wording; the image is billed as a dev/demo backend) | production-shaped |
| Release cadence | Very active (≈8 releases in 2026 per search + v0.36.0 on 2026-10-09) | each component active |

### 4.2 Recommendation

**Use `grafana/otel-lgtm:0.36.0` for the dev/demo Compose** (pin the tag, not `latest`), with an optional **separate** `otel/opentelemetry-collector-contrib:0.162.0` in front **only if** you need processors (redaction of PII/financial fields, tail sampling) — LGTM already embeds a collector. Reasons: one container, zero provisioning, traces + metrics + logs correlated in Grafana, ideal for demo-ability.

Config snippet (app side, env only):
```
OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_SERVICE_NAME=financial-hub-api
```
- Python: `opentelemetry-sdk`/`-exporter-otlp-proto-http` 1.45.x + `opentelemetry-instrumentation-fastapi`/`-sqlalchemy`/`-psycopg` 0.66b1 (beta; version-lock all `opentelemetry-*` together via `opentelemetry-bootstrap` output). Instrument the SQLAlchemy **sync engine of the async engine** (`async_engine.sync_engine`) — UNVERIFIED API detail, confirm via Context7.
- **structlog** → add trace_id/span_id to log records; ship logs via OTLP logs (maturity of Python OTel logs SDK: UNVERIFIED/experimental) **or** stdout JSON picked up by... (LGTM has no log scraper by default) → prefer OTLP logs handler and verify in spike; fallback = logs to stdout only.
- Persist with a named volume only if wanted for demo continuity; ephemeral by default.
- Production later: swap to B or Grafana Cloud with the same OTLP env vars — the app code does not change. This is the main reason to treat the app as OTLP-only.
- Security: never put PII/amounts in span attributes without a policy; add a collector `attributes`/`redaction` processor if a collector is introduced.

---

## 5. Auth: dashboard users + API keys for integrated projects

### 5.1 Dashboard (human) auth options

| Option | Status [V] | Fit |
|---|---|---|
| **Self-built**: JWT/session + `argon2-cffi` | argon2-cffi 25.1.0 (2025-06-03; stable, low churn) | Full control, small surface, multi-tenant claims, no framework lock-in |
| fastapi-users 15.0.5 | **Maintenance mode** — README: security updates & dependency maintenance only, no new features; maintainers are building a successor toolkit [V-doc fastapi-users README] | Don't build a new system on a project whose maintainers say to expect a replacement |
| Authlib 1.8.0 | Active (2026-08-30) | OAuth/OIDC client/server building blocks; useful if adding SSO (Google/Microsoft) or acting as OAuth server for MCP |
| Hosted IdP (Keycloak/Auth0/etc.) | not evaluated | Out of scope; overkill for demo; revisit for SSO |

**Recommendation**: **self-built, minimal**:
- Passwords: Argon2id via `argon2-cffi`. OWASP minimum configs: `m=19456 (19 MiB), t=2, p=1` (or `m=47104,t=1,p=1`) [V-doc OWASP Password Storage Cheat Sheet]; tune up to ~100–250 ms on the deploy hardware; use `check_needs_rehash`. Run hashing off the event loop (`run_in_threadpool`/`asyncio.to_thread`) since it is CPU-bound.
- Dashboard sessions: for a same-origin SPA, **HTTP-only, Secure, SameSite=Lax session cookie** (opaque session id stored in Postgres) is simpler and safer than JWT in localStorage; add CSRF protection (double-submit token or `SameSite=Strict` + custom header) — SSE and fetch both work with cookies. If JWT is preferred: short-lived access token (≤15 min) + rotating refresh token in HTTP-only cookie. Library: `PyJWT` or `joserfc` (versions UNVERIFIED, not queried).
- Tenant context: `tenant_id` resolved server-side from the session/membership, enforced by Postgres RLS (`SET LOCAL app.tenant_id`) as defence-in-depth (recommendation; out of this task's scope).
- Add TOTP/WebAuthn later; Authlib only if/when SSO or OAuth-for-MCP is required.

### 5.2 API keys for integrated projects (machine auth)

Best practice (general industry practice — GitHub/Stripe-style; **UNVERIFIED against a single cited document**, validate with OWASP API/Secrets guidance):
- Format: `fh_<env>_<prefix>_<secret>` e.g. `fh_live_a1b2c3d4_<32+ random bytes base62/base64url>`. The **prefix (public key id)** is stored in plaintext, indexed, used for lookup and shown in UI/logs; the **secret** is never stored.
- Generate with `secrets.token_bytes(32)` (≥128 bits, ideally 256 bits of entropy).
- **Store a hash of the secret, not the secret.** Because API-key secrets are high-entropy random strings (not human passwords), a fast keyed hash is appropriate and avoids the 100 ms-per-request cost of Argon2:
  - **HMAC-SHA256 with a server-side pepper** (`hmac.new(pepper, secret, sha256)`; pepper in env/secret store, versioned for rotation). Compare with `hmac.compare_digest`.
  - Argon2id is *also fine* but expensive on every authenticated request (needs caching) and unnecessary for 256-bit random secrets.
- Lookup flow: parse prefix → fetch row by `key_prefix` (unique index) → verify HMAC in constant time → check `revoked_at`, `expires_at`, `scopes`, `tenant_id`/`project_id` → update `last_used_at` (debounced/async to avoid write amplification).
- Schema essentials: `id`, `tenant_id`, `project_id`, `name`, `key_prefix`, `secret_hash`, `hash_version`, `scopes[]`, `created_by`, `created_at`, `last_used_at`, `expires_at`, `revoked_at`.
- Show the full key **once** at creation. Support multiple active keys per project for zero-downtime rotation; audit log every creation/revocation/use-anomaly.
- Transport: `Authorization: Bearer <key>` (or `X-API-Key`); TLS only; rate limit per key (a Redis-free token bucket in Postgres or in-process is enough at this scale — UNVERIFIED).
- Secret scanning: the recognisable prefix lets GitHub secret scanning/partner patterns catch leaked keys (general practice, UNVERIFIED for custom prefixes).
- Webhook signing (outbound): per-endpoint secret, `HMAC-SHA256(secret, timestamp + "." + body)` with `webhook-id` + `webhook-timestamp` headers (Standard Webhooks style, UNVERIFIED mapping) and a tolerance window; supports consumer-side dedupe.
- MCP server auth: accept the same API keys in `Authorization` for the Streamable HTTP transport initially; adopt OAuth (Authlib as AS, or external IdP) only if third-party MCP clients need it (confirm SDK auth API via Context7).

**Recommendation summary (auth)**: self-built Argon2id (OWASP parameters) + HTTP-only session cookie for dashboard users; project API keys as `prefix.secret` with **HMAC-SHA256+pepper** hashes, scopes, rotation, revocation, audit. Do **not** adopt fastapi-users (maintenance mode). Defer Authlib to SSO/OAuth needs.

---

## 6. Consolidated recommendations

| Area | Decision |
|---|---|
| Python | **3.14** (`python:3.14-slim`), `requires-python >=3.14,<3.15`; avoid 3.15.0 until libraries catch up |
| Backend core | FastAPI 0.143 · Pydantic 2.14 · SQLAlchemy **2.1.4** (async) · Alembic 1.20 · **psycopg 3.3** · Uvicorn 0.54 — pin all |
| Tooling | uv 0.13.0 (pinned in Dockerfile) · Ruff 0.17 · **pyright strict** · pytest 9 + pytest-asyncio 1.4 · Compose Postgres for tests (testcontainers optional) · structlog 26.1 |
| Async infra | **Postgres outbox + Procrastinate 3.x** (psycopg3) — single worker container; fallback hand-rolled `SKIP LOCKED` worker; no Redis/broker initially |
| Realtime | **SSE** + **Postgres LISTEN/NOTIFY** fan-out + outbox replay via `Last-Event-ID`; Centrifugo v6 / WebSockets only if needs change |
| Observability | `grafana/otel-lgtm:0.36.0` (single container) for dev/demo; app is OTLP-only so backend is swappable; optional `otel/opentelemetry-collector-contrib:0.162.0` for redaction |
| Auth | Self-built Argon2id + cookie sessions; API keys `prefix+secret`, HMAC-SHA256+pepper; no fastapi-users |
| Frontend | React 19.3 · Vite 8.3 · TypeScript 7.0 (verify tool compat, fallback 6.x) · TanStack Query 5.104 + **TanStack Router** · Tailwind 4.3 + shadcn · Vitest 5 · Playwright 1.64 (official image, version-matched) · **orval 8** client generation |
| MCP | `mcp` 2.3.x (`>=2.3,<3`), stdio + Streamable HTTP, API-key auth first |
| LLM | **Changed 2026-10-10 by owner decision:** Google Gemini Flash-Lite (free tier) via `google-genai` SDK (version pinned in task 8.x after verification); model id in config (ADR-0009) |

## 7. Open items / UNVERIFIED list (for coordinator)

1. SQLAlchemy **2.1** breaking changes vs 2.0 and Alembic 1.20 ↔ SQLAlchemy 2.1 compatibility — confirm via Context7 / changelogs before locking.
2. Procrastinate 3.x: deferring inside an existing SQLAlchemy/psycopg transaction; coexistence of its schema migrations with Alembic; pgbouncer implications. **Spike required** before committing.
3. TypeScript 7.0 / Vite 8 / Vitest 5 toolchain compatibility (`typescript-eslint`, `@vitejs/plugin-react`, orval) — spike `npm install && build` in Docker.
4. MCP SDK 2.x auth API (token verifier / resource-server metadata) and ASGI mount pattern; 2.2 session-limit changes cited only by an aggregator.
5. OTel Python logs signal maturity and `-sqlalchemy` instrumentation with async engines.
6. `grafana/otel-lgtm` exposed ports and production-suitability statement (tag `0.36.0` verified to exist; port list from general knowledge).
7. Celery/Dramatiq/ARQ delivery-default claims and async-support details in §2.3 are from general knowledge unless marked [V-doc].
8. No benchmarks were run (psycopg vs asyncpg, pyright vs mypy); statements about speed are qualitative.

## 8. Sources

- PyPI JSON API, queried 2026-10-10: `https://pypi.org/pypi/<package>/json` for every Python package in §1 and §2.2.
- npm registry, queried 2026-10-10: `https://registry.npmjs.org/<package>` for every JS package in §1.9.
- GitHub releases/API: `grafana/docker-otel-lgtm` (v0.36.0), `open-telemetry/opentelemetry-collector-releases` (v0.162.0), `centrifugal/centrifugo` (v6.9.7), `nats-io/nats-server` (v2.15.1), `astral-sh/uv`, `tiangolo/fastapi`, `procrastinate-org/procrastinate`, `Bogdanp/dramatiq`, `python-arq/arq`, `taskiq-python/taskiq`, `jaegertracing/jaeger`, `grafana/loki`, `prometheus/prometheus`, `modelcontextprotocol/python-sdk`, `fastapi-users/fastapi-users`.
- Docker Hub tags API: `grafana/otel-lgtm`, `otel/opentelemetry-collector-contrib`, `centrifugo/centrifugo`, `jaegertracing/jaeger`, `library/postgres`, `library/redis`, `library/nats`.
- https://endoflife.date/api/python.json and https://endoflife.date/api/postgresql.json
- READMEs/docs: https://github.com/fastapi-users/fastapi-users (maintenance-mode notice); https://github.com/python-arq/arq (maintenance-only notice); https://github.com/procrastinate-org/procrastinate (README, `docs/discussions.md`, `docs/howto/advanced/retry.md`); https://github.com/taskiq-python/taskiq; https://github.com/modelcontextprotocol/python-sdk (README: install, transports); https://github.com/Bogdanp/dramatiq (changelog); https://github.com/celery/celery (5.6 what's-new: supported Python versions); https://github.com/grafana/docker-otel-lgtm (README); https://github.com/astral-sh/uv (`docs/guides/integration/docker.md`); https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html (Argon2id parameters).
- WebSearch (secondary, low-confidence, used only for hints flagged UNVERIFIED): releasebot.io (asyncpg 3.14 support), newreleases.io / releasebot.io (MCP SDK v2 notes), pkg.go.dev (docker-otel-lgtm release dates).
