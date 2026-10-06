"""bots.mcp_allow на Postgres (нужна БД из conftest.py): список хранится и меняется через API, шлюз approvals отклоняет
чужой MCP-инструмент вне списка с причиной mcp_not_allowed, ход codex/agy с таким вызовом закрывается как guard."""
import asyncio
import hashlib
import hmac
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub.main import create_app
from bothub.mcp_policy import NOT_ALLOWED, UNSUPPORTED, McpAllowUnsupported, McpNotAllowed
from bothub.runner.base import RunnerEvent

OWNER = {"Authorization": "Bearer test-owner"}
FOREIGN = "mcp__github__create_issue"


def bot_token(bot_id):
    digest = hmac.new(b"test-secret", bot_id.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer bot:{bot_id}:{digest}"}


class Runner:
    provider = "fake"

    def __init__(self, events=(), error=None):
        self.events, self.error, self.stopped = list(events), error, []

    async def run(self, turn):
        for event in self.events:
            yield event
        if self.error:
            raise self.error

    async def stop(self, turn_id):
        self.stopped.append(turn_id)


@asynccontextmanager
async def api(runner=None):
    runner = runner or Runner([RunnerEvent("assistant_msg", {"text": "ok", "final": True})])
    app = create_app(lambda provider: runner)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, app, runner


async def make_bot(client, **changes):
    body = {"id": "scout", "name": "Scout", "provider": "fake", "model": "fake"} | changes
    response = await client.post("/api/bots", json=body, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def make_thread(client):
    response = await client.post("/api/threads", json={"bot_id": "scout", "title": "Test"}, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def ask(client, thread_id, tool, args=None):
    body = {"thread_id": thread_id, "turn_id": None, "risk": "other", "title": tool, "tool": tool, "args": args or {"title": "x"}}
    response = await client.post("/api/approvals", json=body, headers=bot_token("scout"))
    assert response.status_code in (200, 201), response.text
    return response.json()


async def events_of(client, thread_id):
    response = await client.get(f"/api/threads/{thread_id}/events", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


async def wait_finished(client, thread_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = await events_of(client, thread_id)
        if any(e["kind"] == "status" and e["payload"].get("status") in {"done", "stopped", "error"} for e in events):
            return events
        await asyncio.sleep(0.05)
    pytest.fail("turn did not finish")


@pytest.mark.asyncio
async def test_list_defaults_to_empty_and_round_trips_through_the_api():
    async with api() as (client, _, _):
        assert (await make_bot(client))["mcp_allow"] == []
        patched = await client.patch("/api/bots/scout", json={"mcp_allow": ["mcp__github__*", "slack"]}, headers=OWNER)
        assert patched.status_code == 200, patched.text
        assert patched.json()["mcp_allow"] == ["mcp__github__*", "slack"]
        assert (await client.get("/api/bots", headers=OWNER)).json()[0]["mcp_allow"] == ["mcp__github__*", "slack"]
        cleared = await client.patch("/api/bots/scout", json={"mcp_allow": []}, headers=OWNER)
        assert cleared.json()["mcp_allow"] == []


@pytest.mark.asyncio
async def test_foreign_tool_is_rejected_until_the_list_names_it():
    async with api() as (client, _, _):
        await make_bot(client)
        thread = await make_thread(client)
        refused = await ask(client, thread["id"], FOREIGN)
        assert refused["status"] == "rejected" and refused["reason"] == NOT_ALLOWED
        decisions = [e for e in await events_of(client, thread["id"]) if e["kind"] == "approval_dec"]
        assert len(decisions) == 1 and decisions[0]["payload"]["reason"] == NOT_ALLOWED
        assert decisions[0]["actor"] == "system"
        assert (await client.get("/api/approvals", headers=OWNER)).json() == []  # владельцу вопрос не показан

        await client.patch("/api/bots/scout", json={"mcp_allow": ["mcp__github__*"]}, headers=OWNER)
        asked = await ask(client, thread["id"], FOREIGN)
        assert asked["status"] == "pending" and "reason" not in asked  # список разрешает, одобряет владелец
        other = await ask(client, thread["id"], "mcp__slack__post_message")
        assert other["status"] == "rejected" and other["reason"] == NOT_ALLOWED


@pytest.mark.asyncio
async def test_remembered_rule_does_not_outlive_the_list():
    async with api() as (client, _, _):
        await make_bot(client, mcp_allow=["github"])
        thread = await make_thread(client)
        asked = await ask(client, thread["id"], FOREIGN)
        answer = await client.post(f"/api/approvals/{asked['id']}/decide", json={"decision": "approve", "remember": True, "client": "iphone"}, headers=OWNER)
        assert answer.status_code == 200, answer.text
        assert (await ask(client, thread["id"], FOREIGN))["status"] == "approved"  # правило «запомнить» работает, пока инструмент в списке
        await client.patch("/api/bots/scout", json={"mcp_allow": []}, headers=OWNER)
        again = await ask(client, thread["id"], FOREIGN)
        assert again["status"] == "rejected" and again["reason"] == NOT_ALLOWED  # убрали из списка: правило молчит


@pytest.mark.asyncio
async def test_own_tools_ignore_the_list():
    async with api() as (client, _, _):
        await make_bot(client)
        thread = await make_thread(client)
        asked = await ask(client, thread["id"], "mcp__bothub__remember", {"text": "x"})
        assert asked["status"] != "rejected"


@pytest.mark.asyncio
async def test_turn_refused_by_agy_with_a_non_empty_list_ends_as_a_guard_stop():
    runner = Runner([], error=McpAllowUnsupported("gemini"))
    async with api(runner) as (client, _, _):
        await make_bot(client, mcp_allow=["github"])
        thread = await make_thread(client)
        response = await client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "go", "client": "api"}, headers=OWNER)
        assert response.status_code in (200, 201, 202), response.text
        events = await wait_finished(client, thread["id"])
        guards = [e for e in events if e["kind"] == "guard"]
        assert [g["payload"]["reason"] for g in guards] == [UNSUPPORTED]
        assert guards[0]["payload"]["detail"] == "gemini"
        assert [e["payload"]["status"] for e in events if e["kind"] == "status"][-1] == "error"


@pytest.mark.asyncio
async def test_turn_with_a_foreign_tool_call_from_a_stream_runner_ends_as_a_guard_stop():
    runner = Runner([RunnerEvent("assistant_msg", {"text": "start", "final": False})], error=McpNotAllowed("github.create_issue"))
    async with api(runner) as (client, _, _):
        await make_bot(client)
        thread = await make_thread(client)
        response = await client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "go", "client": "api"}, headers=OWNER)
        assert response.status_code in (200, 201, 202), response.text
        events = await wait_finished(client, thread["id"])
        guards = [e for e in events if e["kind"] == "guard"]
        assert [g["payload"]["reason"] for g in guards] == [NOT_ALLOWED]
        assert guards[0]["payload"]["detail"] == "github.create_issue"
        assert [e["payload"]["status"] for e in events if e["kind"] == "status"][-1] == "error"
        assert not any(e["kind"] == "assistant_msg" and e["payload"].get("final") for e in events)
