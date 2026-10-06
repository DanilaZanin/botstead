"""CLI раннеры Bot Hub."""

from .base import Runner, RunnerEvent, TurnContext
from .claude import ClaudeRunner
from .codex import CodexRunner
from .fake import FakeRunner
from .gemini import GeminiRunner


def get_runner(provider: str) -> Runner:
    runners = {
        "claude": ClaudeRunner,
        "codex": CodexRunner,
        "gemini": GeminiRunner,
        "fake": FakeRunner,
    }
    try:
        return runners[provider]()
    except KeyError as exc:
        raise ValueError(f"unknown runner provider: {provider}") from exc


create_runner = get_runner  # alias, черновой контракт до правки

__all__ = ["Runner", "RunnerEvent", "TurnContext", "get_runner", "create_runner",
           "ClaudeRunner", "CodexRunner", "GeminiRunner", "FakeRunner"]
