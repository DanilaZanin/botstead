"""Клиент лаунчера: HTTP-контракт, таймауты, ошибки, стриминг exec, фейк для тестов ядра.

Не требует Postgres: запуск без общего conftest, `cd core && uv run pytest tests/test_launcher_client.py --noconftest`.
"""
import asyncio
import base64
import json

import httpx
import pytest

from bothub.launcher_client import (
    BotStatus, ExecChunk, ExecExit, ExecStarted, FakeLauncherClient, HttpLauncherClient, LauncherAuthError,
    LauncherBusy, LauncherConflict, LauncherError, LauncherForbidden, LauncherInvalid, LauncherNotFound,
    LauncherServerError, LauncherTimeout, LauncherUnavailable, LineSplitter, launcher_client_from_env,
    run_exec,
)

SECRET = "s" * 40
STATUS = {"bot_id": "scout", "exists": True, "running": True, "status": "running", "owner_id": "o1",
          "container": "bot-scout", "network": "bothub-u-o1", "image": "bothub-bot", "exit_code": 0,
          "oom_killed": False, "started_at": "2026-10-04T10:00:00Z", "restart_count": 0}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def ndjson(*frames) -> bytes:
    return ("\n".join(json.dumps(f) for f in frames) + "\n").encode()


def make(handler, **kw) -> HttpLauncherClient:
    return HttpLauncherClient(base_url="http://launcher", secret=SECRET, transport=httpx.MockTransport(handler), **kw)


# ---------- обычные вызовы ----------

async def test_create_bot_sends_secret_and_ids_only():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(method=request.method, path=request.url.path, auth=request.headers["authorization"],
                    body=json.loads(request.content))
        return httpx.Response(201, json=STATUS)

    st = await make(handler).create_bot("scout", "o1")
    assert seen == {"method": "POST", "path": "/v1/bots", "auth": f"Bearer {SECRET}",
                    "body": {"bot_id": "scout", "owner_id": "o1"}}
    assert isinstance(st, BotStatus) and st.running and st.owner_id == "o1" and st.container == "bot-scout"


async def test_status_list_recreate_remove():
    def handler(request: httpx.Request):
        p, m = request.url.path, request.method
        if (m, p) == ("GET", "/v1/bots/scout"):
            return httpx.Response(200, json=STATUS)
        if (m, p) == ("GET", "/v1/bots"):
            return httpx.Response(200, json={"bots": [STATUS]})
        if (m, p) == ("POST", "/v1/bots/scout/recreate"):
            return httpx.Response(200, json=STATUS)
        if (m, p) == ("DELETE", "/v1/bots/scout"):
            assert request.url.params["purge"] == "true"
            return httpx.Response(200, json={"bot_id": "scout", "removed": True})
        raise AssertionError((m, p))

    c = make(handler)
    assert (await c.status("scout")).status == "running"
    assert [b.bot_id for b in await c.list_bots()] == ["scout"]
    assert (await c.recreate_bot("scout")).running
    assert await c.remove_bot("scout", purge=True) is True


async def test_status_of_missing_bot():
    c = make(lambda r: httpx.Response(200, json={"bot_id": "g", "exists": False, "running": False, "status": "missing"}))
    st = await c.status("g")
    assert not st.exists and not st.running and st.owner_id is None


async def test_stop_exec_body():
    seen = {}

    def handler(request):
        seen.update(path=request.url.path, body=json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"stopped": True, "was_running": True})

    await make(handler).stop_exec("turn-1", bot_id="scout")
    assert seen == {"path": "/v1/execs/turn-1/stop", "body": {"bot_id": "scout"}}
    await make(handler).stop_exec("turn-1")
    assert seen["body"] == {}


