"""Этап 1, финальная доводка, пункты 5-7 без БД: фейковый пул проигрывает SQL ядра.

5. turn, выходящий из waiting_approval/waiting_mac в running, проходит лимит параллельных turn;
6. outbox пишет попытку доставки (sending) до сети и не держит соединение пула во время отправки;
7. одобрение mac-вызова расходуется после использования (кроме «запомнить»).

Фейк проверяет логику ядра, а не SQL: те же сценарии на Postgres в test_stage1_final_db.py.
Запуск без Postgres: pytest --noconftest tests/test_stage1_final_flow.py
"""
import asyncio
import hashlib
import hmac
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from starlette.testclient import TestClient

from bothub.main import canonical, create_app
from bothub.risk import op_hash

BOT = "scout"
THREAD = uuid.uuid4()
OWNER_ID = uuid.UUID(int=1)


def bot_token(bot_id=BOT):
    digest = hmac.new(b"test-secret", bot_id.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer bot:{bot_id}:{digest}"}


OWNER = {"Authorization": "Bearer test-owner"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("OWNER_TOKEN", "test-owner")
    monkeypatch.setenv("MAC_AGENT_TOKEN", "test-mac")
    monkeypatch.delenv("MAC_WOL_CMD", raising=False)
    monkeypatch.delenv("BOTHUB_MAX_PARALLEL_TURNS", raising=False)


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Pool:
    """acquire() выдаёт соединение из одного общего состояния и считает занятые."""

    def __init__(self, make_con):
        self.make_con = make_con
        self.open = 0

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                pool.open += 1
                return pool.make_con()

            async def __aexit__(self, *exc):
                pool.open -= 1
                return False

        return Ctx()

    async def close(self):
        return None


def _app(pool):
    app = create_app(lambda provider: None)
    app.state.pool = pool
    return app


# --- 5. лимит при выходе из waiting -------------------------------------------------------------------

class Turns:
    """turns + журнал событий и статусов бота."""

    def __init__(self, **statuses):
        self.turns = {name: {"status": status, "thread_id": THREAD} for name, status in statuses.items()}
        self.events = []
        self.bot_status = []
        self.locks = 0

    def status(self, name):
        return self.turns[name]["status"]

    def running(self):
        return sum(1 for turn in self.turns.values() if turn["status"] == "running")


class TurnsCon:
    def __init__(self, db):
        self.db = db

    def transaction(self):
        return Tx()

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            self.db.locks += 1
        elif "update bothub.bots set status='running'" in sql:
            self.db.bot_status.append("running")
        else:
            raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "bothub.settings" in sql:
            return True  # legacy_auth_enabled: поддельная база ведёт себя как установка, обновлённая через OWNER_TOKEN
        if "select count(*) from bothub.turns where status='running'" in sql:
            return self.db.running()
        if "select 1 from bothub.turns where id=$1 and status=$2" in sql:
            turn = self.db.turns.get(str(args[0]))
            return 1 if turn and turn["status"] == args[1] else None
        if "update bothub.threads set last_seq" in sql:
            return len(self.db.events) + 1
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "select id,role,status from bothub.users" in sql or "from bothub.settings s join bothub.users u" in sql:
            return {"id": OWNER_ID, "role": "admin", "status": "active"}
        if "select b.owner_id from bothub.bots b" in sql:
            return {"owner_id": OWNER_ID} if args[0] == BOT else None
        if "update bothub.turns set status='running'" in sql:
            turn = self.db.turns.get(str(args[0]))
            if turn and turn["status"] == args[1]:
                turn["status"] = "running"
                return {"thread_id": turn["thread_id"]}
            return None
        if "insert into bothub.events" in sql:
            self.db.events.append(args[3:5] + (args[6],))
            return {"thread_id": args[0], "seq": args[1], "turn_id": args[2], "kind": args[3], "actor": args[4], "client": args[5], "payload": {}}
        raise AssertionError(sql)


def _resume_app(db):
    return _app(Pool(lambda: TurnsCon(db)))


@pytest.mark.parametrize("waiting", ["waiting_approval", "waiting_mac"])
async def test_turn_leaving_waiting_hits_limit_and_stays_in_queue(monkeypatch, waiting):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "2")
    db = Turns(a="running", b="running", w=waiting)
    app = _resume_app(db)
    async with app.state.pool.acquire() as con:
        assert await app.state.resume_turn(con, "w", waiting) is False
    assert db.status("w") == waiting          # слота нет: ждёт, раннер жив, второй execute_turn не запускаем
    assert db.events == [] and db.bot_status == []
    assert db.locks == 1                       # проверка под той же блокировкой, что и claim_turn


@pytest.mark.parametrize("waiting", ["waiting_approval", "waiting_mac"])
async def test_waiting_turn_resumes_once_slot_is_free(monkeypatch, waiting):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "2")
    db = Turns(a="running", b="running", w=waiting)
    app = _resume_app(db)
    async with app.state.pool.acquire() as con:
        assert await app.state.resume_turn(con, "w", waiting) is False
        db.turns["b"]["status"] = "done"
        assert await app.state.resume_turn(con, "w", waiting) is True
    assert db.status("w") == "running"
    assert db.bot_status == ["running"]
    assert db.events == [("status", "system", canonical({"turn_id": "w", "status": "running"}))]


