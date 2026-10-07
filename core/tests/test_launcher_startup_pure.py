"""Старт ядра при недоступном лаунчере: фоновая сверка с повторами, 503 до неё, отмена при остановке."""
import asyncio
import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import bothub.main as main
from bothub import auth
from bothub.launcher_client import FakeBot, FakeLauncherClient, LauncherNotFound, LauncherUnavailable
from bothub.main import create_app

OWNER = uuid.UUID(int=1)
THREAD = uuid.UUID(int=3)
TURN = uuid.UUID(int=4)
SECRET = "startup-test-secret"
CREATED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

pytestmark = pytest.mark.pure


def down(count):
    return [LauncherUnavailable("лаунчер недоступен: ConnectError", code="unavailable") for _ in range(count)]


class StartupConnection:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch(self, query, *args):
        bots = self.pool.bots
        if "select id from bothub.bots where executor='container' and provider='claude'" in query:
            return [{"id": key} for key, bot in bots.items() if bot["executor"] == "container" and bot["provider"] == "claude"]
        if query == "select id from bothub.bots where executor='container'":
            return [{"id": key} for key, bot in bots.items() if bot["executor"] == "container"]
        if "select t.id,t.thread_id,th.bot_id from bothub.turns t" in query:
            return [{"id": turn["id"], "thread_id": THREAD, "bot_id": turn["bot_id"]}
                    for turn in self.pool.turns if turn["status"] == "running"]
        return []

    async def fetchval(self, query, *args):
        if query == "select 1":
            return 1
        if query == "select 1 from bothub.users limit 1":
            return 1 if self.pool.has_users else None
        if "select browser_control from bothub.bots where id=$1 for update" in query:
            return self.pool.bots[args[0]]["browser_control"]
        if "update bothub.threads set last_seq" in query:
            return 1
        return None

    async def fetchrow(self, query, *args):
        if "from bothub.sessions s join bothub.users u" in query:
            return self.pool.sessions.get(args[0])
        if "select b.owner_id from bothub.bots b join bothub.sessions s" in query:
            bot, session = self.pool.bots.get(args[0]), self.pool.sessions.get(args[1])
            return {"owner_id": bot["owner_id"]} if bot and session and session["user_id"] == bot["owner_id"] else None
        if "select b.owner_id from bothub.bots b join bothub.users u" in query:
            return {"owner_id": OWNER}
        if "select b.browser_control,b.provider,b.executor from bothub.turns t" in query:
            if args[2] != "alpha":  # the only turn belongs to alpha: another bot's token sees no such turn
                return None
            bot = self.pool.bots[args[2]]
            return {"browser_control": bot["browser_control"], "provider": bot["provider"], "executor": bot["executor"]}
        if "from bothub.bots where id=$1 and owner_id=$2" in query:
            bot = self.pool.bots.get(args[0])  # select * (takeover, return, recreate) or select id (delete)
            return dict(bot) if bot and bot["owner_id"] == args[1] else None
        if "from bothub.threads where bot_id" in query:
            return {"id": THREAD}
        if "insert into bothub.events" in query:
            return {"id": uuid.uuid4(), "thread_id": args[0], "seq": args[1], "turn_id": args[2], "kind": args[3],
                    "actor": args[4], "client": None, "payload": {}}
        return None

    async def execute(self, query, *args):
        if "update bothub.bots set browser_control='returning' where id=any($1" in query:
            for key in args[0]:
                self.pool.bots[key]["browser_control"] = "returning"
        elif "update bothub.bots set browser_control='bot' where browser_control<>'bot' and not (id=any($1" in query:
            for key, bot in self.pool.bots.items():
                if key not in args[0]:
                    bot["browser_control"] = "bot"
        elif "update bothub.turns set status='error'" in query:
            for turn in self.pool.turns:
                if turn["id"] == args[0]:
                    turn["status"] = "error"
        return "OK"


class StartupPool:
    def __init__(self, bots, *, has_users=False, turns=()):
        self.bots = bots
        self.has_users = has_users
        self.turns = [dict(turn) for turn in turns]
        self.sessions = {}
        self.connection = StartupConnection(self)
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def close(self):
        self.closed = True


