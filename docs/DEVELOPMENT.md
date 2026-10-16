# Development

The host runs **only Docker**. Python, Node and every tool (ruff, pyright, pytest, eslint, vite...) run in containers.

## Quick start

```bash
cp .env.example .env          # no real secrets in it; .env is git-ignored
docker compose up -d --build  # from the repo root
docker compose ps             # db, api, worker, web healthy; migrate exited 0
```

| Service | URL / port | Notes |
|---|---|---|
| `api` | http://localhost:8000 (`/health/live`, `/health/ready`, `/docs`) | uvicorn `--reload`, source bind-mounted |
| `web` | http://localhost:5173 | Vite dev server; `/api/*` is proxied to `api:8000` (prefix stripped) |
| `db` | localhost:5432 | postgres:18, named volume `db-data` |
| `migrate` | - | one-shot `alembic upgrade head`; api and worker wait for it |
| `worker` | - | `python -m financial_hub.workers` (placeholder loop until M3) |
| `lgtm` | Grafana :3000, OTLP gRPC :4317, OTLP HTTP :4318 | `grafana/otel-lgtm:0.36.0`, opt-in (see below) |

### Resource limits and observability

Every service has a CPU/memory cap in `deploy/compose.yaml` (the `test` service gets 3 CPUs / 2 GB),
and `scripts/test.sh` holds a host-wide lock so parallel agents run one test suite at a time
(`waiting for another test run` means it is queued). On Windows, also cap the WSL2 VM in
`%UserProfile%\.wslconfig` (for example `memory=8GB`, `processors=6`) and run `wsl --shutdown`.
Postgres runs with `synchronous_commit=off` (dev/test only). Vite polls the bind mount every
`VITE_POLL_INTERVAL_MS` (default 1000).

The Grafana LGTM stack (about 0.7 GB idle) is opt-in. To enable it, add to `.env`:
`COMPOSE_PROFILES=observability` and `FH_OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318`, then
`docker compose up -d`. Without it, OTel export is disabled.

Stop with `docker compose down` (add `-v` to also drop the database and the dependency volumes).

Hot reload uses bind mounts (`backend/` -> `/app`, `frontend/` -> `/app`) with named volumes for
`/app/.venv` and `/app/node_modules`. After changing `pyproject.toml`/`uv.lock` or `package.json`/`package-lock.json`,
rebuild and drop the stale dependency volume: `docker compose down && docker volume rm financial-hub_backend-venv financial-hub_web-node-modules && docker compose up -d --build`.

## Tooling commands

All wrappers live in `scripts/` (bash) and are plain `docker compose run --rm` calls.

| Task | Script | Underlying command |
|---|---|---|
| Lint | `scripts/lint.sh` | `docker compose run --rm --no-deps api ruff check .` + `ruff format --check .` |
| Typecheck (pyright strict, `src/`) | `scripts/typecheck.sh` | `docker compose run --rm --no-deps api pyright` |
| Import contracts | `scripts/imports.sh` | `docker compose run --rm --no-deps api lint-imports` |
| Tests (unit + integration, needs db; own `financial_hub_test` database) | `scripts/test.sh [pytest args]` | `docker compose run --rm test pytest` |
| Web lint | `scripts/web-lint.sh` | `docker compose run --rm --no-deps web npm run lint` |
| Web typecheck | `scripts/web-typecheck.sh` | `docker compose run --rm --no-deps web npm run typecheck` |
| Web tests | `scripts/web-test.sh` | `docker compose run --rm --no-deps web npm run test` |
| Web build | `scripts/web-build.sh` | `docker compose run --rm --no-deps web npm run build` |
| Everything | `scripts/check.sh` | all of the above |

Other useful commands:

```bash
docker compose run --rm api ruff format .                              # apply formatting
docker compose run --rm migrate alembic revision -m "describe change"  # new migration
docker compose run --rm migrate alembic upgrade head                   # apply migrations
docker compose run --rm test pytest tests/unit                         # unit tests only
```

## CI

`.github/workflows/ci.yml` runs on every push to `main` and on pull requests (concurrent runs of the same ref are
cancelled; `permissions: contents: read`; third-party actions pinned to commit SHAs; no GitHub secrets needed).

| Job | What it runs |
|---|---|
| `backend` | builds the `dev` image (buildx + GHA cache), `.env` from `.env.example`, then `scripts/lint.sh`, `typecheck.sh`, `imports.sh`, `test.sh` (junit/coverage uploaded as the `backend-test-reports` artifact) |
| `frontend` | `scripts/web-lint.sh`, `web-typecheck.sh`, `web-test.sh`, `web-build.sh` |
| `security` | gitleaks over the full history (`.gitleaks.toml`), `pip-audit` on `backend/uv.lock`, `npm audit --audit-level=high`, Trivy on the `runtime` api and `prod` web images (CRITICAL,HIGH, unfixed ignored) |

