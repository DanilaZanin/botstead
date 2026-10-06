"""Этап 1Б: надёжность на Postgres (пункты 6-13 части Б). Нужен Postgres из conftest.py.

Чистая логика (logging, supervise, health с подменённым пулом, op_hash) проверяется без БД в
test_reliability_logic.py. Здесь: SQL-переходы статусов, outbox по подписчикам, лимиты, бюджет.
"""
import asyncio
import hashlib
import hmac
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub.main import create_app
from bothub.risk import op_hash
from bothub.runner.base import RunnerEvent

OWNER = {"Authorization": "Bearer test-owner"}
GATE = "GATE"  # в списке событий FakeRunner: ждать gate.set()


def bot_token(bot_id):
    digest = hmac.new(b"test-secret", bot_id.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer bot:{bot_id}:{digest}"}


class Runner:
    provider = "fake"

    def __init__(self, events=None, gate=None, before=None):
        self.events = events if events is not None else [
            RunnerEvent("assistant_msg", {"text": "ok", "final": True}),
            RunnerEvent("usage", {"tokens_in": 10, "tokens_out": 20, "model": "fake", "seconds": 0.1}),
        ]
        self.gate = gate
        self.before = before
        self.runs = 0
        self.stopped = []

    async def run(self, turn):
        self.runs += 1
        if self.before:
            await self.before(turn)
        for event in self.events:
            if event == GATE:
                await self.gate.wait()
            else:
                yield event

    async def stop(self, turn_id):
        self.stopped.append(turn_id)


@asynccontextmanager
async def api(runner=None, manual=False):
    runner = runner or Runner()
    app = create_app(lambda provider: runner)
    if manual:
        async def idle():
            await asyncio.Event().wait()

        app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, app, runner


@pytest.fixture(autouse=True)
def quiet_background_loops(monkeypatch):
    # outbox и расписания гоняем вручную через app.state, фоновый проход не мешает
    monkeypatch.setenv("BOTHUB_OUTBOX_INTERVAL", "3600")
    monkeypatch.setenv("BOTHUB_SCHEDULER_INTERVAL", "3600")


async def make_bot(client, bot_id="scout", **changes):
    body = {"id": bot_id, "name": bot_id, "provider": "fake", "model": "fake"} | changes
    response = await client.post("/api/bots", json=body, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def make_thread(client, bot_id="scout"):
    response = await client.post("/api/threads", json={"bot_id": bot_id, "title": "T"}, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def post_turn(client, thread_id, prompt="hi"):
    response = await client.post(f"/api/threads/{thread_id}/turns", json={"prompt": prompt, "client": "api"}, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def events(client, thread_id):
    return (await client.get(f"/api/threads/{thread_id}/events", headers=OWNER)).json()


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(0.05)
    pytest.fail("условие не выполнилось за отведённое время")


async def turn_status(app, turn_id):
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select status from bothub.turns where id=$1", uuid.UUID(str(turn_id)))


async def insert_turn(app, thread_id, status, ping="now()", lease="now()+interval '1 hour'"):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow(
            "insert into bothub.turns(thread_id,prompt,client,status,started_at,lease_until,runner_ping_at) "
            f"values($1,'p','api',$2,now(),{lease},{ping}) returning *", uuid.UUID(str(thread_id)), status)


async def insert_approval(app, thread_id, turn_id, status="pending", expires="now()+interval '1 hour'", created="now()", tool="move_to_trash"):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow(
            "insert into bothub.approvals(thread_id,turn_id,bot_id,risk,title,tool,args,args_hash,status,expires_at,created_at) "
            f"values($1,$2,'scout','delete','x',$3,'{{}}'::jsonb,'h',$4,{expires},{created}) returning *",
            uuid.UUID(str(thread_id)), turn_id, tool, status)


# ---- п.7: расписание не блокируется одним битым -------------------------------

async def test_bad_schedule_does_not_block_other_due_schedules():
    async with api() as (client, app, _):
        await make_bot(client)
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.schedules(bot_id,name,kind,cron,prompt,enabled,next_run_at) values('scout','bad','cron','not a cron','p',true,now()-interval '1 minute')")
            await con.execute("insert into bothub.schedules(bot_id,name,kind,cron,prompt,enabled,next_run_at) values('scout','good','cron','0 * * * *','p',true,now()-interval '1 minute')")
        await app.state.run_due_schedules()  # не бросает
        async with app.state.pool.acquire() as con:
            good_turns = await con.fetchval("select count(*) from bothub.turns t join bothub.threads th on th.id=t.thread_id where th.title='good'")
            good_next = await con.fetchval("select next_run_at from bothub.schedules where name='good'")
        assert good_turns == 1
        assert good_next is not None and good_next.timestamp() > time.time()


# ---- п.8: outbox по каждому получателю ------------------------------------------

class PushHttpError(Exception):
    def __init__(self, code):
        super().__init__(f"push service {code}")
        self.response = type("R", (), {"status_code": code})()


async def outbox_setup(app, monkeypatch, endpoints):
    for key in ("VAPID_PUBLIC", "VAPID_PRIVATE", "VAPID_SUBJECT"):
        monkeypatch.setenv(key, "x")
    async with app.state.pool.acquire() as con:
        for index, endpoint in enumerate(endpoints):
            await con.execute("insert into bothub.push_subscriptions(endpoint,keys,created_at) values($1,'{}'::jsonb,now()+$2*interval '1 second')", endpoint, float(index))
        return await con.fetchval("insert into bothub.outbox(kind,dedup_key,payload) values('push','k1','{\"turn_id\":\"t\"}'::jsonb) returning id")


async def outbox_row(app, outbox_id):
    async with app.state.pool.acquire() as con:
        row = await con.fetchrow("select * from bothub.outbox where id=$1", outbox_id)
        deliveries = {r["endpoint"]: r["status"] for r in await con.fetch("select * from bothub.outbox_deliveries where outbox_id=$1", outbox_id)}
    return row, deliveries


async def make_due(app):
    async with app.state.pool.acquire() as con:
        await con.execute("update bothub.outbox set next_attempt_at=now() where sent_at is null")


async def test_failed_subscription_does_not_resend_to_others(monkeypatch):
    async with api(manual=True) as (_, app, _):
        outbox_id = await outbox_setup(app, monkeypatch, ["https://p/a", "https://p/b", "https://p/c"])
        calls, fail_b = [], [True]

        async def send(sub, body):
            calls.append(sub["endpoint"])
            if sub["endpoint"] == "https://p/b" and fail_b[0]:
                raise PushHttpError(500)

        app.state.send_push = send
        await app.state.deliver_outbox()
        assert calls == ["https://p/a", "https://p/b", "https://p/c"]  # сбой b не прервал c
        row, deliveries = await outbox_row(app, outbox_id)
        assert row["sent_at"] is None and row["attempts"] == 1
        assert deliveries == {"https://p/a": "sent", "https://p/b": "failed", "https://p/c": "sent"}

        calls.clear()
        await app.state.deliver_outbox()  # ещё не пора: backoff
        assert calls == []

        fail_b[0] = False
        await make_due(app)
        await app.state.deliver_outbox()
        assert calls == ["https://p/b"]  # повтор только тому, кому не доставлено
        row, deliveries = await outbox_row(app, outbox_id)
        assert row["sent_at"] is not None
        assert deliveries["https://p/b"] == "sent"


async def test_push_410_deletes_subscription_and_is_not_retried(monkeypatch):
    async with api(manual=True) as (_, app, _):
        outbox_id = await outbox_setup(app, monkeypatch, ["https://p/gone", "https://p/ok"])
        calls = []

        async def send(sub, body):
            calls.append(sub["endpoint"])
            if sub["endpoint"] == "https://p/gone":
                raise PushHttpError(410)

        app.state.send_push = send
        await app.state.deliver_outbox()
        async with app.state.pool.acquire() as con:
            left = [r["endpoint"] for r in await con.fetch("select endpoint from bothub.push_subscriptions")]
        assert left == ["https://p/ok"]
        row, deliveries = await outbox_row(app, outbox_id)
        assert row["sent_at"] is not None  # 410 не ошибка доставки: подписки больше нет
        assert deliveries == {"https://p/gone": "gone", "https://p/ok": "sent"}
        await app.state.deliver_outbox()
        assert calls.count("https://p/gone") == 1


async def test_push_404_also_deletes_subscription(monkeypatch):
    async with api(manual=True) as (_, app, _):
        await outbox_setup(app, monkeypatch, ["https://p/x"])

        async def send(sub, body):
            raise PushHttpError(404)

        app.state.send_push = send
        await app.state.deliver_outbox()
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.push_subscriptions") == 0


async def test_outbox_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setenv("BOTHUB_OUTBOX_MAX_ATTEMPTS", "2")
    async with api(manual=True) as (_, app, _):
        outbox_id = await outbox_setup(app, monkeypatch, ["https://p/a"])
        calls = []

        async def send(sub, body):
            calls.append(1)
            raise PushHttpError(500)

        app.state.send_push = send
        await app.state.deliver_outbox()
        await make_due(app)
        await app.state.deliver_outbox()
        row, _ = await outbox_row(app, outbox_id)
        assert row["failed_at"] is not None and row["sent_at"] is None and row["attempts"] == 2
        calls.clear()
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.outbox set next_attempt_at=now()")
        await app.state.deliver_outbox()
        assert calls == []  # после отказа запись больше не берётся


async def test_failing_outbox_row_does_not_block_next_rows(monkeypatch):
    async with api(manual=True) as (_, app, _):
        await outbox_setup(app, monkeypatch, ["https://p/a"])
        async with app.state.pool.acquire() as con:
            second = await con.fetchval("insert into bothub.outbox(kind,dedup_key,payload) values('push','k2','{\"n\":2}'::jsonb) returning id")
        seen = []

        async def send(sub, body):
            seen.append(body)
            if '"turn_id"' in body:
                raise ValueError("не http-ошибка")

        app.state.send_push = send
        await app.state.deliver_outbox()
        row, deliveries = await outbox_row(app, second)
        assert row["sent_at"] is not None and deliveries == {"https://p/a": "sent"}


async def test_concurrent_outbox_passes_send_each_subscription_once(monkeypatch):
    async with api(manual=True) as (_, app, _):
        outbox_id = await outbox_setup(app, monkeypatch, ["https://p/a"])
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def send(sub, body):
            calls.append(sub["endpoint"])
            entered.set()
            await release.wait()

        app.state.send_push = send
        first = asyncio.create_task(app.state.deliver_outbox())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(app.state.deliver_outbox())
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        finally:
            release.set()
        assert calls == ["https://p/a"]
        row, deliveries = await outbox_row(app, outbox_id)
        assert row["sent_at"] is not None and deliveries == {"https://p/a": "sent"}


# ---- п.9: упавший раннер не оставляет turn в waiting_approval ------------------

async def test_stale_runner_ping_fails_waiting_turn_and_unblocks_thread():
    async with api() as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval", ping="now()-interval '10 minutes'")
        approval = await insert_approval(app, thread["id"], turn["id"])
        await app.state.claim_turn()
        assert await turn_status(app, turn["id"]) == "error"
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select status from bothub.approvals where id=$1", approval["id"]) == "expired"
            assert await con.fetchval("select status from bothub.bots where id='scout'") == "error"
        assert any(e["kind"] == "guard" and e["payload"]["reason"] == "runner_lost" for e in await events(client, thread["id"]))
        # тред разблокирован: новый turn в том же треде выполняется
        fresh = await post_turn(client, thread["id"])
        await until(lambda: _is(app, fresh["id"], "done"))


async def _is(app, turn_id, status):
    return await turn_status(app, turn_id) == status


async def test_recent_ping_keeps_waiting_turn(monkeypatch):
    async with api() as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval", ping="now()-interval '1 minute'")
        await insert_approval(app, thread["id"], turn["id"])
        await app.state.claim_turn()
        assert await turn_status(app, turn["id"]) == "waiting_approval"


async def test_runner_timeout_comes_from_env(monkeypatch):
    monkeypatch.setenv("BOTHUB_RUNNER_TIMEOUT", "60")
    async with api() as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval", ping="now()-interval '10 minutes'")
        await insert_approval(app, thread["id"], turn["id"])
        await app.state.claim_turn()
        assert await turn_status(app, turn["id"]) == "waiting_approval"


async def test_bot_polling_approval_counts_as_runner_ping():
    async with api() as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval", ping="now()-interval '4 minutes'")
        approval = await insert_approval(app, thread["id"], turn["id"])
        response = await client.get(f"/api/approvals/{approval['id']}/wait", params={"timeout": 0}, headers=bot_token("scout"))
        assert response.status_code == 200, response.text
        async with app.state.pool.acquire() as con:
            age = await con.fetchval("select extract(epoch from now()-runner_ping_at) from bothub.turns where id=$1", turn["id"])
        assert age < 30


async def test_runner_crash_while_waiting_approval_closes_turn():
    box = {}

    async def crash_waiting(turn):
        async with box["app"].state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='waiting_approval' where id=$1", uuid.UUID(turn.turn_id))
        raise RuntimeError("runner crashed")

    runner = Runner(before=crash_waiting)
    async with api(runner) as (client, app, _):
        box["app"] = app
        await make_bot(client)
        thread = await make_thread(client)
        turn = await post_turn(client, thread["id"])
        await until(lambda: _is(app, turn["id"], "error"))
        async with app.state.pool.acquire() as con:
            assert "runner crashed" in await con.fetchval("select error from bothub.turns where id=$1", uuid.UUID(turn["id"]))
        second = await post_turn(client, thread["id"])
        await until(lambda: _is(app, second["id"], "error"))  # тред не заблокирован: второй turn тоже дошёл до конца


async def test_runner_exit_while_waiting_approval_closes_turn():
    box = {}

    async def go_waiting(turn):
        async with box["app"].state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='waiting_approval' where id=$1", uuid.UUID(turn.turn_id))

    runner = Runner(events=[], before=go_waiting)
    async with api(runner) as (client, app, _):
        box["app"] = app
        await make_bot(client)
        thread = await make_thread(client)
        turn = await post_turn(client, thread["id"])
        await until(lambda: _is(app, turn["id"], "error"))
        assert any(e["kind"] == "guard" and e["payload"]["reason"] == "runner_exited" for e in await events(client, thread["id"]))


# ---- п.10: истёкший approval не перезапускает turn ---------------------------------

def _expired_event(evs):
    return [e for e in evs if e["kind"] == "approval_expired"]


async def test_expiry_sweep_fails_turn_with_event_and_never_reruns_it():
    async with api() as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval")
        approval = await insert_approval(app, thread["id"], turn["id"], expires="now()-interval '1 minute'")
        await app.state.deliver_outbox()  # expire_approvals
        assert await turn_status(app, turn["id"]) == "error"
        found = _expired_event(await events(client, thread["id"]))
        assert len(found) == 1 and found[0]["payload"]["approval_id"] == str(approval["id"])
        await asyncio.sleep(0.5)  # воркер крутится: turn не должен вернуться в running
        assert await turn_status(app, turn["id"]) == "error" and runner.runs == 0


async def test_decide_on_expired_approval_fails_turn():
    async with api() as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval")
        approval = await insert_approval(app, thread["id"], turn["id"], expires="now()-interval '1 minute'")
        decided = await client.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=OWNER)
        assert decided.status_code == 200 and decided.json()["status"] == "expired"
        assert await turn_status(app, turn["id"]) == "error"
        assert len(_expired_event(await events(client, thread["id"]))) == 1
        await asyncio.sleep(0.5)
        assert runner.runs == 0


async def test_wait_on_expired_approval_fails_turn():
    async with api() as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval")
        approval = await insert_approval(app, thread["id"], turn["id"], expires="now()-interval '1 minute'")
        response = await client.get(f"/api/approvals/{approval['id']}/wait", params={"timeout": 0}, headers=bot_token("scout"))
        assert response.json()["status"] == "expired"
        assert await turn_status(app, turn["id"]) == "error"
        assert len(_expired_event(await events(client, thread["id"]))) == 1


async def test_claim_turn_fails_turn_when_approval_older_than_timeout(monkeypatch):
    monkeypatch.setenv("APPROVAL_TIMEOUT", "1")
    async with api(manual=True) as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval")
        # expires_at ещё в будущем, но approval старше APPROVAL_TIMEOUT (1 мин)
        await insert_approval(app, thread["id"], turn["id"], created="now()-interval '5 minutes'")
        await app.state.claim_turn()
        assert await turn_status(app, turn["id"]) == "error"
        assert len(_expired_event(await events(client, thread["id"]))) == 1
        assert runner.runs == 0


async def test_young_approval_within_timeout_keeps_turn(monkeypatch):
    monkeypatch.setenv("APPROVAL_TIMEOUT", "60")
    async with api() as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval")
        await insert_approval(app, thread["id"], turn["id"], created="now()-interval '5 minutes'")
        await app.state.claim_turn()
        assert await turn_status(app, turn["id"]) == "waiting_approval"


async def test_running_turn_with_expired_approval_is_not_reclaimed():
    # старый баг: истёкший approval возвращал turn в running, после lease claim_turn брал его заново
    async with api(manual=True) as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "running", lease="now()-interval '5 minutes'")
        await insert_approval(app, thread["id"], turn["id"], status="expired")
        assert await app.state.claim_turn() is None
        assert await turn_status(app, turn["id"]) == "error"
        assert runner.runs == 0


async def test_running_turn_with_pending_expired_approval_is_not_reclaimed():
    async with api(manual=True) as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "running", lease="now()-interval '5 minutes'")
        await insert_approval(app, thread["id"], turn["id"], expires="now()-interval '1 minute'")
        assert await app.state.claim_turn() is None
        assert await turn_status(app, turn["id"]) == "error"
        assert len(_expired_event(await events(client, thread["id"]))) == 1
        assert runner.runs == 0


# ---- п.11: лимиты параллельных turn ------------------------------------------------

async def test_global_parallel_turn_limit(monkeypatch):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "2")
    gate = asyncio.Event()
    runner = Runner(events=[RunnerEvent("assistant_msg", {"text": "w", "final": False}), GATE, RunnerEvent("assistant_msg", {"text": "ok", "final": True})], gate=gate)
    async with api(runner) as (client, app, _):
        turns = []
        for name in ("a", "b", "c"):
            await make_bot(client, name)
            thread = await make_thread(client, name)
            turns.append(await post_turn(client, thread["id"]))

        async def two_running():
            async with app.state.pool.acquire() as con:
                return await con.fetchval("select count(*) from bothub.turns where status='running'") == 2

        await until(two_running)
        await asyncio.sleep(0.6)  # воркер опрашивает каждые 0.2 с: третий не должен стартовать
        async with app.state.pool.acquire() as con:
            statuses = [r["status"] for r in await con.fetch("select status from bothub.turns order by created_at")]
        assert sorted(statuses) == ["queued", "running", "running"]
        gate.set()
        for turn in turns:
            await until(lambda t=turn: _is(app, t["id"], "done"))


