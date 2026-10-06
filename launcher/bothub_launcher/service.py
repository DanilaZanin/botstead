"""Логика лаунчера: боты, exec со стримингом, логин-контейнеры с pty. Транспорта здесь нет (см. app.py).

Правило метки: любая операция над контейнером начинается с проверки, что он создан лаунчером (метка, роль, id).
Контейнер без метки лаунчер не удаляет, не пересоздаёт и не запускает в нём команды."""
import asyncio
import base64
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from . import validation as v
from .backend import Backend, ContainerInfo, ExecProcess, PtyHandle, StreamProcess
from .config import Config
from .errors import (BackendError, Busy, Conflict, Frozen, LauncherError, NetPolicyError, NotFound, NotManaged,
                     StateError, ValidationFailed)
from .netpolicy import NetPolicy, NetSpec, PolicyReport

log = logging.getLogger("bothub_launcher")
STDIN_TIMEOUT = 30.0
PROCEDURE_OUT_MAX = 64 * 1024  # вывод исполнителя шага: одна JSON-строка результата, лишнее отбрасывается
# Исполнитель сам укладывается в срок и отвечает кодом `timeout`; запас до жёсткого предела лаунчера.
PROCEDURE_DEADLINE_MARGIN = 3.0
RFB_BANNER_SIZE = 12
# Смена режима браузера: Chromium получает TERM и до 10 секунд на сброс cookies, затем старт нового. Ждём до 40 секунд.
MODE_POLL_INTERVAL = 0.2
MODE_POLL_ATTEMPTS = 200
# Весь browser_mode (ожидание замка, запись режима, старт, ожидание готовности, отчёт о cookies) не дольше 45 секунд.
# Ядро ждёт ответ дольше (BROWSER_MODE_TIMEOUT в core/bothub/launcher_client.py, 60 секунд): ошибку выдаёт лаунчер, а не
# разрыв соединения. Недоделанное переключение безопасно повторить: режим пишется атомарно, супервизор идемпотентен.
BROWSER_MODE_TOTAL_TIMEOUT = 45.0


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _exit_code(code: int) -> int:
    """Отрицательный код процесса (убит сигналом) приводим к виду оболочки: 128 + номер сигнала."""
    return 128 - code if code < 0 else code


@dataclass
class ExecHandle:
    exec_id: str
    bot_id: str
    container: str
    timeout: float
    proc: ExecProcess | None = None
    stopped: bool = False
    timed_out: bool = False
    wait_task: asyncio.Task | None = None
    watch_task: asyncio.Task | None = None
    kind: str = "exec"  # "procedure": исполнитель шага процедуры (uid 1001), сигналы идут своим путём


@dataclass
class LoginSession:
    id: str
    owner_id: str
    created: float
    last: float
    pty: PtyHandle | None = None
    exited: bool = False