async def test_login_container_and_session_calls():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/sessions"):
            return httpx.Response(201, json={"session_id": "ab12cd34ef56ab78"})
        if request.url.path.startswith("/v1/logins/") and request.method == "POST":
            return httpx.Response(201, json={**STATUS, "container": "login-o1", "bot_id": None})
        if request.url.path == "/v1/logins/o1":
            return httpx.Response(200, json={"removed": True})
        return httpx.Response(204)

    c = make(handler)
    assert (await c.create_login_container("o1")).container == "login-o1"
    sid = await c.open_login_session("o1", command="claude", cols=90, rows=25)
    assert sid == "ab12cd34ef56ab78"
    await c.login_input(sid, b"ls\r")
    await c.login_resize(sid, 100, 30)
    await c.close_login_session(sid)
    assert await c.remove_login_container("o1") is True
    assert json.loads(calls[1][2]) == {"command": "claude", "cols": 90, "rows": 25}
    assert calls[2] == ("POST", "/v1/login-sessions/ab12cd34ef56ab78/input", b"ls\r")
    assert json.loads(calls[3][2]) == {"cols": 100, "rows": 30}


async def test_login_output_stream():
    body = ndjson({"t": "out", "s": "stdout", "d": b64(b"hi")}, {"t": "exit", "code": 0, "reason": "exit"})
    c = make(lambda r: httpx.Response(200, content=body))
    events = [e async for e in c.login_output("ab12cd34ef56ab78")]
    assert events == [ExecChunk("stdout", b"hi"), ExecExit(0, "exit")]


async def test_screen_output_yields_short_banner_before_stream_ends():
    release = asyncio.Event()

    class HeldStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'RFB 003.008\n'
            await release.wait()

    def handler(request):
        assert request.url.path == '/v1/screen-sessions/ab12cd34ef56ab78/output'
        return httpx.Response(200, stream=HeldStream(), headers={'content-type':'application/octet-stream'})

    client = make(handler)
    output = client.screen_output('ab12cd34ef56ab78')
    try:
        assert await asyncio.wait_for(anext(output), timeout=.5) == b'RFB 003.008\n'
    finally:
        release.set()
        await output.aclose()
        await client.aclose()


async def test_screen_output_splits_large_raw_chunk():
    class LargeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x' * 65537

    client = make(lambda request: httpx.Response(200, stream=LargeStream()))
    try:
        chunks = [chunk async for chunk in client.screen_output('ab12cd34ef56ab78')]
        assert [len(chunk) for chunk in chunks] == [65536, 1]
    finally:
        await client.aclose()


# ---------- локальная валидация ----------

@pytest.mark.parametrize("bot_id,owner", [("x --privileged", "o1"), ("A", "o1"), ("-x", "o1"), ("a", "O"), ("", "o1"), ("a", "o\n")])
async def test_ids_validated_before_any_request(bot_id, owner):
    def handler(request):
        raise AssertionError("запрос не должен уйти")

    with pytest.raises(LauncherInvalid):
        await make(handler).create_bot(bot_id, owner)


@pytest.mark.parametrize("bot_id", ["x --privileged", "A", "-x", "", "a\n", "../x"])
async def test_bot_id_validated_for_every_call(bot_id):
    def handler(request):
        raise AssertionError("запрос не должен уйти")

    c = make(handler)
    for call in (c.status(bot_id), c.recreate_bot(bot_id), c.remove_bot(bot_id), c.stop_exec("t1", bot_id=bot_id)):
        with pytest.raises(LauncherInvalid):
            await call


async def test_exec_args_validated_before_request():
    c = make(lambda r: (_ for _ in ()).throw(AssertionError("не должен уйти")))
    for kwargs in ({"argv": []}, {"argv": ["--privileged"]}, {"argv": ["id"], "env": {"lower": "x"}},
                   {"argv": ["id"], "exec_id": "a b"}, {"argv": ["id"], "timeout": 0}):
        with pytest.raises(LauncherInvalid):
            async for _ in c.exec("scout", **kwargs):
                pass


# ---------- ошибки ----------

@pytest.mark.parametrize("status,code,exc", [
    (400, "invalid", LauncherInvalid), (401, "unauthorized", LauncherAuthError),
    (403, "not_managed", LauncherForbidden), (404, "not_found", LauncherNotFound),
    (409, "conflict", LauncherConflict), (429, "busy", LauncherBusy),
    (502, "docker_error", LauncherServerError), (503, "netpolicy", LauncherServerError),
])
async def test_http_errors_map_to_exceptions(status, code, exc):
    c = make(lambda r: httpx.Response(status, json={"error": {"code": code, "message": "подробности"}}))
    with pytest.raises(exc) as info:
        await c.status("scout")
    assert info.value.code == code and info.value.status == status and "подробности" in str(info.value)
    assert isinstance(info.value, LauncherError)


