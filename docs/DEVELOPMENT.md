# Development

The host runs **only Docker**. Python, Node and every tool (ruff, pyright, pytest, eslint, vite...) run in containers.

## Quick start

```bash
cp .env.example .env          # no real secrets in it; .env is git-ignored
docker compose up -d --build  # from the repo root
docker compose ps             # db, api, worker, web, lgtm healthy; migrate exited 0
```

| Service | URL / port | Notes |
|---|---|---|
| `api` | http://localhost:8000 (`/health/live`, `/health/ready`, `/docs`) | uvicorn `--reload`, source bind-mounted |
| `web` | http://localhost:5173 | Vite dev server; `/api/*` is proxied to `api:8000` (prefix stripped) |
| `db` | localhost:5432 | postgres:18, named volume `db-data` |
| `migrate` | - | one-shot `alembic upgrade head`; api and worker wait for it |
| `worker` | - | `python -m financial_hub.workers` (placeholder loop until M3) |
| `lgtm` | Grafana :3000, OTLP gRPC :4317, OTLP HTTP :4318 | `grafana/otel-lgtm:0.36.0` |

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
| Tests (unit + integration, needs db) | `scripts/test.sh [pytest args]` | `docker compose run --rm test pytest` |
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