async def test_one_active_turn_per_bot_without_blocking_other_bots():
    gate = asyncio.Event()
    runner = Runner(events=[RunnerEvent("assistant_msg", {"text": "w", "final": False}), GATE, RunnerEvent("assistant_msg", {"text": "ok", "final": True})], gate=gate)
    async with api(runner) as (client, app, _):
        await make_bot(client, "a")
        await make_bot(client, "b")
        first = await post_turn(client, (await make_thread(client, "a"))["id"])
        await until(lambda: _is(app, first["id"], "running"))
        second = await post_turn(client, (await make_thread(client, "a"))["id"])  # другой тред того же бота
        other = await post_turn(client, (await make_thread(client, "b"))["id"])   # другой бот
        await until(lambda: _is(app, other["id"], "running"))
        await asyncio.sleep(0.6)
        assert await turn_status(app, second["id"]) == "queued"
        gate.set()
        for turn in (first, second, other):
            await until(lambda t=turn: _is(app, t["id"], "done"))


async def test_claim_turn_returns_none_when_bot_busy_and_claims_after_release():
    async with api(Runner(events=[])) as (client, app, _):
        await make_bot(client)
        t1, t2 = await make_thread(client), await make_thread(client)
        running = await insert_turn(app, t1["id"], "running")
        async with app.state.pool.acquire() as con:
            queued = await con.fetchrow("insert into bothub.turns(thread_id,prompt,client) values($1,'q','api') returning *", uuid.UUID(t2["id"]))
        await asyncio.sleep(0.5)  # фоновый воркер тоже не должен взять
        assert await app.state.claim_turn() is None
        assert await turn_status(app, queued["id"]) == "queued"
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='done' where id=$1", running["id"])
        await until(lambda: _is(app, queued["id"], "done"))