async def test_non_json_error_body_is_server_error():
    c = make(lambda r: httpx.Response(502, text="<html>Bad gateway</html>"))
    with pytest.raises(LauncherServerError) as info:
        await c.status("scout")
    assert info.value.status == 502


async def test_connect_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LauncherUnavailable):
        await make(handler).status("scout")


async def test_timeout_is_launcher_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LauncherTimeout):
        await make(handler).status("scout")


async def test_secret_never_in_error_text():
    c = make(lambda r: httpx.Response(401, json={"error": {"code": "unauthorized", "message": "bad"}}))
    with pytest.raises(LauncherAuthError) as info:
        await c.status("scout")
    assert SECRET not in str(info.value) and SECRET not in repr(c)


def test_timeouts_are_configured():
    c = HttpLauncherClient(base_url="http://launcher", secret=SECRET, connect_timeout=1, request_timeout=7,
                           stream_idle_timeout=40)
    assert c.request_timeout == httpx.Timeout(7, connect=1)
    assert c.stream_timeout.read == 40 and c.stream_timeout.connect == 1


def test_unix_socket_transport():
    c = HttpLauncherClient(socket_path="/run/bothub-launcher/launcher.sock", secret=SECRET)
    assert c.base_url == "http://launcher"


def test_needs_secret_and_endpoint():
    with pytest.raises(LauncherError):
        HttpLauncherClient(socket_path="/x", secret="")
    with pytest.raises(LauncherError):
        HttpLauncherClient(secret=SECRET)


# ---------- exec ----------

async def test_exec_streams_events():
    seen = {}

    def handler(request):
        seen.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, headers={"X-Exec-Id": "t1"}, content=ndjson(
            {"t": "start", "exec_id": "t1"},
            {"t": "out", "s": "stdout", "d": b64(b'{"a":1}\n')},
            {"t": "ping"},
            {"t": "out", "s": "stderr", "d": b64(b"warn")},
            {"t": "exit", "code": 3, "reason": "exit"},
        ))

    events = [e async for e in make(handler).exec("scout", ["claude", "-p"], env={"BOTHUB_TURN_ID": "t1"},
                                                  stdin="привет", exec_id="t1", timeout=60)]
    assert seen["path"] == "/v1/bots/scout/exec"
    assert seen["body"] == {"argv": ["claude", "-p"], "env": {"BOTHUB_TURN_ID": "t1"}, "stdin": "привет",
                            "exec_id": "t1", "timeout": 60}
    assert events == [ExecStarted("t1"), ExecChunk("stdout", b'{"a":1}\n'), ExecChunk("stderr", b"warn"),
                      ExecExit(3, "exit")]


async def test_exec_omits_optional_fields():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=ndjson({"t": "exit", "code": 0, "reason": "exit"}))

    [_ async for _ in make(handler).exec("scout", ["id"])]
    assert seen["body"] == {"argv": ["id"]}


async def test_exec_http_error_before_stream():
    c = make(lambda r: httpx.Response(404, json={"error": {"code": "not_found", "message": "нет бота"}}))
    with pytest.raises(LauncherNotFound):
        async for _ in c.exec("ghost", ["id"]):
            pass


async def test_exec_error_frame_raises():
    body = ndjson({"t": "start", "exec_id": "t1"}, {"t": "error", "code": "docker_error", "message": "daemon died"})
    with pytest.raises(LauncherServerError) as info:
        async for _ in make(lambda r: httpx.Response(200, content=body)).exec("scout", ["id"]):
            pass
    assert "daemon died" in str(info.value)


async def test_exec_stream_without_exit_frame_is_an_error():
    body = ndjson({"t": "start", "exec_id": "t1"}, {"t": "out", "s": "stdout", "d": b64(b"x")})
    with pytest.raises(LauncherUnavailable):
        async for _ in make(lambda r: httpx.Response(200, content=body)).exec("scout", ["id"]):
            pass


