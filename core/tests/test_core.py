import asyncio
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from bothub.main import create_app
from bothub.risk import op_hash
from bothub.runner.base import RunnerEvent


OWNER = {"Authorization": "Bearer test-owner"}


def bot_token(bot_id):
    digest = hmac.new(b"test-secret", bot_id.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer bot:{bot_id}:{digest}"}


class FakeRunner:
    provider = "fake"

    def __init__(self, events=None):
        self.events = events or [
            RunnerEvent("plan", {"steps": [{"id": "1", "title": "Answer", "status": "done"}]}),
            RunnerEvent("assistant_msg", {"text": "ok", "final": True}),
            RunnerEvent("usage", {"tokens_in": 10, "tokens_out": 20, "model": "fake", "seconds": 0.1}),
        ]
        self.stopped = []

    async def run(self, turn):
        for event in self.events:
            yield event

    async def stop(self, turn_id):
        self.stopped.append(turn_id)


@asynccontextmanager
async def api(runner=None):
    runner = runner or FakeRunner()
    app = create_app(lambda provider: runner)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, app, runner


async def make_bot(client, bot_id="scout", **changes):
    data = {"id": bot_id, "name": "Scout", "provider": "fake", "model": "fake"} | changes
    response = await client.post("/api/bots", json=data, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def make_mac(client, name="Test Mac"):
    response = await client.post("/api/macs", json={"name": name}, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


async def make_thread(client, bot_id="scout"):
    response = await client.post("/api/threads", json={"bot_id": bot_id, "title": "Test"}, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def wait_turn(client, turn_id, thread_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"/api/threads/{thread_id}/events", headers=OWNER)
        assert response.status_code == 200, response.text
        events = response.json()
        if any(e["kind"] == "status" and e["payload"].get("status") in {"done", "stopped", "error"} for e in events):
            return events
        await asyncio.sleep(0.05)
    pytest.fail(f"turn {turn_id} did not finish")


@pytest.mark.asyncio
async def test_health_and_bot_crud():
    async with api() as (client, _, _):
        assert (await client.get("/api/health")).json()["ok"] is True
        assert (await client.get("/api/bots")).status_code in (401, 403)
        bot = await make_bot(client)
        assert bot["id"] == "scout"
        response = await client.patch("/api/bots/scout", json={"role": "research"}, headers=OWNER)
        assert response.status_code == 200, response.text
        assert response.json()["role"] == "research"
        assert [b["id"] for b in (await client.get("/api/bots", headers=OWNER)).json()] == ["scout"]


@pytest.mark.asyncio
async def test_turn_events_since_and_usage():
    async with api() as (client, _, _):
        await make_bot(client)
        thread = await make_thread(client)
        response = await client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "hello", "client": "iphone"}, headers=OWNER)
        assert response.status_code in (200, 201, 202), response.text
        events = await wait_turn(client, response.json()["id"], thread["id"])
        kinds = {e["kind"] for e in events}
        assert {"user_msg", "plan", "assistant_msg", "usage"} <= kinds
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(set(seqs))
        tail = (await client.get(f"/api/threads/{thread['id']}/events", params={"since": seqs[1]}, headers=OWNER)).json()
        assert [e["seq"] for e in tail] == seqs[2:]
        summary = (await client.get("/api/usage/summary", headers=OWNER)).json()
        assert summary["bots"][0]["tokens_today"] == 30


@pytest.mark.asyncio
async def test_approvals_auto_allow_remember_reject_and_wait():
    async with api() as (client, _, _):
        await make_bot(client, auto_allow=[{"tool": "read_file", "match": {"path": "/safe/a"}, "op_hash": op_hash("read_file", {"path": "/safe/a"})}])
        thread = await make_thread(client)
        body = {"thread_id": thread["id"], "turn_id": None, "risk": "other", "title": "Read", "tool": "read_file", "args": {"path": "/safe/a"}}
        auto = await client.post("/api/approvals", json=body, headers=bot_token("scout"))
        assert auto.status_code in (200, 201), auto.text
        assert auto.json()["status"] == "approved"
        assert auto.json()["args_hash"] == hashlib.sha256(b'{"path":"/safe/a"}').hexdigest()
        # Находки 2/4: сервер сам классифицирует risk (bothub.risk.classify), поэтому
        # инструмент должен реально быть "delete" - тело больше не решает.
        pending = await client.post("/api/approvals", json=body | {"risk": "other", "tool": "move_to_trash", "title": "Delete"}, headers=bot_token("scout"))
        assert pending.status_code in (200, 201), pending.text
        approval_id = pending.json()["id"]
        assert pending.json()["status"] == "pending"
        waiter = asyncio.create_task(client.get(f"/api/approvals/{approval_id}/wait", params={"timeout": 2}, headers=bot_token("scout")))
        await asyncio.sleep(0.1)
        decided = await client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "remember": False, "client": "iphone"}, headers=OWNER)
        assert decided.status_code == 200, decided.text
        assert (await waiter).json()["status"] == "rejected"
        remember = await client.post("/api/approvals", json=body | {"tool": "send_message", "risk": "send", "args": {"channel": "ops"}}, headers=bot_token("scout"))
        assert remember.json()["status"] == "pending"
        answer = await client.post(f"/api/approvals/{remember.json()['id']}/decide", json={"decision": "approve", "remember": True, "client": "iphone"}, headers=OWNER)
        assert answer.status_code == 200, answer.text
        bots = (await client.get("/api/bots", headers=OWNER)).json()
        # пункт 6: правило несёт хэш операции (tool + все args)
        assert any(rule == {"tool": "send_message", "match": {"channel": "ops"}, "op_hash": op_hash("send_message", {"channel": "ops"})} for rule in bots[0]["auto_allow"])