class QuietGateway:
    async def startup(self):
        pass

    async def shutdown(self):
        pass


def container_bot(state="human"):
    return {"id": "alpha", "owner_id": OWNER, "executor": "container", "provider": "claude",
            "browser_control": state, "created_at": CREATED_AT}


def make_app(monkeypatch, launcher, pool, *, delay=0.01, cap=0.04, first_wait=1.0):
    async def open_pool():
        return pool

    monkeypatch.setattr(main, "open_pool", open_pool)
    monkeypatch.setattr(main, "LAUNCHER_SYNC_DELAY_START", delay)
    monkeypatch.setattr(main, "LAUNCHER_SYNC_DELAY_MAX", cap)
    monkeypatch.setattr(main, "LAUNCHER_SYNC_FIRST_WAIT", first_wait)
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "docker")
    monkeypatch.setenv("BOTHUB_GATEWAY_TOKEN_SECRET", "gateway-secret")
    for key in pool.bots:
        launcher.bots[key] = FakeBot(key, str(OWNER))
    launcher.frozen.update(pool.bots)
    app = create_app(launcher=launcher)
    app.state.gateway = QuietGateway()
    return app


def owner_headers(pool, user=OWNER, token="session-owner"):
    pool.sessions[auth.token_hash(token)] = {
        "user_id": user, "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
        "last_extended_at": datetime.now(timezone.utc), "role": "admin", "status": "active"}
    return {"Cookie": f"bothub_session={token}", "Origin": "https://testserver",
            "X-CSRF": auth.csrf_token(token, SECRET)}


def bot_headers(bot="alpha"):
    sign = hmac.new(SECRET.encode(), bot.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer bot:{bot}:{sign}"}


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


async def wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def sync_tasks():
    return [task for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__qualname__", "").endswith("launcher_startup_sync")]


BROWSER_CALL = {"thread_id": str(THREAD), "turn_id": str(TURN), "action": "snapshot"}


async def test_core_starts_while_launcher_is_down_and_syncs_when_it_answers(monkeypatch, caplog):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("human")})
    app = make_app(monkeypatch, launcher, pool)
    for exc in down(3):
        launcher.fail_next(exc)  # list_bots at the start of each of the first three passes
    with caplog.at_level("WARNING"):
        async with app.router.lifespan_context(app):  # no "Application startup failed"
            assert app.state.launcher_ready is False
            await wait_for(lambda: app.state.launcher_ready)
            await wait_for(lambda: not sync_tasks())
    assert [call[0] for call in launcher.calls].count("list_bots") == 4, "three refusals, then the pass that went through"
    assert launcher.frozen == set(), "the bot is unfrozen once, after the launcher answered"
    assert pool.bots["alpha"]["browser_control"] == "returning", "the restart reset stays in the database"
    failed = [record for record in caplog.records if record.getMessage() == "launcher_startup_sync_failed"]
    assert [record.attempt for record in failed] == [1, 2, 3]
    assert [record.retry_in for record in failed] == [0.01, 0.02, 0.04], "the pause grows and stops at the cap"
    assert pool.closed


async def test_retry_pause_stays_at_the_cap_without_a_limit_on_attempts(monkeypatch, caplog):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")})
    app = make_app(monkeypatch, launcher, pool, delay=0.001, cap=0.004)
    for exc in down(8):
        launcher.fail_next(exc)
    with caplog.at_level("WARNING"):
        async with app.router.lifespan_context(app):
            await wait_for(lambda: app.state.launcher_ready, timeout=5)
    pauses = [record.retry_in for record in caplog.records if record.getMessage() == "launcher_startup_sync_failed"]
    assert pauses == [0.001, 0.002, 0.004, 0.004, 0.004, 0.004, 0.004, 0.004]


