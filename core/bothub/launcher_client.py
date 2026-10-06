"""Асинхронный клиент лаунчера контейнеров ботов (launcher/, docs/isolation.md).

Ядро больше не трогает docker.sock: боты создаются, пересоздаются и запускают команды только через лаунчер.
Транспорт: unix-сокет (LAUNCHER_SOCKET) или внутренний URL (LAUNCHER_URL), секрет в LAUNCHER_SECRET.
Для тестов ядра есть FakeLauncherClient с той же проверкой аргументов и теми же исключениями.

Формат стрима exec: NDJSON, кадры start / out (base64) / ping / exit / error. exec() отдаёт события
ExecStarted, ExecChunk, ExecExit. Если поток оборвался без ExecExit, поднимается LauncherUnavailable."""
import asyncio
import base64
import itertools
import json
import os
import re
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

BOT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
OWNER_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
EXEC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SESSION_ID_RE = re.compile(r"[0-9a-f]{8,64}")
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
ENV_DENY_EXACT = {"PATH", "HOME", "USER", "SHELL", "IFS", "ENV", "BASH_ENV", "PS4", "PROMPT_COMMAND", "TMPDIR"}
ENV_DENY_PREFIX = ("LD_", "DYLD_", "DOCKER_", "BASH_FUNC_")
LINE_LIMIT = 8 * 1024 * 1024
TRUNCATED = b"[cut]"


# ---------- ошибки ----------

class LauncherError(Exception):
    def __init__(self, message: str, *, code: str = "error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class LauncherUnavailable(LauncherError):
    """Лаунчер не отвечает или поток оборвался."""


class LauncherTimeout(LauncherError):
    pass


class LauncherInvalid(LauncherError):
    pass


class LauncherAuthError(LauncherError):
    pass


class LauncherForbidden(LauncherError):
    """Контейнер, сеть или том без метки лаунчера."""


class LauncherNotFound(LauncherError):
    pass


class LauncherConflict(LauncherError):
    pass


class LauncherBusy(LauncherError):
    pass


class LauncherServerError(LauncherError):
    pass


_BY_STATUS = {400: LauncherInvalid, 413: LauncherInvalid, 401: LauncherAuthError, 403: LauncherForbidden,
              404: LauncherNotFound, 409: LauncherConflict, 429: LauncherBusy}


def _map_error(resp: httpx.Response) -> LauncherError:
    code, message = "error", resp.text[:200].strip() or f"HTTP {resp.status_code}"
    try:
        err = resp.json()["error"]
        code, message = str(err["code"]), str(err["message"])
    except (ValueError, KeyError, TypeError):
        pass
    cls = _BY_STATUS.get(resp.status_code, LauncherServerError)
    return cls(message, code=code, status=resp.status_code)


# ---------- проверка аргументов (то же, что на стороне лаунчера) ----------

def _check(pattern: re.Pattern, value, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LauncherInvalid(f"{what}: недопустимое значение", code="invalid")
    return value


def check_bot_id(value) -> str:
    return _check(BOT_ID_RE, value, "bot_id")


def check_owner_id(value) -> str:
    return _check(OWNER_ID_RE, value, "owner_id")


def check_exec_id(value) -> str:
    return _check(EXEC_ID_RE, value, "exec_id")


def check_session_id(value) -> str:
    return _check(SESSION_ID_RE, value, "session_id")


PROCEDURE_STEP_TIMEOUT_MIN = 5.0  # границы те же, что у лаунчера (validation.PROCEDURE_TIMEOUT_*)
PROCEDURE_STEP_TIMEOUT_MAX = 180.0
PROCEDURE_STEP_TIMEOUT_DEFAULT = 30.0
PROCEDURE_STEP_GRACE = 20.0  # ядро ждёт ответ дольше лаунчера: он сам отвечает кодом timeout, а не обрывом
PROCEDURE_PAYLOAD_MAX = 256 * 1024
BROWSER_MODES = ("human", "bot")
BROWSER_MODE_TIMEOUT = 60.0  # stopping one Chromium, merging cookies and starting the other is slow
NETWORK_TIMEOUT = 60.0  # создание сети, docker network connect и сверка правил: секунды, а не доли секунды


def check_browser_mode(mode, url) -> None:
    if mode not in BROWSER_MODES:
        raise LauncherInvalid("mode: нужно human или bot", code="invalid")
    if url is not None and (mode != "human" or not isinstance(url, str)):
        raise LauncherInvalid("url: только строка и только для режима human", code="invalid")


def check_procedure_step(payload_json, dry_run, timeout, exec_id) -> float:
    """Те же проверки, что на стороне лаунчера. Текст ошибки не повторяет payload: в нём бывают значения секретов."""
    if not isinstance(payload_json, str) or len(payload_json.encode("utf-8", "surrogatepass")) > PROCEDURE_PAYLOAD_MAX:
        raise LauncherInvalid("payload_json: нужна строка до 256 КиБ", code="invalid")
    if not isinstance(dry_run, bool):
        raise LauncherInvalid("dry_run: нужен bool", code="invalid")
    if timeout is None:
        timeout = PROCEDURE_STEP_TIMEOUT_DEFAULT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not PROCEDURE_STEP_TIMEOUT_MIN <= timeout <= PROCEDURE_STEP_TIMEOUT_MAX:
        raise LauncherInvalid("timeout: от 5 до 180 секунд", code="invalid")
    if exec_id is not None:
        check_exec_id(exec_id)
    return float(timeout)


def procedure_step_answer(data: dict) -> dict:
    """Ответ лаунчера на procedure_step в проверенном виде: `{exec_id, exit_code, reason, result}`. `result` объект или
    None; схему результата проверяет вызывающий (ядро не доверяет содержимому: его пишет код в контейнере)."""
    result = data.get("result")
    exit_code = data.get("exit_code")
    if (result is not None and not isinstance(result, dict)) or isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise LauncherServerError("ответ лаунчера: неверная форма", code="bad_response")
    return {"exec_id": data.get("exec_id"), "exit_code": exit_code, "reason": str(data.get("reason", "exit"))[:20],
            "result": result}


def check_exec(argv, env, exec_id, timeout) -> None:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv):
        raise LauncherInvalid("argv: нужен непустой список строк", code="invalid")
    if not argv[0] or argv[0].startswith("-"):
        raise LauncherInvalid("argv: первый элемент должен быть именем программы", code="invalid")
    for name, value in (env or {}).items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name) or name in ENV_DENY_EXACT \
                or name.startswith(ENV_DENY_PREFIX):
            raise LauncherInvalid(f"env: переменная {name!r} недопустима", code="invalid")
        if not isinstance(value, str) or "\x00" in value:
            raise LauncherInvalid(f"env: значение {name} должно быть строкой", code="invalid")
    if exec_id is not None:
        check_exec_id(exec_id)
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        raise LauncherInvalid("timeout: положительное число секунд", code="invalid")