Reproduce locally with the same scripts (`scripts/check.sh`) and:

```bash
docker run --rm -v "$PWD:/repo" ghcr.io/gitleaks/gitleaks:v8.30.1 git --redact --no-banner --config /repo/.gitleaks.toml /repo
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest      # validate workflows
```

`.gitleaks.toml` allowlists are only for documented fake values (currently none). Tool image versions are pinned in the
workflow `env:` block; bump them deliberately.

## Regenerating the API client

`bash scripts/web-api.sh` dumps the backend schema (`create_app().openapi()`, run in the `api` container, no DB needed)
to `frontend/openapi.json` (sorted keys, 2-space indent) and runs [orval](https://orval.dev) in the `web` container. It
writes TanStack Query v5 hooks and types to `frontend/src/api/generated/` (one file per OpenAPI tag under `endpoints/`,
shared types under `model/`). Both the snapshot and the generated code are committed and excluded from eslint; CI
(`frontend` job) re-runs the script and fails on `git diff`. Run it after any backend route or schema change.
Generated hooks call `src/api/mutator.ts`, which wraps `lib/api.ts` (cookies, CSRF header, problem+json -> `ApiError`,
401 redirect). Live updates: `useProjectStream(projectId)` in `src/lib/realtime/` (SSE, invalidates queries by event type).

## Database roles

The runtime never holds owner credentials (ADR-0003 addendum), not even in dev.

| Role | Login | Used by | Can |
|---|---|---|---|
| `POSTGRES_USER` (`financial_hub`) | yes | `migrate`, `test` (fixtures only) | everything; created by the postgres image |
| `fh_owner` | **no** | owns every ledger table and function | granted to the migration login; never logs in |
| `fh_app` | yes | `api`, `worker`, ledger tests | `SELECT` + column-level `INSERT` on ledger tables, `UPDATE(status)` on entries, `EXECUTE` on `ledger_apply` / `ledger_lock_accounts`; no `UPDATE`/`DELETE` of postings or balances, no DDL, no temp tables |

- Both roles are created by migration `0001_ledger_core` (idempotent; the downgrade drops them).
- `FH_APP_DB_PASSWORD` (in `.env`; dev default in `.env.example`) is read by the migration and
  by compose to build the `api`/`worker` URL; compose refuses to start without it. Owner
  credentials (`POSTGRES_*`) reach only `migrate` and the `test` service.
- The migration sets the password only when it runs (upgrade from below `0001`). To rotate it
  later: change `FH_APP_DB_PASSWORD` in `.env`, set the same value with
  `docker compose exec db psql -U financial_hub -c '\password fh_app'`, then
  `docker compose up -d api worker`.
- The `test` service (profile `test`, used by `scripts/test.sh`) gets the owner URL as
  `FH_DATABASE_URL` (fixtures and owner-side trigger tests) and `FH_APP_TEST_DATABASE_URL` as
  `fh_app`, so the ledger tests prove the runtime privileges. Ledger rows are append-only, so tests
  isolate by a fresh `project_id` instead of cleaning up.
- Check the role the api actually uses:
  `docker compose exec db psql -U financial_hub -c "SELECT DISTINCT usename FROM pg_stat_activity WHERE usename = 'fh_app'"`
  after a request to `/health/ready`.

Adding or updating a Python dependency (the host has no uv; use the dev image):

```bash
docker compose run --rm --no-deps api uv add <pkg>==<version>          # rewrites pyproject.toml + uv.lock in backend/
```

Same for npm: `docker compose run --rm --no-deps web npm install --save-exact <pkg>`.

## Bootstrap operator

There is no sign-up: the first operator (and optionally a project it owns) comes from the CLI,
which writes through the `api` image as the restricted `fh_app` role.

```bash
# interactive: prompts twice for the password (12..128 characters), nothing echoed
docker compose run --rm api python -m financial_hub.cli create-operator \
  --email you@example.com --display-name "Your Name" \
  --project-slug acme --project-name "Acme"

# non-interactive (scripts): the password comes only from the environment, never from argv
read -rs FH_BOOTSTRAP_PASSWORD && export FH_BOOTSTRAP_PASSWORD
docker compose run --rm -e FH_BOOTSTRAP_PASSWORD api python -m financial_hub.cli create-operator \
  --email you@example.com --display-name "Your Name"
unset FH_BOOTSTRAP_PASSWORD
```

`--project-slug`/`--project-name` go together; operator and project are created atomically (a taken
slug or email changes nothing). There is no `--password` flag on purpose (shell history, `ps`).
Then sign in through the web app or `POST /v1/auth/login`; the session is the `fh_session` cookie
(HttpOnly, SameSite=Lax, `Secure` outside `FH_APP_ENV=dev`) and unsafe requests must send the
`X-CSRF-Token` returned by login and `GET /v1/auth/me`.

## Demo mode

For evaluation, the console can sign in by picking an operator instead of typing a password.

1. Enable it: `FH_DEMO_LOGIN=true` (already set for the `api` service in `deploy/compose.yaml`;
   off by default everywhere else). The API logs a WARNING at startup when it is on.
2. Seed the demo workspace (idempotent; random passwords are never printed, so password login
   stays unusable for these operators):

   ```bash
   docker compose run --rm api python -m financial_hub.cli seed-demo
   ```

   This creates project "Demo Store" (`demo-store`) with Ana Owner (owner), Bruno Admin (admin),
   Carla Finance (finance) and Diego Viewer (viewer), e.g. `ana@demo.financial-hub.local`.
3. Open the console (http://localhost:5173), choose an operator on the sign-in screen
   (`GET /v1/auth/demo-operators`, `POST /v1/auth/demo-login`). The session, cookie and CSRF
   token are the same as after a password login.

Demo login serves only these seeded personas (emails in `demo.financial-hub.local`); the picker lists
only them and every other operator is refused like an unknown id. Backend tests run on their own database
(`financial_hub_test`, docs/TESTING.md), so they no longer add operators to the demo picker.

### Reset a polluted dev database

A dev database filled by earlier test runs (before the test database existed) keeps its old rows, but they
are no longer listed or usable by the demo login. To wipe them anyway, recreate the volume and re-seed
(destroys all dev data):

```bash
docker compose down -v
docker compose up -d --wait api web   # starts db and applies migrations
docker compose run --rm api python -m financial_hub.cli seed-demo
```

There is deliberately no `purge-test-operators` command: operators are referenced by sessions,
memberships, projects and the append-only audit log, so deleting them would mean touching those tables
with the owner role.

Never enable this in production: it would hand any visitor a persona's session (docs/SECURITY.md).

## Images

- `backend/Dockerfile`: targets `dev` (full deps incl. dev group, `--reload`) and `runtime` (production deps only,
  non-editable install, no uv, no source tree; includes `alembic.ini` + `migrations/`). Python 3.14-slim, uv 0.13.0,
  non-root user `app` (uid 1000), `uv sync --locked`, bytecode compiled.
- `frontend/Dockerfile`: targets `dev` (Vite dev server) and `prod` (nginx-unprivileged serving `dist`, `/api/` proxied to `api:8000`, listens on 8080, non-root). Base `node:24-alpine` (Node 24 LTS).

## Layout and boundaries

`backend/src/financial_hub/` follows `docs/ARCHITECTURE.md` section 3. Import contracts live in
`backend/pyproject.toml` (`[tool.importlinter]`) and cover: `shared` independent, `platform` depends only on
`shared`, each module's `domain` depends only on `shared`, modules never import each other's
`domain`/`infrastructure`/`api`, and `api` layers use only their own `application`. The first-listed contracts are the
architecture rules 1-3; rules 4-5 (cross-module facades, `intelligence` restrictions) are added with the first code that needs them.

## Version deviations and decisions

| Item | Planned | Used | Reason |
|---|---|---|---|
| TypeScript | 7.0.2 | **6.0.3** | `npm install` with TS 7.0.2 fails with ERESOLVE: `typescript-eslint@8.71.1` (latest) declares peer `typescript >=4.8.4 <6.1.0`. 6.0.3 is the latest TS 6.x. Revisit when typescript-eslint supports TS 7. |
| Node image | node:22-alpine or current LTS | node:24-alpine | Node 24 is the current LTS; all tools (Vite 8, Vitest 5) support it. `@types/node` pinned to 24.19.2 to match. |
| pydantic-settings, httpx, pytest-cov, hypothesis, import-linter | "latest compatible" | `>=` ranges in `pyproject.toml`; exact versions in `uv.lock` (pydantic-settings 2.15.0, httpx 0.28.1, pytest-cov 7.1.0, hypothesis 6.168.5, import-linter 2.15) | Only the stack-evaluation pins are exact in `pyproject.toml`. |
| pyright | 1.1.414 | `pyright[nodejs]==1.1.414` | The `nodejs` extra bundles Node so pyright works offline inside the image. |

All other pinned versions from `docs/research/stack-evaluation.md` resolved as listed (FastAPI 0.143.0, uvicorn 0.54.0,
pydantic 2.14.0, SQLAlchemy 2.1.4, Alembic 1.20.0, psycopg 3.3.6, structlog 26.1.0, pytest 9.1.1, pytest-asyncio 1.4.0,
ruff 0.17.0; Vite 8.3.4, React 19.3.0, Tailwind 4.3.3, TanStack Query 5.104.1, TanStack Router 1.170.42, Vitest 5.0.3).
`uv_build` is the build backend (range `>=0.13.0,<0.14.0` to match the pinned uv).
