"""Доступ к Docker. `Backend` это контракт для сервиса, `DockerCLIBackend` реализует его через docker CLI.

Разрушающие операции повторно проверяют метку лаунчера сами: даже ошибка в сервисе не даст удалить чужое."""
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from . import docker_args as da
from .config import Config
from .errors import BackendError, Conflict, NetPolicyError, NotManaged
from .procs import CmdResult, PtyProcess, SubprocessExec, SubprocessStream


# Строка файла режима: `bot`, `human` или `human <токен сессии человека>`. Токен лаунчер генерирует на каждый перехват,
# супервизор возобновляет каталог человека только при совпадении токена в маркере сессии.
_SESSION_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
# Chromium отдаёт по CDP как «страницы» и свои служебные поверхности (chrome://omnibox-popup.top-chrome/, расширения, devtools):
# вкладками они не считаются (как isUserTab в bot-image/procedure-step.mjs). Новая вкладка chrome://newtab/ закрыта политикой и
# для вызывающего выглядит как about:blank.
_NEW_TAB_URLS = frozenset({"chrome://newtab/", "chrome://new-tab-page/"})
_SERVICE_URL_PREFIXES = ("chrome://", "chrome-extension://", "chrome-untrusted://", "chrome-error://", "devtools://")

log = logging.getLogger("bothub_launcher")

# `docker network connect --gw-priority` появился в Docker 28.
GW_PRIORITY_MIN_MAJOR = 28
_DOCKER_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+][0-9A-Za-z.+-]*)?")