# ---------- данные ----------

@dataclass(frozen=True)
class BotStatus:
    bot_id: str | None
    exists: bool
    running: bool
    status: str
    owner_id: str | None = None
    container: str | None = None
    network: str | None = None
    image: str | None = None
    exit_code: int | None = None
    oom_killed: bool = False
    started_at: str | None = None
    restart_count: int = 0

    @classmethod
    def from_dict(cls, d: Mapping) -> "BotStatus":
        return cls(
            bot_id=d.get("bot_id"), exists=bool(d.get("exists", True)), running=bool(d.get("running")),
            status=str(d.get("status", "")), owner_id=d.get("owner_id"), container=d.get("container"),
            network=d.get("network"), image=d.get("image"), exit_code=d.get("exit_code"),
            oom_killed=bool(d.get("oom_killed")), started_at=d.get("started_at"),
            restart_count=int(d.get("restart_count") or 0))


@dataclass(frozen=True)
class ExecStarted:
    exec_id: str


@dataclass(frozen=True)
class ExecChunk:
    stream: str  # "stdout" | "stderr"
    data: bytes


@dataclass(frozen=True)
class ExecExit:
    code: int
    reason: str  # "exit" | "timeout" | "stopped"


ExecEvent = ExecStarted | ExecChunk | ExecExit


@dataclass(frozen=True)
class ExecResult:
    code: int
    stdout: bytes
    stderr: bytes
    reason: str
    exec_id: str | None


class LineSplitter:
    """Собирает строки из кусков stdout: запись stream-json может прийти по частям."""

    def __init__(self, limit: int = LINE_LIMIT):
        if limit < len(TRUNCATED):
            raise ValueError("limit too small for truncation marker")
        self._buf = bytearray()
        self._limit = limit
        self._truncated = False

    def feed(self, data: bytes) -> list[bytes]:
        lines = []
        start = 0
        while (end := data.find(b"\n", start)) != -1:
            self._append(data[start:end])
            lines.append(self._take())
            start = end + 1
        self._append(data[start:])
        return lines

    def _append(self, data: bytes) -> None:
        if len(self._buf) + len(data) <= self._limit and not self._truncated:
            self._buf.extend(data)
        else:
            self._truncated = True
            keep = max(0, self._limit - len(TRUNCATED) - len(self._buf))
            self._buf.extend(data[:keep])
            if len(self._buf) > self._limit - len(TRUNCATED):
                del self._buf[self._limit - len(TRUNCATED):]

    def _take(self) -> bytes:
        tail = bytes(self._buf).removesuffix(b"\r")
        self._buf.clear()
        if self._truncated:
            self._truncated = False
            return tail + TRUNCATED
        return tail

    def flush(self) -> bytes | None:
        return self._take() if self._buf or self._truncated else None