@pytest.mark.parametrize("status", ["running", "error", "done", "stopped"])
async def test_resume_of_turn_that_is_not_waiting_does_not_hold_anything(monkeypatch, status):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "1")
    db = Turns(a="running", w=status)
    app = _resume_app(db)
    async with app.state.pool.acquire() as con:
        assert await app.state.resume_turn(con, "w", "waiting_approval") is True
    assert db.status("w") == status and db.events == []


async def test_resume_uses_same_default_limit_as_claim_turn():
    db = Turns(a="running", b="running", c="running", w="waiting_approval")
    app = _resume_app(db)
    async with app.state.pool.acquire() as con:
        assert await app.state.resume_turn(con, "w", "waiting_approval") is True   # по умолчанию 4: три running, слот есть
    db = Turns(a="running", b="running", c="running", d="running", w="waiting_approval")
    app = _resume_app(db)
    async with app.state.pool.acquire() as con:
        assert await app.state.resume_turn(con, "w", "waiting_approval") is False  # четыре running: лимит


class ApprovalsCon(TurnsCon):
    async def fetchrow(self, sql, *args):
        if "from bothub.approvals a" in sql or "select * from bothub.approvals where id=$1 for update" in sql or "select * from bothub.approvals where id=$1" in sql:
            return dict(self.db.approval)
        if "update bothub.approvals set status=$2" in sql:
            self.db.approval |= {"status": args[1], "remember": args[2]}
            return dict(self.db.approval)
        return await super().fetchrow(sql, *args)

    async def execute(self, sql, *args):
        if "update bothub.turns set runner_ping_at" in sql:
            return None
        return await super().execute(sql, *args)


def _approval(status="approved", turn_id="w"):
    return {"id": uuid.uuid4(), "thread_id": THREAD, "turn_id": turn_id, "bot_id": BOT, "status": status, "tool": "mcp__bothub__mac_type_text",
            "args": {"text": "hi"}, "remember": False, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}