# ---- п.12: бюджет по ходу turn ---------------------------------------------------------

async def test_budget_exceeded_midturn_stops_turn_with_numbers():
    runner = Runner(events=[
        RunnerEvent("assistant_msg", {"text": "start", "final": False}),
        RunnerEvent("usage", {"tokens_in": 60, "tokens_out": 60, "model": "fake", "seconds": 0.1}),
        RunnerEvent("assistant_msg", {"text": "too late", "final": True}),
    ])
    async with api(runner) as (client, app, _):
        await make_bot(client, budget_daily_tokens=100)
        thread = await make_thread(client)
        turn = await post_turn(client, thread["id"])
        await until(lambda: _is(app, turn["id"], "error"))
        evs = await events(client, thread["id"])
        exceeded = [e for e in evs if e["kind"] == "budget_exceeded"]
        assert len(exceeded) == 1
        assert exceeded[0]["payload"]["spent"] == 120 and exceeded[0]["payload"]["budget"] == 100
        assert not any(e["kind"] == "assistant_msg" and e["payload"]["text"] == "too late" for e in evs)
        assert any(e["kind"] == "status" and e["payload"]["status"] == "error" for e in evs)
        assert turn["id"] in runner.stopped
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select sum(tokens_in+tokens_out) from bothub.usage") == 120
            assert "budget_exceeded" in await con.fetchval("select error from bothub.turns where id=$1", uuid.UUID(turn["id"]))