async def test_exec_garbage_lines_are_skipped_not_fatal():
    body = b'not json\n' + ndjson({"t": "exit", "code": 0, "reason": "exit"})
    events = [e async for e in make(lambda r: httpx.Response(200, content=body)).exec("scout", ["id"])]
    assert events == [ExecExit(0, "exit")]


async def test_exec_total_timeout_raises_launcher_timeout():
    class Hang(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield b""

    c = make(lambda r: httpx.Response(200, stream=Hang()), stream_grace=0.05)
    with pytest.raises(LauncherTimeout):
        async for _ in c.exec("scout", ["id"], timeout=0.05):
            pass


async def test_run_exec_collects_everything():
    body = ndjson({"t": "start", "exec_id": "t1"}, {"t": "out", "s": "stdout", "d": b64(b"a")},
                  {"t": "out", "s": "stderr", "d": b64(b"b")}, {"t": "out", "s": "stdout", "d": b64(b"c")},
                  {"t": "exit", "code": 2, "reason": "exit"})
    res = await run_exec(make(lambda r: httpx.Response(200, content=body)), "scout", ["id"])
    assert (res.code, res.stdout, res.stderr, res.reason, res.exec_id) == (2, b"ac", b"b", "exit", "t1")


# ---------- построчное чтение ----------

def test_line_splitter_handles_split_chunks_and_tail():
    s = LineSplitter()
    assert s.feed(b'{"a":') == []
    assert s.feed(b'1}\n{"b":2}\n{"c"') == [b'{"a":1}', b'{"b":2}']
    assert s.feed(b":3}") == []
    assert s.flush() == b'{"c":3}'
    assert s.flush() is None


def test_line_splitter_crlf_and_empty_lines():
    s = LineSplitter()
    assert s.feed(b"a\r\n\nb\n") == [b"a", b"", b"b"]


def test_line_splitter_limit():
    s = LineSplitter(limit=10)
    assert s.feed(b"x" * 11 + b"\n") == [b"xxxxx[cut]"]
    assert s.feed(b"ok\n") == [b"ok"]


def test_line_splitter_large_chunk_is_bounded_and_marked():
    s = LineSplitter(limit=8 * 1024 * 1024)
    assert s.feed(b"x" * (125 * 1024 * 1024)) == []
    tail = s.flush()
    assert len(tail) == 8 * 1024 * 1024 and tail.endswith(b"[cut]")


async def test_non_utf8_stdin_is_launcher_invalid_before_request():
    c = make(lambda r: (_ for _ in ()).throw(AssertionError("request must not be sent")))
    with pytest.raises(LauncherInvalid):
        async for _ in c.exec("scout", ["cat"], stdin=b"\xff"):
            pass


# ---------- фабрика из окружения ----------

def test_from_env_socket():
    c = launcher_client_from_env({"LAUNCHER_SOCKET": "/run/x.sock", "LAUNCHER_SECRET": SECRET})
    assert isinstance(c, HttpLauncherClient)


def test_from_env_url():
    c = launcher_client_from_env({"LAUNCHER_URL": "http://127.0.0.1:9000", "LAUNCHER_SECRET": SECRET})
    assert c.base_url == "http://127.0.0.1:9000"


def test_from_env_not_configured_is_none():
    assert launcher_client_from_env({}) is None


def test_from_env_secret_missing_is_error():
    with pytest.raises(LauncherError):
        launcher_client_from_env({"LAUNCHER_SOCKET": "/run/x.sock"})


# ---------- фейк ----------

async def test_fake_create_status_recreate_remove():
    f = FakeLauncherClient()
    st = await f.create_bot("scout", "o1")
    assert st.running and st.owner_id == "o1"
    assert (await f.create_bot("scout", "o1")).running
    with pytest.raises(LauncherConflict):
        await f.create_bot("scout", "o2")
    assert (await f.status("ghost")).exists is False
    assert [b.bot_id for b in await f.list_bots()] == ["scout"]
    first = f.bots["scout"].generation
    assert (await f.recreate_bot("scout")).running and f.bots["scout"].generation == first + 1
    assert await f.remove_bot("scout") is True and await f.remove_bot("scout") is False
    with pytest.raises(LauncherNotFound):
        await f.recreate_bot("scout")
    assert [c[0] for c in f.calls][:2] == ["create_bot", "create_bot"]


async def test_fake_validates_like_the_real_one():
    f = FakeLauncherClient()
    with pytest.raises(LauncherInvalid):
        await f.create_bot("x --privileged", "o1")
    with pytest.raises(LauncherInvalid):
        async for _ in f.exec("scout", ["--privileged"]):
            pass


async def test_fake_exec_scripted_output():
    f = FakeLauncherClient()
    await f.create_bot("scout", "o1")
    f.script_exec(stdout=[b'{"type":"x"}\n', b"tail"], stderr=[b"w"], code=1)
    res = await run_exec(f, "scout", ["claude"], env={"BOTHUB_TURN_ID": "t"}, stdin="p", exec_id="t1")
    assert (res.code, res.stdout, res.stderr, res.exec_id) == (1, b'{"type":"x"}\ntail', b"w", "t1")
    assert f.execs[-1] == {"bot_id": "scout", "argv": ["claude"], "env": {"BOTHUB_TURN_ID": "t"}, "stdin": "p",
                           "exec_id": "t1", "timeout": None}


async def test_fake_exec_unknown_bot():
    f = FakeLauncherClient()
    with pytest.raises(LauncherNotFound):
        async for _ in f.exec("ghost", ["id"]):
            pass


async def test_fake_stop_exec_ends_hanging_exec():
    f = FakeLauncherClient()
    await f.create_bot("scout", "o1")
    f.script_exec(hang=True)
    events = []

    async def consume():
        async for e in f.exec("scout", ["claude"], exec_id="t1"):
            events.append(e)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await f.stop_exec("t1", bot_id="scout")
    await asyncio.wait_for(task, 2)
    assert events[-1] == ExecExit(143, "stopped")
    assert f.stopped == ["t1"]


async def test_fake_injected_failure():
    f = FakeLauncherClient()
    f.fail_next(LauncherUnavailable("launcher down"))
    with pytest.raises(LauncherUnavailable):
        await f.create_bot("scout", "o1")
    assert (await f.create_bot("scout", "o1")).running


async def test_fake_login_session():
    f = FakeLauncherClient()
    st = await f.create_login_container("o1")
    assert st.container == "login-o1" and st.running
    sid = await f.open_login_session("o1", command="claude")
    f.login_script(sid, [b"Please visit https://example.com\r\n"], code=0)
    await f.login_input(sid, b"code\r")
    await f.login_resize(sid, 100, 30)
    events = [e async for e in f.login_output(sid)]
    assert events == [ExecChunk("stdout", b"Please visit https://example.com\r\n"), ExecExit(0, "exit")]
    assert f.login_inputs[sid] == [b"code\r"]
    await f.close_login_session(sid)
    with pytest.raises(LauncherNotFound):
        await f.login_input(sid, b"x")


async def test_fake_satisfies_protocol_shape():
    names = ["create_bot", "remove_bot", "recreate_bot", "status", "list_bots", "exec", "stop_exec",
             "create_login_container", "remove_login_container", "open_login_session", "login_output",
             "login_input", "login_resize", "close_login_session", "ensure_network", "aclose"]
    http = make(lambda r: httpx.Response(200, json={}))
    for n in names:
        assert callable(getattr(FakeLauncherClient(), n)), n
        assert callable(getattr(http, n)), n

async def test_browser_freeze_http_contract():
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={'bot_id': 'scout', 'running': True, 'frozen': True})
    client = make(handler)
    await client.ensure_browser('scout')
    await client.freeze_bot('scout')
    await client.unfreeze_bot('scout')
    assert seen == [('POST', '/v1/bots/scout/browser'), ('POST', '/v1/bots/scout/freeze'),
                    ('POST', '/v1/bots/scout/unfreeze')]


