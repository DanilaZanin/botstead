"""Операция procedure_step: шаг процедуры под uid 1001 через docker exec, данные только в stdin."""
import asyncio
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from bothub_launcher import docker_args as da
from bothub_launcher.app import create_app
from bothub_launcher.errors import BackendError, Busy, Conflict, NotFound, ValidationFailed
from bothub_launcher.service import PROCEDURE_OUT_MAX, Launcher
from bothub_launcher.testing import FakeBackend, FakeExec, FakeNetPolicy

from .conftest import SECRET

AUTH = {"Authorization": f"Bearer {SECRET}"}
VALUE = "p4ss-SECRET-VALUE"
PAYLOAD = json.dumps({"step": {"action": "fill", "target": {"role": "textbox", "name": "Password"}, "value": VALUE}})
RESULT = {"v": 1, "ok": True, "code": None, "acted": True, "url": "https://example.com/", "found": None}


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def launcher(cfg, backend):
    return Launcher(cfg, backend, FakeNetPolicy(order=backend.order))


def ops(backend, name):
    return [e for e in backend.log if e[0] == name]


def result_exec(result=RESULT, code=0, extra_chunks=()):
    return FakeExec(chunks=[*extra_chunks, ("stdout", (json.dumps(result) + "\n").encode())], code=code)


# ---------- argv: константы, без данных шага ----------

def test_argv_is_constant_runs_as_browser_uid_and_has_no_payload(cfg):
    argv = da.procedure_step_args(cfg, "bot-scout", "abc123")
    assert argv[:2] == [cfg.docker_bin, "exec"]
    assert argv[argv.index("--user") + 1] == "1001:1001"
    assert argv[-3:] == [da.NODE, "--no-warnings", da.PROCEDURE_STEP_SCRIPT]
    assert da.BOT_GUARD not in argv  # браузерный стек идёт без загрузчика uid 1000
    assert "-i" in argv  # stdin открыт
    assert da.PROCEDURE_STEP_SCRIPT == "/usr/local/libexec/procedure-step.mjs"
    # payload и всё похожее на данные в командную строку не попадает: только контейнер, id и пути программ
    assert set(argv) >= {"bot-scout", "abc123"}


def test_executor_starts_with_a_clean_environment_and_no_module_search_path(cfg):
    """Пункт 5 ревью Opus: HOME=/nonexistent, без NODE_PATH и NODE_OPTIONS, cwd /: модуль не подменить ни из HOME
    (том браузера под uid 1001), ни из рабочего каталога, ни переменными образа."""
    argv = da.procedure_step_args(cfg, "bot-scout", "abc123")
    assert argv[argv.index("--workdir") + 1] == "/"
    assert argv.count("-e") == 1 and argv[argv.index("-e") + 1] == "HOME=/nonexistent"
    start = argv.index(da.ENV_BIN)
    assert argv[start:start + 5] == [da.ENV_BIN, "-i", "HOME=/nonexistent", "PATH=/usr/bin:/bin", da.NODE]  # env -i чистит всё остальное
    text = " ".join(argv)
    assert "NODE_PATH" not in text and "NODE_OPTIONS" not in text and "/home/browser/.node" not in text
    assert "$HOME" not in text and "HOME=/home/browser" not in text
    assert da.PROCEDURE_PID_FILE.startswith("/home/browser/") and da.PROCEDURE_PID_FILE in text  # PID-файл по абсолютному пути


def test_kill_runs_as_uid_1001_not_the_bot_uid(cfg):
    argv = da.procedure_kill_args(cfg, "bot-scout", "abc123", "TERM")
    assert argv[argv.index("--user") + 1] == "1001:1001"
    assert argv[-2:] == ["TERM", "abc123"]
    assert da.PROCEDURE_PID_FILE in " ".join(argv) and "$HOME" not in " ".join(argv)
    assert argv[argv.index("--workdir") + 1] == "/"
    with pytest.raises(ValidationFailed):
        da.procedure_kill_args(cfg, "bot-scout", "abc123", "HUP")
    with pytest.raises(ValidationFailed):
        da.procedure_step_args(cfg, "bot-scout", "bad id; rm")


# ---------- сервис ----------