async def test_budget_within_limit_does_not_stop_turn():
    async with api() as (client, app, _):
        await make_bot(client, budget_daily_tokens=1000)
        thread = await make_thread(client)
        turn = await post_turn(client, thread["id"])
        await until(lambda: _is(app, turn["id"], "done"))
        assert not [e for e in await events(client, thread["id"]) if e["kind"] == "budget_exceeded"]


async def test_api_bound_turn_emits_final_usage_without_duplicate_ledger_row():
    runner = Runner(events=[RunnerEvent('usage', {'tokens_in':3,'tokens_out':5,'model':'gpt-test'})])
    async with api(runner) as (client, app, _):
        bot = await make_bot(client)
        thread = await make_thread(client,bot['id'])
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval('select owner_id from bothub.bots where id=$1',bot['id'])
            provider = await con.fetchval("insert into bothub.providers(owner_id,kind,name,status,secret_encrypted) "
                "values($1,'openai_api','API','ok',decode('00','hex')) returning id",owner)
            model = await con.fetchval('insert into bothub.models(provider_id,name) values($1,$2) returning id',provider,'gpt-test')
            await con.execute('update bothub.bots set provider_id=$2,model_id=$3 where id=$1',bot['id'],provider,model)
        turn = await post_turn(client,thread['id'])
        await until(lambda: _is(app,turn['id'],'done'))
        usage_events = [event for event in await events(client,thread['id']) if event['kind']=='usage']
        assert len(usage_events)==1
        assert usage_events[0]['payload']['tokens_in']==3
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.usage where turn_id=$1',uuid.UUID(turn['id']))==0


