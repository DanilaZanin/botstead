"""MCP-сервер bothub: approve (allow/deny/auto-approved), mac 409 retry, remember,
mac_delegate (без retry на 409, таймаут = args.timeout + 60), mac_screenshot/
mac_preview (картинка как ImageContent + приложение файлом в тред).
Все HTTP-вызовы идут через httpx.MockTransport — реального ядра нет.
"""
import base64
import json

import httpx
from mcp.server.mcpserver import Image

from bothub import mcp_server as srv


def _patch_client(monkeypatch, handler):
    monkeypatch.setenv("BOTHUB_URL", "http://core.test")
    monkeypatch.setenv("BOTHUB_TOKEN", "bot:scout:deadbeef")
    monkeypatch.setenv("BOTHUB_THREAD_ID", "thread-1")
    monkeypatch.setenv("BOTHUB_TURN_ID", "turn-1")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(srv, "_client", lambda: httpx.AsyncClient(
        base_url="http://core.test", transport=transport))


def _patch_delegate_client(monkeypatch, handler):
    monkeypatch.setenv("BOTHUB_URL", "http://core.test")
    monkeypatch.setenv("BOTHUB_TOKEN", "bot:scout:deadbeef")
    monkeypatch.setenv("BOTHUB_THREAD_ID", "thread-1")
    monkeypatch.setenv("BOTHUB_TURN_ID", "turn-1")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(srv, "_delegate_client", lambda timeout: httpx.AsyncClient(
        base_url="http://core.test", transport=transport, timeout=timeout))


