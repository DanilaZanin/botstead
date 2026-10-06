#!/usr/bin/env bash
# Управление ботами через лаунчер: единственный сервис с docker.sock (docs/isolation.md).
# Запуск на сервере из любого каталога, от пользователя с доступом к docker:
#   ./launcherctl.sh create <bot-id> <owner-id>
#   ./launcherctl.sh recreate <bot-id>          пересоздать контейнер, том home сохраняется
#   ./launcherctl.sh remove <bot-id> [--purge]  --purge удаляет и том home
#   ./launcherctl.sh status <bot-id> | list | info
#   ./launcherctl.sh login-container <owner-id>
# Заголовок авторизации идёт в stdin curl внутри контейнера: секрета нет в argv curl и в ps хоста.
set -euo pipefail

LAUNCHER_CONTAINER=${LAUNCHER_CONTAINER:-bothub-launcher}
BOT_RE='^[a-z0-9][a-z0-9-]{0,31}$'
OWNER_RE='^[a-z0-9][a-z0-9-]{0,39}$'

call() { # метод путь [json-тело]
    docker exec "$LAUNCHER_CONTAINER" sh -c \
        'printf "Authorization: Bearer %s\n" "$LAUNCHER_SECRET" | curl -sS --fail-with-body --unix-socket "$LAUNCHER_SOCKET" -X "$1" -H @- -H "Content-Type: application/json" ${3:+-d "$3"} "http://launcher$2"' \
        _ "$1" "$2" "${3:-}"
    echo
}

need_bot() { [[ ${1:-} =~ $BOT_RE ]] || { echo "bot-id: строчные латинские буквы, цифры и дефис, до 32 знаков, первый не дефис" >&2; exit 2; }; }
need_owner() { [[ ${1:-} =~ $OWNER_RE ]] || { echo "owner-id: строчные латинские буквы, цифры и дефис, до 40 знаков, первый не дефис" >&2; exit 2; }; }

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

cmd=${1:-}
[ -n "$cmd" ] || usage
shift
case "$cmd" in
    create)
        need_bot "${1:-}"; need_owner "${2:-}"
        call POST /v1/bots "$(printf '{"bot_id":"%s","owner_id":"%s"}' "$1" "$2")"
        ;;
    recreate)
        need_bot "${1:-}"
        call POST "/v1/bots/$1/recreate"
        ;;
    remove)
        need_bot "${1:-}"
        if [ "${2:-}" = "--purge" ]; then call DELETE "/v1/bots/$1?purge=true"; else call DELETE "/v1/bots/$1"; fi
        ;;
    status)
        need_bot "${1:-}"
        call GET "/v1/bots/$1"
        ;;
    list)
        call GET /v1/bots
        ;;
    info)
        call GET /v1/info
        ;;
    login-container)
        need_owner "${1:-}"
        call POST "/v1/logins/$1"
        ;;
    *)
        usage
        ;;
esac
