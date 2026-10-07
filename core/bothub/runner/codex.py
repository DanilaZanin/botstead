"""Codex CLI adapter."""
from bothub.browser_control import BrowserEventMasker

from .base import RunnerEvent, TurnContext
from .subprocess import SubprocessRunner, token_count, usage_event, with_context


# MCP-сервер bothub для codex через -c, значения из окружения процесса (env_vars): токен не попадает в argv.
# -c сливается с ~/.codex/config.toml, а не заменяет его (проверено на codex 0.159), и файл бот может переписать:
# подменить `cwd` или `env` сервера bothub, добавить свои серверы. Поэтому при пустом mcp_allow файл не читается
# вовсе (`--ignore-user-config`, есть у exec и exec resume в 0.156.1 из образа; авторизация всё равно из CODEX_HOME).
# При непустом списке владелец сам подключил сторонние серверы из config.toml: файл читается, чужие вызовы
# останавливает SubprocessRunner.guard уже после старта вызова (docs/contracts.md, раздел 4).
# `required=true`: без него codex 0.156 не ждёт старта MCP-сервера (python + импорт ~1 с) перед первым запросом к модели,
# и в части ходов (на стенде ~3 из 4) инструментов bothub в запросе нет: модель отвечает «такого инструмента нет».
# С required=true первый запрос уходит только после старта сервера (6 из 6 на стенде), а сбой старта валит ход громко.
BOTHUB_MCP_FLAGS = [
    "-c", 'mcp_servers.bothub.command="python"',
    "-c", 'mcp_servers.bothub.args=["-m","bothub.mcp_server"]',
    "-c", "mcp_servers.bothub.required=true",
    "-c", 'mcp_servers.bothub.env_vars=["BOTHUB_URL","BOTHUB_TOKEN","BOTHUB_THREAD_ID","BOTHUB_TURN_ID"]',
]


class CodexRunner(SubprocessRunner):
    provider = "codex"

    def __init__(self) -> None:
        super().__init__()
        self._browser = BrowserEventMasker()  # see ClaudeRunner

    def command(self, turn: TurnContext) -> list[str]:
        # `codex exec resume <sid>` — отдельный подкоманд без -s/--sandbox (сессия уже
        # несёт свою песочницу); флаг -s есть только у "codex exec" без resume.
        # Проверено через `codex exec resume --help` (реальный CLI на этой машине).
        if turn.cli_session_id:
            command = ["codex", "exec", "resume", turn.cli_session_id, "--json", "--skip-git-repo-check"]
            if turn.compact:
                command += ["-c", 'sandbox_mode="read-only"']  # у resume нет -s, то же значение через конфиг
        else:
            sandbox = "read-only" if turn.dry_run or turn.compact else "workspace-write"
            # Домашний каталог бота не git-репозиторий: без флага codex 0.156 отказывается стартовать
            # («Not inside a trusted directory»), проверено на стенде.
            command = ["codex", "exec", "--json", "--skip-git-repo-check", "-s", sandbox]
        if turn.compact:
            # Служебный ход сжатия только пишет сводку. Способа выключить все инструменты у codex нет: отключаем
            # оболочку и unified exec (`--disable <FEATURE>`, `codex features list`), остальное запрещает слой ядра:
            # первый же tool_call хода сжатия его останавливает.
            command += ["--disable", "shell_tool", "--disable", "unified_exec"]
        if turn.compact or not turn.bot.get("mcp_allow"):
            command += ["--ignore-user-config"]
        if not turn.compact:
            command += BOTHUB_MCP_FLAGS
        if turn.bot.get("model"):
            command += ["-m", turn.bot["model"]]
        if turn.bot.get('_gateway_kind') in ('openai_api','openai_compatible'):
            command += ['-c','model_provider="bothub"',
                        '-c','model_providers.bothub.name="botstead"',  # codex 0.156: без name «provider name must not be empty»
                        '-c',f'model_providers.bothub.base_url="{turn.bot["_gateway_url"]}/v1"',
                        '-c','model_providers.bothub.env_key="BOTHUB_GATEWAY_TOKEN"',
                        '-c','model_providers.bothub.wire_api="responses"',
                        '-c','model_providers.bothub.supports_websockets=false']
            ctx_win = turn.bot.get('_context_window')
            if ctx_win:
                command += ['-c', f'model_context_window={ctx_win}',
                            '-c', f'model_auto_compact_token_limit={int(ctx_win * 0.8)}']
        command += ["-"]
        return command

    def prompt(self, turn: TurnContext) -> str:
        return "\n\n".join(filter(None, (turn.bot.get("instructions", ""),
                                         turn.memory_md, turn.prompt)))

    def parse(self, message: dict) -> list[RunnerEvent]:
        kind = message.get("type")
        sid = message.get("thread_id")
        if kind == "thread.started" and sid:
            return [RunnerEvent("system", {"text": "Session started"}, sid)]
        item = message.get("item") or {}
        if kind == "item.completed":
            if item.get("type") == "agent_message":
                return [RunnerEvent("assistant_msg", {
                    "text": item.get("text", ""), "final": False,
                }, sid)]
            if item.get("type") in {"command_execution", "mcp_tool_call"}:
                result = item.get("error") or item.get("result") or item.get("aggregated_output") or ""
                ok = not item.get("error") and (
                    item.get("exit_code") == 0 if item.get("type") == "command_execution"
                    else item.get("status") == "completed")
                payload = {
                    "call_id": item.get("id", ""),
                    "ok": ok,
                    "summary": str(result)[:500],
                }
                if item.get("type") == "mcp_tool_call":
                    payload["data"] = {key: str(item[key])[:500] for key in
                                       ("server", "result", "error") if item.get(key) is not None}
                tool = f"{item['server']}.{item['tool']}" if item.get("server") and item.get("tool") else None
                return [RunnerEvent("tool_result", self._browser.result(payload, tool=tool, args=item.get("arguments")), sid)]
        if kind == "item.started" and item.get("type") in {"command_execution", "mcp_tool_call"}:
            tool = item.get("tool") or item.get("type", "")
            if item.get("server") and item.get("tool"):
                tool = f"{item['server']}.{tool}"
            elif item.get("type") == "mcp_tool_call":
                tool = "mcp_tool_call"  # MCP-вызов без сервера: имя, которое политика считает чужим (fail-closed)
            return [RunnerEvent("tool_call", self._browser.call({
                "call_id": item.get("id", ""),
                "tool": tool,
                "args": ({"command": item["command"]} if item.get("command") else item.get("arguments") or {}),
            }), sid)]
        if kind == "turn.completed":
            events = [RunnerEvent("assistant_msg", {"text": "", "final": True}, sid)]
            if message.get("usage"):
                event = usage_event(message["usage"], message.get("model", ""))
                event.cli_session_id = sid
                # input_tokens включает cached_input_tokens и, по снятому потоку, суммируется по вызовам модели
                # хода: верхняя оценка контекста, не точная цифра.
                with_context(event, token_count(message["usage"], "input_tokens") + token_count(message["usage"], "output_tokens"),
                             estimated=True)
                events.append(event)
            return events
        return []
