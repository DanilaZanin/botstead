#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

LAUNCHER_SECRET=test BOT_TOKEN_SECRET=test docker compose -f docker-compose.yml config --no-env-resolution --format json |
    python3 -c 'import json, sys
launcher = json.load(sys.stdin)["services"]["launcher"]
assert int(launcher["mem_limit"]) == 512 * 1024 * 1024
assert launcher["pids_limit"] == 256
print("launcher compose limits passed")'