def _event_from_frame(frame: Mapping) -> ExecEvent | None:
    kind = frame.get("t")
    try:
        if kind == "start":
            return ExecStarted(str(frame["exec_id"]))
        if kind == "out":
            return ExecChunk(str(frame["s"]), base64.b64decode(frame["d"]))
        if kind == "exit":
            return ExecExit(int(frame["code"]), str(frame.get("reason", "exit")))
    except (KeyError, ValueError, TypeError):
        return None
    if kind == "error":
        raise LauncherServerError(str(frame.get("message", "ошибка лаунчера")), code=str(frame.get("code", "error")))
    return None  # ping и неизвестные кадры


async def run_exec(client: "LauncherClient", bot_id: str, argv: list[str], *, env: Mapping[str, str] | None = None,
                   stdin: str | None = None, exec_id: str | None = None, timeout: float | None = None) -> ExecResult:
    out, err = bytearray(), bytearray()
    code, reason, started = -1, "exit", exec_id
    async for ev in client.exec(bot_id, argv, env=env, stdin=stdin, exec_id=exec_id, timeout=timeout):
        if isinstance(ev, ExecStarted):
            started = ev.exec_id
        elif isinstance(ev, ExecChunk):
            (out if ev.stream == "stdout" else err).extend(ev.data)
        elif isinstance(ev, ExecExit):
            code, reason = ev.code, ev.reason
    return ExecResult(code, bytes(out), bytes(err), reason, started)


class LauncherClient(Protocol):
    async def create_bot(self, bot_id: str, owner_id: str) -> BotStatus: ...
    async def remove_bot(self, bot_id: str, *, purge: bool = False) -> bool: ...
    async def recreate_bot(self, bot_id: str) -> BotStatus: ...
    async def ensure_browser(self, bot_id: str) -> dict: ...
    async def freeze_bot(self, bot_id: str) -> dict: ...
    async def unfreeze_bot(self, bot_id: str) -> dict: ...
    async def browser_mode(self, bot_id: str, mode: str, *, url: str | None = None) -> dict: ...
    async def browser_tab(self, bot_id: str) -> dict: ...
    async def procedure_step(self, bot_id: str, payload_json: str, dry_run: bool = False, timeout: float | None = None,
                             *, exec_id: str | None = None) -> dict: ...
    async def procedure_step_cancel(self, bot_id: str, exec_id: str) -> None: ...
    async def status(self, bot_id: str) -> BotStatus: ...
    async def list_bots(self) -> list[BotStatus]: ...
    def exec(self, bot_id: str, argv: list[str], *, env: Mapping[str, str] | None = None, stdin: str | None = None,
             exec_id: str | None = None, timeout: float | None = None) -> AsyncIterator[ExecEvent]: ...
    async def stop_exec(self, exec_id: str, *, bot_id: str | None = None) -> None: ...
    async def ensure_network(self, owner_id: str) -> dict: ...
    async def create_login_container(self, owner_id: str) -> BotStatus: ...
    async def remove_login_container(self, owner_id: str) -> bool: ...
    async def open_login_session(self, owner_id: str, *, command: str = "shell", cols: int = 80,
                                 rows: int = 24) -> str: ...
    def login_output(self, session_id: str) -> AsyncIterator[ExecEvent]: ...
    async def login_input(self, session_id: str, data: bytes) -> None: ...
    async def login_resize(self, session_id: str, cols: int, rows: int) -> None: ...
    async def close_login_session(self, session_id: str) -> None: ...
    async def open_screen_session(self, bot_id: str, owner_id: str) -> str: ...
    def screen_output(self, session_id: str) -> AsyncIterator[bytes]: ...
    async def screen_input(self, session_id: str, data: bytes) -> None: ...
    async def close_screen_session(self, session_id: str) -> None: ...
    async def aclose(self) -> None: ...


# ---------- HTTP-клиент ----------