async def test_launcher_routes_answer_503_and_the_rest_of_the_api_works_before_the_sync(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("human")})
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    for exc in down(10_000):
        launcher.fail_next(exc)  # the launcher stays down for as long as the test looks
    owner = owner_headers(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await asyncio.sleep(0.05)  # several failed passes already
            assert app.state.launcher_ready is False
            health = await client.get("/api/health")
            assert health.status_code == 200 and health.json()["launcher"] == "unavailable"
            assert health.json()["status"] == "ok"
            state = await client.get("/api/bots/alpha/browser", headers=owner)
            assert state.status_code == 200 and state.json()["state"] == "returning"  # the reset, no launcher needed
            before = len(launcher.calls)
            for response in (
                await client.post("/api/bots/alpha/browser/takeover", headers=owner),
                await client.post("/api/bots/alpha/browser/return", headers=owner),
                await client.post("/api/bots/alpha/browser/secret-input", json={"value": "x"}, headers=owner),
                await client.post("/api/bots/alpha/recreate", headers=owner),
                await client.delete("/api/bots/alpha", headers=owner),
                await client.post("/api/browser/authorize", json=BROWSER_CALL, headers=bot_headers()),
                await client.post("/api/browser/step", json=BROWSER_CALL, headers=bot_headers()),
            ):
                assert response.status_code == 503, response.text
                assert response.json()["error"] == "launcher_unavailable"
            assert pool.bots["alpha"]["browser_control"] == "returning", "nothing took control or changed the state"
            assert all(call[0] == "list_bots" for call in launcher.calls[before:]), "routes did not reach the launcher"
            assert "alpha" in launcher.frozen and not launcher.browsers


async def test_returning_bot_gets_no_browser_authorization_until_the_sync_finishes(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("human")})
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    for exc in down(40):
        launcher.fail_next(exc)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            refused = await client.post("/api/browser/authorize", json=BROWSER_CALL, headers=bot_headers())
            assert refused.status_code == 503
            assert not launcher.browsers and "alpha" in launcher.frozen
            await wait_for(lambda: app.state.launcher_ready)
            granted = await client.post("/api/browser/authorize", json=BROWSER_CALL, headers=bot_headers())
    assert granted.status_code == 200 and "authorization_id" in granted.json()
    assert "alpha" not in launcher.frozen


async def test_routes_work_and_health_is_ok_after_the_sync(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")})
    app = make_app(monkeypatch, launcher, pool)
    launcher.fail_next(down(1)[0])
    owner = owner_headers(pool)
    async with app.router.lifespan_context(app):
        await wait_for(lambda: app.state.launcher_ready)
        await wait_for(lambda: not sync_tasks())
        async with client_for(app) as client:
            health = await client.get("/api/health")
            assert health.status_code == 200 and health.json()["launcher"] == "ok"
            taken = await client.post("/api/bots/alpha/browser/takeover", headers=owner)
    assert taken.status_code == 200 and taken.json()["state"] == "human"
    assert ("freeze_bot", "alpha") in launcher.calls


async def test_background_sync_is_cancelled_when_the_app_stops(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("human")})
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    for exc in down(100_000):
        launcher.fail_next(exc)
    async with app.router.lifespan_context(app):
        await wait_for(lambda: len(launcher.calls) >= 3)
        assert len(sync_tasks()) == 1
    assert sync_tasks() == [], "the task ended with the application"
    calls = len(launcher.calls)
    await asyncio.sleep(0.05)
    assert len(launcher.calls) == calls, "no attempts after the stop"
    assert app.state.launcher_ready is False


async def test_sync_is_cancelled_in_the_middle_of_a_pass(monkeypatch):
    class SlowLauncher(FakeLauncherClient):
        async def list_bots(self):
            await asyncio.sleep(60)

    launcher = SlowLauncher()
    pool = StartupPool({"alpha": container_bot("human")})
    app = make_app(monkeypatch, launcher, pool, first_wait=0.05)
    async with app.router.lifespan_context(app):  # the first pass hangs: startup does not wait for it for good
        assert len(sync_tasks()) == 1 and app.state.launcher_ready is False
    assert sync_tasks() == []