async def test_budget_checked_on_usage_posts_during_running_turn():
    gate = asyncio.Event()
    runner = Runner(events=[RunnerEvent("assistant_msg", {"text": "w", "final": False}), GATE], gate=gate)
    async with api(runner) as (client, app, _):
        await make_bot(client, budget_daily_tokens=100)
        thread = await make_thread(client)
        turn = await post_turn(client, thread["id"])
        await until(lambda: _is(app, turn["id"], "running"))
        body = {"thread_id": thread["id"], "turn_id": turn["id"], "provider": "fake", "model": "fake", "tokens_in": 30, "tokens_out": 30}
        first = await client.post("/api/usage", json=body, headers=bot_token("scout"))
        assert first.status_code in (200, 201) and first.json()["tokens_in"] == 30
        assert await turn_status(app, turn["id"]) == "running"
        await client.post("/api/usage", json=body, headers=bot_token("scout"))
        assert await turn_status(app, turn["id"]) == "error"
        exceeded = [e for e in await events(client, thread["id"]) if e["kind"] == "budget_exceeded"]
        assert exceeded and exceeded[0]["payload"]["spent"] == 120 and exceeded[0]["payload"]["budget"] == 100
        assert turn["id"] in runner.stopped


# ---- п.13: health с БД -----------------------------------------------------------------------