class HttpLauncherClient:
    def __init__(self, *, secret: str, base_url: str | None = None, socket_path: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, connect_timeout: float = 5.0,
                 request_timeout: float = 30.0, stream_idle_timeout: float = 90.0, stream_grace: float = 30.0):
        if not secret:
            raise LauncherError("LAUNCHER_SECRET не задан", code="config")
        if not (base_url or socket_path or transport):
            raise LauncherError("нужен LAUNCHER_SOCKET или LAUNCHER_URL", code="config")
        if socket_path and transport is None:
            transport = httpx.AsyncHTTPTransport(uds=socket_path)
        self.base_url = (base_url or "http://launcher").rstrip("/")
        self.request_timeout = httpx.Timeout(request_timeout, connect=connect_timeout)
        # Стрим молчит, пока CLI думает; лаунчер шлёт ping каждые ~15 с, поэтому read-таймаут это «лаунчер умер».
        self.stream_timeout = httpx.Timeout(connect=connect_timeout, read=stream_idle_timeout,
                                            write=request_timeout, pool=connect_timeout)
        self.screen_timeout = httpx.Timeout(connect=connect_timeout, read=None,
                                            write=request_timeout, pool=connect_timeout)
        self.stream_grace = stream_grace
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=self.request_timeout,
                                       headers={"Authorization": f"Bearer {secret}"})

    def __repr__(self) -> str:
        return f"HttpLauncherClient({self.base_url})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.TimeoutException as exc:
            raise LauncherTimeout(f"лаунчер не ответил вовремя: {path}", code="timeout") from exc
        except httpx.TransportError as exc:
            raise LauncherUnavailable(f"лаунчер недоступен: {exc.__class__.__name__}", code="unavailable") from exc
        if resp.status_code >= 400:
            raise _map_error(resp)
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as exc:
            raise LauncherServerError("ответ лаунчера не JSON", code="bad_response", status=resp.status_code) from exc
        if not isinstance(data, dict):
            raise LauncherServerError("ответ лаунчера не объект", code="bad_response", status=resp.status_code)
        return data

    # ---- боты ----

    async def create_bot(self, bot_id: str, owner_id: str) -> BotStatus:
        check_bot_id(bot_id), check_owner_id(owner_id)
        resp = await self._request("POST", "/v1/bots", json={"bot_id": bot_id, "owner_id": owner_id})
        return BotStatus.from_dict(self._json(resp))

    async def remove_bot(self, bot_id: str, *, purge: bool = False) -> bool:
        check_bot_id(bot_id)
        resp = await self._request("DELETE", f"/v1/bots/{bot_id}", params={"purge": "true"} if purge else None)
        return bool(self._json(resp).get("removed"))

    async def recreate_bot(self, bot_id: str) -> BotStatus:
        check_bot_id(bot_id)
        return BotStatus.from_dict(self._json(await self._request("POST", f"/v1/bots/{bot_id}/recreate")))

    async def ensure_browser(self, bot_id: str) -> dict:
        check_bot_id(bot_id)
        return self._json(await self._request("POST", f"/v1/bots/{bot_id}/browser"))

    async def freeze_bot(self, bot_id: str) -> dict:
        check_bot_id(bot_id)
        return self._json(await self._request("POST", f"/v1/bots/{bot_id}/freeze"))

    async def unfreeze_bot(self, bot_id: str) -> dict:
        check_bot_id(bot_id)
        return self._json(await self._request("POST", f"/v1/bots/{bot_id}/unfreeze"))

    async def browser_mode(self, bot_id: str, mode: str, *, url: str | None = None) -> dict:
        check_bot_id(bot_id)
        check_browser_mode(mode, url)
        return self._json(await self._request("POST", f"/v1/bots/{bot_id}/browser-mode",
                                              json={"mode": mode, "url": url},
                                              timeout=httpx.Timeout(BROWSER_MODE_TIMEOUT,
                                                                    connect=self.request_timeout.connect)))

    async def browser_tab(self, bot_id: str) -> dict:
        check_bot_id(bot_id)
        return self._json(await self._request("GET", f"/v1/bots/{bot_id}/browser-tab"))

    async def procedure_step(self, bot_id: str, payload_json: str, dry_run: bool = False, timeout: float | None = None,
                             *, exec_id: str | None = None) -> dict:
        """Один шаг процедуры в браузере бота (лаунчер: node под uid 1001, данные шага в stdin). Ответ
        `{exec_id, exit_code, reason, result}`; `result` None, если исполнитель упал или вывод не JSON."""
        check_bot_id(bot_id)
        timeout = check_procedure_step(payload_json, dry_run, timeout, exec_id)
        body: dict = {"payload_json": payload_json, "dry_run": dry_run, "timeout": timeout}
        if exec_id is not None:
            body["exec_id"] = exec_id
        resp = await self._request("POST", f"/v1/bots/{bot_id}/procedure-step", json=body,
                                   timeout=httpx.Timeout(timeout + PROCEDURE_STEP_GRACE, connect=self.request_timeout.connect))
        return procedure_step_answer(self._json(resp))

    async def procedure_step_cancel(self, bot_id: str, exec_id: str) -> None:
        """Остановить исполнитель шага процедуры: лаунчер завершает его процесс под uid 1001 (обычный `stop_exec` до него не
        дотягивается, когда лаунчер перезапускался и реестра exec нет). Нет такого шага: `LauncherNotFound` (бота нет)."""
        check_bot_id(bot_id)
        check_exec_id(exec_id)
        await self._request("POST", f"/v1/bots/{bot_id}/procedure-step/cancel", json={"exec_id": exec_id})

    async def status(self, bot_id: str) -> BotStatus:
        check_bot_id(bot_id)
        return BotStatus.from_dict(self._json(await self._request("GET", f"/v1/bots/{bot_id}")))

    async def list_bots(self) -> list[BotStatus]:
        data = self._json(await self._request("GET", "/v1/bots"))
        return [BotStatus.from_dict(b) for b in data.get("bots", [])]

    async def stop_exec(self, exec_id: str, *, bot_id: str | None = None) -> None:
        check_exec_id(exec_id)
        body = {"bot_id": check_bot_id(bot_id)} if bot_id is not None else {}
        await self._request("POST", f"/v1/execs/{exec_id}/stop", json=body)

    # ---- exec ----

    async def _events(self, method: str, path: str, *, body: dict | None = None,
                      total: float | None = None) -> AsyncIterator[ExecEvent]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + total if total is not None else None
        seen_exit = False
        try:
            async with self._http.stream(method, path, json=body, timeout=self.stream_timeout) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _map_error(resp)
                lines = resp.aiter_lines()
                while True:
                    remaining = None if deadline is None else max(deadline - loop.time(), 0.0)
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), remaining)
                    except StopAsyncIteration:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue  # диагностический мусор не публикуем и не падаем
                    event = _event_from_frame(frame) if isinstance(frame, dict) else None
                    if event is None:
                        continue
                    seen_exit = seen_exit or isinstance(event, ExecExit)
                    yield event
        except TimeoutError as exc:
            raise LauncherTimeout(f"поток {path} не завершился вовремя", code="timeout") from exc
        except httpx.TimeoutException as exc:
            raise LauncherTimeout(f"лаунчер замолчал: {path}", code="timeout") from exc
        except httpx.TransportError as exc:
            raise LauncherUnavailable(f"лаунчер недоступен: {exc.__class__.__name__}", code="unavailable") from exc
        if not seen_exit:
            raise LauncherUnavailable("поток оборвался без кода завершения", code="unavailable")

    async def exec(self, bot_id: str, argv: list[str], *, env: Mapping[str, str] | None = None,
                   stdin: str | bytes | None = None, exec_id: str | None = None,
                   timeout: float | None = None) -> AsyncIterator[ExecEvent]:
        check_bot_id(bot_id)
        check_exec(argv, env, exec_id, timeout)
        body: dict = {"argv": list(argv)}
        if env:
            body["env"] = dict(env)
        if stdin is not None:
            if isinstance(stdin, bytes):
                try:
                    stdin = stdin.decode()
                except UnicodeDecodeError as exc:
                    raise LauncherInvalid("stdin: ожидается UTF-8", code="invalid") from exc
            body["stdin"] = stdin
        if exec_id is not None:
            body["exec_id"] = exec_id
        if timeout is not None:
            body["timeout"] = timeout
        total = timeout + self.stream_grace if timeout is not None else None
        async for event in self._events("POST", f"/v1/bots/{bot_id}/exec", body=body, total=total):
            yield event

    # ---- сеть пользователя ----

    async def ensure_network(self, owner_id: str) -> dict:
        """Сеть пользователя и подключение к ней ядра, без контейнера. Ядро зовёт её фоном при создании пользователя."""
        check_owner_id(owner_id)
        return self._json(await self._request("POST", f"/v1/networks/{owner_id}",
                                              timeout=httpx.Timeout(NETWORK_TIMEOUT, connect=self.request_timeout.connect)))

    # ---- вход по подписке ----

    async def create_login_container(self, owner_id: str) -> BotStatus:
        check_owner_id(owner_id)
        return BotStatus.from_dict(self._json(await self._request("POST", f"/v1/logins/{owner_id}")))

    async def remove_login_container(self, owner_id: str) -> bool:
        check_owner_id(owner_id)
        return bool(self._json(await self._request("DELETE", f"/v1/logins/{owner_id}")).get("removed"))

    async def open_login_session(self, owner_id: str, *, command: str = "shell", cols: int = 80,
                                 rows: int = 24) -> str:
        check_owner_id(owner_id)
        resp = await self._request("POST", f"/v1/logins/{owner_id}/sessions",
                                   json={"command": command, "cols": cols, "rows": rows})
        return str(self._json(resp)["session_id"])

    async def login_output(self, session_id: str) -> AsyncIterator[ExecEvent]:
        check_session_id(session_id)
        async for event in self._events("GET", f"/v1/login-sessions/{session_id}/output"):
            yield event

    async def login_input(self, session_id: str, data: bytes) -> None:
        check_session_id(session_id)
        await self._request("POST", f"/v1/login-sessions/{session_id}/input", content=data,
                            headers={"Content-Type": "application/octet-stream"})

    async def login_resize(self, session_id: str, cols: int, rows: int) -> None:
        check_session_id(session_id)
        await self._request("POST", f"/v1/login-sessions/{session_id}/resize", json={"cols": cols, "rows": rows})

    async def close_login_session(self, session_id: str) -> None:
        check_session_id(session_id)
        await self._request("DELETE", f"/v1/login-sessions/{session_id}")

    # ---- RFB screen sessions ----

    async def open_screen_session(self, bot_id: str, owner_id: str) -> str:
        check_bot_id(bot_id), check_owner_id(owner_id)
        resp = await self._request("POST", f"/v1/bots/{bot_id}/screen-sessions", json={"owner_id": owner_id})
        return check_session_id(self._json(resp)["session_id"])

    async def screen_output(self, session_id: str) -> AsyncIterator[bytes]:
        check_session_id(session_id)
        path = f"/v1/screen-sessions/{session_id}/output"
        try:
            async with self._http.stream("GET", path, timeout=self.screen_timeout) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _map_error(resp)
                async for chunk in resp.aiter_raw():
                    for offset in range(0, len(chunk), 65536):
                        yield chunk[offset:offset + 65536]
        except httpx.TimeoutException as exc:
            raise LauncherTimeout("экранный поток замолчал", code="timeout") from exc
        except httpx.TransportError as exc:
            raise LauncherUnavailable("экранный поток оборвался", code="unavailable") from exc

    async def screen_input(self, session_id: str, data: bytes) -> None:
        check_session_id(session_id)
        if len(data) > 65536:
            raise LauncherInvalid("screen input exceeds 64 KiB", code="invalid")
        await self._request("POST", f"/v1/screen-sessions/{session_id}/input", content=data,
                            headers={"Content-Type": "application/octet-stream"})

    async def close_screen_session(self, session_id: str) -> None:
        check_session_id(session_id)
        await self._request("DELETE", f"/v1/screen-sessions/{session_id}")


