# macagent — Mac-агент Bot Hub

Реализует раздел 5 `docs/contracts.md`: LaunchAgent на Mac, который держит WebSocket
`/agent/mac` с ядром и выполняет инструменты (`find_files`, `read_file`, `upload_file`,
`preview`, `screenshot`, `open`, `applescript`, `shortcut`, `shell`, `click`, `type_text`,
`move_to_trash`). Зависимости: только stdlib + `websockets` (см. раздел 0 контракта).

## Структура

```
bothub_mac/
  client.py       WS-соединение: hello, heartbeat 30 с, обработка call → result, реконнект 1–30 с
  protocol.py     Формы сообщений (hello/heartbeat/result)
  config.py       Config.load(): BOTHUB_URL и MAC_AGENT_TOKEN из окружения или config.env
  permissions.py  detect_permissions() для hello.permissions (Accessibility, Screen Recording, файлы)
  state.py        is_locked(), battery_percent() для heartbeat
  tools/          Инструменты Mac: files.py, http.py (upload_file), media.py, automation.py, input.py
  __main__.py     python -m bothub_mac — точка входа для launchd
launchagent/com.bothub.macagent.plist   Шаблон LaunchAgent (путь подставляет install.sh)
install.sh        Установка: uv sync, config.env, launchd bootstrap
tests/            pytest (без реального Mac API — subprocess и ctypes замоканы через monkeypatch)
```

## Установка

Нужен Xcode CLT (для `uv`/сборки) и разрешения TCC, которые macOS запросит при первом
реальном вызове `click`/`type_text` (Accessibility) и `screenshot` (Screen Recording).

```bash
cd macagent
bash install.sh
```

Скрипт:
1. `uv sync` — ставит `.venv` с `websockets`.
2. Создаёт `~/Library/Application Support/BotHubMac/config.env` (`chmod 600`), если его ещё нет —
   впиши туда `BOTHUB_URL` и `MAC_AGENT_TOKEN` (значения из ядра, в git не попадают).
3. Подставляет путь в `launchagent/com.bothub.macagent.plist` и кладёт его в
   `~/Library/LaunchAgents/`.
4. `launchctl bootstrap`/`enable` — агент стартует сразу и при следующих входах в систему.

Повторный запуск `install.sh` безопасен: пересобирает `.venv`, не трогает существующий
`config.env`, перезагружает launchd-юнит.

Проверить: `launchctl print gui/$(id -u)/com.bothub.macagent`, лог — `/tmp/bothub-macagent.log`.
Остановить: `launchctl bootout gui/$(id -u)/com.bothub.macagent`.

## Конфигурация

`BOTHUB_URL` и `MAC_AGENT_TOKEN` (раздел 7 контракта) читаются в таком порядке:
переменные окружения → `~/Library/Application Support/BotHubMac/config.env`. Секретов
в репозитории нет: `config.env` живёт вне git, `chmod 600`.

## Разработка и тесты

```bash
cd macagent
uv sync
uv run pytest
```

Тесты мокают внешний мир (`subprocess.run`, `urllib.request.urlopen`, WS-соединение) —
реальный Mac (mdfind, screencapture, Accessibility) не требуется.
