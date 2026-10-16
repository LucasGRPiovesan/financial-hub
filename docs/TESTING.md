# Testing

Backend and frontend unit/integration suites are described in `docs/DEVELOPMENT.md` (Tooling commands).
This page covers the browser suite.

## Backend test database

Backend tests never touch the developer database (the one `api`, `web` and `seed-demo` use). At the start
of every pytest run `tests/conftest.py` calls `tests/testdb.py`, which, as the owner login and on the same
Postgres server: derives `<dev database>_test` (`financial_hub_test`), creates it if missing, runs
`alembic upgrade head` on it (so it includes every migration present in the working tree, and the `fh_app`
role/grants are exactly the migrations') and points `FH_DATABASE_URL` and `FH_APP_TEST_DATABASE_URL` at it.
It is transparent: `bash scripts/test.sh <paths>` is unchanged, locally and in CI. If no server is reachable
the step is skipped (unit tests still run). Rows accumulate in the test database across runs (nothing is
deletable by design); to start clean, or after editing a migration that was already applied, run
`FH_TEST_DB_RECREATE=1 bash scripts/test.sh ...` (drops and recreates it) or
`docker compose exec db psql -U financial_hub -c "DROP DATABASE financial_hub_test WITH (FORCE)"`.

The demo picker is bounded (100 operators) and ordered by role then email, so an operator created by a
test in the `demo.financial-hub.local` domain can in principle crowd the list in a very old test database;
recreate it if that ever happens.

## End-to-end (Playwright)

Journeys that drive the real console in Chromium, acting as the demo operators, against the real API and
database. Everything runs in Docker; the host needs no Node.

### Run it

```bash
bash scripts/e2e.sh                         # whole suite
bash scripts/e2e.sh --grep "API keys"       # any `playwright test` argument
E2E_KEEP_UP=1 bash scripts/e2e.sh           # leave the stack up for faster reruns
```

`scripts/e2e.sh` brings up `db`, `migrate`, `api` (demo login on) and `web` in a **dedicated compose project**
(`financial-hub-e2e`, its own database, ephemeral host ports; the dev stack and its data are never touched,
and the operators that backend tests leave in the dev database cannot crowd the demo picker). It then runs
`seed-demo` (idempotent), runs the suite in the pinned `mcr.microsoft.com/playwright` image (service `e2e`,
profile `e2e`), and stops the stack. It prints the report path: `e2e/playwright-report/index.html`; traces,
screenshots and videos of failures are under `e2e/test-results/`. Open a trace with
`npx playwright show-trace <trace.zip>` (or <https://trace.playwright.dev>).

- The API runs without `--reload` here (`deploy/compose.e2e.yaml`): the dev reload watcher polls the bind
  mount and starves the browser on a constrained host.
- One run at a time, host-wide (the same lock as `scripts/test.sh`; `FH_NO_TEST_LOCK=1` skips it).
- The Playwright version is pinned in two places that must move together: `e2e/package.json`
  (`@playwright/test`) and the image tag of the `e2e` service in `deploy/compose.yaml`.
- Against another environment: `E2E_BASE_URL=https://... npx playwright test` from `e2e/` (it must have demo
  login enabled and the demo workspace seeded).
- CI: workflow `E2E` (`.github/workflows/e2e.yml`) on pushes to `main` and manual dispatch; the report is
  uploaded as an artifact when it fails.

### Personas

Seeded by `python -m financial_hub.cli seed-demo` into project "Demo Store" (`e2e/support/personas.ts`):

| Persona | Role | Use it for |
|---|---|---|
| Ana Owner (`ana@demo.financial-hub.local`) | owner | everything, creating projects and API keys |
| Bruno Admin | admin | management without ownership |
| Carla Finance | finance | money screens |
| Diego Viewer | viewer | proving that controls are gated |

### Write a journey

1. Add `e2e/tests/<area>.spec.ts`; import `test`, `expect`, `PERSONAS` from `../fixtures`.
2. Get a signed-in page with `const page = await loginAs(PERSONAS.ana)`. It signs in through the API
   (`POST /v1/auth/demo-login`, cookie carried via `storageState`), one session per call, so a journey
   that signs out cannot break another. Use the UI picker (`pages/login-page.ts`) only for journeys about
   signing in.
3. Drive screens through page objects in `e2e/pages/`, using **role/label/text locators only**
   (`getByRole`, `getByLabel`, `getByText`): never CSS classes, ids or layout, since screens are redesigned
   often. If an element has no stable accessible name, fix the UI (add the name) rather than reaching for a
   selector.
4. Create data with unique names (`uniqueName("key")`, `uniqueSlug()` from `support/data.ts`) so reruns and
   parallel workers never collide; never depend on data another test created.
5. Navigate between screens by clicking links where you can. Note: in the Vite dev server a hard load of a
   path starting with `/api` (e.g. `/api-keys`) is routed to the API proxy; `ApiKeysPage.goto()` therefore
   enters through `/`.
6. A journey for a screen that does not exist yet is `test.fixme(...)` with a `TODO(<screen>)` comment
   (see `tests/pending-screens.spec.ts`).

### Flake policy

- **No sleeps**: no `waitForTimeout`, no fixed delays. Wait for what the user would see.
- **Web-first assertions** (`await expect(locator).toBeVisible()`, `toHaveURL`, `toHaveAccessibleName`):
  they retry until the timeout; never assert on a value read once (`isVisible()`, `textContent()`).
- **One retry, only in CI** (`retries: 1` when `CI` is set; none locally). A test that needs the retry is
  flaky: open the trace, fix the cause, do not raise the retry count.
- A journey that fails because a screen is mid-redesign becomes `test.fixme` with a note in the ticket,
  not a loosened assertion.
- `global-setup.ts` warms the Vite dev server (first requests compile modules) so journeys need no inflated
  timeouts.
