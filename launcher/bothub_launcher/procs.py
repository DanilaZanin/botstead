"""Запуск настоящих процессов: короткие команды (docker, iptables), стримящий exec и pty для логина."""
import asyncio
import contextlib
import fcntl
import os
import signal
import struct
import termios
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from .errors import BackendError

CHUNK = 65536
# Минимум окружения для docker CLI: секреты самого лаунчера (LAUNCHER_SECRET, BOT_TOKEN_SECRET) не передаём.
PASS_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR", "LANG", "LC_ALL")


@dataclass(frozen=True)
class CmdResult:
    rc: int
    out: str
    err: str


def base_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(extra or {})
    return env


class SubprocessRunner:
    """Выполняет короткую команду и возвращает код и вывод. Отсутствие бинарника это код 127, таймаут это 124."""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout

    async def __call__(self, argv: list[str], *, input: str | None = None,
                       env: Mapping[str, str] | None = None) -> CmdResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=base_env(env), start_new_session=True,
            )
        except (FileNotFoundError, NotADirectoryError):
            return CmdResult(127, "", f"{argv[0]}: not found")
        except PermissionError:
            return CmdResult(126, "", f"{argv[0]}: permission denied")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input.encode() if input is not None else None), self.timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            return CmdResult(124, "", f"{argv[0]}: timeout after {self.timeout}s")
        return CmdResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


class SubprocessExec:
    """Процесс `docker exec` (клиент). Вывод stdout и stderr приходит одним потоком пар (имя, байты)."""

    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc

    @classmethod
    async def start(cls, argv: list[str], env: Mapping[str, str]) -> "SubprocessExec":
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=base_env(env), start_new_session=True, limit=CHUNK * 4,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise BackendError(f"не удалось запустить {argv[0]}: {exc.strerror or exc}") from exc
        return cls(proc)

    async def write_stdin(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            if data:
                stdin.write(data)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                stdin.close()

    async def output(self) -> AsyncIterator[tuple[str, bytes]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=4)

        async def pump(name: str, stream: asyncio.StreamReader | None):
            try:
                while stream is not None:
                    chunk = await stream.read(CHUNK)
                    if not chunk:
                        break
                    await queue.put((name, chunk))
            finally:
                if not asyncio.current_task().cancelling():
                    await queue.put(None)

        tasks = [asyncio.create_task(pump("stdout", self._proc.stdout)),
                 asyncio.create_task(pump("stderr", self._proc.stderr))]
        try:
            done = 0
            while done < 2:
                item = await queue.get()
                if item is None:
                    done += 1
                else:
                    yield item
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait(self) -> int:
        return await self._proc.wait()

    async def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGKILL)


class SubprocessStream:
    """Duplex stream to a long-running child process with bounded asyncio pipe buffers."""

    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc
        self._stderr_task = asyncio.create_task(self._discard_stderr())

    @classmethod
    async def start(cls, argv: list[str], env: Mapping[str, str]) -> "SubprocessStream":
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=base_env(env), start_new_session=True, limit=CHUNK * 4,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise BackendError(f"не удалось запустить {argv[0]}: {exc.strerror or exc}") from exc
        return cls(proc)

    async def _discard_stderr(self) -> None:
        if self._proc.stderr is not None:
            while await self._proc.stderr.read(CHUNK):
                pass

    async def read(self) -> bytes:
        if self._proc.stdout is None:
            return b""
        return await self._proc.stdout.read(CHUNK)

    async def write_stdin(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None or self._proc.returncode is not None:
            raise BackendError("RFB-поток уже закрыт")
        try:
            stdin.write(data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise BackendError("RFB-поток закрыл соединение") from exc

    async def wait(self) -> int:
        rc = await self._proc.wait()
        await asyncio.gather(self._stderr_task, return_exceptions=True)
        return rc

    async def kill(self) -> None:
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._proc.pid, signal.SIGKILL)


class PtyProcess:
    """Процесс с pty: байты клавиатуры туда, байты экрана обратно."""

    def __init__(self, proc: asyncio.subprocess.Process, master: int):
        self._proc = proc
        self._master = master
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._eof = False
        self._closed = False
        asyncio.get_running_loop().add_reader(master, self._on_readable)

    @classmethod
    async def start(cls, argv: list[str], cols: int, rows: int, env: Mapping[str, str] | None = None) -> "PtyProcess":
        master, slave = os.openpty()
        cls._set_size(master, cols, rows)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=slave, stdout=slave, stderr=slave, env=base_env({"TERM": "xterm-256color", **(env or {})}),
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError) as exc:
            os.close(master)
            raise BackendError(f"не удалось запустить {argv[0]}: {exc.strerror or exc}") from exc
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        return cls(proc, master)

    @staticmethod
    def _set_size(fd: int, cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master, CHUNK)
        except BlockingIOError:
            return
        except OSError:  # Linux отдаёт EIO, когда все держатели slave закрылись
            data = b""
        if data:
            self._queue.put_nowait(data)
        else:
            self._finish()

    def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        with contextlib.suppress(Exception):
            loop.remove_reader(self._master)
        with contextlib.suppress(OSError):
            os.close(self._master)
        self._queue.put_nowait(b"")

    async def read(self) -> bytes:
        if self._eof:
            return b""
        data = await self._queue.get()
        if not data:
            self._eof = True
        return data

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        view = memoryview(data)
        while view:
            try:
                n = os.write(self._master, view)
            except BlockingIOError:
                break  # ввод человека короткий; при полном буфере остаток отбрасываем
            except OSError:
                break
            view = view[n:]

    def resize(self, cols: int, rows: int) -> None:
        if not self._closed:
            with contextlib.suppress(OSError):
                self._set_size(self._master, cols, rows)

    async def wait(self) -> int:
        return await self._proc.wait()

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGKILL)
