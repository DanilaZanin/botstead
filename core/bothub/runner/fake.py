"""Детерминированный раннер для разработки."""
from collections.abc import AsyncIterator

from .base import RunnerEvent, TurnContext


class FakeRunner:
    provider = "fake"

    async def run(self, turn: TurnContext) -> AsyncIterator[RunnerEvent]:
        sid = turn.cli_session_id or f"fake:{turn.thread_id}"
        yield RunnerEvent("plan", {"steps": [
            {"id": "1", "title": "Прочитать запрос", "status": "done"},
            {"id": "2", "title": "Ответить", "status": "done"},
        ]}, sid)
        yield RunnerEvent("assistant_msg", {"text": f"ok: {turn.prompt}", "final": True}, sid)
        yield RunnerEvent("usage", {"tokens_in": 10, "tokens_out": 20,
                                    "model": turn.bot.get("model", "fake"), "seconds": 0}, sid)

    async def stop(self, turn_id: str) -> None:
        pass
