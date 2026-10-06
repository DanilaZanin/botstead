"""recover_stale_turns: три записи одного turn идут одной транзакцией, отмена посередине ничего не оставляет."""
import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest

import bothub.main as main
from bothub.main import create_app

pytestmark = pytest.mark.pure

THREAD = uuid.UUID(int=3)
TURNS = [uuid.UUID(int=4), uuid.UUID(int=5)]


class TxConnection:
    """Записи внутри transaction() попадают в committed только при выходе без исключения (в том числе CancelledError);
    вне транзакции каждая запись фиксируется сразу. hook(kind) вызывается перед записью: на нём тест подвешивает проход."""

    def __init__(self, turns, hook=None):
        self.turns = turns
        self.hook = hook
        self.committed = []
        self.pending = None
        self.depth = 0

    @asynccontextmanager
    async def transaction(self):
        if self.depth == 0:
            self.pending = []
        self.depth += 1
        try:
            yield self
        except BaseException:
            self.depth -= 1
            if self.depth == 0:
                self.pending = None
            raise
        self.depth -= 1
        if self.depth == 0:
            self.committed.extend(self.pending)
            self.pending = None

    async def write(self, kind, *args):
        if self.hook:
            await self.hook(kind)
        (self.pending if self.pending is not None else self.committed).append((kind, *args))

    async def fetch(self, query, *args):
        if "select t.id,t.thread_id,th.bot_id from bothub.turns t" in query:
            return [{"id": turn, "thread_id": THREAD, "bot_id": "alpha"} for turn in self.turns]
        return []

    async def fetchval(self, query, *args):
        if "update bothub.threads set last_seq" in query:
            await self.write("seq", args[0])
            return 7
        return None

    async def fetchrow(self, query, *args):
        if "insert into bothub.events" in query:
            await self.write("event", args[2])
            return {"id": uuid.uuid4(), "thread_id": args[0], "seq": args[1], "turn_id": args[2], "kind": args[3],
                    "actor": args[4], "client": None, "payload": {}}
        return None

    async def execute(self, query, *args):
        if "update bothub.turns set status='error'" in query:
            await self.write("turn", args[0])
        elif "update bothub.bots set status='error'" in query:
            await self.write("bot", args[0])
        return "OK"


class TxPool:
    def __init__(self, turns, hook=None):
        self.connection = TxConnection(turns, hook)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def make_app(monkeypatch, pool):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "local")
    monkeypatch.delenv("LAUNCHER_SOCKET", raising=False)
    monkeypatch.delenv("LAUNCHER_URL", raising=False)
    app = create_app()
    app.state.pool = pool
    return app


async def test_the_three_writes_of_one_turn_are_committed_together(monkeypatch):
    pool = TxPool(TURNS[:1])
    await make_app(monkeypatch, pool).state.recover_stale_turns()
    assert [item[0] for item in pool.connection.committed] == ["turn", "bot", "seq", "event"]


async def test_cancel_in_the_middle_of_a_turn_leaves_none_of_its_writes(monkeypatch):
    reached = asyncio.Event()

    async def hang_on_the_bot_update(kind):
        if kind == "bot":
            reached.set()
            await asyncio.sleep(3600)

    pool = TxPool(TURNS, hang_on_the_bot_update)
    task = asyncio.create_task(make_app(monkeypatch, pool).state.recover_stale_turns())
    await asyncio.wait_for(reached.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.connection.committed == [], "the turn is still stale for the next pass: no half-closed turn"
    assert pool.connection.pending is None


async def test_cancel_after_the_first_turn_keeps_that_turn_whole(monkeypatch):
    seen = []
    reached = asyncio.Event()

    async def hang_on_the_second_turn(kind):
        if kind == "turn":
            seen.append(kind)
            if len(seen) == 2:
                reached.set()
                await asyncio.sleep(3600)

    pool = TxPool(TURNS, hang_on_the_second_turn)
    task = asyncio.create_task(make_app(monkeypatch, pool).state.recover_stale_turns())
    await asyncio.wait_for(reached.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [item[0] for item in pool.connection.committed] == ["turn", "bot", "seq", "event"]
    assert pool.connection.committed[0][1] == TURNS[0]
