"""Claude CLI adapter."""
import json

from bothub.browser_control import BrowserEventMasker
from bothub.context import usable_window

from .base import RunnerEvent, TurnContext
from .subprocess import SubprocessRunner, token_count, usage_event, with_context


class ClaudeRunner(SubprocessRunner):
    provider = "claude"

    def __init__(self) -> None:
        super().__init__()
        # Text typed into the bot's browser must not reach events: args and results are masked here.
        self._browser = BrowserEventMasker()
        self._last_usage: dict[str, dict] = {}  # session_id -> usage последнего assistant-сообщения

    def command(self, turn: TurnContext) -> list[str]:
        config = {"mcpServers": {"bothub": {
            "command": "python", "args": ["-m", "bothub.mcp_server"],
            "env": {"BOTHUB_THREAD_ID": turn.thread_id, "BOTHUB_TURN_ID": turn.turn_id},
        }}}
        command = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if turn.cli_session_id:
            command += ["--resume", turn.cli_session_id]
        if turn.bot.get("model"):
            command += ["--model", turn.bot["model"]]
        if turn.compact:
            # Служебный ход сжатия только пишет сводку: `--tools ""` отключает все встроенные инструменты (claude --help),
            # без --mcp-config и с --strict-mcp-config не поднимается ни один MCP-сервер, в том числе наш approve.
            command += ["--tools", "", "--strict-mcp-config"]
        else:
            command += ["--mcp-config", json.dumps(config), "--permission-prompt-tool", "mcp__bothub__approve"]
        command += ["--append-system-prompt", "\n\n".join(filter(None, (
            turn.bot.get("instructions", ""), turn.memory_md)))]
        if turn.dry_run:
            command += ["--permission-mode", "plan"]
        return command

    def parse(self, message: dict) -> list[RunnerEvent]:
        kind = message.get("type")
        sid = message.get("session_id")
        # session_id тоже стоит на hook_started/hook_response/thinking_tokens и т.п. —
        # без проверки subtype=="init" каждое из них давало бы лишний system-event.
        if kind == "system" and message.get("subtype") == "init" and sid:
            return [RunnerEvent("system", {"text": "Session started"}, sid)]
        if kind == "assistant":
            if sid and isinstance((message.get("message") or {}).get("usage"), dict):
                self._last_usage[sid] = message["message"]["usage"]
            content = (message.get("message") or {}).get("content") or []
            events = []
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    events.append(RunnerEvent("assistant_msg", {"text": block["text"], "final": False}, sid))
                elif block.get("type") == "tool_use":
                    events.append(RunnerEvent("tool_call", self._browser.call({
                        "call_id": block.get("id", ""), "tool": block.get("name", ""),
                        "args": block.get("input") or {},
                    }), sid))
            return events
        if kind == "user":
            events = []
            for block in (message.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result":
                    events.append(RunnerEvent("tool_result", self._browser.result({
                        "call_id": block.get("tool_use_id", ""),
                        "ok": not block.get("is_error", False),
                        "summary": str(block.get("content", ""))[:500],
                    }), sid))
            return events
        if kind == "result":
            events = [RunnerEvent("assistant_msg", {"text": "", "final": True}, sid)]
            if message.get("usage"):
                event = usage_event(message["usage"], message.get("model", ""))
                event.cli_session_id = sid
                self._add_context(event, message, self._last_usage.pop(sid, None))
                events.append(event)
            return events
        return []

    def _add_context(self, event: RunnerEvent, result: dict, last_call: dict | None) -> None:
        """result.usage суммирует все вызовы модели хода и не считает кэш во input_tokens, поэтому контекст берём из
        последнего вызова: usage.iterations[-1], иначе usage последнего assistant-сообщения, иначе (приблизительно)
        сумма по ходу. Окно сообщает modelUsage[модель].contextWindow."""
        usage = result["usage"]
        iterations = usage.get("iterations")
        call = iterations[-1] if isinstance(iterations, list) and iterations and isinstance(iterations[-1], dict) else last_call
        estimated = call is None
        call = call or usage
        tokens = sum(token_count(call, key) for key in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        with_context(event, tokens, estimated=estimated, window=self._window(result.get("modelUsage")))

    @staticmethod
    def _window(model_usage) -> int | None:
        windows = []
        for item in (model_usage.values() if isinstance(model_usage, dict) else ()):
            if isinstance(item, dict) and usable_window(item.get("contextWindow")) is not None:
                spent = sum(token_count(item, key) for key in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
                windows.append((spent, item["contextWindow"]))
        return max(windows)[1] if windows else None
