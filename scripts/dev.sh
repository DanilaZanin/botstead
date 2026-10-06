#!/usr/bin/env bash
# Локальный стенд Bot Hub: Postgres + ядро (fake-раннер) + сиды через API.
# PWA открывается на самом ядре: http://127.0.0.1:8000/
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

export DATABASE_URL="${DATABASE_URL:-postgresql://bothub:bothub@127.0.0.1:55432/bothub}"
export OWNER_TOKEN="${OWNER_TOKEN:-dev-owner}"
export BOT_TOKEN_SECRET="${BOT_TOKEN_SECRET:-dev-secret}"
export MAC_AGENT_TOKEN="${MAC_AGENT_TOKEN:-dev-mac}"
export BOTHUB_RUNNER_EXEC=local
export FILES_DIR="${FILES_DIR:-/tmp/bothub-dev-files}"
mkdir -p "$FILES_DIR"

docker compose -f deploy/docker-compose.dev.yml up -d

echo "Жду Postgres..."
until docker compose -f deploy/docker-compose.dev.yml exec -T db pg_isready -U bothub >/dev/null 2>&1; do sleep 0.5; done

(cd core && uv sync --quiet && uv run python "$ROOT/scripts/dev_server.py") &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

echo "Жду ядро на 127.0.0.1:8000..."
until curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID"
    echo "Ядро завершилось до готовности API" >&2
    exit 1
  fi
  sleep 0.3
done

echo "Создаю тестового администра и сею данные..."
OWNER=(-H "Authorization: Bearer $OWNER_TOKEN" -H "Content-Type: application/json")
post() {
  local response
  if ! response=$(curl --fail-with-body -sS "${OWNER[@]}" -X POST -d "$2" "http://127.0.0.1:8000$1"); then
    echo "$response" >&2
    return 1
  fi
  printf '%s\n' "$response"
}
bot_id() { python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }

setup_json=$(BOTHUB_DEV_EMAIL="${BOTHUB_DEV_EMAIL:-dev@example.com}" BOTHUB_DEV_PASSWORD="${BOTHUB_DEV_PASSWORD:-dev-local-password}" python3 -c 'import json,os; print(json.dumps({"email":os.environ["BOTHUB_DEV_EMAIL"],"password":os.environ["BOTHUB_DEV_PASSWORD"]}))')
post /api/setup "$setup_json" >/dev/null

scout_id=$(post /api/bots '{"id":"scout","name":"Скаут","role":"Ищет вакансии и заявки","provider":"fake","model":"fake","avatar":"scout","executor":"container"}' | bot_id)
mac_id=$(post /api/bots '{"id":"mac","name":"Мак","role":"Ищет файлы, открывает приложения, делает скриншоты","provider":"fake","model":"fake","avatar":"mac","executor":"mac","mac_full_control":true}' | bot_id)
sre_id=$(post /api/bots '{"id":"sre","name":"SRE","role":"Мониторинг app.example.com и разбор инцидентов","provider":"fake","model":"fake","avatar":"sre","executor":"container"}' | bot_id)
coder_id=$(post /api/bots '{"id":"coder","name":"Кодер","role":"Пишет и правит код в контейнере","provider":"fake","model":"fake","avatar":"coder","executor":"container"}' | bot_id)
archive_id=$(post /api/bots '{"id":"archive","name":"Архив","role":"Раскладывает файлы и сканы по папкам","provider":"fake","model":"fake","avatar":"archive","executor":"mac","mac_full_control":true}' | bot_id)

post /api/schedules "{\"bot_id\":\"$sre_id\",\"name\":\"Проверка app.example.com\",\"kind\":\"cron\",\"cron\":\"0 3 * * *\",\"timezone\":\"Europe/Moscow\",\"prompt\":\"Проверить здоровье app.example.com\"}" >/dev/null

post /api/memory "{\"text\":\"Production database runs on db1.example.com\",\"bot_id\":\"$sre_id\"}" >/dev/null
post /api/memory '{"text":"Staging environment runs on staging.example.com"}' >/dev/null

echo "Готово: http://127.0.0.1:8000/"
wait $SERVER_PID