async def test_health_checks_database():
    async with api() as (client, app, _):
        response = await client.get("/api/health")
        assert response.status_code == 200 and response.json()["status"] == "ok"
        await app.state.pool.close()
        broken = await client.get("/api/health")
        assert broken.status_code == 503 and broken.json()["status"] == "error"


# ---- п.6: решение привязано к операции ------------------------------------------------------

async def test_approval_stores_op_hash_of_tool_and_args():
    async with api() as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        body = {"thread_id": thread["id"], "risk": "send", "title": "x", "tool": "send_message", "args": {"channel": "ops"}}
        row = (await client.post("/api/approvals", json=body, headers=bot_token("scout"))).json()
        assert row["op_hash"] == op_hash("send_message", {"channel": "ops"})
        assert row["op_hash"] != row["args_hash"]


async def test_remembered_rule_does_not_cover_extra_args():
    async with api() as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        body = {"thread_id": thread["id"], "risk": "send", "title": "x", "tool": "send_message", "args": {"channel": "ops"}}
        first = (await client.post("/api/approvals", json=body, headers=bot_token("scout"))).json()
        decided = await client.post(f"/api/approvals/{first['id']}/decide", json={"decision": "approve", "remember": True}, headers=OWNER)
        assert decided.status_code == 200
        same = (await client.post("/api/approvals", json=body, headers=bot_token("scout"))).json()
        assert same["status"] == "approved"
        widened = (await client.post("/api/approvals", json=body | {"args": {"channel": "ops", "to": "all"}}, headers=bot_token("scout"))).json()
        assert widened["status"] == "pending"


