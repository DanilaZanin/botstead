#!/usr/bin/env bash
# Создать контейнер бота через лаунчер. Образ, сеть, лимиты и mounts задаёт лаунчер, не этот скрипт.
# Использование: ./bot-create.sh <bot-id> <owner-id>   (owner-id из bots.owner_id)
set -euo pipefail

if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then
    echo "Usage: $0 <bot-id> <owner-id>" >&2
    exit 1
fi

exec "$(dirname "$0")/launcherctl.sh" create "$1" "$2"