@pytest.mark.asyncio
async def test_guard_repeated_tool_errors():
    error = RunnerEvent("tool_result", {"call_id": "x", "ok": False, "summary": "same error"})
    runner = FakeRunner([error, error, error, RunnerEvent("assistant_msg", {"text": "too late", "final": True})])
    async with api(runner) as (client, _, _):
        await make_bot(client)
        thread = await make_thread(client)
        turn = (await client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "fail", "client": "api"}, headers=OWNER)).json()
        events = await wait_turn(client, turn["id"], thread["id"])
        assert any(e["kind"] == "guard" and e["payload"]["reason"] == "repeat_error" for e in events)
        assert not any(e["kind"] == "assistant_msg" for e in events)
        assert turn["id"] in runner.stopped


@pytest.mark.asyncio
async def test_schedule_run_now_and_next_run():
    async with api() as (client, _, _):
        await make_bot(client)
        response = await client.post("/api/schedules", json={"bot_id": "scout", "name": "Morning", "kind": "cron", "cron": "0 9 * * *", "timezone": "Europe/Moscow", "prompt": "brief"}, headers=OWNER)
        assert response.status_code in (200, 201), response.text
        schedule = response.json()
        assert datetime.fromisoformat(schedule["next_run_at"]).astimezone(timezone.utc) > datetime.now(timezone.utc)
        run = await client.post(f"/api/schedules/{schedule['id']}/run", headers=OWNER)
        assert run.status_code in (200, 201, 202), run.text
        threads = (await client.get("/api/threads", headers=OWNER)).json()
        assert len(threads) == 1 and threads[0]["kind"] == "routine"
        again = await client.post(f"/api/schedules/{schedule['id']}/run", headers=OWNER)
        assert again.status_code in (200, 201, 202), again.text
        assert len((await client.get("/api/threads", headers=OWNER)).json()) == 1


@pytest.mark.asyncio
async def test_pwa_index_served():
    async with api() as (client, _, _):
        response = await client.get("/")
        assert response.status_code == 200, response.text
        assert "botstead" in response.text
        response = await client.get("/app.js")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_mac_call_unavailable():
    # Раннер "висит" после первого события, чтобы turn оставался running, пока тест
    # дойдёт до /api/mac/call - иначе быстрый fake-раннер завершает turn (done) раньше,
    # чем мы проверим находку 2 (mac_call требует turn в running/waiting_mac).
    hang = FakeRunner([RunnerEvent("assistant_msg", {"text": "working", "final": False})])
    async def stay(turn):
        yield hang.events[0]
        await asyncio.sleep(10)
    hang.run = stay
    async with api(hang) as (client, _, _):
        mac = await make_mac(client)
        await make_bot(client, mac_id=mac["id"], mac_full_control=True)
        thread = await make_thread(client)
        turn = (await client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "screen", "client": "api"}, headers=OWNER)).json()
        for _ in range(50):
            events = (await client.get(f"/api/threads/{thread['id']}/events", headers=OWNER)).json()
            if any(e["kind"] == "assistant_msg" for e in events):
                break
            await asyncio.sleep(0.05)
        response = await client.post("/api/mac/call", json={"thread_id": thread["id"], "turn_id": turn["id"], "tool": "screenshot", "args": {}}, headers=bot_token("scout"))
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "mac_unavailable"


