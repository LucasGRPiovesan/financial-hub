#!/usr/bin/env bash
# End-to-end suite (Playwright, docs/TESTING.md): brings up db/migrate/api/web with demo login on in a
# dedicated compose project (own database, never the dev data that backend tests fill with operators),
# seeds the demo workspace (idempotent), then runs the suite in the pinned Playwright container.
# Usage: scripts/e2e.sh [playwright test args], e.g. scripts/e2e.sh --grep "API keys".
# Reuses existing images (nothing is built unless an image is missing); one run at a time, host-wide.
# E2E_KEEP_UP=1 leaves the stack and its database running afterwards (faster reruns); otherwise the
# stack is removed with its database volume, so every run starts freshly migrated and freshly seeded.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

export E2E_PROJECT="${E2E_PROJECT:-financial-hub-e2e}"
# Ephemeral host ports: the dev stack may be running on the usual ones, and the suite reaches the
# console from inside the compose network anyway.
export WEB_PORT=0 API_PORT=0 DB_PORT=0
dc() { docker compose -f compose.yaml -f deploy/compose.e2e.yaml -p "$E2E_PROJECT" "$@"; }

run_e2e() {
  local status=0
  # `api` has FH_DEMO_LOGIN=true in deploy/compose.yaml; `web` waits for it to be healthy.
  if dc up -d --wait web && dc run --rm --no-deps api python -m financial_hub.cli seed-demo; then
    dc --profile e2e run --rm e2e test "$@" || status=$?
  else
    status=1
    dc logs --no-color --tail 80 api web || true
  fi
  [[ "${E2E_KEEP_UP:-}" == 1 ]] || { dc --profile e2e down --remove-orphans && docker volume rm "${E2E_PROJECT}_db-data"; } >/dev/null 2>&1 || true
  echo
  echo "Playwright HTML report: $PWD/e2e/playwright-report/index.html"
  return "$status"
}

with_lock run_e2e "$@"