@dataclass
class ScreenSession:
    id: str
    bot_id: str
    owner_id: str
    container: str
    created: float
    last: float
    proc: StreamProcess | None = None
    pending: bytes = b""
    output_claimed: bool = False
    process_exited: bool = False
    closed: bool = False
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Launcher:
    cfg: Config
    backend: Backend
    netpolicy: NetPolicy
    clock: Callable[[], float] = time.monotonic
    report: PolicyReport | None = field(default=None, init=False)

    def __post_init__(self):
        self._execs: dict[str, ExecHandle] = {}
        self._sessions: dict[str, LoginSession] = {}
        self._screen_sessions: dict[str, ScreenSession] = {}
        self._screen_by_bot: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reconcile_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self._freezing: set[str] = set()

    def _frozen_file(self, bot_id: str) -> Path:
        return Path(self.cfg.socket_path).parent / "frozen-bots" / bot_id

    def _is_frozen(self, bot_id: str) -> bool:
        if bot_id in self._freezing:
            return True
        try:
            return self._frozen_file(bot_id).exists()
        except OSError as exc:
            # Метку не прочитать: отказ безопаснее, чем пустить код бота.
            log.warning("метка заморозки бота %s нечитаема, считаю бота замороженным: %s", bot_id, exc)
            return True

    def _repair_marker_dir(self, bot_id: str, directory: Path) -> None:
        """chmod 0700 каталога меток. Не вышло: каталог чужой или на нём нельзя менять права. Бота пересоздавать не
        нужно (метка и бот целы), чинится каталог на сервере."""
        try:
            os.chmod(directory, 0o700)
        except OSError as exc:
            log.error("не удалось выставить права каталога меток заморозки %s: %s", directory, exc)
            raise StateError(
                f"бот {bot_id}: лаунчер не смог выставить права 0700 на каталог меток заморозки {directory} "
                f"({exc.strerror or exc}). Почините права каталога на сервере (владелец: пользователь лаунчера, "
                f"chmod 700); бота пересоздавать не нужно") from exc

    def _clear_frozen_marker(self, bot_id: str) -> None:
        marker = self._frozen_file(bot_id)
        try:
            try:
                marker.unlink(missing_ok=True)
            except PermissionError:
                # Каталог от прежней версии мог получить права без x (umask 0o117): чиним и повторяем.
                self._repair_marker_dir(bot_id, marker.parent)
                marker.unlink(missing_ok=True)
        except OSError as exc:
            log.error("не удалось снять метку заморозки бота %s: %s", bot_id, exc)
            raise StateError(
                f"бот {bot_id}: лаунчер не смог снять метку заморозки ({exc.strerror or exc})") from exc

    def _write_frozen_marker(self, bot_id: str) -> None:
        marker = self._frozen_file(bot_id)
        try:
            marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            log.error("не удалось записать метку заморозки бота %s: %s", bot_id, exc)
            raise StateError(
                f"бот {bot_id}: лаунчер не смог записать метку заморозки ({exc.strerror or exc})") from exc
        # mkdir(mode=) режется umask лаунчера (0o117 даёт drw-------, без x): права ставим явно.
        self._repair_marker_dir(bot_id, marker.parent)
        try:
            marker.write_text("frozen\n")
        except OSError as exc:
            log.error("не удалось записать метку заморозки бота %s: %s", bot_id, exc)
            raise StateError(
                f"бот {bot_id}: лаунчер не смог записать метку заморозки ({exc.strerror or exc})") from exc

    # ---------- общее ----------

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def active_execs(self) -> set[str]:
        return set(self._execs)

    def active_screen_sessions(self) -> set[str]:
        return set(self._screen_sessions)

    async def startup(self) -> PolicyReport:
        await self.backend.detect_docker_version()
        await self.netpolicy.check()
        report = await self.reconcile()
        for info in await self.backend.list_containers(role="bot"):
            bot_id = info.labels.get("bothub.bot_id")
            if bot_id:
                async with self._lock(info.name):
                    await self._managed(info.name, "bot", bot_id)
                    await self.backend.disable_restart(info.name)
                    if not info.running:
                        await self.backend.start_container(info.name)
                    await self._start_browser_best_effort(bot_id, info.name, heal_stale_human=True)
        return report

    async def reconcile(self) -> PolicyReport:
        async with self._reconcile_lock:
            networks = await self.backend.list_networks()
            ipv6_network = next((n.name for n in networks if n.ipv6_enabled), None)
            if ipv6_network is None and any(n.core_ip is None for n in networks):
                # Ядро пересоздали (compose up --build) и оно потеряло подключения к сетям пользователей.
                for n in networks:
                    owner = n.labels.get("bothub.owner_id")
                    if n.core_ip is None and owner:
                        try:
                            await self.backend.ensure_network(owner)
                        except LauncherError as exc:
                            log.warning("не удалось вернуть ядро в сеть %s: %s", n.name, exc)
                networks = await self.backend.list_networks()
            try:
                nets = [NetSpec(n.name, n.bridge, n.core_ip, n.core_ipv6) for n in networks]
            except ValueError as exc:
                raise NetPolicyError(f"сеть с недопустимыми параметрами: {exc}") from exc
            self.report = await self.netpolicy.reconcile(nets)
            if ipv6_network is not None:
                raise NetPolicyError(
                    f"сеть {ipv6_network} создана с IPv6: правила восстановлены, но запуск ботов запрещён до "
                    "отключения IPv6 и пересоздания сети")
            return self.report

    async def _ensure_network(self, owner_id: str) -> None:
        """Сеть пользователя и правила для неё готовы раньше, чем появится контейнер."""
        await self.backend.ensure_network(owner_id)
        await self.reconcile()

    async def ensure_owner_network(self, owner_id) -> dict:
        """Сеть пользователя и подключение к ней ядра без контейнера. Ядро зовёт её сразу после создания пользователя, фоном:
        `docker network connect` перестраивает правила Docker и на время обрывает соединения ядра, поэтому первый бот
        пользователя не должен делать это внутри запроса на своё создание. Повтор безопасен."""
        owner_id = v.validate_owner_id(owner_id)
        info = await self.backend.ensure_network(owner_id)
        await self.reconcile()
        return {"owner_id": owner_id, "network": info.name, "core_connected": info.core_ip is not None}

    def info(self) -> dict:
        lim = self.cfg.limits
        return {
            "image": self.cfg.image, "runtime": self.cfg.runtime, "user": self.cfg.user,
            "limits": {"memory": lim.memory, "cpus": lim.cpus, "pids": lim.pids},
            "api_port": self.cfg.api_port,
            "network_policy": None if self.report is None else
            {"ipv4": self.report.ipv4, "ipv6": self.report.ipv6, "networks": self.report.networks},
        }

    async def _managed(self, name: str, role: str, key: str) -> ContainerInfo | None:
        """Контейнер лаунчера нужной роли или None, если такого имени нет. Чужой или без метки: NotManaged."""
        info = await self.backend.inspect_container(name)
        if info is None:
            return None
        labels = info.labels
        owner_key = "bothub.bot_id" if role == "bot" else "bothub.owner_id"
        if (labels.get(self.cfg.label_key) != self.cfg.label_value or labels.get("bothub.role") != role
                or labels.get(owner_key) != key):
            raise NotManaged(f"контейнер {name} создан не лаунчером или другой роли")
        return info

    @staticmethod
    def _status(info: ContainerInfo) -> dict:
        return {
            "bot_id": info.labels.get("bothub.bot_id"), "owner_id": info.labels.get("bothub.owner_id"),
            "exists": True, "running": info.running, "status": info.status, "exit_code": info.exit_code,
            "oom_killed": info.oom_killed, "started_at": info.started_at, "restart_count": info.restart_count,
            "container": info.name, "network": info.networks[0] if info.networks else None, "image": info.image,
        }

    # ---------- боты ----------

    async def _launch_bot(self, bot_id: str, owner_id: str) -> dict:
        await self.backend.ensure_volume(self.cfg.login_volume(owner_id), owner_id, "login")
        await self.backend.ensure_volume(self.cfg.home_volume(bot_id), owner_id, "home")
        await self.backend.ensure_volume(self.cfg.browser_volume(bot_id), owner_id, "browser")
        await self._ensure_network(owner_id)
        await self.backend.run_bot(bot_id, owner_id)
        info = await self.backend.inspect_container(self.cfg.bot_container(bot_id))
        if info is None:
            raise Conflict(f"контейнер бота {bot_id} исчез сразу после создания")
        await self._start_browser_best_effort(bot_id, info.name, heal_stale_human=True)
        return self._status(info)

    async def create_bot(self, bot_id, owner_id) -> dict:
        bot_id, owner_id = v.validate_bot_id(bot_id), v.validate_owner_id(owner_id)
        name = self.cfg.bot_container(bot_id)
        async with self._lock(name):
            info = await self._managed(name, "bot", bot_id)
            if info is not None:
                if info.labels.get("bothub.owner_id") != owner_id:
                    raise Conflict(f"бот {bot_id} принадлежит другому владельцу")
                if info.running:
                    await self._start_browser_best_effort(bot_id, info.name)
                    return self._status(info)
                await self.backend.remove_container(name)  # остановленный: создаём заново, том остаётся
            return await self._launch_bot(bot_id, owner_id)

    async def recreate_bot(self, bot_id) -> dict:
        bot_id = v.validate_bot_id(bot_id)
        name = self.cfg.bot_container(bot_id)
        async with self._lock(name):
            info = await self._managed(name, "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            owner_id = v.validate_owner_id(info.labels.get("bothub.owner_id"))
            await self._close_screen_sessions_of(bot_id)
            await self._stop_execs_of(bot_id)
            await self.backend.remove_container(name)
            return await self._launch_bot(bot_id, owner_id)

    async def remove_bot(self, bot_id, purge: bool = False) -> dict:
        bot_id = v.validate_bot_id(bot_id)
        name = self.cfg.bot_container(bot_id)
        async with self._lock(name):
            info = await self._managed(name, "bot", bot_id)
            if info is not None:
                await self._close_screen_sessions_of(bot_id)
                await self._stop_execs_of(bot_id)
            removed = await self.backend.remove_container(name) if info is not None else False
            if purge:
                if info is None:
                    raise NotFound(f"бота {bot_id} нет: нельзя проверить владельца тома")
                await self.backend.remove_volume(self.cfg.home_volume(bot_id), info.labels["bothub.owner_id"], "home")
                await self.backend.remove_volume(self.cfg.browser_volume(bot_id), info.labels["bothub.owner_id"], "browser")
            self._clear_frozen_marker(bot_id)
            return {"bot_id": bot_id, "removed": removed, "purged": bool(purge)}

    async def ensure_browser(self, bot_id: str) -> dict:
        bot_id = v.validate_bot_id(bot_id)
        async with self._lock(self.cfg.bot_container(bot_id)):
            info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            if not info.running:
                raise Conflict(f"контейнер бота {bot_id} не запущен")
            return await self._ensure_browser_ready(bot_id, info.name)

    async def _ensure_browser_ready(self, bot_id: str, container: str, heal_stale_human: bool = False) -> dict:
        info = await self._managed(container, "bot", bot_id)
        if info is None or info.labels.get("bothub.browser_isolation") != "2":
            raise Conflict(f"бот {bot_id}: контейнер без изоляции браузера, требуется recreate")
        mode = await self.backend.get_browser_mode(container)
        if heal_stale_human and mode == "human" and not self._is_frozen(bot_id):
            # Только при (пере)создании и старте контейнера. Человек за экраном бывает только под меткой заморозки: ядро
            # замораживает бота до browser_mode human, а unfreeze_bot при живом human отказывает. Режим human без метки
            # остался в томе от остановленного контейнера (его разморозили, не переключив режим): боту нужен свой
            # Chromium, человека не ждёт никто. Обычный ensure_browser и exec такой режим не лечат: exec в human отказ.
            await self._switch_browser_mode(bot_id, container, "bot", None)
            mode = "bot"
        if mode == "human":
            # Человек в чистом Chromium без CDP: здесь нужен только живой супервизор, порт 9222 не ждём и не поднимаем.
            if not await self.backend.browser_mode_ready(container, "human"):
                await self.backend.start_browser(container)
                await self._wait_mode(bot_id, container, "human", attempts=50)
            return {"bot_id": bot_id, "running": True, "started": False, "mode": "human"}
        if mode != "bot":
            raise Conflict(f"бот {bot_id}: файл режима браузера повреждён, нужен browser_mode")
        if await self.backend.browser_running(container):
            return {"bot_id": bot_id, "running": True, "started": False, "mode": "bot"}
        await self.backend.start_browser(container)
        for _ in range(50):
            if await self.backend.browser_running(container):
                return {"bot_id": bot_id, "running": True, "started": True, "mode": "bot"}
            await asyncio.sleep(0.1)
        raise BackendError(f"браузер бота {bot_id} не готов")

    async def _wait_mode(self, bot_id: str, container: str, mode: str, attempts: int = MODE_POLL_ATTEMPTS) -> None:
        for _ in range(attempts):
            if await self.backend.browser_mode_ready(container, mode):
                return
            await asyncio.sleep(MODE_POLL_INTERVAL)
        raise BackendError(f"браузер бота {bot_id} не перешёл в режим {mode}")

    async def browser_mode(self, bot_id, mode, url=None) -> dict:
        """Переключает браузер бота между bot (Chromium с CDP, профиль бота) и human (чистый Chromium без порта
        отладки, отдельный профиль, одна вкладка с адресом от ядра). Идемпотентна: уже в этом режиме и готов, ничего не
        трогает. Xvfb, openbox и x11vnc не перезапускаются: Chromium меняет супервизор внутри контейнера по файлу режима.
        Возврат в bot: супервизор останавливает Chromium человека, переносит cookies и удаляет профиль человека."""
        bot_id = v.validate_bot_id(bot_id)
        mode = v.validate_browser_mode(mode)
        if mode == "human":
            url = v.validate_browser_url(url)
        elif url is not None:
            raise ValidationFailed("url: только для режима human")
        name = self.cfg.bot_container(bot_id)
        try:
            return await asyncio.wait_for(self._browser_mode_locked(bot_id, name, mode, url),
                                          BROWSER_MODE_TOTAL_TIMEOUT)
        except TimeoutError:
            raise BackendError(f"бот {bot_id}: переключение браузера в режим {mode} не уложилось в "
                               f"{BROWSER_MODE_TOTAL_TIMEOUT:g} с") from None

    async def _browser_mode_locked(self, bot_id: str, name: str, mode: str, url) -> dict:
        async with self._lock(name):
            info = await self._managed(name, "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            if not info.running:
                raise Conflict(f"контейнер бота {bot_id} не запущен")
            if info.labels.get("bothub.browser_isolation") != "2":
                raise Conflict(f"бот {bot_id}: контейнер без изоляции браузера, требуется recreate")
            return await self._switch_browser_mode(bot_id, name, mode, url)

    async def _switch_browser_mode(self, bot_id: str, name: str, mode: str, url) -> dict:
        """Замок контейнера держит вызывающий."""
        current = await self.backend.get_browser_mode(name)
        if current == mode and await self.backend.browser_mode_ready(name, mode):
            return {"bot_id": bot_id, "mode": mode, "changed": False, "cookies": None}
        # Режим пишется до старта супервизора: свежий супервизор сразу читает нужный режим и не поднимает CDP в human.
        await self.backend.set_browser_mode(name, mode, url)
        await self.backend.start_browser(name)
        await self._wait_mode(bot_id, name, mode)
        cookies = None
        if mode == "bot" and current == "human":
            status = await self.backend.browser_merge_status(name)
            if status.startswith("merged ") and status[7:].isdigit():
                cookies = {"merged": int(status[7:])}
            elif status:
                cookies = {"error": "перенос cookies не удался, вход человека не сохранён"}
        return {"bot_id": bot_id, "mode": mode, "changed": current != mode, "cookies": cookies}

    async def browser_tab(self, bot_id) -> dict:
        """Адрес вкладки Chromium бота до перехвата. В режиме human CDP нет: url null."""
        bot_id = v.validate_bot_id(bot_id)
        name = self.cfg.bot_container(bot_id)
        info = await self._managed(name, "bot", bot_id)
        if info is None:
            raise NotFound(f"бота {bot_id} нет")
        if not info.running:
            raise Conflict(f"контейнер бота {bot_id} не запущен")
        mode = await self.backend.get_browser_mode(name)
        url = await self.backend.browser_tab_url(name) if mode == "bot" else None
        return {"bot_id": bot_id, "mode": mode, "url": url}

    async def _start_browser_best_effort(self, bot_id: str, container: str, heal_stale_human: bool = False) -> None:
        try:
            await self._ensure_browser_ready(bot_id, container, heal_stale_human)
        except (BackendError, Conflict) as exc:
            log.warning("браузер бота %s недоступен: %s", bot_id, exc)

    async def freeze_bot(self, bot_id: str) -> dict:
        bot_id = v.validate_bot_id(bot_id)
        async with self._lock(self.cfg.bot_container(bot_id)):
            info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            if not info.running:
                raise Conflict(f"контейнер бота {bot_id} не запущен")
            if info.labels.get("bothub.browser_isolation") != "2":
                raise Conflict(f"бот {bot_id}: контейнер без изоляции браузера, требуется recreate")
            self._freezing.add(bot_id)
            try:
                # The marker survives a failed termination and a launcher restart.
                self._write_frozen_marker(bot_id)
                await self._stop_execs_of(bot_id)
                await self.backend.terminate_bot_processes(info.name, "TERM")
                for _ in range(10):
                    remaining = await self.backend.bot_processes(info.name)
                    if not remaining:
                        return {"bot_id": bot_id, "frozen": True}
                    await asyncio.sleep(0.1)
                await self.backend.terminate_bot_processes(info.name, "KILL")
                for _ in range(10):
                    remaining = await self.backend.bot_processes(info.name)
                    if not remaining:
                        return {"bot_id": bot_id, "frozen": True}
                    await asyncio.sleep(0.1)
                raise Conflict(f"у бота {bot_id} остались процессы uid 1000: {remaining}")
            finally:
                self._freezing.discard(bot_id)

    async def unfreeze_bot(self, bot_id: str) -> dict:
        bot_id = v.validate_bot_id(bot_id)
        async with self._lock(self.cfg.bot_container(bot_id)):
            info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            if info.running and await self.backend.get_browser_mode(info.name) == "human":
                # Бот не оживает рядом с человеком: сначала browser_mode bot (профиль человека уходит), потом разморозка.
                raise Conflict(f"бот {bot_id}: браузер в режиме human, сначала верните режим bot")
            self._clear_frozen_marker(bot_id)
            return {"bot_id": bot_id, "frozen": False}

    async def status(self, bot_id) -> dict:
        bot_id = v.validate_bot_id(bot_id)
        info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
        if info is None:
            return {"bot_id": bot_id, "exists": False, "running": False, "status": "missing"}
        return self._status(info)

    async def list_bots(self) -> list[dict]:
        return [self._status(c) for c in await self.backend.list_containers(role="bot")]

    # ---------- screen sessions ----------

    async def open_screen_session(self, bot_id, owner_id) -> dict:
        bot_id = v.validate_bot_id(bot_id)
        owner_id = v.validate_owner_id(owner_id)
        async with self._lock(f"screen:{bot_id}"):
            info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            if info.labels.get("bothub.owner_id") != owner_id:
                raise NotManaged(f"бот {bot_id} принадлежит другому владельцу")
            if not info.running:
                raise Conflict(f"контейнер бота {bot_id} не запущен ({info.status})")
            if bot_id in self._screen_by_bot:
                raise Conflict(f"для бота {bot_id} уже открыт экран")
            if len(self._screen_sessions) >= self.cfg.max_screen_sessions:
                raise Busy("достигнут лимит одновременных экранов")

            now = self.clock()
            session = ScreenSession(secrets.token_hex(16), bot_id, owner_id, info.name, now, now)
            self._screen_sessions[session.id] = session
            self._screen_by_bot[bot_id] = session.id
            try:
                session.proc = await self.backend.spawn_screen(info.name)
                try:
                    first = bytearray()
                    async with asyncio.timeout(self.cfg.screen_connect_timeout):
                        while len(first) < RFB_BANNER_SIZE:
                            chunk = await session.proc.read()
                            if not chunk:
                                break
                            first.extend(chunk)
                except TimeoutError as exc:
                    raise BackendError("RFB-сервер не ответил при открытии потока экрана") from exc
                if not first:
                    try:
                        await asyncio.wait_for(session.proc.wait(), 1.0)
                        session.process_exited = True
                    except TimeoutError:
                        pass
                    raise BackendError("RFB-сервер завершился до отправки приветствия")
                banner = first[:RFB_BANNER_SIZE]
                if (len(banner) != RFB_BANNER_SIZE or banner[:4] != b"RFB " or not banner[4:7].isdigit()
                        or banner[7:8] != b"." or not banner[8:11].isdigit() or banner[11:12] != b"\n"):
                    raise BackendError("при открытии потока получен неверный заголовок RFB")
                session.pending = bytes(first)
            except BaseException:
                await asyncio.shield(self._close_screen(session))
                raise
            return {"session_id": session.id}

    def _screen_session(self, session_id) -> ScreenSession:
        sid = v.validate_session_id(session_id)
        session = self._screen_sessions.get(sid)
        if session is None or session.closed or session.proc is None:
            raise NotFound("сессия экрана не найдена")
        return session

    def screen_output(self, session_id) -> AsyncIterator[bytes]:
        session = self._screen_session(session_id)
        if session.output_claimed:
            raise Conflict("поток экрана уже подключён")
        session.output_claimed = True

        async def stream() -> AsyncIterator[bytes]:
            try:
                if session.pending:
                    data, session.pending = session.pending, b""
                    yield data
                while not session.closed:
                    remaining = self.cfg.screen_idle - (self.clock() - session.last)
                    if remaining <= 0:
                        return
                    try:
                        data = await asyncio.wait_for(session.proc.read(), remaining)
                    except TimeoutError:
                        return
                    if session.closed:
                        return
                    if not data:
                        try:
                            await asyncio.wait_for(session.proc.wait(), 2.0)
                            session.process_exited = True
                        except TimeoutError:
                            pass
                        return
                    session.last = self.clock()
                    yield data
            finally:
                await asyncio.shield(self._close_screen(session))

        return stream()

    async def screen_input(self, session_id, data: bytes) -> None:
        session = self._screen_session(session_id)
        data = v.validate_input(data)
        async with session.write_lock:
            if session.closed or session.proc is None:
                raise NotFound("сессия экрана не найдена")
            try:
                await session.proc.write_stdin(data)
            except BackendError:
                await asyncio.shield(self._close_screen(session))
                raise
            session.last = self.clock()

    async def close_screen_session(self, session_id) -> None:
        sid = v.validate_session_id(session_id)
        session = self._screen_sessions.get(sid)
        if session is not None:
            await asyncio.shield(self._close_screen(session))

    async def _close_screen(self, session: ScreenSession) -> None:
        if session.closed:
            return
        session.closed = True
        try:
            if session.proc is not None and not session.process_exited:
                await session.proc.kill()
                try:
                    await asyncio.wait_for(session.proc.wait(), 2.0)
                except TimeoutError:
                    pass
        finally:
            self._screen_sessions.pop(session.id, None)
            if self._screen_by_bot.get(session.bot_id) == session.id:
                self._screen_by_bot.pop(session.bot_id, None)

    async def _close_screen_sessions_of(self, bot_id: str) -> None:
        for session in list(self._screen_sessions.values()):
            if session.bot_id == bot_id:
                await asyncio.shield(self._close_screen(session))

    # ---------- шаги процедур ----------

    async def procedure_step(self, bot_id, payload_json, dry_run=False, timeout=None, exec_id=None) -> dict:
        """Один шаг процедуры в браузере бота: `node procedure-step.mjs` под uid 1001 (CDP 127.0.0.1:9222).

        Данные шага идут только в stdin (конверт `{dry_run, deadline_ms, payload}`): в argv, окружении, журнале и
        тексте ошибок их нет. Тот же допуск, что у exec (заморозка, режим human, контейнер работает), но один шаг на бота
        за раз. Заморозка (перехват человеком) останавливает исполнителя вместе с остальными exec бота.

        Ответ: `{bot_id, exec_id, exit_code, reason, result}`. `result` это разобранный объект из последней строки
        stdout исполнителя или None (процесс упал, убит, вывод не JSON). Содержимое result лаунчер не проверяет и не
        журналирует: проверка схемы на ядре. stderr отбрасывается."""
        bot_id = v.validate_bot_id(bot_id)
        payload = v.validate_procedure_payload(payload_json)
        if not isinstance(dry_run, bool):
            raise ValidationFailed("dry_run: нужен bool")
        timeout = v.validate_procedure_timeout(timeout)
        exec_id = v.validate_exec_id(exec_id) if exec_id is not None else uuid.uuid4().hex
        envelope = json.dumps({"dry_run": dry_run, "deadline_ms": int((timeout - PROCEDURE_DEADLINE_MARGIN) * 1000),
                               "payload": payload}, separators=(",", ":"))
        data = envelope.encode()
        async with self._lock(self.cfg.bot_container(bot_id)):
            if self._is_frozen(bot_id):
                raise Frozen(f"бот {bot_id} заморожен")
            info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            if not info.running:
                raise Conflict(f"контейнер бота {bot_id} не запущен ({info.status})")
            ready = await self._ensure_browser_ready(bot_id, info.name)
            if ready.get("mode") == "human":
                raise Frozen(f"бот {bot_id}: браузер в режиме human")
            if exec_id in self._execs:
                raise Conflict(f"exec {exec_id} уже выполняется")
            if any(h.kind == "procedure" and h.bot_id == bot_id for h in self._execs.values()):
                raise Busy(f"у бота {bot_id} уже идёт шаг процедуры")
            if len(self._execs) >= self.cfg.max_execs_total:
                raise Busy("слишком много одновременных команд")
            handle = ExecHandle(exec_id, bot_id, info.name, timeout, kind="procedure")
            self._execs[exec_id] = handle  # id занят до первого await
            try:
                handle.proc = await self.backend.spawn_procedure_step(info.name, exec_id)
                try:
                    await asyncio.wait_for(handle.proc.write_stdin(data), STDIN_TIMEOUT)
                except TimeoutError as exc:
                    raise BackendError("исполнитель шага не читает stdin") from exc
            except BaseException:
                self._execs.pop(exec_id, None)
                if handle.proc is not None:
                    await handle.proc.kill()
                raise
            handle.wait_task = asyncio.create_task(handle.proc.wait())
            handle.wait_task.add_done_callback(lambda _t, h=handle: self._execs.pop(h.exec_id, None))
            handle.watch_task = asyncio.create_task(self._watch(handle))
        return await self._collect_procedure_step(handle)

    async def procedure_step_cancel(self, bot_id, exec_id) -> dict:
        """Остановить исполнитель шага процедуры: TERM, через `kill_grace` KILL группе процессов под uid 1001 (`stop_exec` идёт
        под uid 1000 и до неё не дотянулся бы). Исполнитель в реестре: ждём его выхода, вызов шага вернёт `reason: stopped`.
        Реестра нет (перезапуск лаунчера): сигналы по PID-файлу и метке, как после рестарта у `stop_exec`."""
        bot_id = v.validate_bot_id(bot_id)
        exec_id = v.validate_exec_id(exec_id)
        h = self._execs.get(exec_id)
        if h is not None:
            if h.bot_id != bot_id or h.kind != "procedure":
                raise NotFound(f"шаг процедуры {exec_id} у этого бота не найден")
            h.stopped = True
            await self._terminate(h)
            return {"bot_id": bot_id, "exec_id": exec_id, "stopped": True, "was_running": True}
        info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
        if info is None:
            raise NotFound(f"бота {bot_id} нет")
        if info.running:
            await self.backend.kill_procedure_step(info.name, exec_id, "TERM")
            await asyncio.sleep(self.cfg.kill_grace)
            await self.backend.kill_procedure_step(info.name, exec_id, "KILL")
        return {"bot_id": bot_id, "exec_id": exec_id, "stopped": True, "was_running": False}

    async def _collect_procedure_step(self, h: ExecHandle) -> dict:
        out = bytearray()
        finished = False
        try:
            async for stream, chunk in h.proc.output():
                if stream == "stdout" and len(out) < PROCEDURE_OUT_MAX:
                    out += chunk[:PROCEDURE_OUT_MAX - len(out)]
            code = _exit_code(await h.wait_task)
            await asyncio.gather(h.watch_task, return_exceptions=True)
            finished = True
        finally:
            if not finished:
                await self._abort(h)  # клиент ушёл или отмена: исполнитель в контейнере не оставляем
        result = None
        lines = bytes(out).decode(errors="replace").strip().splitlines()
        if lines:
            try:
                parsed = json.loads(lines[-1])
                result = parsed if isinstance(parsed, dict) else None
            except ValueError:
                result = None
        reason = "timeout" if h.timed_out else "stopped" if h.stopped else "exit"
        return {"bot_id": h.bot_id, "exec_id": h.exec_id, "exit_code": 124 if h.timed_out else code,
                "reason": reason, "result": result}

    # ---------- exec ----------

    async def start_exec(self, bot_id, argv, env=None, stdin=None, exec_id=None, timeout=None) -> ExecHandle:
        bot_id = v.validate_bot_id(bot_id)
        argv, env, data = v.validate_argv(argv), v.validate_env(env), v.validate_stdin(stdin)
        exec_id = v.validate_exec_id(exec_id) if exec_id is not None else uuid.uuid4().hex
        if timeout is None:
            timeout = self.cfg.exec_timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not 0 < timeout <= self.cfg.exec_timeout_max:
            raise ValidationFailed(f"timeout: от 0 до {self.cfg.exec_timeout_max} секунд")
        async with self._lock(self.cfg.bot_container(bot_id)):
            if self._is_frozen(bot_id):
                raise Frozen(f"бот {bot_id} заморожен")
            info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
            if info is None:
                raise NotFound(f"бота {bot_id} нет")
            if not info.running:
                raise Conflict(f"контейнер бота {bot_id} не запущен ({info.status})")
            # Do not admit bot code until browser ports and profile are owned by uid 1001.
            ready = await self._ensure_browser_ready(bot_id, info.name)
            if ready.get("mode") == "human":
                raise Frozen(f"бот {bot_id}: браузер в режиме human")
            if exec_id in self._execs:
                raise Conflict(f"exec {exec_id} уже выполняется")
            mine = sum(1 for h in self._execs.values() if h.bot_id == bot_id)
            if mine >= self.cfg.max_execs_per_bot or len(self._execs) >= self.cfg.max_execs_total:
                raise Busy("слишком много одновременных команд")
            handle = ExecHandle(exec_id, bot_id, info.name, float(timeout))
            self._execs[exec_id] = handle  # занимаем id до первого await, чтобы дубликат не прошёл
            try:
                handle.proc = await self.backend.spawn_exec(info.name, argv, env, exec_id)
                try:
                    await asyncio.wait_for(handle.proc.write_stdin(data), STDIN_TIMEOUT)
                except TimeoutError as exc:
                    raise BackendError("процесс не читает stdin: запись не завершилась за 30 секунд") from exc
            except BaseException:
                self._execs.pop(exec_id, None)
                if handle.proc is not None:
                    await handle.proc.kill()
                raise
            handle.wait_task = asyncio.create_task(handle.proc.wait())
            handle.wait_task.add_done_callback(lambda _t, h=handle: self._execs.pop(h.exec_id, None))
            handle.watch_task = asyncio.create_task(self._watch(handle))
            return handle

    async def _signal(self, h: ExecHandle, signal_name: str) -> None:
        try:
            if h.kind == "procedure":
                await self.backend.kill_procedure_step(h.container, h.exec_id, signal_name)
            else:
                await self.backend.kill_in_container(h.container, h.exec_id, signal_name)
        except LauncherError as exc:
            log.warning("kill %s %s: %s", signal_name, h.exec_id, exc)

    async def _exited(self, h: ExecHandle, timeout: float) -> bool:
        done, _ = await asyncio.wait({h.wait_task}, timeout=timeout)
        return bool(done)

    async def _terminate(self, h: ExecHandle) -> None:
        """TERM всей группе в контейнере, через kill_grace KILL и убийство локального клиента docker exec."""
        if h.proc is None or h.wait_task is None or h.wait_task.done():
            return
        await self._signal(h, "TERM")
        if await self._exited(h, self.cfg.kill_grace):
            return
        await self._signal(h, "KILL")
        await h.proc.kill()
        await self._exited(h, 5.0)

    async def _watch(self, h: ExecHandle) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(h.wait_task), h.timeout)
        except TimeoutError:
            h.timed_out = True
            await self._terminate(h)
        except Exception:  # wait() упал сам: поток это увидит через wait_task
            pass

    def _spawn_background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _abort(self, h: ExecHandle) -> None:
        """Клиент ушёл посреди стрима: процесс в контейнере не оставляем. Задача отдельная, чтобы отмена
        внешнего запроса не оборвала зачистку."""
        h.stopped = True
        task = self._spawn_background(self._terminate(h))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass

    async def stream_exec(self, h: ExecHandle) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=4)

        async def pump():
            try:
                async for stream, data in h.proc.output():
                    await queue.put((stream, data))
            finally:
                if not asyncio.current_task().cancelling():
                    await queue.put(None)

        pump_task = asyncio.create_task(pump())
        finished = False
        try:
            yield {"t": "start", "exec_id": h.exec_id}
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), self.cfg.heartbeat)
                except TimeoutError:
                    yield {"t": "ping"}
                    continue
                if item is None:
                    break
                yield {"t": "out", "s": item[0], "d": _b64(item[1])}
            code = _exit_code(await h.wait_task)
            await asyncio.gather(h.watch_task, return_exceptions=True)
            finished = True
            if h.timed_out:
                yield {"t": "exit", "code": 124, "reason": "timeout"}
            else:
                yield {"t": "exit", "code": code, "reason": "stopped" if h.stopped else "exit"}
        finally:
            if not finished:
                await self._abort(h)
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)

    async def stop_exec(self, exec_id, bot_id=None) -> dict:
        exec_id = v.validate_exec_id(exec_id)
        bot_id = v.validate_bot_id(bot_id) if bot_id is not None else None
        h = self._execs.get(exec_id)
        if h is not None:
            if bot_id is not None and h.bot_id != bot_id:
                raise NotFound(f"exec {exec_id} у другого бота")
            h.stopped = True
            await self._terminate(h)
            return {"exec_id": exec_id, "stopped": True, "was_running": True}
        if bot_id is None:
            raise NotFound(f"exec {exec_id} не найден")
        # Лаунчер или ядро перезапускались: реестра нет, но маркер в контейнере остался (PID-файл turn).
        info = await self._managed(self.cfg.bot_container(bot_id), "bot", bot_id)
        if info is None:
            raise NotFound(f"бота {bot_id} нет")
        await self.backend.kill_in_container(info.name, exec_id, "TERM")
        await asyncio.sleep(self.cfg.kill_grace)
        await self.backend.kill_in_container(info.name, exec_id, "KILL")
        return {"exec_id": exec_id, "stopped": True, "was_running": False}

    async def _stop_execs_of(self, bot_id: str) -> None:
        for h in [h for h in self._execs.values() if h.bot_id == bot_id]:
            h.stopped = True
            await self._terminate(h)

    # ---------- логин ----------

    async def create_login_container(self, owner_id) -> dict:
        owner_id = v.validate_owner_id(owner_id)
        name = self.cfg.login_container(owner_id)
        async with self._lock(name):
            info = await self._managed(name, "login", owner_id)
            if info is not None and info.running:
                return self._status(info)
            if info is not None:
                await self.backend.remove_container(name)
            await self.backend.ensure_volume(self.cfg.login_volume(owner_id), owner_id, "login")
            await self._ensure_network(owner_id)
            await self.backend.run_login(owner_id)
            created = await self.backend.inspect_container(name)
            if created is None:
                raise Conflict("логин-контейнер исчез сразу после создания")
            return self._status(created)

    async def remove_login_container(self, owner_id) -> dict:
        owner_id = v.validate_owner_id(owner_id)
        name = self.cfg.login_container(owner_id)
        async with self._lock(name):
            info = await self._managed(name, "login", owner_id)
            for s in [s for s in self._sessions.values() if s.owner_id == owner_id]:
                await self.close_login_session(s.id)
            removed = await self.backend.remove_container(name) if info is not None else False
            return {"owner_id": owner_id, "removed": removed}

    async def open_login_session(self, owner_id, command="shell", cols=80, rows=24) -> dict:
        owner_id = v.validate_owner_id(owner_id)
        command = v.validate_command_name(command)
        if command not in self.cfg.login_commands:
            raise ValidationFailed(f"command: доступно {sorted(self.cfg.login_commands)}")
        cols, rows = v.validate_term_size(cols, rows)
        if any(s.owner_id == owner_id and not s.exited for s in self._sessions.values()):
            raise Conflict("у пользователя уже открыт терминал входа")
        sid = secrets.token_hex(16)
        now = self.clock()
        session = LoginSession(sid, owner_id, now, now)
        self._sessions[sid] = session  # место занято до первого await
        try:
            await self.create_login_container(owner_id)
            session.pty = await self.backend.spawn_pty(
                self.cfg.login_container(owner_id), list(self.cfg.login_commands[command]), cols, rows)
        except BaseException:
            self._sessions.pop(sid, None)
            raise
        return {"session_id": sid, "owner_id": owner_id, "command": command}

    def _session(self, session_id) -> LoginSession:
        sid = v.validate_session_id(session_id)
        s = self._sessions.get(sid)
        if s is None or s.pty is None:
            raise NotFound("сессия входа не найдена")
        return s

    async def login_output(self, session_id) -> AsyncIterator[dict]:
        s = self._session(session_id)
        while True:
            try:
                data = await asyncio.wait_for(s.pty.read(), self.cfg.heartbeat)
            except TimeoutError:
                yield {"t": "ping"}
                continue
            if not data:
                break
            s.last = self.clock()
            yield {"t": "out", "s": "stdout", "d": _b64(data)}
        s.exited = True
        yield {"t": "exit", "code": _exit_code(await s.pty.wait()), "reason": "exit"}

    async def login_input(self, session_id, data: bytes) -> None:
        s = self._session(session_id)
        s.pty.write(v.validate_input(data))
        s.last = self.clock()

    async def login_resize(self, session_id, cols, rows) -> None:
        s = self._session(session_id)
        cols, rows = v.validate_term_size(cols, rows)
        s.pty.resize(cols, rows)
        s.last = self.clock()

    async def close_login_session(self, session_id) -> None:
        s = self._session(session_id)
        self._sessions.pop(s.id, None)
        s.pty.kill()
        try:
            await asyncio.wait_for(s.pty.wait(), 2.0)
        except TimeoutError:
            pass

    async def reap_idle(self) -> None:
        """Закрывает неактивные терминалы входа и потоки экрана."""
        now = self.clock()
        for s in list(self._sessions.values()):
            if s.pty is not None and (now - s.last > self.cfg.login_idle or now - s.created > self.cfg.login_ttl):
                await self.close_login_session(s.id)
        for s in list(self._screen_sessions.values()):
            if now - s.last > self.cfg.screen_idle:
                await asyncio.shield(self._close_screen(s))

    async def shutdown(self) -> None:
        for h in list(self._execs.values()):
            h.stopped = True
            await self._terminate(h)
        for s in list(self._sessions.values()):
            if s.pty is not None:
                await self.close_login_session(s.id)
        for s in list(self._screen_sessions.values()):
            await asyncio.shield(self._close_screen(s))
