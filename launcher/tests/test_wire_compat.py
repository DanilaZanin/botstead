"""Клиент ядра (core/bothub/launcher_client.py) против настоящего приложения лаунчера на фейковом Docker.

Проверяет, что формат кадров, коды ошибок и поля двух пакетов не разошлись.
"""
import sys
from pathlib import Path

import httpx
import pytest

CORE = Path(__file__).resolve().parents[2] / "core"
if not (CORE / "bothub" / "launcher_client.py").exists():  # pragma: no cover
    pytest.skip("core/bothub/launcher_client.py не найден", allow_module_level=True)
sys.path.insert(0, str(CORE))

from bothub.launcher_client import (  # noqa: E402
    ExecChunk, ExecExit, ExecStarted, HttpLauncherClient, LauncherAuthError, LauncherConflict, LauncherForbidden,
    LauncherInvalid, LauncherNotFound, run_exec,
)
from bothub_launcher.app import create_app  # noqa: E402
from bothub_launcher.testing import FakeBackend, FakeExec, FakeNetPolicy  # noqa: E402

from .conftest import SECRET  # noqa: E402


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def client(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy(order=backend.order))
    transport = httpx.ASGITransport(app=app)
    return HttpLauncherClient(base_url="http://launcher", secret=SECRET, transport=transport)


async def test_lifecycle(client):
    st = await client.create_bot("scout", "o1")
    assert st.running and st.owner_id == "o1" and st.container == "bot-scout" and st.network == "bothub-u-o1"
    assert (await client.status("scout")).running
    assert [b.bot_id for b in await client.list_bots()] == ["scout"]
    assert (await client.recreate_bot("scout")).running
    assert await client.remove_bot("scout") is True
    assert (await client.status("scout")).exists is False


async def test_exec_roundtrip(client, backend):
    await client.create_bot("scout", "o1")
    backend.next_exec = FakeExec(chunks=[("stdout", b'{"type":"result"}\n'), ("stderr", b"w")], code=2)
    events = [e async for e in client.exec("scout", ["claude", "-p"], env={"BOTHUB_TURN_ID": "t1"}, stdin="привет",
                                           exec_id="t1", timeout=30)]
    assert events == [ExecStarted("t1"), ExecChunk("stdout", b'{"type":"result"}\n'), ExecChunk("stderr", b"w"),
                      ExecExit(2, "exit")]
    assert backend.last_exec.stdin == "привет".encode()


async def test_run_exec_helper(client, backend):
    await client.create_bot("scout", "o1")
    backend.next_exec = FakeExec(chunks=[("stdout", b"a"), ("stderr", b"b")], code=0)
    res = await run_exec(client, "scout", ["id"])
    assert (res.code, res.stdout, res.stderr, res.reason) == (0, b"a", b"b", "exit")


async def test_errors_cross_the_wire(client, backend):
    with pytest.raises(LauncherNotFound):
        await client.recreate_bot("ghost")
    await client.create_bot("scout", "o1")
    with pytest.raises(LauncherConflict):
        await client.create_bot("scout", "o2")
    backend.add_foreign_container("bot-foreign")
    with pytest.raises(LauncherForbidden):
        await client.remove_bot("foreign")
    with pytest.raises(LauncherNotFound):
        async for _ in client.exec("ghost", ["id"]):
            pass


async def test_wrong_secret(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy())
    bad = HttpLauncherClient(base_url="http://launcher", secret="w" * 40, transport=httpx.ASGITransport(app=app))
    with pytest.raises(LauncherAuthError):
        await bad.status("scout")


async def test_server_side_validation_error_is_invalid(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy())
    c = HttpLauncherClient(base_url="http://launcher", secret=SECRET, transport=httpx.ASGITransport(app=app))
    with pytest.raises(LauncherInvalid):
        await c.create_bot("a", "o" * 41)


async def test_login_flow(client, backend):
    st = await client.create_login_container("o1")
    assert st.container == "login-o1" and st.running
    sid = await client.open_login_session("o1", command="claude", cols=90, rows=25)
    backend.last_pty.feed(b"visit https://x")
    backend.last_pty.finish(0)
    await client.login_input(sid, b"abc\r")
    await client.login_resize(sid, 100, 30)
    events = [e async for e in client.login_output(sid)]
    assert events == [ExecChunk("stdout", b"visit https://x"), ExecExit(0, "exit")]
    assert backend.last_pty.written == [b"abc\r"]
    await client.close_login_session(sid)
    assert await client.remove_login_container("o1") is True


async def test_browser_mode_and_tab_cross_the_wire(client, backend):
    await client.create_bot("scout", "o1")
    backend.browser_tabs["bot-scout"] = "https://example.com/page"
    assert (await client.browser_tab("scout")) == {"bot_id": "scout", "mode": "bot", "url": "https://example.com/page"}
    res = await client.browser_mode("scout", "human", url="https://example.com/page")
    assert res == {"bot_id": "scout", "mode": "human", "changed": True, "cookies": None}
    assert backend.browser_modes["bot-scout"] == "human"
    assert (await client.browser_tab("scout"))["url"] is None
    res = await client.browser_mode("scout", "bot")
    assert res["mode"] == "bot" and res["cookies"] == {"merged": 2}


