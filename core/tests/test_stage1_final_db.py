import json
"""Этап 1, финальная доводка на Postgres: миграции 006/007 и пункты 5-7 на настоящем SQL.

Те же сценарии без БД (фейковый пул) в test_stage1_final_flow.py. Нужен Postgres из conftest.py.
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bothub import db as hub_db
from bothub.main import create_app
from bothub.risk import op_hash
from bothub.runner.base import RunnerEvent
from test_reliability_db import (
    OWNER, Runner, api, bot_token, insert_approval, insert_turn, make_bot, make_thread,
    outbox_row, outbox_setup, turn_status,
)

MIGRATIONS = Path(hub_db.__file__).parent / "migrations"


# ---- п.3: миграция 006 ------------------------------------------------------------------------------------

LEGACY = [
    {"tool": "Bash"},
    {"tool": "WebFetch"},
    {"tool": "mcp__bothub__mac_delegate"},
    {"tool": "Bash", "match": {}},
    {"tool": "WebFetch", "match": None},
    {"tool": "Bash", "match": {"command": "ls"}},
    {"tool": "read_file", "match": {"path": "/safe/a"}},
    {"tool": "mcp__bothub__mac_shell", "match": {"cmd": "ls"}, "op_hash": ""},
]
KEPT = [
    {"tool": "Read"},
    {"tool": "Write"},
    {"tool": "mcp__bothub__mac_screenshot", "match": {}},
    {"tool": "Bash", "match": {"command": "ls"}, "op_hash": op_hash("Bash", {"command": "ls"})},
    {"tool": "read_file", "match": {"path": "/safe/a"}, "op_hash": op_hash("read_file", {"path": "/safe/a"})},
    {"tool": "*"},
]


async def run_migration(app, name):
    async with app.state.pool.acquire() as con:
        await con.execute((MIGRATIONS / name).read_text())


async def stored_rules(app, bot_id):
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select auto_allow from bothub.bots where id=$1", bot_id)


async def removed_rules(app, bot_id):
    async with app.state.pool.acquire() as con:
        return [r["rule"] for r in await con.fetch("select rule from bothub.auto_allow_removed where bot_id=$1 order by id", bot_id)]


async def test_migration_006_removes_legacy_rules_and_archives_them():
    async with api(manual=True) as (client, app, _):
        # порядок важен: удаляются элементы, остальные сохраняют порядок
        mixed = [LEGACY[0], KEPT[0], LEGACY[5], KEPT[3], LEGACY[1], KEPT[1], LEGACY[6], KEPT[2], LEGACY[2], LEGACY[3], LEGACY[4], LEGACY[7], KEPT[4], KEPT[5]]
        # Правила старого формата API больше не принимает (проверка входа): в базу они попадают напрямую, как до миграции.
        await make_bot(client, "legacy", auto_allow=[])
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set auto_allow=$2::jsonb where id=$1", "legacy", json.dumps(mixed))
        await make_bot(client, "clean", auto_allow=KEPT)
        await make_bot(client, "empty", auto_allow=[])
        await run_migration(app, "006_auto_allow_cleanup.sql")
        assert await stored_rules(app, "legacy") == [r for r in mixed if r in KEPT]
        assert await removed_rules(app, "legacy") == [r for r in mixed if r in LEGACY]
        assert await stored_rules(app, "clean") == KEPT and await removed_rules(app, "clean") == []
        assert await stored_rules(app, "empty") == [] and await removed_rules(app, "empty") == []
        # повторный запуск ничего не находит
        await run_migration(app, "006_auto_allow_cleanup.sql")
        assert await stored_rules(app, "legacy") == [r for r in mixed if r in KEPT]
        assert len(await removed_rules(app, "legacy")) == len(LEGACY)


async def test_migration_006_table_has_bot_rule_and_removed_at():
    async with api(manual=True) as (_, app, _):
        async with app.state.pool.acquire() as con:
            columns = {r["column_name"]: r["data_type"] for r in await con.fetch(
                "select column_name, data_type from information_schema.columns where table_schema='bothub' and table_name='auto_allow_removed'")}
        assert columns["bot_id"] == "text" and columns["rule"] == "jsonb" and columns["removed_at"].startswith("timestamp")


async def test_migration_006_keeps_rule_that_remember_stores_now():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        body = {"thread_id": thread["id"], "title": "x", "tool": "mcp__bothub__mac_shell", "risk": "other", "args": {"cmd": "ls"}}
        asked = (await client.post("/api/approvals", json=body, headers=bot_token("scout"))).json()
        await client.post(f"/api/approvals/{asked['id']}/decide", json={"decision": "approve", "remember": True}, headers=OWNER)
        before = await stored_rules(app, "scout")
        assert before and before[0]["op_hash"] == op_hash("mcp__bothub__mac_shell", {"cmd": "ls"})
        await run_migration(app, "006_auto_allow_cleanup.sql")
        assert await stored_rules(app, "scout") == before and await removed_rules(app, "scout") == []


async def test_legacy_rule_in_db_does_not_auto_approve():
    async with api(manual=True) as (client, app, _):
        await make_bot(client, auto_allow=[{"tool": "read_file", "match": {"path": "/safe/a"}}])
        thread = await make_thread(client)
        body = {"thread_id": thread["id"], "title": "Read", "tool": "read_file", "risk": "other", "args": {"path": "/safe/a"}}
        assert (await client.post("/api/approvals", json=body, headers=bot_token("scout"))).json()["status"] == "pending"
        await client.patch("/api/bots/scout", json={"auto_allow": [{"tool": "read_file", "match": {"path": "/safe/a"}, "op_hash": op_hash("read_file", {"path": "/safe/a"})}]}, headers=OWNER)
        assert (await client.post("/api/approvals", json=body, headers=bot_token("scout"))).json()["status"] == "approved"


# ---- п.5: лимит при выходе из waiting ------------------------------------------------------------------------------

async def test_turn_leaving_waiting_hits_limit_and_stays_waiting_then_resumes(monkeypatch):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "1")
    async with api(manual=True) as (client, app, _):
        await make_bot(client, "a")
        await make_bot(client, "b")
        await make_bot(client, "scout")  # insert_approval вставляет approval с bot_id=scout, затем тест переводит его на b
        busy = await insert_turn(app, (await make_thread(client, "a"))["id"], "running")
        thread_b = await make_thread(client, "b")
        waiting = await insert_turn(app, thread_b["id"], "waiting_approval")
        approval = await insert_approval(app, thread_b["id"], waiting["id"])
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.approvals set bot_id='b' where id=$1", approval["id"])
        decided = await client.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=OWNER)
        assert decided.status_code == 200 and decided.json()["status"] == "approved"
        assert await turn_status(app, waiting["id"]) == "waiting_approval"        # слота нет: ждёт
        held = await client.get(f"/api/approvals/{approval['id']}/wait", params={"timeout": 0}, headers=bot_token("b"))
        assert held.json()["status"] == "pending" and held.json()["queued"] is True
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='done' where id=$1", busy["id"])
        free = await client.get(f"/api/approvals/{approval['id']}/wait", params={"timeout": 0}, headers=bot_token("b"))
        assert free.json()["status"] == "approved"
        assert await turn_status(app, waiting["id"]) == "running"


async def test_waiting_turn_resumes_at_once_when_slot_is_free(monkeypatch):
    monkeypatch.setenv("BOTHUB_MAX_PARALLEL_TURNS", "2")
    async with api(manual=True) as (client, app, _):
        await make_bot(client, "a")
        await insert_turn(app, (await make_thread(client, "a"))["id"], "running")
        await make_bot(client, "b")
        await make_bot(client, "scout")  # insert_approval вставляет approval с bot_id=scout, затем тест переводит его на b
        thread_b = await make_thread(client, "b")
        waiting = await insert_turn(app, thread_b["id"], "waiting_approval")
        approval = await insert_approval(app, thread_b["id"], waiting["id"])
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.approvals set bot_id='b' where id=$1", approval["id"])
        await client.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "reject"}, headers=OWNER)
        assert await turn_status(app, waiting["id"]) == "running"


# ---- п.6: outbox с pre-write --------------------------------------------------------------------------------------

class FlakyPool:
    """Один из следующих acquire() падает, как при обрыве связи с БД после отправки."""

    def __init__(self, pool):
        self.pool, self.fail = pool, False

    def acquire(self):
        if self.fail:
            self.fail = False
            raise ConnectionError("db went away")
        return self.pool.acquire()

    def __getattr__(self, name):
        return getattr(self.pool, name)


async def make_due(app):
    async with app.state.pool.acquire() as con:
        await con.execute("update bothub.outbox set next_attempt_at=now() where sent_at is null")


async def test_delivery_record_is_written_before_the_network_send(monkeypatch):
    async with api(manual=True) as (_, app, _):
        outbox_id = await outbox_setup(app, monkeypatch, ["https://p/a"])
        seen = []

        async def send(sub, body):
            seen.append((await outbox_row(app, outbox_id))[1])   # отдельное соединение: пул не занят отправкой

        app.state.send_push = send
        await app.state.deliver_outbox()
        assert seen == [{"https://p/a": "sending"}]
        row, deliveries = await outbox_row(app, outbox_id)
        assert deliveries == {"https://p/a": "sent"} and row["sent_at"] is not None


async def test_db_failure_after_send_does_not_resend(monkeypatch):
    async with api(manual=True) as (_, app, _):
        outbox_id = await outbox_setup(app, monkeypatch, ["https://p/a", "https://p/b"])
        flaky = FlakyPool(app.state.pool)
        app.state.pool = flaky
        calls = []

        async def send(sub, body):
            calls.append(sub["endpoint"])
            if len(calls) == 1:
                flaky.fail = True          # следующий запрос к БД (итог по a) падает

        app.state.send_push = send
        await app.state.deliver_outbox()
        assert calls == ["https://p/a"]
        row, deliveries = await outbox_row(app, outbox_id)
        assert deliveries == {"https://p/a": "sending"} and row["sent_at"] is None
        await make_due(app)
        await app.state.deliver_outbox()
        assert calls == ["https://p/a", "https://p/b"]       # a не повторяется
        row, deliveries = await outbox_row(app, outbox_id)
        assert deliveries == {"https://p/a": "sending", "https://p/b": "sent"} and row["sent_at"] is not None


async def test_network_failure_leaves_failed_record_and_retries_only_that_endpoint(monkeypatch):
    async with api(manual=True) as (_, app, _):
        outbox_id = await outbox_setup(app, monkeypatch, ["https://p/a", "https://p/b"])
        calls, fail_b = [], [True]

        async def send(sub, body):
            calls.append(sub["endpoint"])
            if sub["endpoint"] == "https://p/b" and fail_b[0]:
                raise ConnectionError("network")

        app.state.send_push = send
        await app.state.deliver_outbox()
        _, deliveries = await outbox_row(app, outbox_id)
        assert deliveries == {"https://p/a": "sent", "https://p/b": "failed"}
        calls.clear()
        fail_b[0] = False
        await make_due(app)
        await app.state.deliver_outbox()
        assert calls == ["https://p/b"]
        _, deliveries = await outbox_row(app, outbox_id)
        assert deliveries == {"https://p/a": "sent", "https://p/b": "sent"}


async def test_migration_007_allows_sending_status():
    async with api(manual=True) as (_, app, _):
        async with app.state.pool.acquire() as con:
            outbox_id = await con.fetchval("insert into bothub.outbox(kind,dedup_key,payload) values('push','k','{}'::jsonb) returning id")
            await con.execute("insert into bothub.outbox_deliveries(outbox_id,endpoint,status) values($1,'e','sending')", outbox_id)
            with pytest.raises(Exception):
                await con.execute("insert into bothub.outbox_deliveries(outbox_id,endpoint,status) values($1,'e2','unknown')", outbox_id)


# ---- п.7: одобрение mac-вызова расходуется -------------------------------------------------------------------------

def test_mac_approval_works_once_with_websocket_agent():
    hang = Runner(events=[RunnerEvent("assistant_msg", {"text": "working", "final": False})])

    async def stay(turn):
        yield hang.events[0]
        await asyncio.sleep(30)

    hang.run = stay
    app = create_app(lambda provider: hang)
    tool = "mcp__bothub__mac_type_text"
    args = {"text": "hello"}
    with TestClient(app) as client:
        mac = client.post("/api/macs", json={"name": "Test Mac"}, headers=OWNER).json()
        assert client.post("/api/bots", json={"id": "scout", "name": "Scout", "provider": "fake", "model": "fake", "mac_id": mac["id"], "mac_full_control": True}, headers=OWNER).status_code in (200, 201)
        thread = client.post("/api/threads", json={"bot_id": "scout"}, headers=OWNER).json()
        turn = client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "type", "client": "api"}, headers=OWNER).json()
        for _ in range(50):
            if any(e["kind"] == "assistant_msg" for e in client.get(f"/api/threads/{thread['id']}/events", headers=OWNER).json()):
                break
            time.sleep(0.05)

        def approve(remember=False):
            asked = client.post("/api/approvals", json={"thread_id": thread["id"], "turn_id": turn["id"], "risk": "other", "title": "type", "tool": tool, "args": args}, headers=bot_token("scout")).json()
            assert asked["status"] == "pending", asked
            assert client.post(f"/api/approvals/{asked['id']}/decide", json={"decision": "approve", "remember": remember}, headers=OWNER).status_code == 200

        def call():
            return client.post("/api/mac/call", json={"thread_id": thread["id"], "turn_id": turn["id"], "tool": "type_text", "args": args, "timeout": 3}, headers=bot_token("scout"))

        approve()
        with client.websocket_connect("/agent/mac", headers={"Authorization": "Bearer " + mac["token"]}) as ws:
            ws.send_json({"type": "hello", "host": "t", "os": "macOS", "agent_version": "1", "permissions": {}})
            for _ in range(20):
                if client.get("/api/mac/status", headers=OWNER).json()["state"] == "online":
                    break
                time.sleep(0.05)
            with ThreadPoolExecutor(max_workers=2) as pool:
                incoming = pool.submit(ws.receive_json)
                future = pool.submit(call)
                message = incoming.result(timeout=3)
                ws.send_json({"type": "result", "id": message["id"], "ok": True, "data": {}})
                assert future.result(timeout=3).status_code == 200
            assert call().status_code == 403          # повтор без нового одобрения заблокирован
            approve()
            with ThreadPoolExecutor(max_workers=2) as pool:
                incoming = pool.submit(ws.receive_json)
                future = pool.submit(call)
                message = incoming.result(timeout=3)
                ws.send_json({"type": "result", "id": message["id"], "ok": True, "data": {}})
                assert future.result(timeout=3).status_code == 200
            assert call().status_code == 403