async def test_stale_turn_is_stopped_again_after_a_failed_pass(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")}, has_users=True,
                       turns=[{"id": TURN, "bot_id": "alpha", "status": "running"}])
    app = make_app(monkeypatch, launcher, pool)
    started = []

    async def idle():
        started.append(True)
        await asyncio.sleep(3600)

    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    # list_bots and unfreeze pass, the first stop_exec fails: the turn must stay stale for the next pass
    original = launcher.stop_exec
    attempts = []

    async def flaky_stop(exec_id, *, bot_id=None):
        attempts.append(exec_id)
        if len(attempts) == 1:
            raise LauncherUnavailable("лаунчер недоступен: ConnectError", code="unavailable")
        return await original(exec_id, bot_id=bot_id)

    launcher.stop_exec = flaky_stop
    async with app.router.lifespan_context(app):
        await wait_for(lambda: pool.turns[0]["status"] == "error")
        await wait_for(lambda: len(started) == 3)
    assert attempts == [str(TURN)] * 2, "stopped before the status changed, so the second pass stopped it again"
    assert launcher.stopped == [str(TURN)]


async def test_health_is_syncing_until_the_recovery_after_the_unfreeze_is_done(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")}, has_users=True,
                       turns=[{"id": TURN, "bot_id": "alpha", "status": "running"}])
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    app.state.worker = app.state.scheduler = app.state.outbox_sender = lambda: asyncio.sleep(3600)
    gate = asyncio.Event()
    original = launcher.stop_exec

    async def gated_stop(exec_id, *, bot_id=None):
        if not gate.is_set():
            raise LauncherUnavailable("лаунчер недоступен: ConnectError", code="unavailable")
        return await original(exec_id, bot_id=bot_id)

    launcher.stop_exec = gated_stop
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await wait_for(lambda: app.state.launcher_ready)
            await asyncio.sleep(0.03)  # several passes: the unfreeze is done, the stale turn still is not stopped
            health = await client.get("/api/health")
            assert health.status_code == 200 and health.json()["launcher"] == "syncing", "routes open, sync not finished"
            gate.set()
            await wait_for(lambda: not sync_tasks())
            health = await client.get("/api/health")
            assert health.json()["launcher"] == "ok" and pool.turns[0]["status"] == "error"


async def test_health_is_unavailable_until_the_unfreeze_pass_goes_through(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")}, has_users=True)
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    app.state.worker = app.state.scheduler = app.state.outbox_sender = lambda: asyncio.sleep(3600)
    for exc in down(10_000):
        launcher.fail_next(exc)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await asyncio.sleep(0.03)
            assert (await client.get("/api/health")).json()["launcher"] == "unavailable"


async def test_sync_failures_log_a_warning_and_from_the_fifth_attempt_an_error_with_the_text(monkeypatch, caplog):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")})
    app = make_app(monkeypatch, launcher, pool, delay=0.001, cap=0.002)
    for exc in down(7):
        launcher.fail_next(exc)
    with caplog.at_level("WARNING"):
        async with app.router.lifespan_context(app):
            await wait_for(lambda: app.state.launcher_ready, timeout=5)
    failed = [record for record in caplog.records if record.getMessage() == "launcher_startup_sync_failed"]
    assert [record.attempt for record in failed] == list(range(1, 8)), "one record per attempt"
    assert [record.levelname for record in failed] == ["WARNING"] * 4 + ["ERROR"] * 3
    for record in failed[4:]:
        assert "ConnectError" in record.error and record.exc_info and "ConnectError" in str(record.exc_info[1])


async def test_worker_waits_for_the_sync_scheduler_and_outbox_do_not(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")}, has_users=True)
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    for exc in down(30):
        launcher.fail_next(exc)
    names = []

    def idle(name):
        async def run():
            names.append(name)
            await asyncio.sleep(3600)
        return run

    app.state.worker, app.state.scheduler, app.state.outbox_sender = idle("worker"), idle("scheduler"), idle("outbox")
    async with app.router.lifespan_context(app):
        await wait_for(lambda: {"scheduler", "outbox"} <= set(names), timeout=0.5)
        assert not app.state.launcher_ready, "scheduler and outbox run before the sync, which is still failing"
        await asyncio.sleep(0.03)
        assert not app.state.launcher_ready and "worker" not in names, "no turn is claimed before the sync"
        await wait_for(lambda: "worker" in names)
        assert app.state.launcher_ready
    assert sorted(names) == ["outbox", "scheduler", "worker"] and len(names) == 3


async def test_procedure_runner_starts_after_the_sync_and_only_with_a_launcher(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("bot")}, has_users=True)
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    for exc in down(30):
        launcher.fail_next(exc)
    names = []

    def idle(name):
        async def run():
            names.append(name)
            await asyncio.sleep(3600)
        return run

    app.state.worker, app.state.scheduler, app.state.outbox_sender = idle("worker"), idle("scheduler"), idle("outbox")
    app.state.procedure_runner_loop = idle("procedure_runner")
    async with app.router.lifespan_context(app):
        await wait_for(lambda: {"scheduler", "outbox"} <= set(names), timeout=0.5)
        await asyncio.sleep(0.03)
        assert "procedure_runner" not in names, "steps need the launcher: not before the sync"
        await wait_for(lambda: "procedure_runner" in names)
        assert app.state.launcher_ready and len(app.state.worker_tasks) == 4
    # без лаунчера (режим local) запусков нет и исполнитель не стартует
    local = StartupPool({"alpha": container_bot("human")}, has_users=True)

    async def open_pool():
        return local

    monkeypatch.setattr(main, "open_pool", open_pool)
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "local")
    monkeypatch.delenv("LAUNCHER_SOCKET", raising=False)
    monkeypatch.delenv("LAUNCHER_URL", raising=False)
    plain = create_app()
    plain.state.gateway = QuietGateway()
    names.clear()
    plain.state.worker, plain.state.scheduler, plain.state.outbox_sender = idle("worker"), idle("scheduler"), idle("outbox")
    plain.state.procedure_runner_loop = idle("procedure_runner")
    async with plain.router.lifespan_context(plain):
        await asyncio.sleep(0.02)
        assert sorted(names) == ["outbox", "scheduler", "worker"]