async def test_fake_browser_freeze_blocks_exec_and_records_calls():
    client = FakeLauncherClient()
    await client.create_bot('scout', 'o1')
    assert (await client.ensure_browser('scout'))['started']
    assert not (await client.ensure_browser('scout'))['started']
    await client.freeze_bot('scout')
    with pytest.raises(LauncherConflict) as exc:
        [event async for event in client.exec('scout', ['true'])]
    assert exc.value.code == 'frozen'
    await client.unfreeze_bot('scout')
    assert isinstance((await run_exec(client, 'scout', ['true'])).code, int)
    assert ('freeze_bot', 'scout') in client.calls
    assert ('unfreeze_bot', 'scout') in client.calls
    assert client.calls.count(('ensure_browser', 'scout')) == 2


# ---------- режим браузера: чистый Chromium человека ----------

async def test_browser_mode_http_contract_and_long_timeout():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content), request.extensions["timeout"]))
        return httpx.Response(200, json={"bot_id": "scout", "mode": "human", "changed": True, "cookies": {"merged": 3}})

    client = make(handler, connect_timeout=4.0, request_timeout=7.0)
    answer = await client.browser_mode("scout", "human", url="https://example.com/a")
    assert answer == {"bot_id": "scout", "mode": "human", "changed": True, "cookies": {"merged": 3}}
    await client.browser_mode("scout", "bot")
    assert [s[:3] for s in seen] == [
        ("POST", "/v1/bots/scout/browser-mode", {"mode": "human", "url": "https://example.com/a"}),
        ("POST", "/v1/bots/scout/browser-mode", {"mode": "bot", "url": None})]
    for _, _, _, timeout in seen:
        assert timeout["read"] == 60.0 and timeout["write"] == 60.0 and timeout["connect"] == 4.0


