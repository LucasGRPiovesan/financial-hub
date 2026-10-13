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

# Run "$@" while holding a host-wide lock, so parallel agents never run two test suites at once
# (each one saturates the CPUs it is given). A lock is taken over when its owner is gone for 10
# minutes (pids of other shells may be invisible here) or after 45 minutes in any case.
# FH_NO_TEST_LOCK=1 skips it (CI runners are single-tenant).
with_lock() {
  if [[ "${FH_NO_TEST_LOCK:-}" == 1 ]]; then "$@"; return; fi
  local lock="/tmp/financial-hub-test.lock" waited=0
  until mkdir "$lock" 2>/dev/null; do
    local owner
    owner="$(cat "$lock/pid" 2>/dev/null || true)"
    local age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || date +%s) ))
    if (( age > 2700 )) || { (( age > 600 )) && ! kill -0 "${owner:-0}" 2>/dev/null; }; then
      rm -rf "$lock"
      continue
    fi
    if (( waited % 30 == 0 )); then echo "waiting for another test run (pid ${owner:-?}) ..." >&2; fi
    sleep 2
    waited=$((waited + 2))
  done
  echo $$ >"$lock/pid"
  trap 'rm -rf "'"$lock"'"' EXIT
  "$@"
}