async def test_procedure_runner_loop_recovers_once_then_ticks_and_shuts_down(monkeypatch):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_PROCEDURE_INTERVAL", "0.005")
    app = create_app()
    calls = []

    class Runner:
        async def recover(self):
            calls.append("recover")

        async def tick(self):
            calls.append("tick")
            if calls.count("tick") == 2:
                raise RuntimeError("one bad pass must not stop the loop")
            return False

        async def shutdown(self):
            calls.append("shutdown")

    app.state.procedure_runner = Runner()
    task = asyncio.create_task(app.state.procedure_runner_loop())
    await wait_for(lambda: calls.count("tick") >= 4, timeout=2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls[0] == "recover" and calls.count("recover") == 1 and calls[-1] == "shutdown"
    assert calls.count("tick") >= 4


async def test_release_browser_hook_swallows_a_refused_transition(monkeypatch):
    """returning -> bot для запуска: отказ перехода (не returning, лаунчер не сверен) запуск не роняет."""
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    app = create_app()
    runner = app.state.procedure_runner
    assert runner._release is not None
    await runner._release("nobody", OWNER)  # пула нет: любая ошибка внутри поглощается


async def test_startup_without_a_launcher_is_unchanged(monkeypatch):
    pool = StartupPool({"alpha": container_bot("human")}, has_users=True)

    async def open_pool():
        return pool

    monkeypatch.setattr(main, "open_pool", open_pool)
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "local")
    monkeypatch.delenv("LAUNCHER_SOCKET", raising=False)
    monkeypatch.delenv("LAUNCHER_URL", raising=False)
    app = create_app()
    app.state.gateway = QuietGateway()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = lambda: asyncio.sleep(3600)
    async with app.router.lifespan_context(app):
        assert app.state.launcher_ready is True and len(app.state.worker_tasks) == 3
        assert not sync_tasks()
        async with client_for(app) as client:
            health = await client.get("/api/health")
    assert health.json()["launcher"] == "ok"


async def test_not_found_container_does_not_block_the_sync(monkeypatch):
    class Ghost(FakeLauncherClient):
        async def unfreeze_bot(self, bot_id):
            raise LauncherNotFound("no container", code="not_found", status=404)

    launcher = Ghost()
    pool = StartupPool({"alpha": container_bot("bot")})
    app = make_app(monkeypatch, launcher, pool)
    async with app.router.lifespan_context(app):
        await wait_for(lambda: app.state.launcher_ready)