async def test_browser_mode_rejects_bad_arguments_before_any_request():
    def handler(request):
        raise AssertionError("no request expected")

    client = make(handler)
    for args, kwargs in ((("x --privileged", "bot"), {}), (("scout", "root"), {}), (("scout", None), {}),
                         (("scout", "bot"), {"url": "https://example.com/"}), (("scout", "human"), {"url": 5})):
        with pytest.raises(LauncherInvalid) as exc:
            await client.browser_mode(*args, **kwargs)
        assert exc.value.code == "invalid"
    with pytest.raises(LauncherInvalid):
        await client.browser_tab("x --privileged")


async def test_browser_tab_http_contract():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content, request.extensions["timeout"]["read"]))
        return httpx.Response(200, json={"bot_id": "scout", "mode": "bot", "url": "https://example.com/p?q=1"})

    answer = await make(handler, request_timeout=7.0).browser_tab("scout")
    assert answer == {"bot_id": "scout", "mode": "bot", "url": "https://example.com/p?q=1"}
    assert seen == [("GET", "/v1/bots/scout/browser-tab", b"", 7.0)]


@pytest.mark.parametrize("call", ["browser_mode", "browser_tab"])
async def test_browser_mode_and_tab_map_errors(call):
    async def run(client):
        if call == "browser_mode":
            return await client.browser_mode("scout", "human", url="about:blank")
        return await client.browser_tab("scout")

    for status, exc_type in ((400, LauncherInvalid), (401, LauncherAuthError), (404, LauncherNotFound),
                             (409, LauncherConflict), (500, LauncherServerError)):
        client = make(lambda r, s=status: httpx.Response(s, json={"error": {"code": "c", "message": "m"}}))
        with pytest.raises(exc_type) as exc:
            await run(client)
        assert exc.value.code == "c" and exc.value.status == status

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LauncherUnavailable):
        await run(make(down))

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LauncherTimeout):
        await run(make(slow))
    with pytest.raises(LauncherServerError) as exc:
        await run(make(lambda r: httpx.Response(200, json=[1])))
    assert exc.value.code == "bad_response"


async def test_fake_browser_mode_and_tab_parity():
    f = FakeLauncherClient()
    await f.create_bot("scout", "o1")
    assert f.browser_modes == {} and f.tab_urls == {}
    f.tab_urls["scout"] = "https://example.com/page"
    assert await f.browser_tab("scout") == {"bot_id": "scout", "mode": "bot", "url": "https://example.com/page"}
    assert await f.browser_mode("scout", "human", url="https://example.com/page") == {
        "bot_id": "scout", "mode": "human", "changed": True, "cookies": None}
    assert (await f.browser_mode("scout", "human"))["changed"] is False, "idempotent"
    assert f.browser_modes["scout"] == "human"
    assert await f.browser_tab("scout") == {"bot_id": "scout", "mode": "human", "url": None}
    assert (await f.browser_mode("scout", "bot"))["changed"] is True
    assert f.browser_modes["scout"] == "bot"
    assert f.calls[1:] == [("browser_tab", "scout"), ("browser_mode", "scout", "human", "https://example.com/page"),
                           ("browser_mode", "scout", "human", None), ("browser_tab", "scout"),
                           ("browser_mode", "scout", "bot", None)]
    # the browser stays usable for a human takeover
    assert (await f.ensure_browser("scout"))["running"]


