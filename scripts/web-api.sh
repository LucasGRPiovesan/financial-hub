#!/usr/bin/env bash
# Regenerates the typed API client: dumps the backend OpenAPI schema to frontend/openapi.json (deterministic:
# sorted keys, 2-space indent, trailing newline), then runs orval in the web container.
# create_app() only builds the app (no lifespan, so no DB connection); the dump never touches the network.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

# Anything create_app() logs goes to stderr; only the schema reaches stdout.
dump='
import json, re, sys
out, sys.stdout = sys.stdout, sys.stderr
from financial_hub.main import create_app

spec = create_app().openapi()
# Routes whose {param} is read from request.path_params by an auth dependency (require()) are not declared
# in the schema, which makes the spec invalid for orval. Declare any undeclared path parameter as a string.
for path, item in spec["paths"].items():
    for method, op in item.items():
        if method not in ("get", "put", "post", "delete", "patch"):
            continue
        params = op.setdefault("parameters", [])
        declared = {q["name"] for q in params + item.get("parameters", []) if q.get("in") == "path"}
        for name in re.findall(r"{([^}]+)}", path):
            if name not in declared:
                params.append({"name": name, "in": "path", "required": True, "schema": {"type": "string", "title": name}})
        params.sort(key=lambda q: (q["in"], q["name"]))
out.write(json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
'
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
docker compose run --rm --no-deps -T api python -c "$dump" > "$tmp"
[ "$(head -c1 "$tmp")" = "{" ] || { echo "OpenAPI dump failed" >&2; exit 1; }
chmod 644 "$tmp"  # mktemp creates 0600; the web container user must read it
mv "$tmp" frontend/openapi.json
trap - EXIT

web npm run api:generate