async def test_mac_call_approval_applies_only_to_same_tool_and_args():
    gate = asyncio.Event()
    runner = Runner(events=[RunnerEvent("assistant_msg", {"text": "w", "final": False}), GATE], gate=gate)
    async with api(runner) as (client, app, _):
        await make_bot(client, mac_full_control=True)
        thread = await make_thread(client)
        turn = await post_turn(client, thread["id"])
        await until(lambda: _is(app, turn["id"], "running"))
        tool = "mcp__bothub__mac_type_text"
        asked = (await client.post("/api/approvals", json={"thread_id": thread["id"], "turn_id": turn["id"], "risk": "other", "title": "type", "tool": tool, "args": {"text": "hello"}}, headers=bot_token("scout"))).json()
        assert asked["status"] == "pending"
        await client.post(f"/api/approvals/{asked['id']}/decide", json={"decision": "approve"}, headers=OWNER)

        def call(args):
            return client.post("/api/mac/call", json={"thread_id": thread["id"], "turn_id": turn["id"], "tool": "type_text", "args": args, "timeout": 1}, headers=bot_token("scout"))

        assert (await call({"text": "other text"})).status_code == 403   # другие аргументы
        assert (await call({"text": "hello", "x": 1})).status_code == 403
        assert (await call({"text": "hello"})).status_code == 409        # одобренная операция дошла до Mac (агент офлайн)


# ---- закрытие turn'а закрывает его pending-одобрения (approval_dec expired, как при истечении по сроку) ----------

async def approval_status(app, approval_id):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow("select status, decided_at from bothub.approvals where id=$1", approval_id)


def _decisions(evs):
    return [e for e in evs if e["kind"] == "approval_dec"]