async def test_fake_browser_mode_and_tab_validate_and_fail_like_the_real_one():
    f = FakeLauncherClient()
    for args, kwargs in ((("x --privileged", "bot"), {}), (("scout", "root"), {}),
                         (("scout", "bot"), {"url": "https://example.com/"})):
        with pytest.raises(LauncherInvalid) as exc:
            await f.browser_mode(*args, **kwargs)
        assert exc.value.code == "invalid"
    with pytest.raises(LauncherInvalid):
        await f.browser_tab("x --privileged")
    with pytest.raises(LauncherNotFound):
        await f.browser_mode("ghost", "bot")
    with pytest.raises(LauncherNotFound):
        await f.browser_tab("ghost")
    await f.create_bot("scout", "o1")
    f.fail_next(LauncherUnavailable("down"))
    with pytest.raises(LauncherUnavailable):
        await f.browser_mode("scout", "human")
    assert "scout" not in f.browser_modes, "a failed call changes nothing"
    f.fail_next(LauncherUnavailable("down"))
    with pytest.raises(LauncherUnavailable):
        await f.browser_tab("scout")


async def test_fake_and_http_both_have_browser_mode_and_tab():
    http = make(lambda r: httpx.Response(200, json={}))
    for name in ("browser_mode", "browser_tab"):
        assert callable(getattr(FakeLauncherClient(), name)) and callable(getattr(http, name))


# ---------- шаг процедуры (procedure_step) ----------

STEP_VALUE = "p4ss-SECRET-VALUE"
STEP_PAYLOAD = json.dumps({"step": {"action": "fill", "value": STEP_VALUE}})
STEP_RESULT = {"v": 1, "ok": True, "code": None, "acted": True}


async def test_procedure_step_http_contract_timeout_and_answer():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content), request.extensions["timeout"]))
        return httpx.Response(200, json={"bot_id": "scout", "exec_id": "ps-1", "exit_code": 0, "reason": "exit",
                                         "result": STEP_RESULT})

    client = make(handler, connect_timeout=4.0, request_timeout=7.0)
    answer = await client.procedure_step("scout", STEP_PAYLOAD, True, 25, exec_id="ps-1")
    assert answer == {"exec_id": "ps-1", "exit_code": 0, "reason": "exit", "result": STEP_RESULT}
    await client.procedure_step("scout", STEP_PAYLOAD)
    assert seen[0][:3] == ("POST", "/v1/bots/scout/procedure-step",
                           {"payload_json": STEP_PAYLOAD, "dry_run": True, "timeout": 25.0, "exec_id": "ps-1"})
    assert seen[1][2] == {"payload_json": STEP_PAYLOAD, "dry_run": False, "timeout": 30.0}
    assert seen[0][3]["read"] == 45.0 and seen[0][3]["connect"] == 4.0  # срок шага плюс запас: лаунчер отвечает сам


async def test_procedure_step_checks_arguments_before_any_request_and_never_quotes_the_payload():
    def handler(request):
        raise AssertionError("no request expected")

    client = make(handler)
    bad = [("scout", 5), ("scout", "x" * (256 * 1024 + 1)), ("scout", STEP_PAYLOAD, "yes"), ("scout", STEP_PAYLOAD, False, 1),
           ("scout", STEP_PAYLOAD, False, 999), ("scout", STEP_PAYLOAD, False, True), ("scout", STEP_PAYLOAD, False, "30"),
           ("a b", STEP_PAYLOAD)]
    for args in bad:
        with pytest.raises(LauncherInvalid) as exc:
            await client.procedure_step(*args)
        assert STEP_VALUE not in str(exc.value)
    with pytest.raises(LauncherInvalid):
        await client.procedure_step("scout", STEP_PAYLOAD, exec_id="bad id")


