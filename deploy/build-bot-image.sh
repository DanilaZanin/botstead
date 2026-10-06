#!/usr/bin/env bash
# Собрать образ ботов bothub-bot. Необязательный agy ставится, только если заданы AGY_URL и AGY_SHA256
# (в окружении или в deploy/.env); сумма проверяется до установки.
# Запуск на сервере: ./build-bot-image.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f deploy/.env ]; then
    AGY_URL=${AGY_URL:-$(sed -n 's/^AGY_URL=//p' deploy/.env | tail -n1)}
    AGY_SHA256=${AGY_SHA256:-$(sed -n 's/^AGY_SHA256=//p' deploy/.env | tail -n1)}
fi

args=()
if [ -n "${AGY_URL:-}" ] || [ -n "${AGY_SHA256:-}" ]; then
    [ -n "${AGY_URL:-}" ] && [ -n "${AGY_SHA256:-}" ] || { echo "AGY_URL и AGY_SHA256 задаются вместе" >&2; exit 1; }
    args+=(--build-arg "AGY_URL=$AGY_URL" --build-arg "AGY_SHA256=$AGY_SHA256")
else
    echo "agy пропущен: AGY_URL и AGY_SHA256 не заданы (раннер gemini будет недоступен)"
fi

docker build ${args[@]+"${args[@]}"} -t bothub-bot -f bot-image/Dockerfile .
