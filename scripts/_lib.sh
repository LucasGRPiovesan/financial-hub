#!/usr/bin/env bash
# Shared helpers: every project tool runs inside Docker, never on the host.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Backend tools: no dependencies started (use `test_db` for tests that need Postgres).
api() { docker compose run --rm --no-deps api "$@"; }
api_db() { docker compose run --rm api "$@"; }
# Tests: opt-in `test` service (owner credentials for fixtures/DDL + FH_APP_TEST_DATABASE_URL).
test_db() { docker compose run --rm test "$@"; }
web() { docker compose run --rm --no-deps web "$@"; }
