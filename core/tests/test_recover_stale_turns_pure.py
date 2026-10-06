"""recover_stale_turns: три записи одного turn идут одной транзакцией, отмена посередине ничего не оставляет."""
import asyncio
import json
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


APPROVALS = [uuid.UUID(int=10), uuid.UUID(int=11)]


class ApprovalConnection(TxConnection):
    """TxConnection плюс pending-одобрения turn'а: expire-запрос отдаёт их и пишет «approval» в той же транзакции;
    события запоминаются вместе с видом, актором и payload."""

    def __init__(self, turns, approvals, hook=None):
        super().__init__(turns, hook)
        self.approvals = {turn: list(ids) for turn, ids in approvals.items()}
        self.events = []

    async def fetch(self, query, *args):
        if "update bothub.approvals set status='expired'" in query and "turn_id=$1 and status='pending'" in query:
            ids = self.approvals.pop(args[0], [])
            for approval in ids:
                await self.write("approval", approval)
            return [{"id": approval} for approval in ids]
        return await super().fetch(query, *args)

    async def fetchrow(self, query, *args):
        row = await super().fetchrow(query, *args)
        if "insert into bothub.events" in query:
            payload = json.loads(args[6])
            self.events.append((args[3], args[4], payload))
            return {**row, "kind": args[3], "payload": payload}
        return row


def make_approval_pool(turns, approvals, hook=None):
    pool = TxPool(turns, hook)
    pool.connection = ApprovalConnection(turns, approvals, hook)
    return pool


async def test_pending_approvals_of_a_stale_turn_expire_with_it_in_one_transaction(monkeypatch):
    pool = make_approval_pool(TURNS[:1], {TURNS[0]: APPROVALS})
    await make_app(monkeypatch, pool).state.recover_stale_turns()
    kinds = [item[0] for item in pool.connection.committed]
    # turn, bot, один UPDATE одобрений, по паре (seq, event) на каждое одобрение, затем status-событие
    assert kinds == ["turn", "bot", "approval", "approval", "seq", "event", "seq", "event", "seq", "event"]
    decisions = [(actor, payload) for kind, actor, payload in pool.connection.events if kind == "approval_dec"]
    assert [payload["approval_id"] for _, payload in decisions] == [str(item) for item in APPROVALS]
    for actor, payload in decisions:  # тот же вид, что у истечения по сроку (expire_approvals)
        assert actor == "system"
        assert payload == {"approval_id": payload["approval_id"], "decision": "expired", "remember": False, "client": "system"}
    assert pool.connection.events[-1][0] == "status" and pool.connection.events[-1][2]["status"] == "error"


async def test_turn_without_pending_approvals_closes_as_before(monkeypatch):
    pool = make_approval_pool(TURNS[:1], {})
    await make_app(monkeypatch, pool).state.recover_stale_turns()
    assert [item[0] for item in pool.connection.committed] == ["turn", "bot", "seq", "event"]
    assert [kind for kind, _, _ in pool.connection.events] == ["status"]


async def test_approvals_of_each_stale_turn_expire_with_that_turn_only(monkeypatch):
    pool = make_approval_pool(TURNS, {TURNS[0]: APPROVALS[:1], TURNS[1]: APPROVALS[1:]})
    await make_app(monkeypatch, pool).state.recover_stale_turns()
    decided = [payload["approval_id"] for kind, _, payload in pool.connection.events if kind == "approval_dec"]
    assert decided == [str(item) for item in APPROVALS]
    assert pool.connection.approvals == {}


async def test_cancel_after_the_approvals_expired_leaves_none_of_the_writes(monkeypatch):
    reached = asyncio.Event()
    seen = []

    async def hang_on_the_status_event(kind):
        if kind == "event":
            seen.append(kind)
            if len(seen) == 2:  # approval_dec записано, status-событие нет
                reached.set()
                await asyncio.sleep(3600)

    pool = make_approval_pool(TURNS[:1], {TURNS[0]: APPROVALS[:1]}, hang_on_the_status_event)
    task = asyncio.create_task(make_app(monkeypatch, pool).state.recover_stale_turns())
    await asyncio.wait_for(reached.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.connection.committed == [], "одобрение не закрыто без закрытия turn'а"
