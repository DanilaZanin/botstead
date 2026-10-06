#!/bin/bash
# Установка Mac-агента Bot Hub как LaunchAgent пользователя.
# Идемпотентен: можно запускать повторно после обновления кода.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_LABEL="com.bothub.macagent"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
CONFIG_DIR="$HOME/Library/Application Support/BotHubMac"
CONFIG_FILE="$CONFIG_DIR/config.env"

echo "==> Ставлю зависимости (uv sync)"
(cd "$DIR" && uv sync)

if [ ! -f "$CONFIG_FILE" ]; then
  echo "==> Создаю $CONFIG_FILE (заполни BOTHUB_URL и MAC_AGENT_TOKEN)"
  mkdir -p "$CONFIG_DIR"
  cat > "$CONFIG_FILE" <<'EOF'
BOTHUB_URL=https://bots.example.com
MAC_AGENT_TOKEN=
EOF
  chmod 600 "$CONFIG_FILE"
  echo "    Файл создан с пустым MAC_AGENT_TOKEN — заполни его перед запуском:"
  echo "    ${CONFIG_FILE}"
else
  echo "==> $CONFIG_FILE уже существует, не трогаю"
fi

echo "==> Готовлю ${PLIST_LABEL}.plist"
sed "s#__BOTHUB_MAC_DIR__#${DIR}#g" "$DIR/launchagent/${PLIST_LABEL}.plist" > "$PLIST_DST"

if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
  echo "==> Перезагружаю уже запущенный агент"
  launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1 || true
fi

echo "==> Запускаю launchd-агент"
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/${PLIST_LABEL}"

echo "==> Готово. Лог: /tmp/bothub-macagent.log"
echo "    Проверить статус: launchctl print gui/$(id -u)/${PLIST_LABEL}"
