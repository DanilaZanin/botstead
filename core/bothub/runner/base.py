"""Интерфейс раннера. Контракт: docs/contracts.md, раздел 6."""
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass
class TurnContext:
    turn_id: str
    thread_id: str
    bot: dict
    prompt: str
    cli_session_id: str | None
    dry_run: bool
    memory_md: str
    exec_prefix: list[str] = field(default_factory=list)
    launcher: object | None = None
    bot_container_id: str | None = None
    exec_env: dict[str, str] = field(default_factory=dict)
    turn_type: str = 'normal'  # compact/proactive запускаются с ограниченными инструментами CLI

    @property
    def compact(self) -> bool:
        """Раннер применяет ограничения CLI к обоим служебным ходам."""
        return self.turn_type in ('compact', 'proactive')


@dataclass
class RunnerEvent:
    kind: str
    payload: dict
    cli_session_id: str | None = None


class Runner(Protocol):
    provider: str

    def run(self, turn: TurnContext) -> AsyncIterator[RunnerEvent]: ...

    async def stop(self, turn_id: str) -> None: ...
