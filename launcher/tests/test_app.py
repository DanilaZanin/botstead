"""HTTP-слой: аутентификация по секрету, коды ошибок, NDJSON-стрим, отказ при старте без прав."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from bothub_launcher.app import AuthMiddleware, MAX_BODY, create_app
from bothub_launcher.errors import NetPolicyError
from bothub_launcher.testing import FakeBackend, FakeExec, FakeNetPolicy, FakeStream

from .conftest import SECRET

AUTH = {"Authorization": f"Bearer {SECRET}"}


async def test_chunked_body_limit_rejects_before_app():
    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    messages = iter([{"type": "http.request", "body": b"x" * MAX_BODY, "more_body": True},
                     {"type": "http.request", "body": b"x", "more_body": False}])

    async def receive():
        return next(messages)

    sent = []

    async def send(message):
        sent.append(message)

    await AuthMiddleware(inner, SECRET)({"type": "http", "path": "/v1/bots", "method": "POST",
                                         "headers": [(b"authorization", f"Bearer {SECRET}".encode())]},
                                        receive, send)
    assert sent[0]["status"] == 413 and not called


async def test_many_small_chunks_replayed_as_one_bounded_body():
    received = []

    async def inner(scope, receive, send):
        received.append(await receive())

    remaining = 10000

    async def receive():
        nonlocal remaining
        remaining -= 1
        return {"type": "http.request", "body": b"x", "more_body": remaining > 0}

    async def send(message):
        raise AssertionError("unexpected response")

    await AuthMiddleware(inner, SECRET)({"type": "http", "path": "/v1/bots", "method": "POST",
                                         "headers": [(b"authorization", f"Bearer {SECRET}".encode())]},
                                        receive, send)
    assert len(received) == 1 and len(received[0]["body"]) == 10000
    assert received[0]["more_body"] is False


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def client(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy(order=backend.order))
    with TestClient(app) as c:
        yield c


def frames(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def err(resp):
    return resp.json()["error"]


# ---------- аутентификация ----------

def test_health_needs_no_secret(client):
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {SECRET}"}, {"Authorization": SECRET},
    {"Authorization": "Bearer "}, {"X-Launcher-Secret": SECRET},
])
def test_everything_else_requires_the_secret(client, headers):
    for method, path in [("get", "/v1/bots"), ("get", "/v1/bots/a"), ("post", "/v1/bots"), ("delete", "/v1/bots/a"),
                         ("post", "/v1/bots/a/recreate"), ("post", "/v1/bots/a/exec"), ("post", "/v1/execs/x/stop"),
                         ("post", "/v1/bots/a/screen-sessions"), ("get", "/v1/screen-sessions/x/output"),
                         ("post", "/v1/screen-sessions/x/input"), ("delete", "/v1/screen-sessions/x"),
                         ("post", "/v1/logins/o1"), ("post", "/v1/logins/o1/sessions"),
                         ("get", "/v1/login-sessions/abc/output"), ("get", "/v1/info"),
                         ("post", "/v1/bots/a/browser"), ("post", "/v1/bots/a/freeze"),
                         ("post", "/v1/bots/a/unfreeze"), ("post", "/v1/bots/a/browser-mode"),
                         ("get", "/v1/bots/a/browser-tab")]:
        r = getattr(client, method)(path, headers=headers)
        assert r.status_code == 401, (method, path)
        assert err(r)["code"] == "unauthorized"


def test_secret_in_query_string_is_not_accepted(client):
    assert client.get(f"/v1/bots?secret={SECRET}").status_code == 401


def test_info_reports_policy_state(client):
    r = client.get("/v1/info", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["image"] == "bothub-bot" and body["runtime"] is None and "secret" not in json.dumps(body)


# ---------- боты ----------

def test_create_status_list_recreate_remove(client):
    r = client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    assert r.status_code == 201 and r.json()["running"] and r.json()["owner_id"] == "o1"
    assert client.get("/v1/bots/scout", headers=AUTH).json()["status"] == "running"
    assert [b["bot_id"] for b in client.get("/v1/bots", headers=AUTH).json()["bots"]] == ["scout"]
    assert client.post("/v1/bots/scout/recreate", headers=AUTH).json()["running"]
    r = client.delete("/v1/bots/scout?purge=true", headers=AUTH)
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.get("/v1/bots/scout", headers=AUTH).json()["exists"] is False


@pytest.mark.parametrize("body", [
    {"bot_id": "x --privileged", "owner_id": "o1"}, {"bot_id": "a", "owner_id": "O"}, {"bot_id": "a"}, {},
    {"bot_id": "a", "owner_id": "o1", "image": "evil"}, {"bot_id": "a", "owner_id": "o1", "network": "host"},
    {"bot_id": 1, "owner_id": "o1"},
    {"bot_id": "a", "owner_id": "o1", "seccomp_profile": "/dev/null"},
    {"bot_id": "a", "owner_id": "o1", "security_opt": ["seccomp=unconfined"]},
])
def test_create_bot_rejects_bad_or_extra_fields(client, backend, body):
    r = client.post("/v1/bots", json=body, headers=AUTH)
    assert r.status_code == 400 and err(r)["code"] == "invalid"
    assert not [e for e in backend.log if e[0] == "run_bot"]


def test_malformed_json_is_400(client):
    r = client.post("/v1/bots", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400 and err(r)["code"] == "invalid"


def test_invalid_path_ids_are_400(client):
    assert client.get("/v1/bots/..%2Fx", headers=AUTH).status_code in (400, 404)
    r = client.get("/v1/bots/A", headers=AUTH)
    assert r.status_code == 400 and err(r)["code"] == "invalid"
    r = client.post("/v1/bots/-x/recreate", headers=AUTH)
    assert r.status_code == 400


def test_error_codes(client, backend):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    r = client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o2"}, headers=AUTH)
    assert r.status_code == 409 and err(r)["code"] == "conflict"
    backend.add_foreign_container("bot-foreign")
    r = client.delete("/v1/bots/foreign", headers=AUTH)
    assert r.status_code == 403 and err(r)["code"] == "not_managed"
    r = client.post("/v1/bots/ghost/recreate", headers=AUTH)
    assert r.status_code == 404 and err(r)["code"] == "not_found"


# ---------- exec ----------

def test_exec_streams_ndjson(client, backend):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    backend.next_exec = FakeExec(chunks=[("stdout", b"line1\n"), ("stderr", b"e")], code=0)
    r = client.post("/v1/bots/scout/exec", json={"argv": ["claude", "-p"], "env": {"BOTHUB_TURN_ID": "t1"},
                                                  "stdin": "hi", "exec_id": "t1", "timeout": 30}, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["x-exec-id"] == "t1"
    fs = frames(r)
    assert fs[0] == {"t": "start", "exec_id": "t1"}
    assert base64.b64decode(fs[1]["d"]) == b"line1\n" and fs[1]["s"] == "stdout"
    assert fs[-1] == {"t": "exit", "code": 0, "reason": "exit"}
    assert backend.last_exec.stdin == b"hi"


@pytest.mark.parametrize("body", [
    {}, {"argv": "claude"}, {"argv": []}, {"argv": ["--privileged"]}, {"argv": ["id"], "env": {"LD_PRELOAD": "x"}},
    {"argv": ["id"], "user": "root"}, {"argv": ["id"], "privileged": True}, {"argv": ["id"], "workdir": "/"},
])
def test_exec_validation_is_400_and_does_not_spawn(client, backend, body):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    r = client.post("/v1/bots/scout/exec", json=body, headers=AUTH)
    assert r.status_code == 400
    assert not [e for e in backend.log if e[0] == "spawn_exec"]


def test_exec_errors_arrive_as_http_status_before_stream(client, backend):
    assert client.post("/v1/bots/ghost/exec", json={"argv": ["id"]}, headers=AUTH).status_code == 404
    backend.add_foreign_container("bot-foreign")
    assert client.post("/v1/bots/foreign/exec", json={"argv": ["id"]}, headers=AUTH).status_code == 403


def test_stop_exec(client, backend):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    r = client.post("/v1/execs/old-turn/stop", json={"bot_id": "scout"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["stopped"] is True
    assert client.post("/v1/execs/unknown/stop", headers=AUTH).status_code == 404
    assert client.post("/v1/execs/a b/stop", headers=AUTH).status_code in (400, 404)


def test_screen_session_roundtrip_streams_raw_rfb_and_accepts_raw_input(client, backend):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    backend.next_screen = FakeStream(chunks=[b"RFB 003.008\n", b"\x00frame"])
    response = client.post("/v1/bots/scout/screen-sessions", json={"owner_id": "o1"}, headers=AUTH)
    assert response.status_code == 201
    session_id = response.json()["session_id"]
    assert client.post(f"/v1/screen-sessions/{session_id}/input", content=b"\x04key", headers=AUTH).status_code == 204
    assert backend.last_screen.written == [b"\x04key"]
    backend.last_screen.finish(0)
    stream = client.get(f"/v1/screen-sessions/{session_id}/output", headers=AUTH)
    assert stream.status_code == 200 and stream.headers["content-type"] == "application/octet-stream"
    assert stream.content == b"RFB 003.008\n\x00frame"
    assert client.delete(f"/v1/screen-sessions/{session_id}", headers=AUTH).status_code == 204


def test_screen_session_requires_owner_and_single_slot(client):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    wrong_owner = client.post("/v1/bots/scout/screen-sessions", json={"owner_id": "o2"}, headers=AUTH)
    assert wrong_owner.status_code == 403 and err(wrong_owner)["code"] == "not_managed"
    opened = client.post("/v1/bots/scout/screen-sessions", json={"owner_id": "o1"}, headers=AUTH)
    assert opened.status_code == 201
    duplicate = client.post("/v1/bots/scout/screen-sessions", json={"owner_id": "o1"}, headers=AUTH)
    assert duplicate.status_code == 409 and err(duplicate)["code"] == "conflict"


def test_screen_input_limit_is_413(client):
    response = client.post("/v1/screen-sessions/abcdef12/input", content=b"x" * (64 * 1024 + 1), headers=AUTH)
    assert response.status_code == 413 and err(response)["code"] == "invalid"


# ---------- логин ----------

def test_login_container_and_session(client, backend):
    r = client.post("/v1/logins/o1", headers=AUTH)
    assert r.status_code == 201 and r.json()["container"] == "login-o1"
    r = client.post("/v1/logins/o1/sessions", json={"command": "claude", "cols": 90, "rows": 25}, headers=AUTH)
    assert r.status_code == 201
    sid = r.json()["session_id"]
    pty = backend.last_pty
    pty.feed(b"Welcome")
    pty.finish(0)
    assert client.post(f"/v1/login-sessions/{sid}/input", content=b"ls\r", headers=AUTH).status_code == 204
    assert client.post(f"/v1/login-sessions/{sid}/resize", json={"cols": 100, "rows": 30}, headers=AUTH).status_code == 204
    out = client.get(f"/v1/login-sessions/{sid}/output", headers=AUTH)
    fs = frames(out)
    assert base64.b64decode(fs[0]["d"]) == b"Welcome" and fs[-1]["t"] == "exit"
    assert pty.written == [b"ls\r"] and pty.size == (100, 30)
    assert client.delete(f"/v1/login-sessions/{sid}", headers=AUTH).status_code == 204
    assert client.delete("/v1/logins/o1", headers=AUTH).status_code == 200


def test_login_rejects_unknown_command_and_extra_fields(client):
    r = client.post("/v1/logins/o1/sessions", json={"command": "rm -rf /"}, headers=AUTH)
    assert r.status_code == 400
    r = client.post("/v1/logins/o1/sessions", json={"argv": ["sh"]}, headers=AUTH)
    assert r.status_code == 400


# ---------- старт ----------

def test_startup_fails_loudly_without_network_rights(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy(check_error=NetPolicyError("нет прав NET_ADMIN")))
    with pytest.raises(NetPolicyError):
        with TestClient(app):
            pass


def test_requests_do_not_work_before_startup_succeeds(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy(check_error=NetPolicyError("x")))
    with pytest.raises(NetPolicyError):
        with TestClient(app) as c:
            c.get("/v1/health")


def test_freeze_state_failure_is_a_clean_json_error(cfg, tmp_path):
    import dataclasses
    (tmp_path / "frozen-bots").write_text("file, not a directory")
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / "launcher.sock"))
    backend = FakeBackend()
    with TestClient(create_app(cfg, backend=backend, netpolicy=FakeNetPolicy(order=backend.order))) as client:
        assert client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH).status_code == 201
        response = client.post("/v1/bots/scout/freeze", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "state_error"
    assert "Traceback" not in response.text


# ---------- режим браузера ----------

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {SECRET}"}])
def test_browser_mode_routes_require_the_secret(client, headers):
    assert client.post("/v1/bots/a/browser-mode", json={"mode": "human"}, headers=headers).status_code == 401
    assert client.get("/v1/bots/a/browser-tab", headers=headers).status_code == 401


def test_browser_mode_roundtrip_over_http(client, backend):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    backend.browser_tabs["bot-scout"] = "https://example.com/page"
    r = client.get("/v1/bots/scout/browser-tab", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"bot_id": "scout", "mode": "bot", "url": "https://example.com/page"}
    r = client.post("/v1/bots/scout/browser-mode", json={"mode": "human", "url": "https://example.com/page"},
                    headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"bot_id": "scout", "mode": "human", "changed": True, "cookies": None}
    assert client.post("/v1/bots/scout/browser-mode", json={"mode": "human", "url": "https://example.com/page"},
                       headers=AUTH).json()["changed"] is False
    assert client.get("/v1/bots/scout/browser-tab", headers=AUTH).json()["url"] is None
    r = client.post("/v1/bots/scout/browser-mode", json={"mode": "bot"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["cookies"] == {"merged": 2}


@pytest.mark.parametrize("body", [
    {}, {"mode": "root"}, {"mode": "human", "url": "javascript:alert(1)"}, {"mode": "human", "url": "--no-sandbox"},
    {"mode": "bot", "url": "https://example.com/"}, {"mode": "human", "extra": 1}, {"mode": 1},
    {"mode": "human", "url": "file:///etc/passwd"},
])
def test_browser_mode_rejects_bad_bodies_before_touching_docker(client, backend, body):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    r = client.post("/v1/bots/scout/browser-mode", json=body, headers=AUTH)
    assert r.status_code == 400 and err(r)["code"] == "invalid"
    assert not [e for e in backend.log if e[0] == "set_browser_mode"]


def test_browser_mode_errors_use_the_launcher_error_format(client, backend):
    r = client.post("/v1/bots/ghost/browser-mode", json={"mode": "human"}, headers=AUTH)
    assert r.status_code == 404 and err(r)["code"] == "not_found"
    assert client.get("/v1/bots/ghost/browser-tab", headers=AUTH).status_code == 404
    assert client.post("/v1/bots/-x/browser-mode", json={"mode": "bot"}, headers=AUTH).status_code in (400, 404)
    backend.add_foreign_container("bot-foreign")
    r = client.post("/v1/bots/foreign/browser-mode", json={"mode": "human"}, headers=AUTH)
    assert r.status_code == 403 and err(r)["code"] == "not_managed"


# ---------- сеть пользователя ----------

def test_owner_network_route_needs_the_secret_and_answers_200(client, backend):
    assert client.post("/v1/networks/o1").status_code == 401
    r = client.post("/v1/networks/o1", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"owner_id": "o1", "network": "bothub-u-o1", "core_connected": True}
    assert ("ensure_network", "o1") in backend.log
    assert client.post("/v1/networks/o1%3B%20reboot", headers=AUTH).status_code == 400