def launcher_client_from_env(environ: Mapping[str, str] | None = None) -> HttpLauncherClient | None:
    """None, если лаунчер не настроен (локальный режим BOTHUB_RUNNER_EXEC=local). Ошибка, если нет секрета."""
    env = os.environ if environ is None else environ
    socket_path, url = env.get("LAUNCHER_SOCKET"), env.get("LAUNCHER_URL")
    if not socket_path and not url:
        return None
    secret = env.get("LAUNCHER_SECRET", "")
    if not secret:
        raise LauncherError("LAUNCHER_SECRET не задан", code="config")
    kwargs = {}
    for key, name in (("LAUNCHER_CONNECT_TIMEOUT", "connect_timeout"), ("LAUNCHER_REQUEST_TIMEOUT", "request_timeout"),
                      ("LAUNCHER_STREAM_IDLE_TIMEOUT", "stream_idle_timeout")):
        if env.get(key):
            try:
                kwargs[name] = float(env[key])
            except ValueError as exc:
                raise LauncherError(f"{key}: ожидается число", code="config") from exc
    return HttpLauncherClient(secret=secret, socket_path=socket_path or None,
                              base_url=None if socket_path else url, **kwargs)


# ---------- фейк для тестов ядра ----------

@dataclass
class FakeBot:
    bot_id: str
    owner_id: str
    generation: int = 1
    running: bool = True


