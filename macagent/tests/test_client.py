import asyncio
import json

import pytest

from bothub_mac import client
from bothub_mac.config import Config
from bothub_mac.tools.errors import ToolError


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))


@pytest.fixture
def config():
    return Config(bothub_url="https://x", mac_agent_token="t")


async def test_run_call_success(monkeypatch, config):
    async def fake_dispatch(tool, args, cfg):
        return {"echo": args["text"]}

    monkeypatch.setattr(client, "dispatch", fake_dispatch)
    ws = FakeWS()
    await client._run_call(ws, "c1", "shell", {"text": "hi"}, 5, config)
    assert ws.sent == [{"type": "result", "id": "c1", "ok": True, "data": {"echo": "hi"}}]


async def test_run_call_tool_error(monkeypatch, config):
    async def fake_dispatch(tool, args, cfg):
        raise ToolError("bad args")

    monkeypatch.setattr(client, "dispatch", fake_dispatch)
    ws = FakeWS()
    await client._run_call(ws, "c1", "shell", {}, 5, config)
    assert ws.sent == [{"type": "result", "id": "c1", "ok": False, "error": "bad args"}]


async def test_run_call_timeout(monkeypatch, config):
    async def fake_dispatch(tool, args, cfg):
        await asyncio.sleep(10)

    monkeypatch.setattr(client, "dispatch", fake_dispatch)
    ws = FakeWS()
    await client._run_call(ws, "c1", "shell", {}, 0.01, config)
    assert ws.sent[0]["ok"] is False
    assert "timeout" in ws.sent[0]["error"]


async def test_run_call_unexpected_exception(monkeypatch, config):
    async def fake_dispatch(tool, args, cfg):
        raise ValueError("kaboom")

    monkeypatch.setattr(client, "dispatch", fake_dispatch)
    ws = FakeWS()
    await client._run_call(ws, "c1", "shell", {}, 5, config)
    assert ws.sent[0]["ok"] is False
    assert "kaboom" in ws.sent[0]["error"]


def test_handle_message_ignores_non_call(config):
    ws = FakeWS()
    client._handle_message(ws, json.dumps({"type": "ping"}), config)
    assert ws.sent == []


def test_handle_message_ignores_malformed_json(config):
    ws = FakeWS()
    client._handle_message(ws, "not json", config)
    assert ws.sent == []


async def test_handle_message_schedules_call(monkeypatch, config):
    calls = []

    async def fake_dispatch(tool, args, cfg):
        calls.append((tool, args))
        return {}

    monkeypatch.setattr(client, "dispatch", fake_dispatch)
    ws = FakeWS()
    client._handle_message(
        ws, json.dumps({"type": "call", "id": "c1", "tool": "screenshot", "args": {}}), config
    )
    await asyncio.sleep(0)
    assert calls == [("screenshot", {})]
    assert ws.sent == [{"type": "result", "id": "c1", "ok": True, "data": {}}]