async def test_recover_stale_turns_expires_pending_approvals_of_the_turn():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        stale = await insert_turn(app, thread["id"], "waiting_approval", lease="now()-interval '1 minute'")
        first = await insert_approval(app, thread["id"], stale["id"])
        second = await insert_approval(app, thread["id"], stale["id"], tool="send_mail")
        decided = await insert_approval(app, thread["id"], stale["id"], status="approved")
        await app.state.recover_stale_turns()
        assert await turn_status(app, stale["id"]) == "error"
        for approval in (first, second):
            row = await approval_status(app, approval["id"])
            assert row["status"] == "expired" and row["decided_at"] is not None
        assert (await approval_status(app, decided["id"]))["status"] == "approved"  # уже решённое не трогаем
        evs = await events(client, thread["id"])
        found = _decisions(evs)
        assert sorted(e["payload"]["approval_id"] for e in found) == sorted([str(first["id"]), str(second["id"])])
        assert all(e["payload"] == {"approval_id": e["payload"]["approval_id"], "decision": "expired", "remember": False,
                                    "client": "system"} and e["actor"] == "system" for e in found)
        status_seq = [e["seq"] for e in evs if e["kind"] == "status" and e["payload"].get("status") == "error"]
        assert status_seq and all(e["seq"] < status_seq[-1] for e in found)  # решения идут раньше закрытия turn'а


async def test_recover_stale_turns_leaves_approvals_of_a_live_turn_pending():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        live = await insert_turn(app, thread["id"], "waiting_approval")  # lease на час вперёд: не stale
        approval = await insert_approval(app, thread["id"], live["id"])
        await app.state.recover_stale_turns()
        assert await turn_status(app, live["id"]) == "waiting_approval"
        assert (await approval_status(app, approval["id"]))["status"] == "pending"
        assert not _decisions(await events(client, thread["id"]))


async def test_recover_stale_turns_twice_does_not_repeat_the_decisions():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        stale = await insert_turn(app, thread["id"], "running", lease="now()-interval '1 minute'")
        await insert_approval(app, thread["id"], stale["id"])
        await app.state.recover_stale_turns()
        await app.state.recover_stale_turns()
        assert len(_decisions(await events(client, thread["id"]))) == 1


async def test_runner_exit_expires_pending_approvals_of_the_turn():
    box = {}

    async def wait_with_approval(turn):
        async with box["app"].state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='waiting_approval' where id=$1", uuid.UUID(turn.turn_id))
        box["approval"] = await insert_approval(box["app"], box["thread"], uuid.UUID(turn.turn_id))

    runner = Runner(events=[], before=wait_with_approval)
    async with api(runner) as (client, app, _):
        box["app"] = app
        await make_bot(client)
        thread = await make_thread(client)
        box["thread"] = thread["id"]
        turn = await post_turn(client, thread["id"])
        await until(lambda: _is(app, turn["id"], "error"))
        assert (await approval_status(app, box["approval"]["id"]))["status"] == "expired"
        evs = await events(client, thread["id"])
        assert any(e["kind"] == "guard" and e["payload"]["reason"] == "runner_exited" for e in evs)
        found = _decisions(evs)
        assert len(found) == 1 and found[0]["payload"]["approval_id"] == str(box["approval"]["id"])
        assert found[0]["payload"]["decision"] == "expired"


async def test_expiry_sweep_also_expires_the_other_pending_approvals_of_the_turn():
    async with api() as (client, app, runner):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await insert_turn(app, thread["id"], "waiting_approval")
        due = await insert_approval(app, thread["id"], turn["id"], expires="now()-interval '1 minute'")
        other = await insert_approval(app, thread["id"], turn["id"], tool="send_mail")  # срок через час
        await app.state.deliver_outbox()  # expire_approvals -> fail_turn(approval_expired)
        assert await turn_status(app, turn["id"]) == "error"
        assert (await approval_status(app, other["id"]))["status"] == "expired"
        found = _decisions(await events(client, thread["id"]))
        assert sorted(e["payload"]["approval_id"] for e in found) == sorted([str(due["id"]), str(other["id"])])
        assert runner.runs == 0