async def test_step_sends_the_envelope_through_stdin_and_returns_the_parsed_result(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = result_exec(extra_chunks=[("stderr", b"noise that must be dropped")])
    answer = await launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1")
    assert answer == {"bot_id": "scout", "exec_id": "ps-1", "exit_code": 0, "reason": "exit", "result": RESULT}
    assert ops(backend, "spawn_procedure_step") == [("spawn_procedure_step", "bot-scout", "ps-1")]
    assert not ops(backend, "spawn_exec")  # обычный exec (uid 1000) не использован
    sent = json.loads(backend.last_exec.stdin)
    assert sent == {"dry_run": False, "deadline_ms": 27000, "payload": json.loads(PAYLOAD)}
    assert backend.last_exec.stdin_closed
    assert "ps-1" not in launcher.active_execs()


async def test_dry_run_flag_and_default_timeout_travel_in_the_envelope(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = result_exec()
    await launcher.procedure_step("scout", PAYLOAD, True)
    sent = json.loads(backend.last_exec.stdin)
    assert sent["dry_run"] is True and sent["deadline_ms"] == 27000


async def test_secret_value_is_nowhere_but_stdin(launcher, backend, caplog):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = result_exec()
    caplog.set_level("DEBUG")
    answer = await launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1")
    assert VALUE in backend.last_exec.stdin.decode()
    assert VALUE not in json.dumps(answer) and VALUE not in caplog.text
    assert VALUE not in json.dumps([e for e in backend.log])


async def test_unparsable_output_and_crash_give_no_result(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(chunks=[("stdout", b"not json at all\n")], code=1)
    answer = await launcher.procedure_step("scout", PAYLOAD)
    assert answer["result"] is None and answer["exit_code"] == 1
    backend.next_exec = FakeExec(chunks=[("stdout", b"[1,2]\n")], code=0)
    assert (await launcher.procedure_step("scout", PAYLOAD))["result"] is None
    backend.next_exec = FakeExec(code=0)
    assert (await launcher.procedure_step("scout", PAYLOAD))["result"] is None


async def test_the_last_stdout_line_is_the_result_and_output_is_capped(launcher, backend):
    await launcher.create_bot("scout", "o1")
    noise = [("stdout", b"x" * (PROCEDURE_OUT_MAX * 2))]
    backend.next_exec = result_exec(extra_chunks=noise)
    answer = await launcher.procedure_step("scout", PAYLOAD)
    assert answer["result"] is None  # результат не поместился в предел: обрезан и не разобрался
    backend.next_exec = FakeExec(chunks=[("stdout", b"log line\n"), ("stdout", (json.dumps(RESULT) + "\n").encode())])
    assert (await launcher.procedure_step("scout", PAYLOAD))["result"] == RESULT


async def test_timeout_kills_the_group_as_uid_1001_and_reports_it(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    launcher_timeout = 5  # минимум; ждём срабатывания жёсткого предела подменой часов нельзя, поэтому правим handle
    task = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, launcher_timeout, "ps-1"))
    await asyncio.sleep(0.05)
    handle = launcher._execs["ps-1"]
    handle.timed_out = True
    await launcher._terminate(handle)
    answer = await asyncio.wait_for(task, 2)
    assert answer["reason"] == "timeout" and answer["exit_code"] == 124 and answer["result"] is None
    assert ("kill_procedure_step", "bot-scout", "ps-1", "TERM") in backend.log
    assert not ops(backend, "kill_in_container")  # kill uid 1000 до процесса uid 1001 не дотянулся бы


async def test_stop_exec_signals_the_procedure_step_through_its_own_path(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    task = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    assert (await launcher.stop_exec("ps-1", "scout"))["was_running"] is True
    answer = await asyncio.wait_for(task, 2)
    assert answer["reason"] == "stopped" and answer["result"] is None
    assert ops(backend, "kill_procedure_step") and not ops(backend, "kill_in_container")


async def test_freeze_stops_a_running_step(cfg, backend, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / "launcher.sock"))
    launcher = Launcher(cfg, backend, FakeNetPolicy(order=backend.order))
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    task = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    await launcher.freeze_bot("scout")
    answer = await asyncio.wait_for(task, 2)
    assert answer["reason"] == "stopped"
    with pytest.raises(Conflict) as exc:
        await launcher.procedure_step("scout", PAYLOAD)
    assert exc.value.code == "frozen"


async def test_human_mode_frozen_missing_and_stopped_bots_are_refused_without_spawning(launcher, backend):
    with pytest.raises(NotFound):
        await launcher.procedure_step("ghost", PAYLOAD)
    await launcher.create_bot("scout", "o1")
    backend.browser_modes["bot-scout"] = "human"
    with pytest.raises(Conflict) as exc:
        await launcher.procedure_step("scout", PAYLOAD)
    assert exc.value.code == "frozen"
    backend.browser_modes["bot-scout"] = "bot"
    backend.containers["bot-scout"] = dataclasses.replace(backend.containers["bot-scout"], running=False)
    with pytest.raises(Conflict):
        await launcher.procedure_step("scout", PAYLOAD)
    assert not ops(backend, "spawn_procedure_step")


async def test_one_step_per_bot_and_unique_exec_id(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    first = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    with pytest.raises(Conflict):
        await launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1")
    with pytest.raises(Busy):
        await launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-2")
    await launcher.stop_exec("ps-1", "scout")
    await asyncio.wait_for(first, 2)


async def test_unlabelled_container_is_not_touched(launcher, backend):
    backend.add_foreign_container("bot-victim")
    with pytest.raises(Exception) as exc:
        await launcher.procedure_step("victim", PAYLOAD)
    assert getattr(exc.value, "code", "") in ("not_found", "not_managed")
    assert not ops(backend, "spawn_procedure_step")


@pytest.mark.parametrize("kwargs", [
    {"payload_json": 5},
    {"payload_json": "not json"},
    {"payload_json": "[1]"},
    {"payload_json": "null"},
    {"payload_json": "{" + '"a":' * 10 + "1" + "}" * 10 + " x"},
    {"payload_json": json.dumps({"x": "y" * (256 * 1024)})},
    {"dry_run": "yes"},
    {"dry_run": 1},
    {"timeout": 1},
    {"timeout": 9999},
    {"timeout": True},
    {"timeout": "30"},
    {"exec_id": "bad id"},
])
async def test_validation_fails_before_any_docker_call(launcher, backend, kwargs):
    await launcher.create_bot("scout", "o1")
    args = {"payload_json": PAYLOAD, "dry_run": False, "timeout": 30, "exec_id": None} | kwargs
    with pytest.raises(ValidationFailed) as exc:
        await launcher.procedure_step("scout", **args)
    assert VALUE not in str(exc.value)
    assert not ops(backend, "spawn_procedure_step")


async def test_validation_error_never_quotes_the_payload(launcher):
    await launcher.create_bot("scout", "o1")
    for bad in (f"{VALUE} not json", json.dumps([VALUE])):
        with pytest.raises(ValidationFailed) as exc:
            await launcher.procedure_step("scout", bad)
        assert VALUE not in str(exc.value)


# ---------- HTTP ----------

@pytest.fixture
def client(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy(order=backend.order))
    with TestClient(app) as c:
        yield c


def test_http_route_runs_the_step_and_answers_json(client, backend):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    backend.next_exec = result_exec()
    r = client.post("/v1/bots/scout/procedure-step",
                    json={"payload_json": PAYLOAD, "dry_run": True, "timeout": 20, "exec_id": "ps-9"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"bot_id": "scout", "exec_id": "ps-9", "exit_code": 0, "reason": "exit", "result": RESULT}
    assert json.loads(backend.last_exec.stdin)["dry_run"] is True


def test_http_route_needs_the_secret_and_rejects_extra_fields(client, backend):
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    body = {"payload_json": PAYLOAD}
    assert client.post("/v1/bots/scout/procedure-step", json=body).status_code == 401
    r = client.post("/v1/bots/scout/procedure-step", json=body | {"argv": ["id"]}, headers=AUTH)
    assert r.status_code == 400
    assert VALUE not in r.text
    r = client.post("/v1/bots/scout/procedure-step", json={"payload_json": 5}, headers=AUTH)
    assert r.status_code == 400 and VALUE not in r.text
    assert not ops(backend, "spawn_procedure_step")


def test_http_errors_use_the_launcher_error_shape(client, backend):
    r = client.post("/v1/bots/ghost/procedure-step", json={"payload_json": PAYLOAD}, headers=AUTH)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
    backend.browser_modes["bot-scout"] = "human"
    r = client.post("/v1/bots/scout/procedure-step", json={"payload_json": PAYLOAD}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["code"] == "frozen" and VALUE not in r.text


async def test_a_failed_spawn_or_a_stuck_stdin_releases_the_slot(launcher, backend, monkeypatch):
    from bothub_launcher import service
    await launcher.create_bot("scout", "o1")

    async def boom(container, exec_id):
        raise BackendError("docker не найден")

    original = backend.spawn_procedure_step
    backend.spawn_procedure_step = boom
    with pytest.raises(BackendError):
        await launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1")
    assert "ps-1" not in launcher.active_execs()
    backend.spawn_procedure_step = original

    class Stuck(FakeExec):
        async def write_stdin(self, data):
            await asyncio.sleep(3600)

    monkeypatch.setattr(service, "STDIN_TIMEOUT", 0.05)
    backend.next_exec = Stuck()
    with pytest.raises(BackendError) as exc:
        await launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-2")
    assert VALUE not in str(exc.value) and backend.last_exec.killed and "ps-2" not in launcher.active_execs()
    backend.next_exec = result_exec()
    assert (await launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-3"))["result"] == RESULT  # слот свободен


async def test_total_exec_limit_refuses_a_step_of_another_bot(cfg, backend):
    launcher = Launcher(dataclasses.replace(cfg, max_execs_total=1), backend, FakeNetPolicy(order=backend.order))
    await launcher.create_bot("scout", "o1")
    await launcher.create_bot("rover", "o1")
    backend.next_exec = FakeExec(hang=True)
    first = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    with pytest.raises(Busy):
        await launcher.procedure_step("rover", PAYLOAD, False, 30, "ps-2")
    assert len(ops(backend, "spawn_procedure_step")) == 1
    await launcher.stop_exec("ps-1", "scout")
    await asyncio.wait_for(first, 2)


async def test_client_leaving_mid_step_stops_the_executor_in_the_container(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    task = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("kill_procedure_step", "bot-scout", "ps-1", "TERM") in backend.log
    assert not ops(backend, "kill_in_container") and "ps-1" not in launcher.active_execs()


async def test_failed_signal_is_logged_without_data_and_the_local_client_is_killed(launcher, backend, caplog):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)

    async def refuse(container, exec_id, signal_name):
        raise BackendError("docker exec упал")

    backend.kill_procedure_step = refuse
    task = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    with caplog.at_level("WARNING"):
        await launcher.stop_exec("ps-1", "scout")
        answer = await asyncio.wait_for(task, 2)
    assert answer["reason"] == "stopped" and backend.last_exec.killed  # сигналы не прошли: убит локальный docker exec
    assert "kill TERM ps-1" in caplog.text and VALUE not in caplog.text


# ---------- procedure_step_cancel (пункт 11 ревью Opus) ----------

async def test_cancel_terminates_a_running_step_under_uid_1001(launcher, backend):
    await launcher.create_bot("scout", "o1")
    backend.next_exec = FakeExec(hang=True)
    task = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    answer = await launcher.procedure_step_cancel("scout", "ps-1")
    assert answer == {"bot_id": "scout", "exec_id": "ps-1", "stopped": True, "was_running": True}
    step = await asyncio.wait_for(task, 2)
    assert step["reason"] == "stopped" and step["result"] is None
    assert ("kill_procedure_step", "bot-scout", "ps-1", "TERM") in backend.log
    assert not ops(backend, "kill_in_container")  # под uid 1000 процесс uid 1001 не достать


async def test_cancel_after_a_launcher_restart_signals_by_pid_file_and_marker(launcher, backend):
    await launcher.create_bot("scout", "o1")
    answer = await launcher.procedure_step_cancel("scout", "ps-lost")  # реестра exec нет
    assert answer["was_running"] is False and answer["stopped"] is True
    assert [e[3] for e in ops(backend, "kill_procedure_step")] == ["TERM", "KILL"]
    assert all(e[1:3] == ("bot-scout", "ps-lost") for e in ops(backend, "kill_procedure_step"))
    assert not ops(backend, "kill_in_container")


async def test_cancel_does_not_touch_other_bots_ordinary_execs_or_missing_bots(launcher, backend):
    await launcher.create_bot("scout", "o1")
    await launcher.create_bot("rover", "o1")
    backend.next_exec = FakeExec(hang=True)
    task = asyncio.create_task(launcher.procedure_step("scout", PAYLOAD, False, 30, "ps-1"))
    await asyncio.sleep(0.05)
    with pytest.raises(NotFound):
        await launcher.procedure_step_cancel("rover", "ps-1")  # шаг чужого бота
    handle = await launcher.start_exec("rover", ["sleep", "9"], exec_id="plain-1")
    with pytest.raises(NotFound):
        await launcher.procedure_step_cancel("rover", "plain-1")  # обычный exec этой операцией не гасится
    with pytest.raises(NotFound):
        await launcher.procedure_step_cancel("ghost", "ps-9")
    with pytest.raises(ValidationFailed):
        await launcher.procedure_step_cancel("scout", "bad id; rm")
    assert not [e for e in ops(backend, "kill_procedure_step") if e[1] == "bot-rover"]
    await launcher.stop_exec("plain-1", "rover")
    await launcher.procedure_step_cancel("scout", "ps-1")
    await asyncio.wait_for(task, 2)
    assert handle.exec_id == "plain-1"


def test_cancel_route_needs_the_secret_and_a_strict_body(cfg, backend):
    app = create_app(cfg, backend=backend, netpolicy=FakeNetPolicy(order=backend.order))
    with TestClient(app) as client:
        client.post("/v1/bots", json={"bot_id": "scout", "owner_id": "o1"}, headers=AUTH)
        url = "/v1/bots/scout/procedure-step/cancel"
        assert client.post(url, json={"exec_id": "ps-1"}).status_code in (401, 403)
        ok = client.post(url, json={"exec_id": "ps-1"}, headers=AUTH)
        assert ok.status_code == 200 and ok.json()["was_running"] is False
        assert client.post(url, json={"exec_id": "ps-1", "extra": 1}, headers=AUTH).status_code == 400
        assert client.post(url, json={}, headers=AUTH).status_code == 400
        assert client.post("/v1/bots/ghost/procedure-step/cancel", json={"exec_id": "ps-1"}, headers=AUTH).status_code == 404