async def test_browser_mode_errors_cross_the_wire(client, backend):
    with pytest.raises(LauncherNotFound):
        await client.browser_mode("ghost", "human")
    with pytest.raises(LauncherNotFound):
        await client.browser_tab("ghost")
    await client.create_bot("scout", "o1")
    with pytest.raises(LauncherInvalid):
        await client.browser_mode("scout", "human", url="javascript:alert(1)")  # server-side check: the client allows any string
    backend.add_foreign_container("bot-foreign")
    with pytest.raises(LauncherForbidden):
        await client.browser_mode("foreign", "human")


def test_core_waits_for_a_mode_switch_longer_than_the_launcher_allows_itself():
    """The launcher caps browser_mode at 45 s and answers with an error; the core must not give up first."""
    from bothub.launcher_client import BROWSER_MODE_TIMEOUT
    from bothub_launcher import service
    assert service.BROWSER_MODE_TOTAL_TIMEOUT == 45.0
    assert BROWSER_MODE_TIMEOUT >= service.BROWSER_MODE_TOTAL_TIMEOUT + 10


async def test_a_mode_switch_that_runs_out_of_time_is_a_launcher_error_on_the_wire(client, backend, monkeypatch):
    from bothub.launcher_client import LauncherServerError
    from bothub_launcher import service
    await client.create_bot("scout", "o1")
    monkeypatch.setattr(service, "BROWSER_MODE_TOTAL_TIMEOUT", 0.3)
    backend.mode_ready_after = 10 ** 9
    with pytest.raises(LauncherServerError):
        await client.browser_mode("scout", "human", url="https://example.com/")


async def test_procedure_step_crosses_the_wire_and_the_secret_stays_in_stdin(client, backend):
    import json
    await client.create_bot("scout", "o1")
    result = {"v": 1, "ok": True, "code": None, "acted": True, "url": "https://example.com/"}
    backend.next_exec = FakeExec(chunks=[("stdout", (json.dumps(result) + "\n").encode())])
    value = "p4ss-SECRET-VALUE"
    payload = json.dumps({"step": {"action": "fill", "value": value}})
    answer = await client.procedure_step("scout", payload, False, 40, exec_id="ps-7")
    assert answer == {"exec_id": "ps-7", "exit_code": 0, "reason": "exit", "result": result}
    sent = json.loads(backend.last_exec.stdin)
    assert sent["dry_run"] is False and sent["deadline_ms"] == 37000 and sent["payload"] == json.loads(payload)
    assert value not in json.dumps([e for e in backend.log])  # ни argv, ни окружения с данными шага
    assert [e for e in backend.log if e[0] == "spawn_procedure_step"] == [("spawn_procedure_step", "bot-scout", "ps-7")]


async def test_procedure_step_errors_cross_the_wire(client, backend):
    with pytest.raises(LauncherNotFound):
        await client.procedure_step("ghost", "{}")
    await client.create_bot("scout", "o1")
    backend.browser_modes["bot-scout"] = "human"
    with pytest.raises(LauncherConflict) as exc:
        await client.procedure_step("scout", "{}")
    assert exc.value.code == "frozen"
    backend.browser_modes["bot-scout"] = "bot"
    with pytest.raises(LauncherInvalid):
        await client.procedure_step("scout", "[1]")  # клиент пропускает любую строку, объект проверяет лаунчер


async def test_procedure_step_cancel_crosses_the_wire(client, backend):
    with pytest.raises(LauncherNotFound):
        await client.procedure_step_cancel("ghost", "ps-1")
    await client.create_bot("scout", "o1")
    await client.procedure_step_cancel("scout", "ps-1")  # шага нет (рестарт лаунчера): сигналы по PID-файлу и метке
    assert [e[3] for e in backend.log if e[0] == "kill_procedure_step"] == ["TERM", "KILL"]
    with pytest.raises(LauncherInvalid):
        await client.procedure_step_cancel("scout", "bad id; rm")


def test_core_waits_for_a_step_longer_than_the_launcher_allows_itself():
    from bothub.launcher_client import (PROCEDURE_PAYLOAD_MAX, PROCEDURE_STEP_GRACE, PROCEDURE_STEP_TIMEOUT_DEFAULT,
                                        PROCEDURE_STEP_TIMEOUT_MAX, PROCEDURE_STEP_TIMEOUT_MIN)
    from bothub_launcher import validation
    assert (PROCEDURE_STEP_TIMEOUT_MIN, PROCEDURE_STEP_TIMEOUT_MAX, PROCEDURE_STEP_TIMEOUT_DEFAULT) == (
        validation.PROCEDURE_TIMEOUT_MIN, validation.PROCEDURE_TIMEOUT_MAX, validation.PROCEDURE_TIMEOUT_DEFAULT)
    assert PROCEDURE_PAYLOAD_MAX == validation.PROCEDURE_PAYLOAD_MAX
    assert PROCEDURE_STEP_GRACE > 0


async def test_ensure_network_roundtrip(client, backend):
    assert await client.ensure_network("o1") == {"owner_id": "o1", "network": "bothub-u-o1", "core_connected": True}
    assert ("ensure_network", "o1") in backend.log
    with pytest.raises(LauncherInvalid):
        await client.ensure_network("o" * 41)
