"""Сервис лаунчера на фейковом backend: метки, владельцы, exec, логин-терминал."""
import asyncio
import base64
import dataclasses
import os
import stat

import pytest

from bothub_launcher.errors import (BackendError, Busy, Conflict, NetPolicyError, NotFound, NotManaged, StateError,
                                    ValidationFailed)
from bothub_launcher.service import Launcher
from bothub_launcher.backend import NetworkInfo
from bothub_launcher.testing import FakeBackend, FakeExec, FakeNetPolicy, FakeStream


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def netpolicy(backend):
    return FakeNetPolicy(order=backend.order)  # общий журнал: видно порядок «правила, потом контейнер»


@pytest.fixture
def launcher(cfg, backend, netpolicy):
    return Launcher(cfg, backend, netpolicy)


def ops(backend, name):
    return [e for e in backend.log if e[0] == name]


async def collect(launcher, handle):
    return [f async for f in launcher.stream_exec(handle)]


def out_text(frames, stream="stdout"):
    return b"".join(base64.b64decode(f["d"]) for f in frames if f["t"] == "out" and f["s"] == stream)


# ---------- старт ----------

async def test_startup_checks_then_reconciles(launcher, backend, netpolicy):
    await backend.ensure_network("o1")
    await launcher.startup()
    assert netpolicy.events[0] == "check"
    assert netpolicy.events[1][0] == "reconcile" and netpolicy.events[1][1] == ["bothub-u-o1"]


async def test_startup_detects_docker_version_once_before_anything_else(launcher, backend, monkeypatch):
    calls = []

    async def detect():
        calls.append(list(backend.order))

    monkeypatch.setattr(backend, "detect_docker_version", detect)
    await launcher.startup()
    assert calls == [[]]


async def test_startup_fails_loudly_without_rights(cfg, backend):
    np = FakeNetPolicy(check_error=NetPolicyError("нет NET_ADMIN"))
    with pytest.raises(NetPolicyError):
        await Launcher(cfg, backend, np).startup()


async def test_startup_restarts_stopped_bots_only_after_rules(launcher, backend):
    await backend.ensure_network("o1")
    await backend.run_bot("scout", "o1")
    backend.containers["bot-scout"] = dataclasses.replace(backend.containers["bot-scout"], running=False)
    await launcher.startup()
    assert backend.containers["bot-scout"].running
    assert backend.order.index("reconcile") < backend.order.index("disable_restart") < backend.order.index("start_container")


async def test_startup_disables_restart_for_running_legacy_managed_bot(launcher, backend):
    await backend.run_bot("scout", "o1")
    await launcher.startup()
    assert ("disable_restart", "bot-scout") in backend.log
    assert not ops(backend, "start_container")