async def test_wait_holds_decided_approval_until_slot_is_free(monkeypatch):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "1")
    db = Turns(a="running", w="waiting_approval")
    db.approval = _approval()
    app = _app(Pool(lambda: ApprovalsCon(db)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        held = await client.get(f"/api/approvals/{db.approval['id']}/wait", params={"timeout": 0}, headers=bot_token())
        assert held.status_code == 200
        # решение принято, но слота нет: раннеру отвечают «pending», его цикл опроса повторит /wait
        assert held.json()["status"] == "pending" and held.json()["queued"] is True
        assert db.status("w") == "waiting_approval"
        db.turns["a"]["status"] = "done"
        free = await client.get(f"/api/approvals/{db.approval['id']}/wait", params={"timeout": 0}, headers=bot_token())
        assert free.json()["status"] == "approved" and "queued" not in free.json()
        assert db.status("w") == "running"


async def test_wait_answers_at_once_when_turn_is_already_running():
    db = Turns(w="running")
    db.approval = _approval()
    app = _app(Pool(lambda: ApprovalsCon(db)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/approvals/{db.approval['id']}/wait", params={"timeout": 0}, headers=bot_token())
    assert response.json()["status"] == "approved"


async def test_decide_at_limit_leaves_turn_waiting_and_free_slot_resumes_it(monkeypatch):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "1")
    db = Turns(a="running", w="waiting_approval")
    db.approval = _approval(status="pending")
    app = _app(Pool(lambda: ApprovalsCon(db)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/approvals/{db.approval['id']}/decide", json={"decision": "approve"}, headers=OWNER)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "approved"      # решение записано
        assert db.status("w") == "waiting_approval"          # а turn ждёт слот
        db.turns["a"]["status"] = "done"
        db.approval["status"] = "pending"
        again = await client.post(f"/api/approvals/{db.approval['id']}/decide", json={"decision": "approve"}, headers=OWNER)
        assert again.status_code == 200, again.text
        assert db.status("w") == "running"


# --- 6. outbox: pre-write, без удержания соединения ---------------------------------------------------------

class Outbox:
    def __init__(self, endpoints, fail_record_for=()):
        self.rows = {1: {"id": 1, "payload": {"turn_id": "t"}, "attempts": 0, "sent_at": None, "failed_at": None, "due": 0.0}}
        self.subs = [{"endpoint": e, "keys": {}} for e in endpoints]
        self.deliveries = {}
        self.fail_record_for = list(fail_record_for)
        self.now = 1000.0

    def due(self, row):
        return row["sent_at"] is None and row["failed_at"] is None and row["due"] <= self.now


class OutboxCon:
    def __init__(self, db):
        self.db = db

    async def fetch(self, sql, *args):
        if "update bothub.approvals set status='expired'" in sql:
            return []
        if "select id from bothub.outbox where sent_at is null" in sql:
            return [{"id": r["id"]} for r in self.db.rows.values() if self.db.due(r)]
        if "select * from bothub.push_subscriptions" in sql:
            return list(self.db.subs)
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "update bothub.outbox set next_attempt_at=now()+$2::float8 * interval '1 second' where id=$1 and sent_at is null" in sql:
            row = self.db.rows[args[0]]
            if not self.db.due(row):
                return None
            snapshot = dict(row)
            row["due"] = self.db.now + args[1]
            return snapshot
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "bothub.settings" in sql:
            return True  # legacy_auth_enabled: поддельная база ведёт себя как установка, обновлённая через OWNER_TOKEN
        if "insert into bothub.outbox_deliveries" in sql:
            key = (args[0], args[1])
            current = self.db.deliveries.get(key)
            if current is None:
                self.db.deliveries[key] = {"status": "sending", "attempts": 0}
                return 1
            if current["status"] == "failed":
                current["status"] = "sending"
                return 1
            return None
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "update bothub.outbox_deliveries set status=$3" in sql:
            if self.db.fail_record_for and self.db.fail_record_for[0] == args[1]:
                self.db.fail_record_for.pop(0)
                raise ConnectionError("db went away after the send")
            self.db.deliveries[(args[0], args[1])] |= {"status": args[2], "attempts": self.db.deliveries[(args[0], args[1])]["attempts"] + args[3]}
        elif "delete from bothub.push_subscriptions" in sql:
            self.db.subs = [s for s in self.db.subs if s["endpoint"] != args[0]]
        elif "update bothub.outbox set sent_at=now()" in sql:
            self.db.rows[args[0]]["sent_at"] = self.db.now
        elif "update bothub.outbox set attempts=$2,failed_at=now()" in sql:
            self.db.rows[args[0]] |= {"attempts": args[1], "failed_at": self.db.now}
        elif "update bothub.outbox set attempts=$2,next_attempt_at" in sql:
            self.db.rows[args[0]] |= {"attempts": args[1], "due": self.db.now + args[2]}
        elif "update bothub.outbox set attempts=attempts+1,next_attempt_at" in sql:
            self.db.rows[args[0]]["attempts"] += 1
            self.db.rows[args[0]]["due"] = self.db.now + 60
        else:
            raise AssertionError(sql)


class HttpError(Exception):
    def __init__(self, code):
        super().__init__(f"push service {code}")
        self.response = type("R", (), {"status_code": code})()


@pytest.fixture
def vapid(monkeypatch):
    for key in ("VAPID_PUBLIC", "VAPID_PRIVATE", "VAPID_SUBJECT"):
        monkeypatch.setenv(key, "x")


def _outbox_app(db):
    app = _app(Pool(lambda: OutboxCon(db)))
    return app


def _states(db):
    return {endpoint: v["status"] for (_, endpoint), v in db.deliveries.items()}


async def test_delivery_record_exists_before_network_send_and_pool_is_free(vapid):
    db = Outbox(["https://p/a"])
    app = _outbox_app(db)
    seen = []

    async def send(sub, body):
        seen.append((dict(_states(db)), app.state.pool.open))

    app.state.send_push = send
    await app.state.deliver_outbox()
    assert seen == [({"https://p/a": "sending"}, 0)]   # запись уже есть, соединение из пула не удерживается
    assert _states(db) == {"https://p/a": "sent"} and db.rows[1]["sent_at"] is not None


async def test_network_failure_keeps_record_and_retry_goes_only_to_failed_endpoint(vapid):
    db = Outbox(["https://p/a", "https://p/b"])
    app = _outbox_app(db)
    calls, in_flight = [], []
    fail_b = [True]

    async def send(sub, body):
        calls.append(sub["endpoint"])
        in_flight.append(_states(db)[sub["endpoint"]])
        if sub["endpoint"] == "https://p/b" and fail_b[0]:
            raise HttpError(500)

    app.state.send_push = send
    await app.state.deliver_outbox()
    assert in_flight == ["sending", "sending"]                           # pre-write до сети у каждого
    assert _states(db) == {"https://p/a": "sent", "https://p/b": "failed"}
    assert db.rows[1]["sent_at"] is None and db.rows[1]["attempts"] == 1
    calls.clear()
    fail_b[0] = False
    db.now += 3600
    await app.state.deliver_outbox()
    assert calls == ["https://p/b"]                                      # a повторно не шлём
    assert _states(db) == {"https://p/a": "sent", "https://p/b": "sent"} and db.rows[1]["sent_at"] is not None


async def test_db_failure_after_send_does_not_cause_a_resend(vapid):
    db = Outbox(["https://p/a", "https://p/b"], fail_record_for=["https://p/a"])
    app = _outbox_app(db)
    calls = []

    async def send(sub, body):
        calls.append(sub["endpoint"])

    app.state.send_push = send
    await app.state.deliver_outbox()                       # сбой БД при записи итога по a; исключение не вылетает
    assert calls == ["https://p/a"]
    assert _states(db)["https://p/a"] == "sending"          # итог не записан, но попытка зафиксирована заранее
    assert db.rows[1]["sent_at"] is None
    db.now += 3600
    await app.state.deliver_outbox()
    assert calls == ["https://p/a", "https://p/b"]          # a не повторяется, b доставляется
    assert _states(db)["https://p/a"] == "sending"
    assert db.rows[1]["sent_at"] is not None                # запись закрыта, зацикливания нет


async def test_410_removes_subscription_and_is_not_retried(vapid):
    db = Outbox(["https://p/gone", "https://p/ok"])
    app = _outbox_app(db)
    calls = []

    async def send(sub, body):
        calls.append(sub["endpoint"])
        if sub["endpoint"] == "https://p/gone":
            raise HttpError(410)

    app.state.send_push = send
    await app.state.deliver_outbox()
    assert [s["endpoint"] for s in db.subs] == ["https://p/ok"]
    assert _states(db) == {"https://p/gone": "gone", "https://p/ok": "sent"} and db.rows[1]["sent_at"] is not None


async def test_outbox_gives_up_after_max_attempts(vapid, monkeypatch):
    monkeypatch.setenv("BOTHUB_OUTBOX_MAX_ATTEMPTS", "2")
    db = Outbox(["https://p/a"])
    app = _outbox_app(db)
    calls = []

    async def send(sub, body):
        calls.append(1)
        raise HttpError(500)

    app.state.send_push = send
    await app.state.deliver_outbox()
    db.now += 3600
    await app.state.deliver_outbox()
    assert db.rows[1]["failed_at"] is not None and db.rows[1]["attempts"] == 2
    db.now += 3600
    calls.clear()
    await app.state.deliver_outbox()
    assert calls == []


async def test_concurrent_pass_does_not_send_the_same_row_twice(vapid):
    db = Outbox(["https://p/a"])
    app = _outbox_app(db)
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def send(sub, body):
        calls.append(sub["endpoint"])
        entered.set()
        await release.wait()

    app.state.send_push = send
    first = asyncio.create_task(app.state.deliver_outbox())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(app.state.deliver_outbox(), 2)   # второй проход видит аренду и пропускает запись
        release.set()
        await asyncio.wait_for(first, 2)
    finally:
        release.set()
    assert calls == ["https://p/a"] and db.rows[1]["sent_at"] is not None


async def test_without_vapid_row_is_closed_without_deliveries(monkeypatch):
    for key in ("VAPID_PUBLIC", "VAPID_PRIVATE", "VAPID_SUBJECT"):
        monkeypatch.delenv(key, raising=False)
    db = Outbox(["https://p/a"])
    app = _outbox_app(db)
    await app.state.deliver_outbox()
    assert db.rows[1]["sent_at"] is not None and db.deliveries == {}


# --- 7. одобрение расходуется ---------------------------------------------------------------------------------------

class MacDB(Turns):
    def __init__(self):
        super().__init__()
        self.approvals = []
        self.mac_state = "online"
        self.turn_status = "running"
        self.claims = 0

    def approve(self, tool, args, remember=False):
        self.approvals.append({"id": uuid.uuid4(), "turn_id": None, "op_hash": op_hash(tool, args), "used_at": None, "remember": remember,
                               "created": len(self.approvals)})


class MacCon(TurnsCon):
    async def fetchrow(self, sql, *args):
        if "select b.owner_id from bothub.bots b" in sql:
            return await super().fetchrow(sql, *args)
        if "from bothub.turns" in sql and "thread_id=$2" in sql:
            turn = self.db.turns.get(str(args[0]))
            return {"status": turn["status"] if turn else self.db.turn_status}
        if "bothub.threads" in sql and "select" in sql:
            return {"bot_id": BOT}
        if "from bothub.bots" in sql:
            return {"executor": "mac", "mac_full_control": False, "auto_allow": []}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if "bothub.settings" in sql:
            return True  # legacy_auth_enabled: поддельная база ведёт себя как установка, обновлённая через OWNER_TOKEN
        if "select 1 from bothub.mac_status" in sql:
            return 1
        if "update bothub.approvals set used_at=case when remember" in sql:
            rows = sorted((a for a in self.db.approvals if a["op_hash"] == args[1] and a["used_at"] is None), key=lambda a: (a["remember"], a["created"]))
            if not rows:
                return None
            if not rows[0]["remember"]:
                rows[0]["used_at"] = time.time()
            self.db.claims += 1
            return rows[0]["id"]
        if "select state from bothub.mac_status" in sql:
            return self.db.mac_state
        return await super().fetchval(sql, *args)

    async def execute(self, sql, *args):
        if "update bothub.approvals set used_at=null" in sql:
            for approval in self.db.approvals:
                if approval["id"] == args[0]:
                    approval["used_at"] = None
            return None
        if "runner_ping_at" in sql or "insert into bothub.mac_status" in sql or "update bothub.mac_status" in sql:
            return None
        return await super().execute(sql, *args)


@asynccontextmanager
async def _no_lifespan(app):
    yield


@pytest.fixture
def mac_env():
    db = MacDB()
    app = _app(Pool(lambda: MacCon(db)))
    app.router.lifespan_context = _no_lifespan
    return db, app


def _call_body(args, tool="type_text"):
    return {"thread_id": str(THREAD), "turn_id": str(uuid.uuid4()), "tool": tool, "args": args, "timeout": 3}


def _mac_call(client, body):
    return client.post("/api/mac/call", json=body, headers=bot_token())


def _serve_one(client, ws, body):
    """Один вызов: HTTP /api/mac/call в потоке, Mac-агент отвечает по websocket."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        incoming = pool.submit(ws.receive_json)
        future = pool.submit(_mac_call, client, body)
        call = incoming.result(timeout=3)
        ws.send_json({"type": "result", "id": call["id"], "ok": True, "data": {}})
        return future.result(timeout=3)


def _hello(ws):
    ws.send_json({"type": "hello", "host": "t", "os": "macOS", "agent_version": "1", "permissions": {}})
    time.sleep(0.1)


def test_mac_approval_works_once_and_repeat_is_blocked(mac_env):
    db, app = mac_env
    args = {"text": "hello"}
    db.approve("mcp__bothub__mac_type_text", args)
    body = _call_body(args)
    with TestClient(app) as client, client.websocket_connect("/agent/mac?token=test-mac") as ws:
        _hello(ws)
        first = _serve_one(client, ws, body)
        assert first.status_code == 200, first.text
        repeat = _mac_call(client, body)
        assert repeat.status_code == 403 and repeat.json()["detail"] == "approval required"
        db.approve("mcp__bothub__mac_type_text", args)       # новое одобрение даёт ещё один вызов
        assert _serve_one(client, ws, body).status_code == 200
        assert _mac_call(client, body).status_code == 403


def test_mac_approval_with_remember_is_not_spent(mac_env):
    db, app = mac_env
    args = {"text": "hello"}
    db.approve("mcp__bothub__mac_type_text", args, remember=True)
    body = _call_body(args)
    with TestClient(app) as client, client.websocket_connect("/agent/mac?token=test-mac") as ws:
        _hello(ws)
        for _ in range(3):
            assert _serve_one(client, ws, body).status_code == 200
    assert db.approvals[0]["used_at"] is None


def test_one_shot_approval_is_spent_before_remembered_one(mac_env):
    db, app = mac_env
    args = {"text": "hello"}
    db.approve("mcp__bothub__mac_type_text", args, remember=True)
    db.approve("mcp__bothub__mac_type_text", args)
    with TestClient(app) as client, client.websocket_connect("/agent/mac?token=test-mac") as ws:
        _hello(ws)
        assert _serve_one(client, ws, _call_body(args)).status_code == 200
    assert db.approvals[1]["used_at"] is not None and db.approvals[0]["used_at"] is None


def test_mac_approval_survives_an_offline_mac_and_is_spent_on_the_retry(mac_env):
    db, app = mac_env
    args = {"text": "hello"}
    db.approve("mcp__bothub__mac_type_text", args)
    body = _call_body(args)
    db.mac_state = "offline"
    with TestClient(app) as client:
        offline = _mac_call(client, body)
        assert offline.status_code == 409 and offline.json()["error"] == "mac_unavailable"
        assert db.approvals[0]["used_at"] is None             # до Mac не дошло: одобрение цело
        db.mac_state = "online"
        with client.websocket_connect("/agent/mac?token=test-mac") as ws:
            _hello(ws)
            assert _serve_one(client, ws, body).status_code == 200
        assert db.approvals[0]["used_at"] is not None


def test_mac_approval_for_other_args_is_not_used(mac_env):
    db, app = mac_env
    db.approve("mcp__bothub__mac_type_text", {"text": "hello"})
    with TestClient(app) as client:
        assert _mac_call(client, _call_body({"text": "other"})).status_code == 403
        assert _mac_call(client, _call_body({"text": "hello", "x": 1})).status_code == 403
    assert db.approvals[0]["used_at"] is None


def test_mac_call_at_turn_limit_waits_in_waiting_mac_and_keeps_approval(monkeypatch, mac_env):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "1")
    db, app = mac_env
    args = {"text": "hello"}
    db.approve("mcp__bothub__mac_type_text", args)
    body = _call_body(args)
    # другой turn уже занимает единственный слот; турн запроса ждёт Mac
    turn_id = body["turn_id"]
    db.turns[turn_id] = {"status": "waiting_mac", "thread_id": THREAD}
    db.turns["other"] = {"status": "running", "thread_id": THREAD}
    with TestClient(app) as client, client.websocket_connect("/agent/mac?token=test-mac") as ws:
        _hello(ws)
        queued = _mac_call(client, body)
        assert queued.status_code == 409 and queued.json()["error"] == "queued"
        assert db.approvals[0]["used_at"] is None and db.turns[turn_id]["status"] == "waiting_mac"
        db.turns["other"]["status"] = "done"
        assert _serve_one(client, ws, body).status_code == 200
        assert db.turns[turn_id]["status"] == "running"