@pytest.mark.parametrize("answer", [{"exit_code": "0", "result": None}, {"exit_code": True, "result": None},
                                    {"exit_code": 0, "result": [1]}, {"result": {}}])
async def test_procedure_step_rejects_malformed_launcher_answers(answer):
    client = make(lambda request: httpx.Response(200, json=answer))
    with pytest.raises(LauncherServerError) as exc:
        await client.procedure_step("scout", STEP_PAYLOAD)
    assert exc.value.code == "bad_response"


async def test_procedure_step_maps_launcher_errors():
    for status, code, cls in ((409, "frozen", LauncherConflict), (404, "not_found", LauncherNotFound), (429, "busy", LauncherBusy),
                              (403, "not_managed", LauncherForbidden), (400, "invalid", LauncherInvalid)):
        client = make(lambda request, s=status, c=code: httpx.Response(s, json={"error": {"code": c, "message": "m"}}))
        with pytest.raises(cls) as exc:
            await client.procedure_step("scout", STEP_PAYLOAD)
        assert exc.value.code == code


async def test_fake_procedure_step_records_the_call_and_follows_scripts_and_state():
    fake = FakeLauncherClient()
    await fake.create_bot("scout", "o1")
    answer = await fake.procedure_step("scout", STEP_PAYLOAD, True, 20, exec_id="ps-1")
    assert answer["exec_id"] == "ps-1" and answer["result"]["ok"] is True and answer["result"]["acted"] is False
    assert fake.procedure_steps == [{"bot_id": "scout", "payload": json.loads(STEP_PAYLOAD), "dry_run": True, "timeout": 20.0,
                                     "exec_id": "ps-1"}]
    fake.procedure_handler = lambda payload, dry_run: (1, "exit", None)
    assert (await fake.procedure_step("scout", STEP_PAYLOAD))["result"] is None
    fake.procedure_handler = lambda payload, dry_run: LauncherTimeout("slow", code="timeout")
    with pytest.raises(LauncherTimeout):
        await fake.procedure_step("scout", STEP_PAYLOAD)
    fake.procedure_handler = None
    fake.browser_modes["scout"] = "human"
    with pytest.raises(LauncherConflict) as exc:
        await fake.procedure_step("scout", STEP_PAYLOAD)
    assert exc.value.code == "frozen"
    fake.browser_modes["scout"] = "bot"
    fake.frozen.add("scout")
    with pytest.raises(LauncherConflict):
        await fake.procedure_step("scout", STEP_PAYLOAD)
    with pytest.raises(LauncherNotFound):
        await fake.procedure_step("ghost", STEP_PAYLOAD)
    with pytest.raises(LauncherInvalid):
        await fake.procedure_step("scout", "[]" * 200000)


# ---------- сеть пользователя ----------

async def test_ensure_network_http_contract_and_validation():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content, request.extensions["timeout"]["read"]))
        return httpx.Response(200, json={"owner_id": "o1", "network": "bothub-u-o1", "core_connected": True})

    answer = await make(handler, request_timeout=7.0).ensure_network("o1")
    assert answer == {"owner_id": "o1", "network": "bothub-u-o1", "core_connected": True}
    assert seen == [("POST", "/v1/networks/o1", b"", 60.0)]  # docker network connect медленный: отдельный срок, не 7 секунд
    with pytest.raises(LauncherInvalid):
        await make(handler).ensure_network("o1; reboot")


async def test_fake_ensure_network_is_recorded_and_can_fail_or_wait():
    f = FakeLauncherClient()
    assert await f.ensure_network("o1") == {"owner_id": "o1", "network": "bothub-u-o1", "core_connected": True}
    assert f.networks == {"o1"} and f.calls == [("ensure_network", "o1")]
    f.fail_next(LauncherUnavailable("launcher down"))
    with pytest.raises(LauncherUnavailable):
        await f.ensure_network("o2")
    with pytest.raises(LauncherInvalid):
        await f.ensure_network("o" * 41)
