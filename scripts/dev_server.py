"""Локальный запуск ядра Bot Hub с фейковым раннером.

bothub.runner.get_runner ещё не реализован (это делает другой разработчик),
поэтому здесь свой runner_factory: фейковый раннер по контракту
(docs/contracts.md, раздел 6) — план из 2 шагов, ответ "ok: <prompt>",
usage 10/20 — независимо от bots.provider, чтобы бейджи и аватары в PWA
оставались как в дизайне (claude/codex/gemini), а не все "fake".

Запуск: scripts/dev.sh (сам поднимает Postgres и выставляет env).
"""
import uvicorn

from bothub.main import create_app
from bothub.runner.base import RunnerEvent


class DevFakeRunner:
    provider = "fake"

    async def run(self, turn):
        yield RunnerEvent("plan", {"steps": [
            {"id": "1", "title": "Понять запрос", "status": "done"},
            {"id": "2", "title": "Ответить", "status": "done"},
        ]})
        yield RunnerEvent("assistant_msg", {"text": f"ok: {turn.prompt}", "final": True})
        yield RunnerEvent("usage", {"tokens_in": 10, "tokens_out": 20, "model": turn.bot.get("model") or "fake"})

    async def stop(self, turn_id):
        pass


app = create_app(lambda provider: DevFakeRunner())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