async def test_approve_auto_approved(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/approvals"
        return httpx.Response(200, json={"id": "a1", "status": "approved"})

    _patch_client(monkeypatch, handler)
    result = await srv.approve("mac_read_file", {"path": "/x"})
    assert result == {"behavior": "allow", "updatedInput": {"path": "/x"}}


async def test_approve_allow_after_wait(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/approvals":
            return httpx.Response(200, json={"id": "a2", "status": "pending"})
        assert request.url.path == "/api/approvals/a2/wait"
        assert request.url.params["timeout"] == str(srv.APPROVAL_WAIT_POLL)
        return httpx.Response(200, json={"id": "a2", "status": "approved"})

    _patch_client(monkeypatch, handler)
    result = await srv.approve("git_push", {"remote": "origin"})
    assert result == {"behavior": "allow", "updatedInput": {"remote": "origin"}}
    assert calls == ["/api/approvals", "/api/approvals/a2/wait"]


async def test_approve_deny(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/approvals":
            return httpx.Response(200, json={"id": "a3", "status": "pending"})
        return httpx.Response(200, json={"id": "a3", "status": "rejected"})

    _patch_client(monkeypatch, handler)
    result = await srv.approve("mac_move_to_trash", {"path": "/etc/passwd"})
    assert result == {"behavior": "deny", "message": "approval rejected"}


async def test_mac_call_retries_on_409_then_succeeds(monkeypatch):
    monkeypatch.setattr(srv, "MAC_RETRY_INTERVAL", 0.0)
    monkeypatch.setattr(srv, "MAC_RETRY_TIMEOUT", 5.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(409, json={"state": "offline"})
        return httpx.Response(200, json={"png_b64": "abc"})

    _patch_client(monkeypatch, handler)
    result = await srv._mac_call("screenshot", {})
    assert result == {"png_b64": "abc"}
    assert attempts["n"] == 3


async def test_mac_call_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(srv, "MAC_RETRY_INTERVAL", 0.0)
    monkeypatch.setattr(srv, "MAC_RETRY_TIMEOUT", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"state": "offline"})

    _patch_client(monkeypatch, handler)
    result = await srv._mac_call("screenshot", {})
    assert result == {"state": "offline"}


async def test_remember(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/memory"
        body = json.loads(request.content)
        assert body == {"text": "владелец любит кофе"}
        return httpx.Response(200, json={"id": "m1", "status": "proposed"})

    _patch_client(monkeypatch, handler)
    result = await srv.remember("владелец любит кофе")
    assert result == {"id": "m1", "status": "proposed"}


async def test_mac_tools_registered_for_every_section5_tool():
    tools = {t.name for t in await srv.mcp.list_tools()}
    for name in srv.MAC_TOOLS:
        assert f"mac_{name}" in tools
    assert {"approve", "attach_file", "remember"} <= tools


async def test_mac_delegate_sends_timeout_plus_slack(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tool"] == "delegate"
        assert body["args"] == {"engine": "gemini", "prompt": "x", "timeout": 60}
        assert body["timeout"] == 60 + srv.DELEGATE_HTTP_SLACK
        return httpx.Response(200, json={"ok": True, "data": {
            "engine": "gemini", "output": "done", "seconds": 1.2, "exit_code": 0,
        }})

    _patch_delegate_client(monkeypatch, handler)
    result = await srv._mac_delegate({"engine": "gemini", "prompt": "x", "timeout": 60})
    assert result["data"]["output"] == "done"


async def test_mac_delegate_clamps_timeout_to_max(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["timeout"] == srv.DELEGATE_TIMEOUT_MAX + srv.DELEGATE_HTTP_SLACK
        return httpx.Response(200, json={"ok": True, "data": {}})

    _patch_delegate_client(monkeypatch, handler)
    await srv._mac_delegate({"engine": "codex", "prompt": "x", "timeout": 999999})


async def test_mac_delegate_does_not_retry_on_409(monkeypatch):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(409, json={"error": "mac_unavailable", "state": "offline"})

    _patch_delegate_client(monkeypatch, handler)
    result = await srv._mac_delegate({"engine": "claude", "prompt": "x"})
    assert attempts["n"] == 1
    assert result == {"ok": False, "error": "Mac не в сети, выполни сам"}


async def test_mac_image_tool_returns_image_and_caption_and_attaches_file(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/mac/call":
            return httpx.Response(200, json={
                "jpeg_b64": base64.b64encode(b"IMG").decode(), "width": 100, "height": 50, "bytes": 3,
            })
        assert request.url.path == "/api/files"
        return httpx.Response(200, json={"file_id": "f1"})

    _patch_client(monkeypatch, handler)
    result = await srv._mac_image_tool("screenshot", {})

    assert calls == ["/api/mac/call", "/api/files"]
    assert len(result) == 2
    image, caption = result
    assert isinstance(image, Image)
    assert image.data == b"IMG"
    assert image._mime_type == "image/jpeg"
    assert caption == "screenshot: 100x50, 0 КБ"


async def test_mac_image_tool_survives_attach_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/mac/call":
            return httpx.Response(200, json={
                "jpeg_b64": base64.b64encode(b"IMG").decode(), "width": 10, "height": 10, "bytes": 3,
            })
        return httpx.Response(500, text="boom")

    _patch_client(monkeypatch, handler)
    result = await srv._mac_image_tool("preview", {"path": "/x"})

    assert isinstance(result[0], Image)
    assert result[0].data == b"IMG"


async def test_mac_image_tool_falls_back_to_text_when_mac_offline(monkeypatch):
    monkeypatch.setattr(srv, "MAC_RETRY_INTERVAL", 0.0)
    monkeypatch.setattr(srv, "MAC_RETRY_TIMEOUT", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"state": "offline"})

    _patch_client(monkeypatch, handler)
    result = await srv._mac_image_tool("preview", {"path": "/x"})

    assert result == [str({"state": "offline"})]


async def test_mac_image_tool_handles_real_nested_result_shape(monkeypatch):
    """Живой баг 2026-09-25: ядро отдаёт {"type":"result","ok":true,"data":{...}}, картинка лежит в data."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/mac/call":
            return httpx.Response(200, json={"type": "result", "id": "x", "ok": True, "data": {
                "jpeg_b64": base64.b64encode(b"IMG").decode(), "width": 1440, "height": 900, "bytes": 3}})
        return httpx.Response(200, json={"file_id": "f1"})

    _patch_client(monkeypatch, handler)
    image, caption = await srv._mac_image_tool("screenshot", {})
    assert isinstance(image, Image) and image.data == b"IMG"
    assert caption.startswith("screenshot: 1440x900")
