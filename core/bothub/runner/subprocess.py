"""Общий жизненный цикл потоковых CLI раннеров."""
import asyncio
import json
import logging
import os
import signal
from collections.abc import AsyncIterator

from .base import RunnerEvent, TurnContext
from bothub.context import usable_window
from bothub.mcp_policy import McpNotAllowed, mcp_allowed
from bothub.launcher_client import ExecChunk, ExecExit, LineSplitter


class SubprocessRunner:
    provider = ""
    # У CLI без permission-prompt (codex, agy) чужой MCP-инструмент вне mcp_allow ловится в потоке событий: ход
    # останавливается с mcp_not_allowed. У Claude отказ даёт approve (mcp_server), ход продолжается: там False.
    stream_guard = True

    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._stopped: set[str] = set()
        self._launchers: dict[str, tuple[object, str]] = {}

    def command(self, turn: TurnContext) -> list[str]:
        raise NotImplementedError

    def parse(self, message: dict) -> list[RunnerEvent]:
        raise NotImplementedError

    def guard(self, turn: TurnContext, event: RunnerEvent) -> None:
        """McpNotAllowed на tool_call чужого MCP-инструмента вне bots.mcp_allow. Ход сжатия не трогаем: его останавливает ядро."""
        if self.stream_guard and not turn.compact and event.kind == "tool_call" \
                and not mcp_allowed(turn.bot.get("mcp_allow"), event.payload.get("tool")):
            raise McpNotAllowed(str(event.payload.get("tool")))

    async def run(self, turn: TurnContext) -> AsyncIterator[RunnerEvent]:
        if turn.turn_id in self._processes or turn.turn_id in self._launchers:
            raise RuntimeError("turn is already running")
        self._stopped.discard(turn.turn_id)
        timeout = int(turn.bot.get("max_turn_seconds", 1800))
        if timeout <= 0:
            raise ValueError("max_turn_seconds must be positive")
        command = self.command(turn)
        if turn.launcher is not None:
            self._launchers[turn.turn_id] = (turn.launcher, turn.bot_container_id)
            lines = LineSplitter()
            session_id = turn.cli_session_id
            started = asyncio.get_running_loop().time()
            exit_code = None
            stderr_tail = b''  # хвост stderr CLI: без него в turn_error только «exited with status 1»
            try:
                async for frame in turn.launcher.exec(turn.bot_container_id, command,
                                                       env=turn.exec_env, stdin=self.prompt(turn),
                                                       exec_id=turn.turn_id, timeout=timeout):
                    if isinstance(frame, ExecExit):
                        exit_code = frame.code
                    elif isinstance(frame, ExecChunk) and frame.stream == 'stderr':
                        stderr_tail = (stderr_tail + frame.data)[-2048:]
                    elif isinstance(frame, ExecChunk) and frame.stream == 'stdout':
                        for line in lines.feed(frame.data):
                            try:
                                message = json.loads(line)
                            except (json.JSONDecodeError, UnicodeError):
                                continue
                            if isinstance(message, dict):
                                # codex/claude печатают сбой в stdout JSON-строкой ({"type":"error"} или turn.failed):
                                # запоминаем текст, чтобы turn_error сказал причину, а не только код выхода
                                if message.get('type') in ('error', 'turn.failed'):
                                    err = message.get('message') or (message.get('error') or {}).get('message') if isinstance(message.get('error'), dict) else message.get('message')
                                    if err:
                                        stderr_tail = (stderr_tail + f' {err}'.encode())[-2048:]
                                for event in self.parse(message):
                                    session_id = event.cli_session_id or session_id
                                    event.cli_session_id = session_id
                                    if event.kind == 'usage':
                                        event.payload['model'] = event.payload.get('model') or turn.bot.get('model', '')
                                        event.payload['seconds'] = round(asyncio.get_running_loop().time() - started, 3)
                                    self.guard(turn, event)
                                    yield event
                tail = lines.flush()
                if tail:
                    try: message = json.loads(tail)
                    except (json.JSONDecodeError, UnicodeError): message = None
                    if isinstance(message, dict):
                        for event in self.parse(message):
                            session_id = event.cli_session_id or session_id
                            event.cli_session_id = session_id
                            if event.kind == 'usage':
                                event.payload['model'] = event.payload.get('model') or turn.bot.get('model', '')
                                event.payload['seconds'] = round(asyncio.get_running_loop().time() - started, 3)
                            self.guard(turn, event)
                            yield event
                if exit_code and turn.turn_id not in self._stopped:
                    detail = ' '.join(stderr_tail.decode('utf-8', 'replace').split())[-300:]
                    raise RuntimeError(f'{self.provider} exited with status {exit_code}' + (f': {detail}' if detail else ''))
            except McpNotAllowed:
                # закрытие потока не гарантирует остановку процесса в контейнере: убиваем явно, вызов уже не должен идти дальше
                try:
                    await turn.launcher.stop_exec(turn.turn_id, bot_id=turn.bot_container_id)
                except Exception:
                    logging.getLogger(__name__).warning('mcp_not_allowed_stop_failed', extra={'turn_id': turn.turn_id}, exc_info=True)
                raise
            finally:
                self._launchers.pop(turn.turn_id, None)
                self._stopped.discard(turn.turn_id)
            return
        process = await asyncio.create_subprocess_exec(
            *turn.exec_prefix, *command,
            env=self._local_env(turn),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # stream-json строка с результатом инструмента бывает мегабайтами (скриншот, read_file):
            # при прежнем лимите 1 МБ чтение падало «chunk exceed the limit» (скриншот 2.5 МБ).
            limit=32 * 1024 * 1024,
            start_new_session=True,
        )
        self._processes[turn.turn_id] = process
        stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        started = asyncio.get_running_loop().time()
        try:
            assert process.stdin and process.stdout
            session_id = turn.cli_session_id
            async with asyncio.timeout(timeout):
                process.stdin.write(self.prompt(turn).encode())
                try:
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                process.stdin.close()
                async for line in process.stdout:
                    try:
                        message = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        continue  # Diagnostic text may contain credentials; never publish it.
                    if not isinstance(message, dict):
                        continue
                    for event in self.parse(message):
                        session_id = event.cli_session_id or session_id
                        event.cli_session_id = session_id
                        if event.kind == "usage":
                            event.payload["model"] = event.payload.get("model") or turn.bot.get("model", "")
                            event.payload["seconds"] = round(asyncio.get_running_loop().time() - started, 3)
                        self.guard(turn, event)
                        yield event
                code = await process.wait()
            if code and turn.turn_id not in self._stopped:
                raise RuntimeError(f"{self.provider} exited with status {code}")
        except TimeoutError as exc:
            raise TimeoutError(f"{self.provider} exceeded {timeout} seconds") from exc
        finally:
            await self._terminate(process)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            self._processes.pop(turn.turn_id, None)
            self._stopped.discard(turn.turn_id)

    @staticmethod
    def _local_env(turn: TurnContext) -> dict[str, str]:
        env = dict(os.environ)
        if turn.bot.get('_gateway_kind'):
            for name in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY',
                         'GOOGLE_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
                env.pop(name, None)
        env.update(turn.exec_env)
        return env

    async def stop(self, turn_id: str) -> None:
        if turn_id in self._launchers:
            self._stopped.add(turn_id)
            launcher, bot_id = self._launchers[turn_id]
            await launcher.stop_exec(turn_id, bot_id=bot_id)
            return
        process = self._processes.get(turn_id)
        if process:
            self._stopped.add(turn_id)
            await self._terminate(process)
    def prompt(self, turn: TurnContext) -> str:
        return turn.prompt

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline:
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 0.05)
                except TimeoutError:
                    pass
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    @staticmethod
    async def _drain_stderr(stream: asyncio.StreamReader | None) -> None:
        if stream:
            while await stream.read(65536):
                pass


def token_count(usage: dict, *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if type(value) is not int or value < 0:
            continue
        if value > 10**9:
            logging.getLogger(__name__).warning('runner_usage_token_limit', extra={'field': key})
            return 0
        return value
    return 0


def usage_event(usage: dict, model: str = "") -> RunnerEvent:
    return RunnerEvent("usage", {
        "tokens_in": token_count(usage, "input_tokens", "inputTokens"),
        "tokens_out": token_count(usage, "output_tokens", "outputTokens"),
        "model": model,
        "seconds": 0,
    })


def with_context(event: RunnerEvent, tokens: int, *, estimated: bool = False, window: int | None = None) -> RunnerEvent:
    """Размер контекста сессии после хода (docs/contracts.md, раздел 15): промпт последнего вызова модели
    плюс его ответ. estimated: цифра приблизительная (CLI отдал сумму по вызовам хода, а не последний вызов)."""
    event.payload["context_tokens"] = tokens
    event.payload["context_estimated"] = estimated
    if usable_window(window) is not None:
        event.payload["context_window"] = window
    return event