async def test_browser_failure_does_not_block_startup_but_blocks_exec(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    backend.browser_running_names.clear()

    async def browser_down(container):
        raise BackendError('chromium failed')

    backend.start_browser = browser_down
    await launcher.startup()
    with pytest.raises(BackendError, match='chromium failed'):
        await launcher.start_exec('scout', ['true'])
    with pytest.raises(BackendError, match='chromium failed'):
        await launcher.ensure_browser('scout')


async def test_browser_failure_does_not_block_bot_creation_but_blocks_exec(launcher, backend):
    async def browser_down(container):
        raise BackendError('chromium failed')

    backend.start_browser = browser_down
    assert (await launcher.create_bot('scout', 'o1'))['running']
    with pytest.raises(BackendError, match='chromium failed'):
        await launcher.start_exec('scout', ['true'])
    with pytest.raises(BackendError, match='chromium failed'):
        await launcher.ensure_browser('scout')


async def test_legacy_bot_requires_recreate_for_browser_and_takeover(launcher, backend):
    await backend.run_bot('scout', 'o1')
    info = backend.containers['bot-scout']
    backend.containers['bot-scout'] = dataclasses.replace(info, labels={
        key: value for key, value in info.labels.items() if key != 'bothub.browser_isolation'})
    await launcher.startup()
    assert not ops(backend, 'start_browser')
    with pytest.raises(Conflict, match='recreate'):
        await launcher.ensure_browser('scout')
    with pytest.raises(Conflict, match='recreate'):
        await launcher.freeze_bot('scout')
    with pytest.raises(Conflict, match='recreate'):
        await launcher.start_exec('scout', ['true'])
    await launcher.recreate_bot('scout')
    assert backend.containers['bot-scout'].labels['bothub.browser_isolation'] == '2'
    assert ops(backend, 'start_browser')
    handle = await launcher.start_exec('scout', ['true'])
    await collect(launcher, handle)


async def test_startup_does_not_start_bots_when_rules_fail(cfg, backend):
    await backend.run_bot("scout", "o1")
    backend.containers["bot-scout"] = dataclasses.replace(backend.containers["bot-scout"], running=False)
    with pytest.raises(NetPolicyError):
        await Launcher(cfg, backend, FakeNetPolicy(reconcile_error=NetPolicyError("v6 failed"))).startup()
    assert not backend.containers["bot-scout"].running


async def test_startup_refuses_existing_ipv6_network_before_bot_start(launcher, backend):
    await backend.run_bot("scout", "o1")
    backend.containers["bot-scout"] = dataclasses.replace(backend.containers["bot-scout"], running=False)
    backend.networks["bothub-u-o1"] = NetworkInfo(
        name="bothub-u-o1", labels={"bothub.managed": "1", "bothub.owner_id": "o1"},
        bridge="bhuaaaaaaaaaa", core_ip="172.20.0.2", ipv6_enabled=True)
    with pytest.raises(NetPolicyError, match="IPv6"):
        await launcher.startup()
    assert any(event[0] == "reconcile" for event in launcher.netpolicy.events if isinstance(event, tuple))
    assert not ops(backend, "start_container")


# ---------- create / remove / recreate ----------

async def test_create_bot_orders_network_policy_before_container(launcher, backend, netpolicy):
    st = await launcher.create_bot("scout", "o1")
    assert st["exists"] and st["running"] and st["owner_id"] == "o1" and st["container"] == "bot-scout"
    kinds = [e[0] for e in backend.log]
    assert kinds.index("ensure_network") < kinds.index("run_bot")
    assert netpolicy.events[-1][0] == "reconcile", "правила применены до запуска контейнера"
    assert backend.order.index("reconcile") < backend.order.index("run_bot")
    assert ("ensure_volume", "bothub-login-o1", "o1", "login") in backend.log
    assert ("ensure_volume", "bot-scout-home", "o1", "home") in backend.log


async def test_create_bot_is_idempotent(launcher, backend):
    await launcher.create_bot("scout", "o1")
    st = await launcher.create_bot("scout", "o1")
    assert st["running"]
    assert len(ops(backend, "run_bot")) == 1


async def test_create_bot_starts_an_existing_stopped_container_by_recreating(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.containers["bot-scout"] = dataclasses.replace(backend.containers["bot-scout"], running=False, status="exited")
    st = await launcher.create_bot("scout", "o1")
    assert st["running"]
    assert len(ops(backend, "run_bot")) == 2


async def test_create_bot_with_other_owner_conflicts(launcher, backend):
    await launcher.create_bot("scout", "o1")
    with pytest.raises(Conflict):
        await launcher.create_bot("scout", "o2")
    assert len(ops(backend, "run_bot")) == 1


async def test_create_bot_refuses_unlabelled_container(launcher, backend):
    backend.add_foreign_container("bot-scout")
    with pytest.raises(NotManaged):
        await launcher.create_bot("scout", "o1")
    assert not ops(backend, "run_bot")


async def test_create_bot_validates_ids(launcher, backend):
    for bot_id, owner in [("x --privileged", "o"), ("a", "o\n"), ("-a", "o"), ("a", "O")]:
        with pytest.raises(ValidationFailed):
            await launcher.create_bot(bot_id, owner)
    assert not backend.log


async def test_remove_bot_keeps_home_volume_by_default(launcher, backend):
    await launcher.create_bot("scout", "o1")
    res = await launcher.remove_bot("scout")
    assert res["removed"] is True
    assert "bot-scout" not in backend.containers
    assert not ops(backend, "remove_volume")


async def test_remove_bot_purge_removes_only_its_home(launcher, backend):
    await launcher.create_bot("scout", "o1")
    await launcher.remove_bot("scout", purge=True)
    assert ops(backend, "remove_volume") == [("remove_volume", "bot-scout-home", "o1", "home"),
                                               ("remove_volume", "bot-scout-browser", "o1", "browser")]


async def test_remove_bot_missing_is_not_an_error(launcher):
    assert (await launcher.remove_bot("ghost"))["removed"] is False


async def test_remove_refuses_container_without_label(launcher, backend):
    backend.add_foreign_container("bot-scout")
    with pytest.raises(NotManaged):
        await launcher.remove_bot("scout")
    assert not ops(backend, "remove_container")


async def test_remove_refuses_container_with_label_but_other_role(launcher, backend):
    await launcher.create_bot("scout", "o1")
    c = backend.containers["bot-scout"]
    backend.containers["bot-scout"] = dataclasses.replace(c, labels={**c.labels, "bothub.role": "login"})
    with pytest.raises(NotManaged):
        await launcher.remove_bot("scout")


async def test_recreate_keeps_volume_and_owner(launcher, backend):
    await launcher.create_bot("scout", "o1")
    old_id = backend.containers["bot-scout"].id
    st = await launcher.recreate_bot("scout")
    assert st["owner_id"] == "o1" and st["running"]
    assert backend.containers["bot-scout"].id != old_id
    assert not ops(backend, "remove_volume")
    assert ("run_bot", "scout", "o1") == ops(backend, "run_bot")[-1]
    kinds = [e[0] for e in backend.log]
    assert kinds.index("remove_container") < len(kinds) - 1 - kinds[::-1].index("run_bot")


async def test_recreate_missing_is_not_found(launcher):
    with pytest.raises(NotFound):
        await launcher.recreate_bot("ghost")


async def test_recreate_refuses_unlabelled(launcher, backend):
    backend.add_foreign_container("bot-scout")
    with pytest.raises(NotManaged):
        await launcher.recreate_bot("scout")
    assert not ops(backend, "remove_container")


async def test_recreate_stops_running_execs_first(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    handle = await launcher.start_exec("scout", ["claude"], exec_id="t1")
    await launcher.recreate_bot("scout")
    frames = await asyncio.wait_for(collect(launcher, handle), 2)
    assert frames[-1]["reason"] == "stopped"


# ---------- status / list ----------

async def test_status_variants(launcher, backend):
    assert (await launcher.status("ghost")) == {"bot_id": "ghost", "exists": False, "running": False, "status": "missing"}
    await launcher.create_bot("scout", "o1")
    st = await launcher.status("scout")
    assert st["running"] and st["status"] == "running" and st["network"] == "bothub-u-o1" and st["oom_killed"] is False
    backend.add_foreign_container("bot-other")
    with pytest.raises(NotManaged):
        await launcher.status("other")


async def test_list_bots_only_returns_managed(launcher, backend):
    await launcher.create_bot("a", "o1")
    await launcher.create_bot("b", "o2")
    backend.add_foreign_container("bot-foreign")
    ids = sorted(s["bot_id"] for s in await launcher.list_bots())
    assert ids == ["a", "b"]


async def test_screen_session_validates_managed_owner_and_running_bot(launcher, backend):
    await launcher.create_bot("scout", "o1")
    with pytest.raises(NotManaged):
        await launcher.open_screen_session("scout", "o2")
    backend.containers["bot-scout"] = dataclasses.replace(backend.containers["bot-scout"], running=False,
                                                           status="exited")
    with pytest.raises(Conflict):
        await launcher.open_screen_session("scout", "o1")
    backend.add_foreign_container("bot-foreign")
    with pytest.raises(NotManaged):
        await launcher.open_screen_session("foreign", "o1")
    with pytest.raises(NotFound):
        await launcher.open_screen_session("ghost", "o1")
    assert not ops(backend, "spawn_screen")


async def test_screen_session_one_per_bot_and_bounded_total_slots(cfg, backend, netpolicy):
    launcher = Launcher(dataclasses.replace(cfg, max_screen_sessions=1), backend, netpolicy)
    await launcher.create_bot("a", "o1")
    await launcher.create_bot("b", "o1")
    opened = await launcher.open_screen_session("a", "o1")
    with pytest.raises(Conflict):
        await launcher.open_screen_session("a", "o1")
    with pytest.raises(Busy):
        await launcher.open_screen_session("b", "o1")
    await launcher.close_screen_session(opened["session_id"])
    assert backend.last_screen.killed and not launcher.active_screen_sessions()
    assert (await launcher.open_screen_session("b", "o1"))["session_id"]


async def test_screen_output_is_raw_and_client_cancellation_releases_slot(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_screen = FakeStream(chunks=[b"RFB ", b"003.008\n", b"frame"])
    sid = (await launcher.open_screen_session("scout", "o1"))["session_id"]
    stream = launcher.screen_output(sid)
    assert await stream.__anext__() == b"RFB 003.008\n"
    with pytest.raises(Conflict):
        launcher.screen_output(sid)
    await stream.aclose()
    assert backend.last_screen.killed and not launcher.active_screen_sessions()
    assert not launcher._screen_by_bot
    next_sid = (await launcher.open_screen_session("scout", "o1"))["session_id"]
    await launcher.close_screen_session(next_sid)


async def test_screen_output_streams_until_eof_and_preserves_all_bytes(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_screen = FakeStream(chunks=[b"RFB 003.008\n", b"\x00\xffframe"], hang=False)
    sid = (await launcher.open_screen_session("scout", "o1"))["session_id"]
    assert [chunk async for chunk in launcher.screen_output(sid)] == [b"RFB 003.008\n", b"\x00\xffframe"]
    assert not launcher.active_screen_sessions()


async def test_screen_input_is_raw_and_limited(launcher, backend):
    await launcher.create_bot("scout", "o1")
    sid = (await launcher.open_screen_session("scout", "o1"))["session_id"]
    await launcher.screen_input(sid, b"\x04key\x00")
    assert backend.last_screen.written == [b"\x04key\x00"]
    with pytest.raises(ValidationFailed):
        await launcher.screen_input(sid, b"x" * (64 * 1024 + 1))
    await launcher.close_screen_session(sid)


async def test_failed_screen_open_returns_clear_error_and_releases_slot(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_screen = FakeStream(chunks=[], code=1, hang=False)
    with pytest.raises(BackendError, match="RFB-сервер завершился"):
        await launcher.open_screen_session("scout", "o1")
    assert backend.last_screen.killed is False
    assert not launcher.active_screen_sessions() and not launcher._screen_by_bot


async def test_reap_idle_screen_and_bot_removal_close_process(launcher, backend):
    await launcher.create_bot("scout", "o1")
    sid = (await launcher.open_screen_session("scout", "o1"))["session_id"]
    session = launcher._screen_sessions[sid]
    session.last -= launcher.cfg.screen_idle + 1
    await launcher.reap_idle()
    assert backend.last_screen.killed and not launcher.active_screen_sessions()

    await launcher.open_screen_session("scout", "o1")
    await launcher.remove_bot("scout")
    assert backend.last_screen.killed and not launcher.active_screen_sessions()


# ---------- exec ----------

async def test_exec_streams_output_and_exit_code(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(chunks=[("stdout", b'{"a":1}\n'), ("stderr", b"warn"), ("stdout", b"tail")], code=3)
    handle = await launcher.start_exec("scout", ["claude", "-p"], env={"BOTHUB_TURN_ID": "t1"}, stdin="привет", exec_id="t1")
    frames = await collect(launcher, handle)
    assert frames[0] == {"t": "start", "exec_id": "t1"}
    assert out_text(frames) == b'{"a":1}\ntail'
    assert out_text(frames, "stderr") == b"warn"
    assert frames[-1] == {"t": "exit", "code": 3, "reason": "exit"}
    spawned = ops(backend, "spawn_exec")[0]
    assert spawned[1:4] == ("bot-scout", ["claude", "-p"], {"BOTHUB_TURN_ID": "t1"})
    assert backend.last_exec.stdin == "привет".encode() and backend.last_exec.stdin_closed
    assert "t1" not in launcher.active_execs()


async def test_stream_exec_backpressures_silent_reader(launcher, backend):
    await launcher.create_bot("scout", "o1")

    class LargeOutput(FakeExec):
        produced = 0

        async def output(self):
            for _ in range(2000):  # 125 MiB total
                self.produced += 1
                yield "stdout", b"x" * 65536

    backend.next_exec = LargeOutput()
    handle = await launcher.start_exec("scout", ["cat"])
    stream = launcher.stream_exec(handle)
    assert (await stream.__anext__())["t"] == "start"
    await asyncio.sleep(0.05)
    assert backend.last_exec.produced <= 5
    await stream.aclose()


async def test_exec_generates_id_when_missing(launcher):
    await launcher.create_bot("scout", "o1")
    handle = await launcher.start_exec("scout", ["id"])
    assert handle.exec_id and len(handle.exec_id) >= 16
    await collect(launcher, handle)


async def test_exec_unknown_bot_not_found(launcher):
    with pytest.raises(NotFound):
        await launcher.start_exec("ghost", ["id"])


async def test_exec_refuses_unlabelled_container(launcher, backend):
    backend.add_foreign_container("bot-victim")
    with pytest.raises(NotManaged):
        await launcher.start_exec("victim", ["cat", "/etc/shadow"])
    assert not ops(backend, "spawn_exec")


async def test_exec_requires_running_container(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.containers["bot-scout"] = dataclasses.replace(backend.containers["bot-scout"], running=False, status="exited")
    with pytest.raises(Conflict):
        await launcher.start_exec("scout", ["id"])


@pytest.mark.parametrize("kwargs", [
    {"argv": []}, {"argv": ["--privileged"]}, {"argv": ["id"], "env": {"LD_PRELOAD": "x"}},
    {"argv": ["id"], "exec_id": "a b"}, {"argv": ["id"], "timeout": 0}, {"argv": ["id"], "timeout": 10 ** 9},
])
async def test_exec_validation(launcher, backend, kwargs):
    await launcher.create_bot("scout", "o1")
    with pytest.raises(ValidationFailed):
        await launcher.start_exec("scout", **kwargs)
    assert not ops(backend, "spawn_exec")


async def test_exec_duplicate_id_conflicts(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    await launcher.start_exec("scout", ["a"], exec_id="same")
    with pytest.raises(Conflict):
        await launcher.start_exec("scout", ["b"], exec_id="same")


async def test_exec_per_bot_limit(cfg, backend, netpolicy):
    launcher = Launcher(dataclasses.replace(cfg, max_execs_per_bot=2), backend, netpolicy)
    await launcher.create_bot("scout", "o1")
    for i in range(2):
        backend.next_exec = FakeExec(hang=True)
        await launcher.start_exec("scout", ["a"], exec_id=f"e{i}")
    with pytest.raises(Busy):
        await launcher.start_exec("scout", ["a"], exec_id="e3")
    await launcher.stop_exec("e0")
    backend.next_exec = FakeExec()
    await launcher.start_exec("scout", ["a"], exec_id="e4")


async def test_exec_timeout_kills_process_group(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    handle = await launcher.start_exec("scout", ["claude"], exec_id="t1", timeout=0.05)
    frames = await asyncio.wait_for(collect(launcher, handle), 2)
    assert frames[-1] == {"t": "exit", "code": 124, "reason": "timeout"}
    assert ("kill_in_container", "bot-scout", "t1", "TERM") in backend.log


async def test_stop_exec_terminates_then_kills(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.kill_ends_exec = False  # TERM игнорируется, нужен KILL
    backend.next_exec = FakeExec(hang=True)
    handle = await launcher.start_exec("scout", ["claude"], exec_id="t1")
    task = asyncio.create_task(collect(launcher, handle))
    await asyncio.sleep(0)
    res = await launcher.stop_exec("t1")
    assert res["stopped"] is True
    sigs = [e[3] for e in backend.log if e[0] == "kill_in_container"]
    assert sigs == ["TERM", "KILL"]
    frames = await asyncio.wait_for(task, 2)
    assert frames[-1]["reason"] == "stopped"
    assert "t1" not in launcher.active_execs()


async def test_stop_exec_term_is_enough_when_process_exits(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    handle = await launcher.start_exec("scout", ["claude"], exec_id="t1")
    task = asyncio.create_task(collect(launcher, handle))
    await asyncio.sleep(0)
    await launcher.stop_exec("t1")
    assert [e[3] for e in backend.log if e[0] == "kill_in_container"] == ["TERM"]
    await asyncio.wait_for(task, 2)


async def test_stop_unknown_exec_with_bot_uses_marker_kill(launcher, backend):
    await launcher.create_bot("scout", "o1")
    res = await launcher.stop_exec("old-turn", bot_id="scout")  # после рестарта лаунчера или ядра
    assert res["stopped"] is True and res["was_running"] is False
    assert [e[3] for e in backend.log if e[0] == "kill_in_container"] == ["TERM", "KILL"]


async def test_stop_unknown_exec_without_bot_is_not_found(launcher):
    with pytest.raises(NotFound):
        await launcher.stop_exec("nope")


async def test_stop_exec_for_unlabelled_bot_refused(launcher, backend):
    backend.add_foreign_container("bot-victim")
    with pytest.raises(NotManaged):
        await launcher.stop_exec("x", bot_id="victim")
    assert not ops(backend, "kill_in_container")


async def test_client_disconnect_kills_exec(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(chunks=[("stdout", b"x")], hang=True)
    handle = await launcher.start_exec("scout", ["claude"], exec_id="t1")
    gen = launcher.stream_exec(handle)
    assert (await gen.__anext__())["t"] == "start"
    await gen.aclose()
    assert ("kill_in_container", "bot-scout", "t1", "TERM") in backend.log
    assert "t1" not in launcher.active_execs()


async def test_shutdown_stops_everything(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    await launcher.start_exec("scout", ["claude"], exec_id="t1")
    await launcher.shutdown()
    assert not launcher.active_execs()


# ---------- логин-контейнер и pty ----------

async def test_create_login_container(launcher, backend):
    st = await launcher.create_login_container("o1")
    assert st["container"] == "login-o1" and st["running"] and st["owner_id"] == "o1"
    assert ("ensure_volume", "bothub-login-o1", "o1", "login") in backend.log
    again = await launcher.create_login_container("o1")
    assert again["running"] and len(ops(backend, "run_login")) == 1
    assert backend.order.index("reconcile") < backend.order.index("run_login")


async def test_login_container_validates_owner(launcher):
    with pytest.raises(ValidationFailed):
        await launcher.create_login_container("o1; reboot")


async def test_login_container_refuses_foreign_name(launcher, backend):
    backend.add_foreign_container("login-o1")
    with pytest.raises(NotManaged):
        await launcher.create_login_container("o1")


async def test_remove_login_container_keeps_credentials(launcher, backend):
    await launcher.create_login_container("o1")
    await launcher.remove_login_container("o1")
    assert "login-o1" not in backend.containers
    assert not ops(backend, "remove_volume")


async def test_login_session_roundtrip(launcher, backend):
    sess = await launcher.open_login_session("o1", command="claude", cols=100, rows=30)
    sid = sess["session_id"]
    spawned = ops(backend, "spawn_pty")[0]
    assert spawned[1] == "login-o1" and spawned[2] == ["claude"] and spawned[3:] == (100, 30)
    pty = backend.last_pty
    pty.feed(b"hello\r\n")
    await launcher.login_input(sid, b"ls\r")
    await launcher.login_resize(sid, 120, 40)
    pty.finish(0)
    frames = [f async for f in launcher.login_output(sid)]
    assert out_text(frames) == b"hello\r\n"
    assert frames[-1] == {"t": "exit", "code": 0, "reason": "exit"}
    assert pty.written == [b"ls\r"] and pty.size == (120, 40)


async def test_login_session_only_allowlisted_commands(launcher, backend):
    with pytest.raises(ValidationFailed):
        await launcher.open_login_session("o1", command="rm -rf /")
    with pytest.raises(ValidationFailed):
        await launcher.open_login_session("o1", command="../bin/sh")
    assert not ops(backend, "spawn_pty")


async def test_login_session_one_per_owner(launcher):
    await launcher.open_login_session("o1")
    with pytest.raises(Conflict):
        await launcher.open_login_session("o1")
    other = await launcher.open_login_session("o2")
    assert other["session_id"]


async def test_login_session_close_kills_and_forgets(launcher, backend):
    sid = (await launcher.open_login_session("o1"))["session_id"]
    await launcher.close_login_session(sid)
    assert backend.last_pty.killed
    with pytest.raises(NotFound):
        await launcher.login_input(sid, b"x")
    await launcher.open_login_session("o1")  # слот освободился


async def test_login_session_unknown_is_not_found(launcher):
    for call in (launcher.login_input("deadbeef", b"x"), launcher.login_resize("deadbeef", 80, 24),
                 launcher.close_login_session("deadbeef")):
        with pytest.raises(NotFound):
            await call


async def test_login_input_size_limited(launcher):
    sid = (await launcher.open_login_session("o1"))["session_id"]
    with pytest.raises(ValidationFailed):
        await launcher.login_input(sid, b"x" * 70_000)
    with pytest.raises(ValidationFailed):
        await launcher.login_resize(sid, 0, 10)


async def test_idle_login_session_is_reaped(cfg, backend, netpolicy):
    now = [1000.0]
    launcher = Launcher(cfg, backend, netpolicy, clock=lambda: now[0])
    sid = (await launcher.open_login_session("o1"))["session_id"]
    now[0] += cfg.login_idle - 1
    await launcher.reap_idle()
    await launcher.login_input(sid, b"x")  # активность продлевает
    now[0] += cfg.login_idle - 1
    await launcher.reap_idle()
    await launcher.login_input(sid, b"x")
    now[0] += cfg.login_idle + 1
    await launcher.reap_idle()
    assert backend.last_pty.killed
    with pytest.raises(NotFound):
        await launcher.login_input(sid, b"x")


async def test_login_session_hard_lifetime(cfg, backend, netpolicy):
    now = [0.0]
    launcher = Launcher(cfg, backend, netpolicy, clock=lambda: now[0])
    sid = (await launcher.open_login_session("o1"))["session_id"]
    for _ in range(10):
        now[0] += cfg.login_idle - 1
        await launcher.login_input(sid, b"x")  # активен, но жизнь ограничена login_ttl
    assert now[0] > cfg.login_ttl
    await launcher.reap_idle()
    assert backend.last_pty.killed


async def test_exec_sends_heartbeat_while_silent(cfg, backend, netpolicy):
    launcher = Launcher(dataclasses.replace(cfg, heartbeat=0.01), backend, netpolicy)
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    handle = await launcher.start_exec("scout", ["claude"], exec_id="t1")
    seen = []
    async for frame in launcher.stream_exec(handle):
        seen.append(frame["t"])
        if frame["t"] == "ping" and seen.count("ping") == 2:
            await launcher.stop_exec("t1")
    assert seen[0] == "start" and seen.count("ping") >= 2 and seen[-1] == "exit"


async def test_login_output_sends_heartbeat(cfg, backend, netpolicy):
    launcher = Launcher(dataclasses.replace(cfg, heartbeat=0.01), backend, netpolicy)
    sid = (await launcher.open_login_session("o1"))["session_id"]
    seen = []
    async for frame in launcher.login_output(sid):
        seen.append(frame["t"])
        if frame["t"] == "ping" and seen.count("ping") == 2:
            backend.last_pty.finish(0)
    assert seen[:2] == ["ping", "ping"] and seen[-1] == "exit"


async def test_reconcile_reattaches_core_after_it_was_recreated(launcher, backend, netpolicy):
    await launcher.create_bot("scout", "o1")
    backend.detach_core("o1")
    backend.log.clear()
    await launcher.reconcile()
    assert ("ensure_network", "o1") in backend.log
    assert backend.networks["bothub-u-o1"].core_ip == "172.20.0.2"
    assert netpolicy.events[-1] == ("reconcile", ["bothub-u-o1"])


async def test_reconcile_survives_failed_reattach(launcher, backend, netpolicy):
    await launcher.create_bot("scout", "o1")
    backend.detach_core("o1")

    async def boom(owner_id):
        raise BackendError("нет контейнера ядра")

    backend.ensure_network = boom
    report = await launcher.reconcile()  # правила всё равно применяются
    assert report.networks == 1


async def test_exec_stdin_that_is_never_read_fails_instead_of_hanging(cfg, backend, netpolicy, monkeypatch):
    import bothub_launcher.service as svc
    monkeypatch.setattr(svc, "STDIN_TIMEOUT", 0.05)
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot("scout", "o1")

    class Stuck(FakeExec):
        async def write_stdin(self, data):
            await asyncio.sleep(10)

    backend.next_exec = Stuck()
    with pytest.raises(BackendError):
        await launcher.start_exec("scout", ["claude"], stdin="x" * 10, exec_id="t1")
    assert "t1" not in launcher.active_execs() and backend.last_exec.killed

# ---------- browser isolation and freeze ----------

async def test_ensure_browser_is_idempotent(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    assert not (await launcher.ensure_browser('scout'))['started']
    assert not (await launcher.ensure_browser('scout'))['started']
    assert ops(backend, 'start_browser') == [('start_browser', 'bot-scout')]
    assert ('ensure_volume', 'bot-scout-browser', 'o1', 'browser') in backend.log


async def test_exec_restarts_browser_before_run(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    backend.browser_running_names.remove('bot-scout')
    handle = await launcher.start_exec('scout', ['true'])
    await collect(launcher, handle)
    assert len(ops(backend, 'start_browser')) == 2
    assert ops(backend, 'spawn_exec')


async def test_ensure_browser_waits_for_readiness(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    running = backend.browser_running
    checks = 0

    async def delayed_ready(container):
        nonlocal checks
        checks += 1
        return checks > 3 and await running(container)

    backend.browser_running = delayed_ready
    assert (await launcher.ensure_browser('scout'))['started']
    assert checks == 4


async def test_freeze_blocks_exec_and_unfreeze_restores_it(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    assert (await launcher.freeze_bot('scout'))['frozen']
    assert (tmp_path / 'frozen-bots' / 'scout').exists()
    with pytest.raises(Conflict) as exc:
        await launcher.start_exec('scout', ['true'])
    assert exc.value.code == 'frozen'
    assert not ops(backend, 'spawn_exec')
    restarted = Launcher(cfg, backend, netpolicy)
    with pytest.raises(Conflict) as exc:
        await restarted.start_exec('scout', ['true'])
    assert exc.value.code == 'frozen'
    await restarted.unfreeze_bot('scout')
    assert not (tmp_path / 'frozen-bots' / 'scout').exists()
    handle = await restarted.start_exec('scout', ['true'])
    await collect(restarted, handle)


async def test_freeze_fails_if_uid1000_processes_remain(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    backend.remaining_bot_pids = [1234]
    with pytest.raises(Conflict, match='остались процессы'):
        await launcher.freeze_bot('scout')
    assert (tmp_path / 'frozen-bots' / 'scout').exists()
    with pytest.raises(Conflict) as exc:
        await launcher.start_exec('scout', ['true'])
    assert exc.value.code == 'frozen'


async def test_freeze_escalates_to_kill(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    backend.remaining_bot_pids = [1234]
    terminate = backend.terminate_bot_processes

    async def clear_on_kill(container, signal_name='TERM'):
        await terminate(container, signal_name)
        if signal_name == 'KILL':
            backend.remaining_bot_pids = []

    backend.terminate_bot_processes = clear_on_kill
    assert (await launcher.freeze_bot('scout'))['frozen']
    assert ops(backend, 'terminate_bot_processes')[-1] == ('terminate_bot_processes', 'bot-scout', 'KILL')


async def test_freeze_waits_for_inflight_exec_spawn(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    entered, release = asyncio.Event(), asyncio.Event()
    spawn = backend.spawn_exec

    async def delayed_spawn(*args):
        entered.set()
        await release.wait()
        return await spawn(*args)

    backend.spawn_exec = delayed_spawn
    starting = asyncio.create_task(launcher.start_exec('scout', ['true'], exec_id='race'))
    await entered.wait()
    freezing = asyncio.create_task(launcher.freeze_bot('scout'))
    await asyncio.sleep(0)
    assert not freezing.done()
    release.set()
    await starting
    assert (await freezing)['frozen']
    assert (tmp_path / 'frozen-bots' / 'scout').exists()


# ---------- метка заморозки: права каталога и сбои файловой системы ----------

@pytest.fixture
def strict_umask():
    old = os.umask(0o177)  # хуже боевого 0o117: mkdir(mode=0o700) даёт drw-------, без x
    yield
    os.umask(old)


async def test_freeze_marker_dir_is_traversable_under_strict_umask(cfg, backend, netpolicy, tmp_path, strict_umask):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    assert (await launcher.freeze_bot('scout'))['frozen']
    marker_dir = tmp_path / 'frozen-bots'
    assert stat.S_IMODE(marker_dir.stat().st_mode) == 0o700
    assert (marker_dir / 'scout').read_text() == 'frozen\n'
    with pytest.raises(Conflict) as exc:
        await launcher.start_exec('scout', ['true'])
    assert exc.value.code == 'frozen'
    await launcher.unfreeze_bot('scout')
    assert not (marker_dir / 'scout').exists()
    handle = await launcher.start_exec('scout', ['true'])
    await collect(launcher, handle)


async def test_freeze_repairs_marker_dir_left_without_x_by_an_older_launcher(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    (tmp_path / 'frozen-bots').mkdir(mode=0o600)
    os.chmod(tmp_path / 'frozen-bots', 0o600)
    assert (await launcher.freeze_bot('scout'))['frozen']
    assert stat.S_IMODE((tmp_path / 'frozen-bots').stat().st_mode) == 0o700
    os.chmod(tmp_path / 'frozen-bots', 0o600)
    await launcher.unfreeze_bot('scout')  # снятие метки тоже чинит каталог
    assert not (tmp_path / 'frozen-bots' / 'scout').exists()


async def test_freeze_marker_write_failure_is_a_launcher_error_not_a_traceback(cfg, backend, netpolicy, tmp_path):
    blocker = tmp_path / 'frozen-bots'
    blocker.write_text('not a directory')  # mkdir упадёт
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    with pytest.raises(StateError) as exc:
        await launcher.freeze_bot('scout')
    assert exc.value.code == 'state_error' and 'метку заморозки' in str(exc.value)
    assert not ops(backend, 'terminate_bot_processes')


async def test_freeze_chmod_failure_tells_to_fix_the_directory_not_to_recreate_the_bot(
        cfg, backend, netpolicy, tmp_path, monkeypatch):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')

    def deny(path, mode):
        raise PermissionError(1, 'Operation not permitted')

    monkeypatch.setattr(os, 'chmod', deny)
    with pytest.raises(StateError) as exc:
        await launcher.freeze_bot('scout')
    message = str(exc.value)
    assert exc.value.code == 'state_error'
    assert str(tmp_path / 'frozen-bots') in message
    assert 'Почините права каталога на сервере' in message and 'пересоздавать не нужно' in message
    assert not ops(backend, 'terminate_bot_processes')


async def test_unfreeze_chmod_failure_gives_the_same_instruction(cfg, backend, netpolicy, tmp_path, monkeypatch):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    assert (await launcher.freeze_bot('scout'))['frozen']
    marker_dir = tmp_path / 'frozen-bots'
    os.chmod(marker_dir, 0o600)  # без x: unlink даст PermissionError и пойдёт починка

    def deny(path, mode):
        raise PermissionError(1, 'Operation not permitted')

    monkeypatch.setattr(os, 'chmod', deny)
    with pytest.raises(StateError) as exc:
        await launcher.unfreeze_bot('scout')
    assert 'Почините права каталога на сервере' in str(exc.value)
    assert 'пересоздавать не нужно' in str(exc.value)
    monkeypatch.undo()
    os.chmod(marker_dir, 0o700)


async def test_unreadable_marker_counts_as_frozen(cfg, backend, netpolicy, tmp_path, monkeypatch):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')

    class Unreadable:
        def exists(self):
            raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(launcher, '_frozen_file', lambda bot_id: Unreadable())
    assert launcher._is_frozen('scout')
    with pytest.raises(Conflict) as exc:
        await launcher.start_exec('scout', ['true'])
    assert exc.value.code == 'frozen'
    assert not ops(backend, 'spawn_exec')


# ---------- режим браузера: human (чистый Chromium без CDP) и bot ----------

async def test_browser_mode_human_then_bot(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    started = len(ops(backend, 'start_browser'))
    res = await launcher.browser_mode('scout', 'human', 'https://example.com/a?b=1')
    assert res == {'bot_id': 'scout', 'mode': 'human', 'changed': True, 'cookies': None}
    assert ('set_browser_mode', 'bot-scout', 'human', 'https://example.com/a?b=1') in backend.log
    assert backend.browser_modes['bot-scout'] == 'human'
    assert backend.browser_urls['bot-scout'] == 'https://example.com/a?b=1'
    assert 'bot-scout' not in backend.browser_running_names  # CDP не отвечает
    # режим записан до запуска супервизора: свежий супервизор не поднимет CDP в human
    assert backend.order[backend.order.index('set_browser_mode') + 1] == 'start_browser'
    assert len(ops(backend, 'start_browser')) == started + 1

    res = await launcher.browser_mode('scout', 'bot')
    assert res == {'bot_id': 'scout', 'mode': 'bot', 'changed': True, 'cookies': {'merged': 2}}
    assert backend.browser_modes['bot-scout'] == 'bot' and 'bot-scout' in backend.browser_running_names
    assert 'bot-scout' not in backend.browser_urls


async def test_browser_mode_is_idempotent(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    for _ in range(2):
        res = await launcher.browser_mode('scout', 'human', 'https://example.com/')
    assert res['changed'] is False
    assert len(ops(backend, 'set_browser_mode')) == 1  # второй вызов ничего не пишет и ничего не перезапускает
    for _ in range(2):
        res = await launcher.browser_mode('scout', 'bot')
    assert res['changed'] is False and res['cookies'] is None
    assert len(ops(backend, 'set_browser_mode')) == 2


async def test_browser_mode_bot_when_never_switched_is_a_no_op(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    res = await launcher.browser_mode('scout', 'bot')
    assert res['changed'] is False and not ops(backend, 'set_browser_mode')


async def test_browser_mode_repeats_the_write_when_mode_is_set_but_browser_is_not_ready(launcher, backend, monkeypatch):
    await launcher.create_bot('scout', 'o1')
    await launcher.browser_mode('scout', 'human', 'https://example.com/')
    backend.mode_ready_after = 10_000  # супервизор лежит: режим в файле есть, Chromium нет
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)
    monkeypatch.setattr('bothub_launcher.service.asyncio.sleep', fast_sleep)
    with pytest.raises(BackendError, match='не перешёл'):
        await launcher.browser_mode('scout', 'human', 'https://example.com/')
    assert len(ops(backend, 'set_browser_mode')) == 2 and len(ops(backend, 'start_browser')) >= 2


async def test_browser_mode_waits_for_readiness(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    backend.mode_ready_after = 3
    res = await launcher.browser_mode('scout', 'human', 'https://example.com/')
    assert res['changed'] is True
    assert len(ops(backend, 'browser_mode_ready')) >= 4


async def test_browser_mode_human_defaults_to_blank_tab(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    await launcher.browser_mode('scout', 'human')
    assert backend.browser_urls['bot-scout'] == 'about:blank'


@pytest.mark.parametrize('url', ['javascript:alert(1)', '--remote-debugging-port=9222', 'file:///etc/passwd',
                                 'chrome://settings', 'https://a b/', 'http://x\\y/', 'https://e.com/\x07',
                                 'data:text/html,x', 'https://e.com/' + 'a' * 2100, 7])
async def test_browser_mode_rejects_urls_that_could_become_flags_or_local_pages(launcher, backend, url):
    await launcher.create_bot('scout', 'o1')
    with pytest.raises(ValidationFailed):
        await launcher.browser_mode('scout', 'human', url)
    assert not ops(backend, 'set_browser_mode')


@pytest.mark.parametrize('mode', ['', 'Human', 'both', 'bot ', None, 1])
async def test_browser_mode_rejects_unknown_modes(launcher, backend, mode):
    await launcher.create_bot('scout', 'o1')
    with pytest.raises(ValidationFailed):
        await launcher.browser_mode('scout', mode)
    assert not ops(backend, 'set_browser_mode')


async def test_browser_mode_url_only_for_human(launcher):
    await launcher.create_bot('scout', 'o1')
    with pytest.raises(ValidationFailed):
        await launcher.browser_mode('scout', 'bot', 'https://example.com/')


async def test_browser_mode_unknown_stopped_and_foreign_bots(launcher, backend):
    with pytest.raises(NotFound):
        await launcher.browser_mode('ghost', 'human')
    backend.add_foreign_container('bot-foreign')
    with pytest.raises(NotManaged):
        await launcher.browser_mode('foreign', 'human')
    await launcher.create_bot('scout', 'o1')
    backend.containers['bot-scout'] = dataclasses.replace(backend.containers['bot-scout'], running=False)
    with pytest.raises(Conflict, match='не запущен'):
        await launcher.browser_mode('scout', 'human')
    assert not ops(backend, 'set_browser_mode')


async def test_browser_mode_legacy_container_requires_recreate(launcher, backend):
    await backend.run_bot('scout', 'o1')
    info = backend.containers['bot-scout']
    backend.containers['bot-scout'] = dataclasses.replace(info, labels={
        key: value for key, value in info.labels.items() if key != 'bothub.browser_isolation'})
    with pytest.raises(Conflict, match='recreate'):
        await launcher.browser_mode('scout', 'human')
    assert not ops(backend, 'set_browser_mode')


async def test_browser_mode_write_failure_is_a_backend_error_and_leaves_the_mode(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    starts = len(ops(backend, 'start_browser'))

    async def failing(container, mode, url):
        raise BackendError('запись режима браузера human: нет места')

    backend.set_browser_mode = failing
    with pytest.raises(BackendError):
        await launcher.browser_mode('scout', 'human', 'https://example.com/')
    assert backend.browser_modes.get('bot-scout', 'bot') == 'bot'
    assert len(ops(backend, 'start_browser')) == starts  # супервизор после сбоя записи не трогали


async def test_browser_mode_reports_a_failed_cookie_merge(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    await launcher.browser_mode('scout', 'human', 'https://example.com/')
    original = backend.set_browser_mode

    async def set_mode(container, mode, url):
        await original(container, mode, url)
        backend.merge_status[container] = 'failed'
    backend.set_browser_mode = set_mode
    res = await launcher.browser_mode('scout', 'bot')
    assert res['cookies'] == {'error': 'перенос cookies не удался, вход человека не сохранён'}


async def test_browser_mode_survives_launcher_restart(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    first = Launcher(cfg, backend, netpolicy)
    await first.create_bot('scout', 'o1')
    await first.freeze_bot('scout')  # человек за экраном только под меткой заморозки (метка переживает рестарт)
    await first.browser_mode('scout', 'human', 'https://example.com/')
    restarted = Launcher(cfg, backend, netpolicy)  # режим живёт в томе контейнера, не в памяти лаунчера
    await restarted.startup()
    assert backend.browser_modes['bot-scout'] == 'human'
    assert 'bot-scout' not in backend.browser_running_names  # рестарт лаунчера не поднял CDP
    assert (await restarted.browser_mode('scout', 'human', 'https://example.com/'))['changed'] is False
    assert (await restarted.ensure_browser('scout'))['mode'] == 'human'
    assert 'bot-scout' not in backend.browser_running_names
    assert len(ops(backend, 'set_browser_mode')) == 1


async def test_ensure_browser_in_human_mode_never_waits_for_or_starts_cdp(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    await launcher.browser_mode('scout', 'human', 'https://example.com/')
    before = len(ops(backend, 'browser_running'))
    res = await launcher.ensure_browser('scout')
    assert res == {'bot_id': 'scout', 'running': True, 'started': False, 'mode': 'human'}
    assert len(ops(backend, 'browser_running')) == before  # CDP не проверяется


async def test_exec_is_refused_in_human_mode_even_without_a_freeze_marker(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    await launcher.browser_mode('scout', 'human', 'https://example.com/')
    with pytest.raises(Conflict) as exc:
        await launcher.start_exec('scout', ['true'])
    assert exc.value.code == 'frozen'
    assert not ops(backend, 'spawn_exec')


async def test_unfreeze_is_refused_while_the_browser_is_human(cfg, backend, netpolicy, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / 'launcher.sock'))
    launcher = Launcher(cfg, backend, netpolicy)
    await launcher.create_bot('scout', 'o1')
    await launcher.freeze_bot('scout')
    await launcher.browser_mode('scout', 'human', 'https://example.com/')
    with pytest.raises(Conflict, match='human'):
        await launcher.unfreeze_bot('scout')
    assert (tmp_path / 'frozen-bots' / 'scout').exists()
    await launcher.browser_mode('scout', 'bot')
    await launcher.unfreeze_bot('scout')
    assert not (tmp_path / 'frozen-bots' / 'scout').exists()
    handle = await launcher.start_exec('scout', ['true'])
    await collect(launcher, handle)


async def test_corrupt_mode_file_is_reported_and_fixed_by_browser_mode(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    backend.browser_modes['bot-scout'] = 'invalid'
    with pytest.raises(Conflict, match='повреждён'):
        await launcher.ensure_browser('scout')
    with pytest.raises(Conflict, match='повреждён'):
        await launcher.start_exec('scout', ['true'])
    res = await launcher.browser_mode('scout', 'bot')
    assert res['changed'] is True and backend.browser_modes['bot-scout'] == 'bot'


async def test_browser_tab_returns_the_bot_page_only_in_bot_mode(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    backend.browser_tabs['bot-scout'] = 'https://example.com/page'
    assert await launcher.browser_tab('scout') == {'bot_id': 'scout', 'mode': 'bot', 'url': 'https://example.com/page'}
    await launcher.browser_mode('scout', 'human', 'https://example.com/')
    assert await launcher.browser_tab('scout') == {'bot_id': 'scout', 'mode': 'human', 'url': None}
    with pytest.raises(NotFound):
        await launcher.browser_tab('ghost')


async def test_browser_tab_is_none_when_the_browser_does_not_answer(launcher, backend):
    await launcher.create_bot('scout', 'o1')
    assert (await launcher.browser_tab('scout'))['url'] is None


# ---------- сеть пользователя без контейнера (ядро подключает её при создании пользователя) ----------

async def test_ensure_owner_network_creates_the_network_and_connects_core_before_any_bot(launcher, backend):
    res = await launcher.ensure_owner_network("o1")
    assert res == {"owner_id": "o1", "network": "bothub-u-o1", "core_connected": True}
    assert ("ensure_network", "o1") in backend.log
    assert backend.order.index("ensure_network") < backend.order.index("reconcile")
    assert not ops(backend, "run_bot")  # контейнеров нет: только сеть и правила
    again = await launcher.ensure_owner_network("o1")  # повтор ничего не ломает
    assert again["core_connected"] is True


async def test_ensure_owner_network_brings_core_back_after_it_fell_out(launcher, backend):
    await launcher.ensure_owner_network("o1")
    backend.detach_core("o1")
    res = await launcher.ensure_owner_network("o1")
    assert res["core_connected"] is True and backend.networks["bothub-u-o1"].core_ip is not None


async def test_ensure_owner_network_validates_owner(launcher):
    with pytest.raises(ValidationFailed):
        await launcher.ensure_owner_network("o1; reboot")
