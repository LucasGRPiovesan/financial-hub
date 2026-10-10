#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
for s in lint typecheck imports test web-lint web-typecheck web-test web-build; do
  echo "==> $s"
  "$(dirname "${BASH_SOURCE[0]}")/$s.sh"
done