def parse_docker_version(text: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) из `29.1.3`, `28.0.0-rc.1`, `v27.5.1+azure`. Мусор и пустая строка: None."""
    match = _DOCKER_VERSION_RE.fullmatch(text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def parse_browser_mode_line(line: str) -> str:
    """bot, human или invalid. Токен у bot, чужой вид токена и лишние слова это invalid."""
    mode, _, token = line.strip().partition(" ")
    if mode == "bot" and not token:
        return "bot"
    if mode == "human" and (not token or _SESSION_TOKEN_RE.fullmatch(token)):
        return "human"
    return "invalid"


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    id: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)
    running: bool = False
    status: str = "created"
    exit_code: int = 0
    oom_killed: bool = False
    started_at: str = ""
    restart_count: int = 0
    image: str = ""
    networks: tuple[str, ...] = ()
    pid: int = 0


@dataclass(frozen=True)
class NetworkInfo:
    name: str
    id: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)
    bridge: str = ""
    core_ip: str | None = None
    core_ipv6: str | None = None
    ipv6_enabled: bool = False


class ExecProcess(Protocol):
    async def write_stdin(self, data: bytes) -> None: ...
    def output(self) -> AsyncIterator[tuple[str, bytes]]: ...
    async def wait(self) -> int: ...
    async def kill(self) -> None: ...


class PtyHandle(Protocol):
    async def read(self) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def resize(self, cols: int, rows: int) -> None: ...
    async def wait(self) -> int: ...
    def kill(self) -> None: ...


class StreamProcess(Protocol):
    async def read(self) -> bytes: ...
    async def write_stdin(self, data: bytes) -> None: ...
    async def wait(self) -> int: ...
    async def kill(self) -> None: ...


class Backend(Protocol):
    async def ensure_volume(self, name: str, owner_id: str, kind: str) -> None: ...
    async def remove_volume(self, name: str, owner_id: str, kind: str) -> None: ...
    async def detect_docker_version(self) -> None: ...
    async def ensure_network(self, owner_id: str) -> NetworkInfo: ...
    async def list_networks(self) -> list[NetworkInfo]: ...
    async def inspect_container(self, name: str) -> ContainerInfo | None: ...
    async def list_containers(self, role: str | None = None) -> list[ContainerInfo]: ...
    async def run_bot(self, bot_id: str, owner_id: str) -> str: ...
    async def run_login(self, owner_id: str) -> str: ...
    async def start_container(self, name: str) -> None: ...
    async def disable_restart(self, name: str) -> None: ...
    async def remove_container(self, name: str) -> bool: ...
    async def spawn_exec(self, container: str, argv: list[str], env: dict[str, str], exec_id: str) -> ExecProcess: ...
    async def kill_in_container(self, container: str, exec_id: str, signal_name: str) -> None: ...
    async def spawn_procedure_step(self, container: str, exec_id: str) -> ExecProcess: ...
    async def kill_procedure_step(self, container: str, exec_id: str, signal_name: str) -> None: ...
    async def spawn_pty(self, container: str, argv: list[str], cols: int, rows: int) -> PtyHandle: ...
    async def spawn_screen(self, container: str) -> StreamProcess: ...
    async def browser_running(self, container: str) -> bool: ...
    async def start_browser(self, container: str) -> None: ...
    async def get_browser_mode(self, container: str) -> str: ...
    async def set_browser_mode(self, container: str, mode: str, url: str | None) -> None: ...
    async def browser_mode_ready(self, container: str, mode: str) -> bool: ...
    async def browser_merge_status(self, container: str) -> str: ...
    async def browser_tab_url(self, container: str) -> str | None: ...
    async def terminate_bot_processes(self, container: str, signal_name: str = "TERM") -> None: ...
    async def bot_processes(self, container: str) -> list[int]: ...


Runner = Callable[..., Awaitable[CmdResult]]


def _short(text: str, limit: int = 300) -> str:
    return text.strip()[:limit]


def _is_missing(res: CmdResult) -> bool:
    low = res.err.lower()
    return res.rc != 0 and res.rc != 127 and ("no such" in low or "not found" in low)


class DockerCLIBackend:
    def __init__(self, cfg: Config, runner: Runner, *, spawn=None, spawn_pty=None, spawn_stream=None):
        self.cfg = cfg
        self._run = runner
        self._spawn = spawn or SubprocessExec.start
        self._spawn_pty = spawn_pty or PtyProcess.start
        self._spawn_stream = spawn_stream or SubprocessStream.start
        self._volume_names: dict[str, str] = {}
        # Решается один раз в detect_docker_version при старте. До этого флаг не передаётся.
        self._gw_priority_supported = False

    # ---------- низкий уровень ----------

    async def _docker(self, argv: list[str], what: str, *, env: Mapping[str, str] | None = None,
                      input: str | None = None) -> CmdResult:
        kwargs = {key: value for key, value in (("env", env), ("input", input)) if value}
        res = await self._run(argv, **kwargs)
        if res.rc == 127:
            raise BackendError(f"{what}: docker не найден ({self.cfg.docker_bin})")
        return res

    def _fail(self, res: CmdResult, what: str) -> BackendError:
        return BackendError(f"{what}: {_short(res.err) or _short(res.out) or f'код {res.rc}'}")

    async def _inspect(self, kind: str, names: list[str]) -> list[dict] | None:
        argv = [self.cfg.docker_bin, kind, "inspect", *names] if kind != "container" \
            else [self.cfg.docker_bin, "inspect", "--type", "container", *names]
        res = await self._docker(argv, f"inspect {names[0]}")
        if _is_missing(res):
            return None
        if res.rc != 0:
            raise self._fail(res, f"inspect {names[0]}")
        try:
            data = json.loads(res.out)
        except json.JSONDecodeError as exc:
            raise BackendError(f"inspect {names[0]}: ответ не JSON") from exc
        return data if isinstance(data, list) else None

    def _managed(self, labels: Mapping[str, str] | None) -> bool:
        return (labels or {}).get(self.cfg.label_key) == self.cfg.label_value

    @staticmethod
    def _container(raw: dict) -> ContainerInfo:
        state = raw.get("State") or {}
        cfg = raw.get("Config") or {}
        nets = ((raw.get("NetworkSettings") or {}).get("Networks")) or {}
        return ContainerInfo(
            name=str(raw.get("Name", "")).lstrip("/"), id=str(raw.get("Id", "")),
            labels=cfg.get("Labels") or {}, running=bool(state.get("Running")),
            status=str(state.get("Status", "")), exit_code=int(state.get("ExitCode") or 0),
            oom_killed=bool(state.get("OOMKilled")), started_at=str(state.get("StartedAt", "")),
            restart_count=int(raw.get("RestartCount") or 0), image=str(cfg.get("Image", "")),
            networks=tuple(nets),
            pid=int(state.get("Pid") or 0),
        )

    def _network(self, raw: dict) -> NetworkInfo:
        core_ip = None
        core_ipv6 = None
        for item in (raw.get("Containers") or {}).values():
            if item.get("Name") == self.cfg.core_container:
                core_ip = str(item.get("IPv4Address", "")).split("/")[0] or None
                core_ipv6 = str(item.get("IPv6Address", "")).split("/")[0] or None
        nid = str(raw.get("Id", ""))
        bridge = (raw.get("Options") or {}).get(da.BRIDGE_OPT) or f"br-{nid[:12]}"
        return NetworkInfo(name=str(raw.get("Name", "")), id=nid, labels=raw.get("Labels") or {},
                           bridge=bridge, core_ip=core_ip, core_ipv6=core_ipv6,
                           ipv6_enabled=bool(raw.get("EnableIPv6")))

    # ---------- контейнеры ----------

    async def inspect_container(self, name: str) -> ContainerInfo | None:
        data = await self._inspect("container", [name])
        return self._container(data[0]) if data else None

    async def list_containers(self, role: str | None = None) -> list[ContainerInfo]:
        argv = [self.cfg.docker_bin, "ps", "-a", "--filter", f"label={self.cfg.label_key}={self.cfg.label_value}"]
        if role:
            argv += ["--filter", f"label=bothub.role={role}"]
        argv += ["--format", "{{.Names}}"]
        res = await self._docker(argv, "ps")
        if res.rc != 0:
            raise self._fail(res, "ps")
        names = res.out.split()
        if not names:
            return []
        data = await self._inspect("container", names) or []
        return [self._container(raw) for raw in data]

    async def _start(self, spec: da.RunSpec, what: str) -> str:
        res = await self._docker(spec.argv, what, env=spec.env)
        if res.rc != 0:
            if "is already in use" in res.err:
                raise Conflict(f"{what}: имя контейнера занято")
            raise self._fail(res, what)
        return res.out.strip()

    async def run_bot(self, bot_id: str, owner_id: str) -> str:
        return await self._start(da.bot_run_args(
            self.cfg, bot_id, owner_id, home_volume=self._volume_names.get(self.cfg.home_volume(bot_id)),
            login_volume=self._volume_names.get(self.cfg.login_volume(owner_id)),
            browser_volume=self._volume_names.get(self.cfg.browser_volume(bot_id))), f"создание бота {bot_id}")

    async def run_login(self, owner_id: str) -> str:
        return await self._start(da.login_run_args(
            self.cfg, owner_id, login_volume=self._volume_names.get(self.cfg.login_volume(owner_id))),
            f"создание логин-контейнера {owner_id}")

    async def start_container(self, name: str) -> None:
        info = await self.inspect_container(name)
        if info is None or not self._managed(info.labels) or info.labels.get("bothub.role") != "bot":
            raise NotManaged(f"контейнер {name} не принадлежит лаунчеру")
        res = await self._docker([self.cfg.docker_bin, "start", name], f"запуск {name}")
        if res.rc != 0:
            raise self._fail(res, f"запуск {name}")

    async def disable_restart(self, name: str) -> None:
        info = await self.inspect_container(name)
        if info is None or not self._managed(info.labels) or info.labels.get("bothub.role") != "bot":
            raise NotManaged(f"контейнер {name} не принадлежит лаунчеру")
        res = await self._docker([self.cfg.docker_bin, "update", "--restart=no", name],
                                 f"отключение автозапуска {name}")
        if res.rc != 0:
            raise self._fail(res, f"отключение автозапуска {name}")

    async def remove_container(self, name: str) -> bool:
        info = await self.inspect_container(name)
        if info is None:
            return False
        if not self._managed(info.labels):
            raise NotManaged(f"контейнер {name} без метки лаунчера")
        res = await self._docker([self.cfg.docker_bin, "rm", "-f", name], f"удаление {name}")
        if res.rc != 0 and not _is_missing(res):
            raise self._fail(res, f"удаление {name}")
        return True

    # ---------- сети и тома ----------

    async def detect_docker_version(self) -> None:
        """Версия daemon определяется один раз при старте: от неё зависит, можно ли передавать `--gw-priority`.
        Меньше 28 или версия не разобрана: флаг не передаётся и в журнал уходит одно предупреждение."""
        self._gw_priority_supported = False
        try:
            res = await self._docker([self.cfg.docker_bin, "version", "--format", "{{.Server.Version}}"],
                                     "docker version")
        except BackendError as exc:
            log.warning("версия Docker неизвестна (%s): --gw-priority не передаётся", exc)
            return
        raw = res.out.strip()
        version = parse_docker_version(raw) if res.rc == 0 else None
        if version is None:
            log.warning("версия Docker неизвестна (%s): --gw-priority не передаётся, шлюзом ядра может стать сеть "
                        "пользователя", _short(raw or res.err, 80) or f"код {res.rc}")
        elif version[0] < GW_PRIORITY_MIN_MAJOR:
            log.warning("Docker %s старше %d: --gw-priority не передаётся, шлюзом ядра может стать сеть пользователя "
                        "(обновите Docker)", raw, GW_PRIORITY_MIN_MAJOR)
        else:
            self._gw_priority_supported = True

    def _connect_core_args(self, network: str) -> list[str]:
        args = [self.cfg.docker_bin, "network", "connect", "--alias", self.cfg.core_alias]
        if self._gw_priority_supported:
            args += ["--gw-priority", str(self.cfg.core_gw_priority)]
        return [*args, network, self.cfg.core_container]

    async def ensure_network(self, owner_id: str) -> NetworkInfo:
        name = self.cfg.network_name(owner_id)
        data = await self._inspect("network", [name])
        if data is None:
            res = await self._docker(da.network_create_args(self.cfg, owner_id), f"создание сети {name}")
            if res.rc != 0 and "already exists" not in res.err:
                raise self._fail(res, f"создание сети {name}")
            data = await self._inspect("network", [name])
            if not data:
                raise BackendError(f"сеть {name} не появилась после создания")
        info = self._network(data[0])
        if not self._managed(info.labels):
            raise NotManaged(f"сеть {name} без метки лаунчера")
        if info.ipv6_enabled:
            raise NetPolicyError(f"сеть {name} создана с IPv6: отключите IPv6 и пересоздайте сеть до запуска ботов")
        if info.core_ip is None:
            res = await self._docker(self._connect_core_args(name), f"подключение ядра к {name}")
            if res.rc != 0 and "already exists" not in res.err:
                raise BackendError(f"подключение {self.cfg.core_container} к {name}: "
                                   f"{_short(res.err) or f'код {res.rc}'}")
            data = await self._inspect("network", [name])
            if not data:
                raise BackendError(f"сеть {name} исчезла")
            info = self._network(data[0])
        return info

    async def list_networks(self) -> list[NetworkInfo]:
        res = await self._docker(
            [self.cfg.docker_bin, "network", "ls", "--filter", f"label={self.cfg.label_key}={self.cfg.label_value}",
             "--format", "{{.Name}}"], "network ls")
        if res.rc != 0:
            raise self._fail(res, "network ls")
        names = res.out.split()
        if not names:
            return []
        data = await self._inspect("network", names) or []
        return [self._network(raw) for raw in data]

    async def ensure_volume(self, name: str, owner_id: str, kind: str) -> None:
        data = await self._inspect("volume", [name])
        if data is None:
            res = await self._docker(da.volume_create_args(self.cfg, name, owner_id, kind), f"создание тома {name}")
            if res.rc != 0:
                raise self._fail(res, f"создание тома {name}")
            data = await self._inspect("volume", [name])
            if not data:
                raise BackendError(f"том {name} не появился после создания")
        labels = data[0].get("Labels") or {}
        if self._managed(labels):
            if labels.get("bothub.owner_id") != owner_id or labels.get("bothub.role") != kind:
                raise Conflict(f"том {name} принадлежит другому владельцу или имеет другую роль")
            return
        if labels:
            raise NotManaged(f"том {name} имеет чужие метки")
        if kind == "browser":
            raise NotManaged(f"том {name} без метки лаунчера: профиль браузера нельзя переносить как том бота")
        adopted = f"{name}-adopted"
        target = await self._inspect("volume", [adopted])
        if target is not None:
            target_labels = target[0].get("Labels") or {}
            if not self._managed(target_labels) or target_labels.get("bothub.owner_id") != owner_id \
                    or target_labels.get("bothub.role") != kind:
                raise Conflict(f"том {adopted} занят")
            marker = await self._inspect("volume", [f"{adopted}-complete"])
            if marker is not None:
                marker_labels = marker[0].get("Labels") or {}
                if not self._managed(marker_labels) or marker_labels.get("bothub.owner_id") != owner_id \
                        or marker_labels.get("bothub.role") != "adoption-complete":
                    raise Conflict(f"маркер тома {adopted} занят")
                self._volume_names[name] = adopted
                return
            if not self.cfg.adopt_legacy_volumes:
                raise Conflict(f"копирование тома {adopted} не завершено; включите adopt_legacy_volumes для повтора")
        if not self.cfg.adopt_legacy_volumes:
            raise NotManaged(f"том {name} без метки лаунчера; для переноса старых данных включите adopt_legacy_volumes")
        if target is None:
            res = await self._docker(da.volume_create_args(self.cfg, adopted, owner_id, kind),
                                     f"создание тома {adopted}")
            if res.rc != 0:
                raise self._fail(res, f"создание тома {adopted}")
        # Старый том остаётся для отката. Из новой копии удаляем прежние логины.
        script = ("find /to -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && cp -a /from/. /to/ && "
                  + "rm -rf /to/.claude /to/.codex /to/.gemini /to/.auth && "
                  + "chown -R 1000:1000 /to")
        res = await self._docker([self.cfg.docker_bin, "run", "--rm", "--pull", "never", "--network", "none",
                                  "--user", "0:0", "-v", f"{name}:/from:ro", "-v", f"{adopted}:/to",
                                  "--entrypoint", "sh", self.cfg.image, "-c", script],
                                 f"копирование тома {name}")
        if res.rc != 0:
            raise self._fail(res, f"копирование тома {name}")
        marker_name = f"{adopted}-complete"
        res = await self._docker(da.volume_create_args(self.cfg, marker_name, owner_id, "adoption-complete"),
                                 f"создание маркера тома {adopted}")
        if res.rc != 0:
            raise self._fail(res, f"создание маркера тома {adopted}")
        self._volume_names[name] = adopted

    async def remove_volume(self, name: str, owner_id: str, kind: str) -> None:
        data = await self._inspect("volume", [name])
        if data is None:
            return
        labels = data[0].get("Labels") or {}
        if not self._managed(labels):
            if kind == "browser":
                raise NotManaged(f"том {name} без метки лаунчера: профиль браузера нельзя переносить")
            adopted = await self._inspect("volume", [f"{name}-adopted"])
            if adopted is not None and self._managed(adopted[0].get("Labels")):
                name = f"{name}-adopted"
                labels = adopted[0].get("Labels") or {}
        if not self._managed(labels):
            raise NotManaged(f"том {name} без метки лаунчера")
        if labels.get("bothub.owner_id") != owner_id or labels.get("bothub.role") != kind:
            raise Conflict(f"том {name} принадлежит другому владельцу или имеет другую роль")
        if name.endswith("-adopted"):
            marker_name = f"{name}-complete"
            marker = await self._inspect("volume", [marker_name])
            if marker is not None:
                marker_labels = marker[0].get("Labels") or {}
                if not self._managed(marker_labels) or marker_labels.get("bothub.owner_id") != owner_id \
                        or marker_labels.get("bothub.role") != "adoption-complete":
                    raise Conflict(f"маркер тома {name} принадлежит другому владельцу")
                res = await self._docker([self.cfg.docker_bin, "volume", "rm", marker_name],
                                         f"удаление маркера {marker_name}")
                if res.rc != 0 and not _is_missing(res):
                    raise self._fail(res, f"удаление маркера {marker_name}")
        res = await self._docker([self.cfg.docker_bin, "volume", "rm", name], f"удаление тома {name}")
        if res.rc != 0 and not _is_missing(res):
            raise self._fail(res, f"удаление тома {name}")

    # ---------- exec и pty ----------

    async def spawn_exec(self, container: str, argv: list[str], env: dict[str, str], exec_id: str) -> ExecProcess:
        args = da.exec_args(self.cfg, container, argv, sorted(env), exec_id)
        return await self._spawn(args, env)

    async def kill_in_container(self, container: str, exec_id: str, signal_name: str) -> None:
        await self._run(da.kill_args(self.cfg, container, exec_id, signal_name))

    async def spawn_procedure_step(self, container: str, exec_id: str) -> ExecProcess:
        """Исполнитель шага под uid 1001. Окружение процесса docker CLI пустое: данных шага в нём нет."""
        return await self._spawn(da.procedure_step_args(self.cfg, container, exec_id), {})

    async def kill_procedure_step(self, container: str, exec_id: str, signal_name: str) -> None:
        await self._run(da.procedure_kill_args(self.cfg, container, exec_id, signal_name))

    async def spawn_pty(self, container: str, argv: list[str], cols: int, rows: int) -> PtyHandle:
        return await self._spawn_pty(da.pty_exec_args(self.cfg, container, argv), cols, rows)

    async def spawn_screen(self, container: str) -> StreamProcess:
        # Сокет x11vnc лежит в каталоге uid 1001 (0700): TCP-порта у экрана нет, коду бота (uid 1000) он недоступен.
        argv = [self.cfg.docker_bin, "exec", "-i", "--user", "1001:1001", container,
                "socat", "-", f"UNIX-CONNECT:{da.BROWSER_VNC_SOCKET}"]
        return await self._spawn_stream(argv, {})

    async def browser_running(self, container: str) -> bool:
        script = ('p=$(cat "$HOME/.browser-supervisor.pid" 2>/dev/null) || exit 1; '
                  'case "$p" in ""|*[!0-9]*) exit 1;; esac; '
                  'cmd=$(tr "\\000" " " < "/proc/$p/cmdline" 2>/dev/null) || exit 1; '
                  'case "$cmd" in *chromium-supervisor.sh*) ;; *) exit 1;; esac; '
                  '[ "$p" -gt 1 ] && kill -0 "$p" 2>/dev/null && '
                  'test -f "$HOME/.browser-ready" && '
                  'curl -fsS --max-time 1 --noproxy "*" '
                  'http://127.0.0.1:9222/json/version >/dev/null 2>&1 || exit 1')
        res = await self._docker([self.cfg.docker_bin, "exec", "--user", "1001:1001", "-e", "HOME=/home/browser", container,
                                  "sh", "-c", script],
                                 "проверка браузера")
        if res.rc not in (0, 1):
            raise self._fail(res, "проверка браузера")
        return res.rc == 0

    async def start_browser(self, container: str) -> None:
        # A running supervisor may still be starting CDP. Do not launch a second one.
        script = ('p=$(cat "$HOME/.browser-supervisor.pid" 2>/dev/null) || p=; '
                  'case "$p" in ""|*[!0-9]*) p=;; esac; '
                  'if [ -n "$p" ] && [ "$p" -gt 1 ] && kill -0 "$p" 2>/dev/null; then '
                  'cmd=$(tr "\\000" " " < "/proc/$p/cmdline" 2>/dev/null) || cmd=; '
                  'case "$cmd" in *chromium-supervisor.sh*) exit 0;; esac; fi; '
                  'exec /usr/local/bin/chromium-supervisor.sh')
        res = await self._docker([self.cfg.docker_bin, "exec", "-d", "--user", "1001:1001", "-e", "HOME=/home/browser", container,
                                  "sh", "-c", script], "запуск браузера")
        if res.rc != 0:
            raise self._fail(res, "запуск браузера")

    # ---------- режим браузера: bot (CDP) или human (чистый Chromium без порта отладки) ----------

    def _browser_exec(self, container: str, script: str, *args: str, stdin: bool = False) -> list[str]:
        """Служебный скрипт под uid 1001 (без bot-guard, как остальной браузерный стек). Значения идут аргументами
        и stdin, не в текст скрипта."""
        return [self.cfg.docker_bin, "exec", *(["-i"] if stdin else []), "--user", "1001:1001", *da.BROWSER_ENV,
                container, "sh", "-c", script, "_", *args]

    async def get_browser_mode(self, container: str) -> str:
        """bot, human или invalid (ссылка, не обычный файл, чужое содержимое). Нет файла: bot (свежий контейнер)."""
        script = (f'f="{da.BROWSER_MODE_FILE}"; '
                  'if [ -L "$f" ]; then echo invalid; elif [ -f "$f" ]; then IFS= read -r v < "$f"; '
                  'printf "%s\\n" "$v"; elif [ -e "$f" ]; then echo invalid; else echo bot; fi')
        res = await self._docker(self._browser_exec(container, script), "чтение режима браузера")
        if res.rc != 0:
            raise self._fail(res, "чтение режима браузера")
        return parse_browser_mode_line(res.out)

    @staticmethod
    def _new_session_token() -> str:
        return secrets.token_hex(16)

    async def set_browser_mode(self, container: str, mode: str, url: str | None) -> None:
        """Сначала адрес, потом режим, оба через rename: супервизор не увидит режим human без адреса. Адрес и токен сессии
        идут через stdin, чтобы query-строка не попала в список процессов хоста. human пишется как `human <токен>`: токен
        уже идущей сессии (в файле стоит human с токеном) остаётся, иначе берётся новый; по токену супервизор
        возобновляет каталог человека, а подложенный Chromium'ом бота каталог с маркером не возобновит."""
        if mode not in ("bot", "human"):
            raise BackendError("недопустимый режим браузера")
        script = ('umask 077; mode=$1; tok=; '
                  'if [ "$mode" = human ]; then IFS= read -r url || url=about:blank; IFS= read -r new || new=; '
                  f'f="{da.BROWSER_MODE_FILE}"; '
                  'if [ -f "$f" ] && [ ! -L "$f" ]; then read -r cm ct extra < "$f" || :; '
                  'if [ "$cm" = human ] && [ -z "$extra" ] && [ "${#ct}" = 32 ]; then '
                  'case "$ct" in *[!0-9a-f]*) ;; *) tok=$ct ;; esac; fi; fi; '
                  '[ -n "$tok" ] || tok=$new; '
                  'case "$tok" in ""|*[!0-9a-f]*) exit 1 ;; esac; [ "${#tok}" = 32 ] || exit 1; '
                  f'printf "%s\\n" "$url" > "{da.BROWSER_URL_FILE}.tmp" && '
                  f'mv -f "{da.BROWSER_URL_FILE}.tmp" "{da.BROWSER_URL_FILE}" || exit 1; '
                  f'else rm -f "{da.BROWSER_URL_FILE}"; fi; '
                  'if [ "$mode" = human ]; then line="human $tok"; else line=bot; fi; '
                  f'printf "%s\\n" "$line" > "{da.BROWSER_MODE_FILE}.tmp" && '
                  f'mv -f "{da.BROWSER_MODE_FILE}.tmp" "{da.BROWSER_MODE_FILE}"')
        human = mode == "human"
        payload = f"{url or 'about:blank'}\n{self._new_session_token()}\n" if human else None
        res = await self._docker(self._browser_exec(container, script, mode, stdin=human),
                                 f"запись режима браузера {mode}", input=payload)
        if res.rc != 0:
            raise self._fail(res, f"запись режима браузера {mode}")

    async def browser_mode_ready(self, container: str, mode: str) -> bool:
        """Режим применён: файл режима совпадает, супервизор жив, Chromium запущен именно в этом режиме. bot: CDP
        отвечает. human: процесс прожил не меньше двух секунд (окно есть), в его командной строке нет
        remote-debugging, профиль человека, порт 9222 не отвечает."""
        if mode not in ("bot", "human"):
            raise BackendError("недопустимый режим браузера")
        cdp = 'curl -fsS --max-time 1 --noproxy "*" http://127.0.0.1:9222/json/version >/dev/null 2>&1'
        script = (f'm=$1; f="{da.BROWSER_MODE_FILE}"; '
                  'if [ -L "$f" ]; then exit 1; elif [ -f "$f" ]; then read -r cur _tok _extra < "$f"; '
                  'elif [ -e "$f" ]; then exit 1; else cur=bot; fi; '
                  '[ "$cur" = "$m" ] || exit 1; '
                  'p=$(cat "$HOME/.browser-supervisor.pid" 2>/dev/null) || exit 1; '
                  'case "$p" in ""|*[!0-9]*) exit 1;; esac; '
                  'cmd=$(tr "\\000" " " < "/proc/$p/cmdline" 2>/dev/null) || exit 1; '
                  'case "$cmd" in *chromium-supervisor.sh*) ;; *) exit 1;; esac; '
                  '[ "$p" -gt 1 ] && kill -0 "$p" 2>/dev/null && test -f "$HOME/.browser-ready" || exit 1; '
                  f'read -r am cp ts < "{da.BROWSER_ACTIVE_FILE}" 2>/dev/null || exit 1; '
                  '[ "$am" = "$m" ] || exit 1; '
                  'case "$cp" in ""|*[!0-9]*) exit 1;; esac; case "$ts" in ""|*[!0-9]*) exit 1;; esac; '
                  'kill -0 "$cp" 2>/dev/null || exit 1; '
                  'args=$(tr "\\000" " " < "/proc/$cp/cmdline" 2>/dev/null) || exit 1; '
                  'if [ "$m" = human ]; then '
                  'case "$args" in *remote-debugging*) exit 1;; esac; '
                  'case "$args" in *botstead-browser-human*) ;; *) exit 1;; esac; '
                  'now=$(date +%s); [ $((now - ts)) -ge 2 ] || exit 1; '
                  f'if {cdp}; then exit 1; fi; '
                  'else '
                  'case "$args" in *remote-debugging-port=9222*) ;; *) exit 1;; esac; '
                  f'{cdp} || exit 1; fi')
        res = await self._docker(self._browser_exec(container, script, mode), f"проверка режима браузера {mode}")
        if res.rc not in (0, 1):
            raise self._fail(res, f"проверка режима браузера {mode}")
        return res.rc == 0

    async def browser_merge_status(self, container: str) -> str:
        """`merged N` или `failed` от последнего возврата из human; пусто, если слияния не было."""
        script = f'cat "{da.BROWSER_MERGE_STATUS_FILE}" 2>/dev/null; true'
        res = await self._docker(self._browser_exec(container, script), "чтение итога переноса cookies")
        if res.rc != 0:
            raise self._fail(res, "чтение итога переноса cookies")
        return res.out.strip()[:100]

    async def browser_tab_url(self, container: str) -> str | None:
        """Адрес первой вкладки типа page по CDP /json/list. Браузер не отвечает или вкладок нет: None."""
        script = 'curl -fsS --max-time 2 --noproxy "*" http://127.0.0.1:9222/json/list'
        res = await self._docker(self._browser_exec(container, script), "адрес вкладки браузера")
        if res.rc != 0:
            return None
        try:
            targets = json.loads(res.out)
        except json.JSONDecodeError:
            return None
        for target in targets if isinstance(targets, list) else []:
            if not (isinstance(target, dict) and target.get("type") == "page" and isinstance(target.get("url"), str)):
                continue
            url = target["url"]
            if url in _NEW_TAB_URLS:
                return "about:blank"  # политика закрывает chrome://*: для человека и ядра это пустая вкладка
            if not url.startswith(_SERVICE_URL_PREFIXES):
                return url[:4096]
        return None

    async def terminate_bot_processes(self, container: str, signal_name: str = "TERM") -> None:
        if signal_name not in ("TERM", "KILL"):
            raise BackendError("недопустимый сигнал для заморозки")
        script = ('for f in /proc/[0-9]*/status; do p=${f%/status}; p=${p##*/}; '
                  '[[ "$p" = 1 || "$p" = "$$" ]] && continue; '
                  '[[ -r "$f" ]] || { [[ -e "$f" ]] && exit 2; continue; }; '
                  'uid=; while read -r key value rest; do '
                  'if [[ "$key" = Uid: ]]; then uid=$value; break; fi; done < "$f"; '
                  '[[ -n "$uid" ]] || { [[ -e "$f" ]] && exit 2; continue; }; '
                  f'[[ "$uid" = 1000 ]] && kill -{signal_name} "$p" 2>/dev/null || true; done')
        res = await self._docker(da.bot_script_args(self.cfg, container, script), "заморозка бота")
        if res.rc != 0:
            raise self._fail(res, "заморозка бота")

    async def bot_processes(self, container: str) -> list[int]:
        # PID 1 is the bot entrypoint; zombies have no code or open descriptors and cannot be killed.
        script = ('for f in /proc/[0-9]*/status; do p=${f%/status}; p=${p##*/}; '
                  '[[ "$p" = 1 || "$p" = "$$" ]] && continue; '
                  '[[ -r "$f" ]] || { [[ -e "$f" ]] && exit 2; continue; }; '
                  'uid= state=; while read -r key value rest; do '
                  '[[ "$key" = Uid: ]] && uid=$value; '
                  '[[ "$key" = State: ]] && state=$value; '
                  '[[ -n "$uid" && -n "$state" ]] && break; done < "$f"; '
                  '[[ -n "$uid" && -n "$state" ]] || { [[ -e "$f" ]] && exit 2; continue; }; '
                  '[[ "$uid" = 1000 && "$state" != Z ]] && printf "%s\\n" "$p" || true; done')
        res = await self._docker(da.bot_script_args(self.cfg, container, script), "проверка процессов бота")
        if res.rc != 0:
            raise self._fail(res, "проверка процессов бота")
        if any(not line.isdigit() or int(line) <= 1 for line in res.out.splitlines()):
            raise BackendError("проверка процессов бота: неверный PID")
        return [int(line) for line in res.out.splitlines()]