async def test_healthy_launcher_is_synced_before_the_first_request(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool({"alpha": container_bot("human")})
    app = make_app(monkeypatch, launcher, pool)
    async with app.router.lifespan_context(app):
        assert app.state.launcher_ready is True, "a normal restart has no window with 503"
        assert launcher.frozen == set()


# Порядок проверок до готовности лаунчера: аутентификация, CSRF, владелец и существование (404 чужому), тело, и только
# потом 503. Иначе 503 выдаёт чужому, что бот существует и запрос дошёл до обработки.
STRANGER = uuid.UUID(int=2)
DOWN_FOREVER = 10_000


def two_owner_bots():
    return {"alpha": container_bot("bot"), "beta": {**container_bot("bot"), "id": "beta", "owner_id": STRANGER}}


def waiting_app(monkeypatch):
    launcher = FakeLauncherClient()
    pool = StartupPool(two_owner_bots())
    app = make_app(monkeypatch, launcher, pool, delay=0.005, cap=0.005)
    for exc in down(DOWN_FOREVER):
        launcher.fail_next(exc)
    return app, pool, launcher


BOT_ROUTES = (
    ("POST", "/api/bots/alpha/browser/takeover", None),
    ("POST", "/api/bots/alpha/browser/return", None),
    ("POST", "/api/bots/alpha/browser/secret-input", {"value": "x"}),
    ("POST", "/api/bots/alpha/recreate", None),
    ("DELETE", "/api/bots/alpha", None),
)


async def test_stranger_gets_404_and_the_owner_503_while_the_launcher_is_not_ready(monkeypatch):
    app, pool, launcher = waiting_app(monkeypatch)
    owner = owner_headers(pool)
    stranger = owner_headers(pool, STRANGER, "session-stranger")
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await asyncio.sleep(0.03)
            assert app.state.launcher_ready is False
            state = pool.bots["alpha"]["browser_control"]  # "returning": the restart reset, done without the launcher
            for method, path, body in BOT_ROUTES:
                foreign = await client.request(method, path, json=body, headers=stranger)
                assert foreign.status_code == 404 and foreign.json()["error"] == "not_found", (path, foreign.text)
                own = await client.request(method, path, json=body, headers=owner)
                assert own.status_code == 503 and own.json()["error"] == "launcher_unavailable", (path, own.text)
            assert pool.bots["alpha"]["browser_control"] == state and set(pool.bots) == {"alpha", "beta"}, "the refusals changed nothing"


async def test_missing_session_and_missing_csrf_are_answered_before_the_launcher_503(monkeypatch):
    app, pool, _ = waiting_app(monkeypatch)
    owner = owner_headers(pool)
    no_csrf = {key: value for key, value in owner.items() if key != "X-CSRF"}
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await asyncio.sleep(0.03)
            assert app.state.launcher_ready is False
            routes = BOT_ROUTES + (("POST", "/api/bots/draft", {"description": "valid draft text"}),
                                   ("POST", "/api/bots", {"name": "Own", "provider": "fake", "model": "fake"}))
            for method, path, body in routes:
                anonymous = await client.request(method, path, json=body)
                assert anonymous.status_code == 401, (path, anonymous.text)
                forged = await client.request(method, path, json=body, headers=no_csrf)
                assert forged.status_code == 403 and forged.json()["detail"] == "csrf", (path, forged.text)
            for path in ("/api/browser/authorize", "/api/browser/step"):
                anonymous = await client.post(path, json=BROWSER_CALL)
                assert anonymous.status_code == 401, (path, anonymous.text)


async def test_foreign_bot_token_gets_404_on_browser_steps_and_the_own_one_503(monkeypatch):
    app, pool, launcher = waiting_app(monkeypatch)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await asyncio.sleep(0.03)
            assert app.state.launcher_ready is False
            for path in ("/api/browser/authorize", "/api/browser/step"):
                foreign = await client.post(path, json=BROWSER_CALL, headers=bot_headers("beta"))
                assert foreign.status_code == 404, (path, foreign.text)
                own = await client.post(path, json=BROWSER_CALL, headers=bot_headers())
                assert own.status_code == 503 and own.json()["error"] == "launcher_unavailable", (path, own.text)
            assert not launcher.browsers


async def test_a_stale_browser_step_gets_its_409_before_the_launcher_503(monkeypatch):
    app, pool, _ = waiting_app(monkeypatch)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await asyncio.sleep(0.03)
            assert pool.bots["alpha"]["browser_control"] == "returning" and app.state.launcher_ready is False
            stale = await client.post("/api/browser/step", json={**BROWSER_CALL, "action": "click"}, headers=bot_headers())
    assert stale.status_code == 409 and stale.json()["error"] == "browser_stale"


async def test_invalid_body_gets_its_error_before_the_launcher_503(monkeypatch):
    app, pool, _ = waiting_app(monkeypatch)
    owner = owner_headers(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await asyncio.sleep(0.03)
            assert app.state.launcher_ready is False
            secret = await client.post("/api/bots/alpha/browser/secret-input", json={"value": ""}, headers=owner)
            draft = await client.post("/api/bots/draft", json={"description": "short"}, headers=owner)
            bot = await client.post("/api/bots", json={"name": "Own", "provider": "fake", "model": "fake",
                                                       "mcp_allow": ["x"] * 201}, headers=owner)
            valid_draft = await client.post("/api/bots/draft", json={"description": "valid draft text"}, headers=owner)
            valid_bot = await client.post("/api/bots", json={"name": "Own", "provider": "fake", "model": "fake"},
                                          headers=owner)
    assert secret.status_code == 400 and draft.status_code == 400 and bot.status_code == 400
    assert valid_draft.status_code == 503 and valid_bot.status_code == 503
    assert valid_bot.json()["error"] == "launcher_unavailable"
    assert set(pool.bots) == {"alpha", "beta"}, "no bot was written before the 503"


async def test_without_a_launcher_the_order_and_codes_stay_as_before_the_sync(monkeypatch):
    pool = StartupPool(two_owner_bots())

    async def open_pool():
        return pool

    monkeypatch.setattr(main, "open_pool", open_pool)
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "local")
    monkeypatch.delenv("LAUNCHER_SOCKET", raising=False)
    monkeypatch.delenv("LAUNCHER_URL", raising=False)
    app = create_app()
    app.state.gateway = QuietGateway()
    owner = owner_headers(pool)
    stranger = owner_headers(pool, STRANGER, "session-stranger")
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            for path in ("takeover", "return"):
                foreign = await client.post(f"/api/bots/alpha/browser/{path}", headers=stranger)
                assert foreign.status_code == 404, (path, foreign.text)
            takeover = await client.post("/api/bots/alpha/browser/takeover", headers=owner)
            assert takeover.status_code == 503 and takeover.json()["error"] == "launcher_unavailable"
            recreate = await client.post("/api/bots/alpha/recreate", headers=owner)
            assert recreate.status_code == 503
            secret = await client.post("/api/bots/alpha/browser/secret-input", json={"value": "x"}, headers=owner)
            assert secret.status_code == 409 and secret.json()["error"] == "human_required", "as before: no launcher check here"
            step = await client.post("/api/browser/step", json=BROWSER_CALL, headers=bot_headers())
            assert step.status_code == 503
            health = await client.get("/api/health")
    assert health.json()["launcher"] == "ok"


def test_screen_socket_checks_the_owner_before_it_closes_with_the_launcher_code(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app, pool, _ = waiting_app(monkeypatch)
    owner = owner_headers(pool)
    stranger = owner_headers(pool, STRANGER, "session-stranger")
    ws_url = "/api/bots/alpha/screen"

    def close_code(headers):
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(ws_url, headers=headers):
                pass
        return closed.value.code

    with TestClient(app, base_url="https://testserver") as client:
        assert app.state.launcher_ready is False
        assert close_code({"Origin": "https://testserver", "Cookie": stranger["Cookie"]}) == 4404, "a stranger learns nothing"
        assert close_code({"Origin": "https://testserver", "Cookie": owner["Cookie"]}) == 1013, "the owner gets the launcher code"
        assert close_code({"Cookie": owner["Cookie"]}) == 4401, "no Origin: refused before anything else"
