# Bot Hub launcher

Единственный сервис с `docker.sock`. Создаёт, пересоздаёт и удаляет контейнеры ботов, запускает в них команды со
стримингом, ведёт логин-контейнеры с pty и ставит правила iptables для сетей ботов. Ядро ходит сюда через
`core/bothub/launcher_client.py`. Модель угроз и гарантии: `docs/isolation.md`.

## Запуск

```bash
cd launcher && uv run python -m bothub_launcher
```

В проде: сервис `launcher` в `deploy/docker-compose.yml` (сеть хоста, `NET_ADMIN`, `NET_RAW`, read-only rootfs).
Без прав на iptables, без цепочки `DOCKER-USER` или без `br_netfilter` процесс завершается с понятной ошибкой.
Правила IPv4 и IPv6 должны установиться до запуска остановленных ботов. Контейнеры имеют `--restart no`;
после перезапуска Docker их запускает лаунчер после `startup()` и восстановления правил.

| Переменная | Значение |
|---|---|
| `LAUNCHER_SECRET` | обязательна, не короче 32 символов; клиент шлёт `Authorization: Bearer <секрет>` |
| `BOT_TOKEN_SECRET` | обязательна, тот же, что у ядра: из него считаются токены ботов |
| `LAUNCHER_CONFIG` | путь к TOML (`deploy/launcher.toml`); без него действуют значения по умолчанию |
| `LAUNCHER_SOCKET` | unix-сокет, по умолчанию `/run/bothub-launcher/launcher.sock` (права 0660) |
| `LAUNCHER_LISTEN` | `unix:/путь` или `tcp://127.0.0.1:8099` вместо сокета |
| `LAUNCHER_RUNTIME` | `runsc` для gVisor (то же, что `runtime` в TOML) |

`owner_id` берётся из `bots.owner_id` и может быть UUID с дефисами длиной 36 символов. `bot_id` проверяется
шаблоном `^[a-z0-9][a-z0-9-]{0,31}$`. Текущий прямой Docker путь в ядре допускает ведущий дефис в `bot_id`;
при переключении ядра на лаунчер эту проверку надо привести к контракту. Том логинов называется
`bothub-login-<owner_id>` и принадлежит одному владельцу. Для `BOTHUB_RUNNER_EXEC=docker` ядро должно требовать
рабочее подключение к лаунчеру при старте. Подключение ядра, pty в PWA и вызов `recreate_bot` из ядра пока не выполнены.

## API

Тела запросов строгие: неизвестное поле даёт 400. Ошибки: `{"error": {"code": "...", "message": "..."}}`.

| Метод и путь | Тело | Ответ |
|---|---|---|
| `GET /v1/health` | | `{"ok": true}`, без секрета |
| `GET /v1/info` | | образ, runtime, лимиты, состояние сетевой политики |
| `POST /v1/bots` | `{bot_id, owner_id}` | 201, статус; повтор для работающего бота ничего не меняет |
| `GET /v1/bots`, `GET /v1/bots/{id}` | | статус (`exists: false`, если нет) |
| `DELETE /v1/bots/{id}?purge=true` | | `{removed}`; `purge` удаляет и том home |
| `POST /v1/bots/{id}/recreate` | | новый контейнер, том и владелец те же |
| `POST /v1/bots/{id}/browser-mode` | `{mode: "human" или "bot", url?}` | `{bot_id, mode, changed, cookies}`, идемпотентна. human: чистый Chromium без CDP, профиль человека, одна вкладка с `url`; bot: перенос cookies, удаление профиля человека, Chromium бота с CDP |
| `GET /v1/bots/{id}/browser-tab` | | `{bot_id, mode, url}`: адрес первой вкладки по CDP (в human и без ответа браузера `null`); служебные поверхности пропускаются, закрытая новая вкладка это `about:blank` |
| `POST /v1/networks/{owner}` | | `{owner_id, network, core_connected}`: сеть пользователя и подключение ядра без контейнера (ядро зовёт при создании пользователя) |
| `POST /v1/bots/{id}/procedure-step/cancel` | `{exec_id}` | остановить исполнитель шага процедуры: TERM, через `kill_grace` KILL группе процессов под uid 1001; `{bot_id, exec_id, stopped, was_running}`; нет бота, шаг чужой или это обычный exec: `404` |
| `POST /v1/bots/{id}/procedure-step` | `{payload_json, dry_run?, timeout?, exec_id?}` | шаг процедуры: `node procedure-step.mjs` под uid 1001, данные только в stdin; `{bot_id, exec_id, exit_code, reason, result}`, `result` разобранная JSON-строка исполнителя или `null`; `timeout` 5–180 с, один шаг на бота (`429 busy`), `409 frozen` в human и при заморозке |
| `POST /v1/bots/{id}/exec` | `{argv, env?, stdin?, exec_id?, timeout?}` | поток NDJSON (ниже) |
| `POST /v1/execs/{exec_id}/stop` | `{bot_id?}` | `{stopped, was_running}`; с `bot_id` работает и после рестарта лаунчера |
| `POST /v1/bots/{id}/screen-sessions` | `{owner_id}` | 201 `{session_id}`; один экран на бота, владелец сверяется с меткой контейнера |
| `GET /v1/screen-sessions/{id}/output` | | сырой поток RFB, `application/octet-stream`; обрыв клиента завершает `docker exec` |
| `POST /v1/screen-sessions/{id}/input` | сырые байты, до 64 КиБ | 204 |
| `DELETE /v1/screen-sessions/{id}` | | 204; повторное закрытие безопасно |
| `POST /v1/logins/{owner}` | | 201, логин-контейнер пользователя |
| `DELETE /v1/logins/{owner}` | | `{removed}` |
| `POST /v1/logins/{owner}/sessions` | `{command?, cols?, rows?}` | 201 `{session_id}`; `command` из списка `login_commands` |
| `GET /v1/login-sessions/{id}/output` | | поток NDJSON |
| `POST /v1/login-sessions/{id}/input` | сырые байты | 204 |
| `POST /v1/login-sessions/{id}/resize` | `{cols, rows}` | 204 |
| `DELETE /v1/login-sessions/{id}` | | 204 |