def test_mac_call_with_websocket_agent():
    hang = FakeRunner([RunnerEvent("assistant_msg", {"text": "working", "final": False})])
    async def stay(turn):
        yield hang.events[0]
        await asyncio.sleep(10)
    hang.run = stay
    app = create_app(lambda provider: hang)
    with TestClient(app) as client:
        mac = client.post("/api/macs", json={"name": "Test Mac"}, headers=OWNER).json()
        assert client.post("/api/bots", json={"id": "scout", "name": "Scout", "provider": "fake", "model": "fake", "mac_id": mac["id"], "mac_full_control": True}, headers=OWNER).status_code in (200, 201)
        thread = client.post("/api/threads", json={"bot_id": "scout"}, headers=OWNER).json()
        turn = client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "screen", "client": "api"}, headers=OWNER).json()
        for _ in range(50):
            events = client.get(f"/api/threads/{thread['id']}/events", headers=OWNER).json()
            if any(e["kind"] == "assistant_msg" for e in events):
                break
            time.sleep(0.05)
        with client.websocket_connect("/agent/mac", headers={"Authorization": "Bearer " + mac["token"]}) as ws:
            ws.send_json({"type": "hello", "host": "test", "os": "macOS", "agent_version": "1", "permissions": {"files": True, "screen": True, "automation": True, "accessibility": True}})
            for _ in range(20):
                if client.get("/api/mac/status", headers=OWNER).json()["state"] == "online":
                    break
                time.sleep(0.05)
            with ThreadPoolExecutor(max_workers=2) as pool:
                incoming = pool.submit(ws.receive_json)
                future = pool.submit(client.post, "/api/mac/call", json={"thread_id": thread["id"], "turn_id": turn["id"], "tool": "screenshot", "args": {}}, headers=bot_token("scout"))
                try:
                    call = incoming.result(timeout=3)
                except TimeoutError:
                    ws.close()
                    raise
                assert call["type"] == "call" and call["tool"] == "screenshot"
                ws.send_json({"type": "result", "id": call["id"], "ok": True, "data": {"png_b64": "abc"}})
                response = future.result(timeout=3)
                assert response.status_code == 200, response.text
                assert response.json()["data"]["png_b64"] == "abc"


async def test_event_websocket_replays_since():
    # Живой uvicorn: starlette TestClient отменяет портал при закрытии WS и даёт ложный CancelledError.
    import json, uvicorn, websockets, httpx
    app = create_app(lambda provider: FakeRunner())
    srv = uvicorn.Server(uvicorn.Config(app, port=18182, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.05)
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18182", headers=OWNER) as c:
            assert (await c.post("/api/bots", json={"id": "scout", "name": "Scout", "provider": "fake", "model": "fake"})).status_code in (200, 201)
            thread = (await c.post("/api/threads", json={"bot_id": "scout"})).json()
            url = f"ws://127.0.0.1:18182/api/ws?token=test-owner&thread_id={thread['id']}"
            async with websockets.connect(url + "&since=0", origin="http://127.0.0.1:18182") as ws:
                await c.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "hello", "client": "api"})
                kinds = []
                while "usage" not in kinds:
                    kinds.append(json.loads(await asyncio.wait_for(ws.recv(), 5))["kind"])
            assert kinds[0] == "user_msg" and "assistant_msg" in kinds
            async with websockets.connect(url + "&since=2", origin="http://127.0.0.1:18182") as ws:
                assert json.loads(await asyncio.wait_for(ws.recv(), 5))["seq"] == 3
    finally:
        srv.should_exit = True
        await asyncio.wait_for(task, 10)


def test_owner_via_trusted_proxy(monkeypatch):
    monkeypatch.setenv("BOTHUB_PROXY_SECRET", "proxy-secret")
    monkeypatch.setenv("BOTHUB_OWNER_USER", "owner")  # без имени владельца прокси-вход закрыт (этап 4)
    app = create_app(lambda provider: FakeRunner())
    good = {"X-Bothub-Proxy": "proxy-secret", "Remote-User": "owner"}
    with TestClient(app) as client:
        assert client.get("/api/bots", headers=good).status_code == 200
        assert client.get("/api/bots", headers={"X-Bothub-Proxy": "wrong", "Remote-User": "owner"}).status_code == 401
        assert client.get("/api/bots", headers={"Remote-User": "owner"}).status_code == 401
        assert client.get("/api/bots", headers={"X-Bothub-Proxy": "proxy-secret"}).status_code == 401
        client.post("/api/bots", json={"id": "scout", "name": "Scout", "provider": "fake", "model": "fake"}, headers=good)
        thread = client.post("/api/threads", json={"bot_id": "scout"}, headers=good).json()
        with client.websocket_connect(f"/api/ws?thread_id={thread['id']}", headers=good | {"Origin": "http://testserver"}):
            pass


def test_proxy_header_ignored_without_secret(monkeypatch):
    monkeypatch.delenv("BOTHUB_PROXY_SECRET", raising=False)
    app = create_app(lambda provider: FakeRunner())
    with TestClient(app) as client:
        assert client.get("/api/bots", headers={"X-Bothub-Proxy": "", "Remote-User": "owner"}).status_code == 401


def test_owner_proxy_checks_remote_user_when_configured(monkeypatch):
    # Находка 19: BOTHUB_OWNER_USER задан - секрет прокси уже не пускает owner'ом
    # любого Remote-User, только настроенного.
    monkeypatch.setenv("BOTHUB_PROXY_SECRET", "proxy-secret")
    monkeypatch.setenv("BOTHUB_OWNER_USER", "owner")
    app = create_app(lambda provider: FakeRunner())
    with TestClient(app) as client:
        good = {"X-Bothub-Proxy": "proxy-secret", "Remote-User": "owner"}
        wrong_user = {"X-Bothub-Proxy": "proxy-secret", "Remote-User": "someone-else"}
        assert client.get("/api/bots", headers=good).status_code == 200
        assert client.get("/api/bots", headers=wrong_user).status_code == 401
