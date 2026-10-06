#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if ./bot-create.sh sample-bot >/dev/null 2>&1; then
    echo 'bot-create accepted a missing owner-id' >&2
    exit 1
fi
if ./login.sh >/dev/null 2>&1; then
    echo 'login accepted a missing owner-id' >&2
    exit 1
fi
printf 'owner-id is required by entrypoint scripts\n'
