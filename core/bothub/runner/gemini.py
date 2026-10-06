"""Antigravity (agy) CLI adapter.

Формат разобран по реальному выводу `agy --output-format stream-json -p "..."`
(снято на этой машине, agy CLI, модель gemini-3.8-flash-low) — черновой парсер
ориентировался на несуществующие поля ("type", "session_id"): реальные события
это {"event": "<kind>", "<kind>": {...}}, а не {"type": ...}.

Важно: `-p` требует значение прямо в аргументе, а не через stdin (`agy -p` без
аргумента или с последующим флагом сразу же падает: "empty prompt" / флаг съедает
следующий токен как промпт). Поэтому промпт собирается в command(), а prompt()
переопределён, чтобы SubprocessRunner ничего лишнего не писал в stdin agy.
"""
from .base import RunnerEvent, TurnContext
from .subprocess import SubprocessRunner, token_count, usage_event, with_context


class GeminiRunner(SubprocessRunner):
    provider = "gemini"

    def __init__(self) -> None:
        super().__init__()
        self._step_usage: dict[str, dict] = {}  # conversation_id -> usage последнего шага

    def command(self, turn: TurnContext) -> list[str]:
        command = ["agy", "--output-format", "stream-json"]
        if turn.bot.get("model"):
            command += ["--model", turn.bot["model"]]
        if turn.cli_session_id:
            command += ["--conversation", turn.cli_session_id]
        if turn.dry_run or turn.compact:
            # Служебный ход сжатия идёт в режиме plan и без автоодобрения (`agy --help`); запрета инструментов у agy
            # больше нет, остальное держит слой ядра: первый же tool_call хода сжатия его останавливает.
            command += ["--mode", "plan"]
        else:
            command += ["--dangerously-skip-permissions"]
        # -p должен идти последним и сразу со своим значением (см. докстринг).
        command += ["-p", "\n\n".join(filter(None, (
            turn.bot.get("instructions", ""), turn.memory_md, turn.prompt)))]
        return command

    def prompt(self, turn: TurnContext) -> str:
        return ""  # промпт уже передан как аргумент -p, stdin agy не читает

    def parse(self, message: dict) -> list[RunnerEvent]:
        event = message.get("event")
        if event == "init":
            sid = message.get("conversation_id")
            return [RunnerEvent("system", {"text": "Session started"}, sid)] if sid else []
        if event == "step_update":
            step = message.get("step_update") or {}
            sid = step.get("conversation_id")
            if sid and isinstance(step.get("usage"), dict) and token_count(step["usage"], "input_tokens"):
                self._step_usage[sid] = step["usage"]
            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                return [RunnerEvent("assistant_msg", {"text": step["text_delta"], "final": False}, sid)]
            # TODO(runner): шаги tool_call/tool_result у agy не подтверждены реальным
            # выводом — тестовый прогон был без инструментов (--dangerously-skip-permissions,
            # но промпт "скажи привет одним словом" их не вызвал). Прежде чем сюда добавлять
            # маппинг args/id для tool_call/tool_result, снять живой пример с реальным
            # вызовом инструмента agy. Когда маппинг появится, пропускать args и результат через
            # bothub.browser_control.BrowserEventMasker (как в claude.py и codex.py): сейчас
            # парсер событий инструментов не отдаёт, маскировать нечего.
            return []
        if event == "result":
            result = message.get("result") or {}
            sid = result.get("conversation_id")
            # На успехе текст уже пришёл через step_update text_delta — здесь только
            # маркер конца (как у claude/codex). На ошибке текста не было вовсе, его
            # нужно вытащить из result.error, иначе ответ бота потеряется молча.
            text = "" if result.get("status") == "SUCCESS" else (result.get("error") or "")
            events = [RunnerEvent("assistant_msg", {"text": text, "final": True}, sid)]
            usage = result.get("usage")
            if usage:
                item = usage_event(usage, result.get("model", ""))
                item.cli_session_id = sid
                # result.usage может суммировать шаги: контекст берём из последнего шага (промпт плюс ответ).
                step = self._step_usage.pop(sid, None) if sid else None
                source = step or usage
                with_context(item, token_count(source, "input_tokens") + token_count(source, "output_tokens"),
                             estimated=step is None)
                events.append(item)
            return events
        return []