Статусы ошибок: 400 `invalid`, 401 `unauthorized`, 403 `not_managed` (объект без метки лаунчера), 404 `not_found`,
409 `conflict`, 429 `busy`, 502 `docker_error`, 503 `netpolicy`.

Кадры потока, по одному JSON в строке:

```
{"t":"start","exec_id":"..."}
{"t":"out","s":"stdout"|"stderr","d":"<base64>"}
{"t":"ping"}                                          каждые 15 с молчания
{"t":"exit","code":0,"reason":"exit"|"timeout"|"stopped"}
{"t":"error","code":"...","message":"..."}
```

`exec_id` выбирает вызывающий (удобно взять id turn): по нему работает `stop`. Таймаут и обрыв соединения убивают
группу процессов в контейнере. Лимиты: до 8 одновременных команд на бота, до 64 всего, до 32 экранов всего.

## Фиксированные параметры

Образ, mounts, сеть, лимиты, пользователь, DNS, runtime, исключения сети и список команд логина задаёт только
конфигурация. Бот получает `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory 2g`, `--cpus 2`,
`--pids-limit 512`, `--read-only`, tmpfs для `/tmp` и `/run`, том home и том логинов владельца read-only.
`/run` принадлежит uid/gid 1000 и доступен для `entrypoint.sh`; `--ulimit nofile=1024:1024` ограничивает дескрипторы.
Параметр `internal_dns` в TOML задаёт IPv4 адрес внутреннего резолвера. Бот получает этот DNS, а сетевые правила
разрешают к нему UDP и TCP на порт 53. Создаваемые сети отключают IPv6; контейнеры также отключают его через sysctl.
Если IPv6 правила не установились, лаунчер завершает запуск.
Для существующей помеченной сети с `EnableIPv6=true` лаунчер сначала восстанавливает правила IPv4/IPv6, затем
блокирует запуск остановленных ботов. Отключите IPv6 и пересоздайте эту сеть в окне обслуживания.
При переносе старого тома лаунчер создаёт отдельный том `<новый-том>-complete` после очистки каталогов
`.claude`, `.codex`, `.gemini`, `.auth` и установки
владельца. Этот том не монтируется боту. Незавершённая копия не используется; с `adopt_legacy_volumes = true`
копирование повторяется.
Сборка аргументов: `bothub_launcher/docker_args.py`; правила сети: `bothub_launcher/netpolicy.py`.

## Тесты

```bash
cd launcher && uv run pytest            # Docker не нужен
cd launcher && uv run pytest -m docker  # на хосте с Docker, iptables и запущенным bothub-core
../deploy/tests/isolation_check.sh      # проверка фактом на сервере
```

Тесты pty пропускаются там, где `os.openpty()` запрещён (песочницы). `tests/test_wire_compat.py` гоняет клиент ядра
против приложения лаунчера на фейковом Docker, поэтому изменения формата кадров ломают его сразу.

## Переключение ядра на клиент

Клиент и фейк готовы, ядро ещё ходит в `docker` напрямую. Что заменить:

`core/bothub/main.py`
- строки 29 и 285: `docker_drafter` заменить на конструктор через `launcher.exec` (см. `builder.py` ниже);
- строка 32: убрать импорт `container_kill_command`;
- строки 130 и 147: удалить `build_docker_run_args` и `run_docker`;
- строки 439 и 442: вместо префикса `docker exec` передавать раннеру `launcher` и имя бота, env `BOTHUB_TURN_ID`,
  `BOTHUB_THREAD_ID` уходят в `exec(env=...)`;
- строки 676–683: очистка зависших turn это `launcher.stop_exec(str(row['id']), bot_id=row['bot_id'])`;
- строки 881–887: `await app.state.launcher.create_bot(bot['id'], owner_id)` вместо `run_docker(build_docker_run_args(...))`;
- `create_app()` (строка 280) принимает `launcher=None`, берёт `launcher_client_from_env()`, закрывает его в `lifespan`.

`core/bothub/runner/subprocess.py`
- строки 11–29: `docker_marker_wrap` и `container_kill_command` уже живут в лаунчере, удалить;
- строки 38 и 54–69: ветка `exec_prefix[0] == "docker"` и `create_subprocess_exec` заменяются на
  `async for ev in launcher.exec(...)` с `LineSplitter` для stdout и `ExecExit.code` вместо `process.wait()`;
- строки 98–125: `stop()` и `_kill_in_container` заменяются на `launcher.stop_exec(turn_id, bot_id=...)`;
- `core/bothub/runner/base.py` строка 15: у `TurnContext` вместо `exec_prefix` поля `launcher` и `bot_container_id`.

`core/bothub/builder.py` строки 114–135: `_first_bot_container` и `docker_drafter` переписать на `launcher.list_bots()` и
`launcher.exec(...)`.

Тесты, которые придётся обновить: `core/tests/runner/test_parsers.py` (строки 253–333, маркер и pkill) и
`core/tests/contract/test_contract_api.py` (строки 1079–1133, подмена `bothub.main.run_docker`): их заменяют проверки
с `FakeLauncherClient`.