@dataclass
class _ExecScript:
    stdout: list[bytes]
    stderr: list[bytes]
    code: int
    hang: bool


class FakeLauncherClient:
    """Лаунчер в памяти: те же проверки аргументов и те же исключения, что у HttpLauncherClient.

    script_exec() задаёт вывод следующего exec, fail_next() вызывает исключение на следующем вызове,
    calls/execs/stopped хранят журнал для проверок."""

    def __init__(self) -> None:
        self.bots: dict[str, FakeBot] = {}
        self.networks: set[str] = set()  # owner_id сетей, которые ядро попросило создать до первого бота
        self.frozen: set[str] = set()
        self.browsers: set[str] = set()
        self.browser_modes: dict[str, str] = {}  # bot_id -> "human" | "bot" (absent means bot)
        self.tab_urls: dict[str, str | None] = {}  # what browser_tab reports for the bot's first page
        self.calls: list[tuple] = []
        self.execs: list[dict] = []
        self.stopped: list[str] = []
        self.login_inputs: dict[str, list[bytes]] = {}
        self.login_sizes: dict[str, tuple[int, int]] = {}
        self.screen_inputs: dict[str, list[bytes]] = {}
        self.screen_chunks: dict[str, list[bytes]] = {}
        self.cancelled: list[tuple[str, str]] = []  # procedure_step_cancel: (bot_id, exec_id)
        self.procedure_steps: list[dict] = []  # журнал вызовов procedure_step: {bot_id, payload, dry_run, timeout, exec_id}
        self.procedure_handler = None  # (payload: dict, dry_run: bool) -> result dict | None | Exception | (exit_code, reason, result)
        self._scripts: deque[_ExecScript] = deque()
        self._failures: deque[Exception] = deque()
        self._running: dict[str, asyncio.Event] = {}
        self._sessions: dict[str, tuple[list[bytes], int]] = {}
        self._ids = itertools.count(1)

    # ---- настройка ----
    def script_exec(self, *, stdout=(), stderr=(), code: int = 0, hang: bool = False) -> None:
        self._scripts.append(_ExecScript(list(stdout), list(stderr), code, hang))

    def fail_next(self, exc: Exception) -> None:
        self._failures.append(exc)

    def login_script(self, session_id: str, chunks, code: int = 0) -> None:
        self._sessions[session_id] = (list(chunks), code)

    def _enter(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if self._failures:
            raise self._failures.popleft()

    @staticmethod
    def _status(bot: FakeBot) -> BotStatus:
        return BotStatus(bot.bot_id, True, bot.running, "running" if bot.running else "exited",
                         owner_id=bot.owner_id, container=f"bot-{bot.bot_id}", network=f"bothub-u-{bot.owner_id}",
                         image="bothub-bot")

    # ---- боты ----
    async def create_bot(self, bot_id, owner_id):
        check_bot_id(bot_id), check_owner_id(owner_id)
        self._enter("create_bot", bot_id, owner_id)
        bot = self.bots.get(bot_id)
        if bot and bot.owner_id != owner_id:
            raise LauncherConflict(f"бот {bot_id} принадлежит другому владельцу", code="conflict", status=409)
        bot = self.bots.setdefault(bot_id, FakeBot(bot_id, owner_id))
        bot.running = True
        return self._status(bot)

    async def ensure_network(self, owner_id):
        check_owner_id(owner_id)
        self._enter("ensure_network", owner_id)
        self.networks.add(owner_id)
        return {"owner_id": owner_id, "network": f"bothub-u-{owner_id}", "core_connected": True}

    async def remove_bot(self, bot_id, *, purge=False):
        check_bot_id(bot_id)
        self._enter("remove_bot", bot_id, purge)
        self.frozen.discard(bot_id)
        self.browsers.discard(bot_id)
        return self.bots.pop(bot_id, None) is not None

    async def recreate_bot(self, bot_id):
        check_bot_id(bot_id)
        self._enter("recreate_bot", bot_id)
        bot = self.bots.get(bot_id)
        if bot is None:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        bot.generation += 1
        bot.running = True
        return self._status(bot)

    async def ensure_browser(self, bot_id):
        check_bot_id(bot_id)
        self._enter("ensure_browser", bot_id)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        started = bot_id not in self.browsers
        self.browsers.add(bot_id)
        return {"bot_id": bot_id, "running": True, "started": started}

    async def freeze_bot(self, bot_id):
        check_bot_id(bot_id)
        self._enter("freeze_bot", bot_id)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        self.frozen.add(bot_id)
        return {"bot_id": bot_id, "frozen": True}

    async def unfreeze_bot(self, bot_id):
        check_bot_id(bot_id)
        self._enter("unfreeze_bot", bot_id)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        self.frozen.discard(bot_id)
        return {"bot_id": bot_id, "frozen": False}

    async def browser_mode(self, bot_id, mode, *, url=None):
        check_bot_id(bot_id)
        check_browser_mode(mode, url)
        self._enter("browser_mode", bot_id, mode, url)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        changed = self.browser_modes.get(bot_id, "bot") != mode
        self.browser_modes[bot_id] = mode
        return {"bot_id": bot_id, "mode": mode, "changed": changed, "cookies": None}

    async def browser_tab(self, bot_id):
        check_bot_id(bot_id)
        self._enter("browser_tab", bot_id)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        mode = self.browser_modes.get(bot_id, "bot")
        return {"bot_id": bot_id, "mode": mode, "url": None if mode == "human" else self.tab_urls.get(bot_id)}

    async def procedure_step(self, bot_id, payload_json, dry_run=False, timeout=None, *, exec_id=None):
        check_bot_id(bot_id)
        timeout = check_procedure_step(payload_json, dry_run, timeout, exec_id)
        self._enter("procedure_step", bot_id, dry_run)
        if bot_id in self.frozen or self.browser_modes.get(bot_id) == "human":
            raise LauncherConflict(f"бот {bot_id} заморожен", code="frozen", status=409)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        payload = json.loads(payload_json)
        exec_id = exec_id or f"fake-{next(self._ids)}"
        self.procedure_steps.append({"bot_id": bot_id, "payload": payload, "dry_run": dry_run, "timeout": timeout,
                                     "exec_id": exec_id})
        outcome = self.procedure_handler(payload, dry_run) if self.procedure_handler else {
            "v": 1, "ok": True, "code": None, "acted": not dry_run, "url": None, "precondition_visible": None,
            "found": None, "expect": None}
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            exit_code, reason, result = outcome
            return {"exec_id": exec_id, "exit_code": exit_code, "reason": reason, "result": result}
        return {"exec_id": exec_id, "exit_code": 0, "reason": "exit", "result": outcome}

    async def procedure_step_cancel(self, bot_id, exec_id):
        check_bot_id(bot_id)
        check_exec_id(exec_id)
        self._enter("procedure_step_cancel", bot_id, exec_id)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        self.cancelled.append((bot_id, exec_id))

    async def status(self, bot_id):
        check_bot_id(bot_id)
        self._enter("status", bot_id)
        bot = self.bots.get(bot_id)
        return self._status(bot) if bot else BotStatus(bot_id, False, False, "missing")

    async def list_bots(self):
        self._enter("list_bots")
        return [self._status(b) for b in self.bots.values()]

    # ---- exec ----
    async def exec(self, bot_id, argv, *, env=None, stdin=None, exec_id=None, timeout=None):
        check_bot_id(bot_id)
        check_exec(argv, env, exec_id, timeout)
        self._enter("exec", bot_id, list(argv))
        if bot_id in self.frozen:
            raise LauncherConflict(f"бот {bot_id} заморожен", code="frozen", status=409)
        if bot_id not in self.bots:
            raise LauncherNotFound(f"бота {bot_id} нет", code="not_found", status=404)
        script = self._scripts.popleft() if self._scripts else _ExecScript([], [], 0, False)
        exec_id = exec_id or f"fake-{next(self._ids)}"
        self.execs.append({"bot_id": bot_id, "argv": list(argv), "env": dict(env or {}), "stdin": stdin,
                           "exec_id": exec_id, "timeout": timeout})
        yield ExecStarted(exec_id)
        for chunk in script.stdout:
            yield ExecChunk("stdout", chunk)
        for chunk in script.stderr:
            yield ExecChunk("stderr", chunk)
        if script.hang:
            gate = self._running[exec_id] = asyncio.Event()
            try:
                await gate.wait()
            finally:
                self._running.pop(exec_id, None)
            yield ExecExit(143, "stopped")
        else:
            yield ExecExit(script.code, "exit")

    async def stop_exec(self, exec_id, *, bot_id=None):
        check_exec_id(exec_id)
        if bot_id is not None:
            check_bot_id(bot_id)
        self._enter("stop_exec", exec_id, bot_id)
        gate = self._running.get(exec_id)
        if gate is None and bot_id is None:
            raise LauncherNotFound(f"exec {exec_id} не найден", code="not_found", status=404)
        self.stopped.append(exec_id)
        if gate is not None:
            gate.set()

    # ---- вход по подписке ----
    async def create_login_container(self, owner_id):
        check_owner_id(owner_id)
        self._enter("create_login_container", owner_id)
        return BotStatus(None, True, True, "running", owner_id=owner_id, container=f"login-{owner_id}",
                         network=f"bothub-u-{owner_id}", image="bothub-bot")

    async def remove_login_container(self, owner_id):
        check_owner_id(owner_id)
        self._enter("remove_login_container", owner_id)
        return True

    async def open_login_session(self, owner_id, *, command="shell", cols=80, rows=24):
        check_owner_id(owner_id)
        self._enter("open_login_session", owner_id, command)
        sid = f"{next(self._ids):016x}"
        self._sessions.setdefault(sid, ([], 0))
        self.login_inputs[sid] = []
        self.login_sizes[sid] = (cols, rows)
        return sid

    def _session(self, session_id: str):
        check_session_id(session_id)
        if session_id not in self.login_inputs:
            raise LauncherNotFound("сессия входа не найдена", code="not_found", status=404)

    async def login_output(self, session_id):
        self._session(session_id)
        chunks, code = self._sessions[session_id]
        for chunk in chunks:
            yield ExecChunk("stdout", chunk)
        yield ExecExit(code, "exit")

    async def login_input(self, session_id, data):
        self._session(session_id)
        self.login_inputs[session_id].append(data)

    async def login_resize(self, session_id, cols, rows):
        self._session(session_id)
        self.login_sizes[session_id] = (cols, rows)

    async def close_login_session(self, session_id):
        self._enter('close_login_session', session_id)
        self._session(session_id)
        del self.login_inputs[session_id]
        self._sessions.pop(session_id, None)

    async def open_screen_session(self, bot_id, owner_id):
        check_bot_id(bot_id), check_owner_id(owner_id)
        self._enter("open_screen_session", bot_id, owner_id)
        bot = self.bots.get(bot_id)
        if bot is None or not bot.running:
            raise LauncherNotFound("бот не запущен", code="not_found", status=404)
        if bot.owner_id != owner_id:
            raise LauncherForbidden("чужой бот", code="forbidden", status=403)
        sid = f"{next(self._ids):016x}"
        self.screen_inputs[sid] = []
        self.screen_chunks[sid] = []
        return sid

    async def screen_output(self, session_id):
        check_session_id(session_id)
        if session_id not in self.screen_inputs:
            raise LauncherNotFound("экранная сессия не найдена", code="not_found", status=404)
        for chunk in self.screen_chunks[session_id]:
            yield chunk

    async def screen_input(self, session_id, data):
        check_session_id(session_id)
        if len(data) > 65536:
            raise LauncherInvalid("screen input exceeds 64 KiB", code="invalid")
        if session_id not in self.screen_inputs:
            raise LauncherNotFound("экранная сессия не найдена", code="not_found", status=404)
        self.screen_inputs[session_id].append(data)

    async def close_screen_session(self, session_id):
        self._enter("close_screen_session", session_id)
        self.screen_inputs.pop(session_id, None)
        self.screen_chunks.pop(session_id, None)

    async def aclose(self):
        return None
