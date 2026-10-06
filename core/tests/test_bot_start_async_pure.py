"""Создание бота отвечает сразу (201, status starting), сеть, контейнер и браузер идут фоном; сеть пользователя
подключается к ядру при создании пользователя. Без Postgres и Docker: пул и лаунчер фейковые."""
import asyncio
import hashlib
import hmac
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import bothub.main as main
from bothub import auth
from bothub.launcher_client import FakeLauncherClient, LauncherUnavailable
from bothub.main import create_app

OWNER = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)
SECRET = "async-start-secret"
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.pure

NEW_BOT = {"name": "Scout", "provider": "fake", "model": "fake"}


class Connection:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch(self, query, *args):
        bots = self.pool.bots
        if query == "select id from bothub.bots where status='starting'":
            return [{"id": key} for key, bot in bots.items() if bot["status"] == "starting"]
        if "select * from bothub.bots where owner_id=$1 order by created_at" in query:
            return [dict(bot) for bot in bots.values() if bot["owner_id"] == args[0]]
        return []

    async def fetchval(self, query, *args):
        if query == "select 1 from bothub.users limit 1":
            return 1 if self.pool.has_users else None
        if query.startswith("select count(*)"):
            return 0
        if query == "select 1 from bothub.bots where id=$1":
            return 1 if args[0] in self.pool.bots else None
        return None  # провайдеров у владельца нет, настроек нет

    async def fetchrow(self, query, *args):
        bots = self.pool.bots
        if "from bothub.sessions s join bothub.users u" in query:
            return self.pool.sessions.get(args[0])
        if "from bothub.bots where id=$1 and owner_id=$2 and status='starting'" in query:
            bot = bots.get(args[0])
            return dict(bot) if bot and bot["owner_id"] == args[1] and bot["status"] == "starting" else None
        if "select id,owner_id from bothub.bots where id=$1 and status='starting'" in query:
            bot = bots.get(args[0])
            return {"id": bot["id"], "owner_id": bot["owner_id"]} if bot and bot["status"] == "starting" else None
        if query.startswith("insert into bothub.bots ("):
            columns = query[query.index("(") + 1:query.index(") values")].split(",")
            row = {"status": "idle", "need_restart": False, "browser_control": "bot", "paused": False,
                   "created_at": NOW, **dict(zip(columns, args))}
            if row["id"] in bots:
                import asyncpg
                raise asyncpg.UniqueViolationError("duplicate")
            bots[row["id"]] = row
            self.pool.inserts.append(row["id"])
            return dict(row)
        return None

    async def execute(self, query, *args):
        bots = self.pool.bots
        if "update bothub.bots set status=$2 where id=$1 and status='starting'" in query:
            bot = bots.get(args[0])
            if bot and bot["status"] == "starting":
                bot["status"] = args[1]
                return "UPDATE 1"
            return "UPDATE 0"
        return "OK"


class Pool:
    def __init__(self, bots=(), *, has_users=True):
        self.bots = {bot["id"]: bot for bot in bots}
        self.has_users = has_users
        self.sessions = {}
        self.inserts = []
        self.connection = Connection(self)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def close(self):
        pass


class Quiet:
    async def startup(self):
        pass

    async def shutdown(self):
        pass


class BrokenCreate(FakeLauncherClient):
    """create_bot отказывает столько раз, сколько задано (list_bots стартовой сверки не трогает)."""

    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    async def create_bot(self, bot_id, owner_id):
        if self.failures:
            self.failures -= 1
            self.calls.append(("create_bot", bot_id, owner_id))
            raise LauncherUnavailable("лаунчер недоступен: ConnectError", code="unavailable")
        return await super().create_bot(bot_id, owner_id)


def calls_of(launcher, *names):
    return [call for call in launcher.calls if call[0] in names]


class SlowLauncher(FakeLauncherClient):
    """create_bot и ensure_network ждут ворота: пока они закрыты, любой вызов лаунчера виден как зависший запрос."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def create_bot(self, bot_id, owner_id):
        self.entered.set()
        await self.gate.wait()
        return await super().create_bot(bot_id, owner_id)

    async def ensure_network(self, owner_id):
        self.entered.set()
        await self.gate.wait()
        return await super().ensure_network(owner_id)


def starting_bot(bot_id="scout-aaaa", owner=OWNER):
    return {"id": bot_id, "owner_id": owner, "name": "Scout", "status": "starting", "need_restart": False,
            "executor": "container", "provider": "fake", "created_at": NOW}


def make_app(monkeypatch, launcher, pool, *, delay=0.0, legacy_ids=False):
    async def open_pool():
        return pool

    if legacy_ids:
        monkeypatch.setenv("BOTHUB_TEST_LEGACY_IDS", "1")
    else:
        monkeypatch.delenv("BOTHUB_TEST_LEGACY_IDS", raising=False)  # как в проде: id выдаёт ядро

    monkeypatch.setattr(main, "open_pool", open_pool)
    monkeypatch.setattr(main, "BOT_START_DELAY", delay)
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "docker")
    monkeypatch.setenv("BOTHUB_GATEWAY_TOKEN_SECRET", "gateway-secret")
    app = create_app(launcher=launcher)
    app.state.gateway = Quiet()
    return app


def headers_for(pool, user=OWNER, token=None):
    token = token or f"session-{user}"
    pool.sessions[auth.token_hash(token)] = {
        "user_id": user, "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
        "last_extended_at": datetime.now(timezone.utc), "role": "admin", "status": "active"}
    return {"Cookie": f"bothub_session={token}", "Origin": "https://testserver",
            "X-CSRF": auth.csrf_token(token, SECRET)}


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


async def wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def test_post_answers_201_starting_without_waiting_for_the_launcher(monkeypatch):
    launcher, pool = SlowLauncher(), Pool()
    app = make_app(monkeypatch, launcher, pool)
    owner = headers_for(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            async with asyncio.timeout(1):  # лаунчер заперт: ответ не может ждать его
                response = await client.post("/api/bots", json=NEW_BOT, headers=owner)
            assert response.status_code == 201, response.text
            body = response.json()
            assert re.fullmatch(r"scout-[a-z0-9]{4}", body["id"])
            assert (body["status"], body["container"], body["recreate_url"]) == ("starting", "starting", None)
            assert pool.bots[body["id"]]["status"] == "starting" and pool.inserts == [body["id"]]
            await asyncio.wait_for(launcher.entered.wait(), 1)  # фон дошёл до лаунчера уже после ответа
            assert pool.bots[body["id"]]["status"] == "starting", "статус держится, пока контейнер не создан"
            listed = (await client.get("/api/bots", headers=owner)).json()
            assert [(bot["id"], bot["status"]) for bot in listed] == [(body["id"], "starting")]
            launcher.gate.set()
            await wait_for(lambda: pool.bots[body["id"]]["status"] == "idle")
    assert calls_of(launcher, "create_bot") == [("create_bot", body["id"], str(OWNER))]


async def test_the_launcher_is_not_called_before_the_response_is_out(monkeypatch):
    # ответ уходит клиенту раньше первого вызова лаунчера: пауза BOT_START_DELAY держит фон, пока ответ не сброшен в сокет
    launcher, pool = FakeLauncherClient(), Pool()
    app = make_app(monkeypatch, launcher, pool, delay=0.15)
    owner = headers_for(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.post("/api/bots", json=NEW_BOT, headers=owner)
            assert response.status_code == 201
            assert calls_of(launcher, "create_bot", "ensure_network") == [], "к моменту ответа лаунчер не вызывался"
            await wait_for(lambda: pool.bots[response.json()["id"]]["status"] == "idle")
    assert [call[0] for call in calls_of(launcher, "create_bot")] == ["create_bot"]


async def test_failed_start_ends_in_error_starting_with_a_log_event(monkeypatch, caplog):
    launcher, pool = BrokenCreate(), Pool()
    app = make_app(monkeypatch, launcher, pool)
    owner = headers_for(pool)
    with caplog.at_level("INFO"):
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                created = (await client.post("/api/bots", json=NEW_BOT, headers=owner)).json()
                assert created["status"] == "starting"
                await wait_for(lambda: pool.bots[created["id"]]["status"] == "error_starting")
                listed = (await client.get("/api/bots", headers=owner)).json()
    assert listed[0]["status"] == "error_starting" and listed[0]["recreate_url"] == f"/api/bots/{created['id']}/recreate"
    failed = [record for record in caplog.records if record.getMessage() == "bot_start_failed"]
    assert len(failed) == 1 and failed[0].bot_id == created["id"]


async def test_success_is_logged_as_an_event_too(monkeypatch, caplog):
    launcher, pool = FakeLauncherClient(), Pool()
    app = make_app(monkeypatch, launcher, pool)
    owner = headers_for(pool)
    with caplog.at_level("INFO"):
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                created = (await client.post("/api/bots", json=NEW_BOT, headers=owner)).json()
                await wait_for(lambda: pool.bots[created["id"]]["status"] == "idle")
    done = [record for record in caplog.records if record.getMessage() == "bot_started"]
    assert len(done) == 1 and done[0].bot_id == created["id"]


async def test_repeat_post_with_the_same_id_of_a_starting_bot_makes_no_duplicate(monkeypatch):
    launcher, pool = SlowLauncher(), Pool()
    app = make_app(monkeypatch, launcher, pool)
    owner = headers_for(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            first = (await client.post("/api/bots", json=NEW_BOT, headers=owner)).json()
            await asyncio.wait_for(launcher.entered.wait(), 1)
            again = await client.post("/api/bots", json=NEW_BOT | {"id": first["id"]}, headers=owner)
            third = await client.post("/api/bots", json=NEW_BOT | {"id": first["id"]}, headers=owner)
            launcher.gate.set()
            await wait_for(lambda: pool.bots[first["id"]]["status"] == "idle")
    for replay in (again, third):
        assert replay.status_code == 200, replay.text
        assert (replay.json()["id"], replay.json()["status"], replay.json()["container"]) == (first["id"], "starting", "starting")
    assert pool.inserts == [first["id"]] and list(pool.bots) == [first["id"]]
    assert calls_of(launcher, "create_bot") == [("create_bot", first["id"], str(OWNER))]


async def test_another_owner_cannot_replay_a_starting_bot_of_someone_else(monkeypatch):
    launcher, pool = FakeLauncherClient(), Pool([starting_bot("scout-aaaa", OWNER)])
    app = make_app(monkeypatch, launcher, pool, delay=0.5)
    other = headers_for(pool, OTHER)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.post("/api/bots", json=NEW_BOT | {"id": "scout-aaaa"}, headers=other)
    assert response.status_code == 201 and response.json()["id"] != "scout-aaaa", "чужой id молча игнорируется, как раньше"
    assert pool.bots["scout-aaaa"]["owner_id"] == OWNER and len(pool.bots) == 2


async def test_a_bot_that_is_not_starting_is_never_replayed(monkeypatch):
    done = starting_bot("scout-aaaa") | {"status": "idle"}
    launcher, pool = FakeLauncherClient(), Pool([done])
    app = make_app(monkeypatch, launcher, pool, delay=0.5)
    owner = headers_for(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.post("/api/bots", json=NEW_BOT | {"id": "scout-aaaa"}, headers=owner)
    assert response.status_code == 201 and response.json()["id"] != "scout-aaaa" and len(pool.bots) == 2


async def test_core_start_picks_up_starting_bots_and_finishes_them(monkeypatch, caplog):
    launcher = BrokenCreate()  # первый из двух запусков отказывает
    pool = Pool([starting_bot("alpha-aaaa"), starting_bot("beta-bbbb"), starting_bot("gone-cccc") | {"status": "idle"}])
    app = make_app(monkeypatch, launcher, pool)
    with caplog.at_level("INFO"):
        async with app.router.lifespan_context(app):
            await wait_for(lambda: not any(bot["status"] == "starting" for bot in pool.bots.values()))
    statuses = {key: bot["status"] for key, bot in pool.bots.items()}
    assert sorted(statuses.values()) == ["error_starting", "idle", "idle"], statuses
    assert statuses["gone-cccc"] == "idle", "бот, который уже не starting, не трогается"
    created = [call[1] for call in calls_of(launcher, "create_bot")]
    assert sorted(created) == ["alpha-aaaa", "beta-bbbb"]
    assert [r.getMessage() for r in caplog.records if r.getMessage() in ("bot_started", "bot_start_failed")].count("bot_start_failed") == 1


async def test_core_start_with_a_bot_whose_container_already_exists_marks_it_idle(monkeypatch):
    from bothub.launcher_client import FakeBot
    launcher = FakeLauncherClient()
    launcher.bots["alpha-aaaa"] = FakeBot("alpha-aaaa", str(OWNER))  # ядро упало после создания контейнера, до записи статуса
    pool = Pool([starting_bot("alpha-aaaa")])
    app = make_app(monkeypatch, launcher, pool)
    async with app.router.lifespan_context(app):
        await wait_for(lambda: pool.bots["alpha-aaaa"]["status"] == "idle")
    assert len(calls_of(launcher, "create_bot")) == 1 and calls_of(launcher, "remove_bot") == []


async def test_a_bot_deleted_while_starting_does_not_leave_a_container(monkeypatch):
    launcher, pool = SlowLauncher(), Pool()
    app = make_app(monkeypatch, launcher, pool)
    owner = headers_for(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            created = (await client.post("/api/bots", json=NEW_BOT, headers=owner)).json()
            await asyncio.wait_for(launcher.entered.wait(), 1)
            del pool.bots[created["id"]]  # удаление бота, пока лаунчер создаёт контейнер
            launcher.gate.set()
            await wait_for(lambda: ("remove_bot", created["id"], False) in launcher.calls)
    assert created["id"] not in launcher.bots


async def test_a_second_start_of_the_same_bot_is_not_run_in_parallel(monkeypatch):
    launcher, pool = SlowLauncher(), Pool([starting_bot("alpha-aaaa")])
    app = make_app(monkeypatch, launcher, pool)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(launcher.entered.wait(), 1)
        await asyncio.gather(app.state.start_new_bot("alpha-aaaa"), app.state.start_new_bot("alpha-aaaa"),
                             _open_gate_soon(launcher))
        await wait_for(lambda: pool.bots["alpha-aaaa"]["status"] == "idle")
    assert calls_of(launcher, "create_bot") == [("create_bot", "alpha-aaaa", str(OWNER))]


async def _open_gate_soon(launcher):
    await asyncio.sleep(0.05)
    launcher.gate.set()


async def test_no_launcher_call_for_a_bot_without_container_in_local_mode(monkeypatch):
    launcher, pool = FakeLauncherClient(), Pool()
    app = make_app(monkeypatch, launcher, pool)
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "local")
    owner = headers_for(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            for body in (NEW_BOT, NEW_BOT | {"skip_container": True}, NEW_BOT | {"start_container": False}):
                response = await client.post("/api/bots", json=body, headers=owner)
                assert response.status_code == 200 and response.json()["container"] == "skipped"
                assert response.json()["status"] == "idle"
    assert calls_of(launcher, "create_bot", "ensure_network") == []


async def test_post_route_matrix_in_docker_mode(monkeypatch):
    launcher, pool = FakeLauncherClient(), Pool([starting_bot("alpha-aaaa")])
    app = make_app(monkeypatch, launcher, pool, delay=0.5)
    sign = hmac.new(SECRET.encode(), b"alpha-aaaa", hashlib.sha256).hexdigest()
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            assert (await client.post("/api/bots", json=NEW_BOT)).status_code == 401  # без входа
            bot = {"Authorization": f"Bearer bot:alpha-aaaa:{sign}"}
            assert (await client.post("/api/bots", json=NEW_BOT, headers=bot)).status_code in (401, 403)
            assert (await client.post("/api/bots", json=NEW_BOT, headers=headers_for(pool))).status_code == 201
            assert (await client.post("/api/bots", json=NEW_BOT | {"provider": "bogus"}, headers=headers_for(pool))).status_code == 400
            assert (await client.post("/api/bots", json=NEW_BOT | {"id": "BAD ID"}, headers=headers_for(pool))).status_code in (400, 422)
    assert len(pool.bots) == 2  # отказы никого не создали


# ---- сеть пользователя при создании пользователя ----

class SetupConnection(Connection):
    async def fetchval(self, query, *args):
        if query == "select 1 from bothub.users limit 1":
            return None if self.pool.fresh else 1
        return None

    async def fetchrow(self, query, *args):
        if query.startswith("insert into bothub.users"):
            self.pool.fresh = False
            return {"id": OTHER, "email": args[0], "role": args[2] if len(args) > 2 else "admin", "status": "active",
                    "created_at": NOW}
        if "select * from bothub.invites where token_hash=$1 for update" in query:
            return {"token_hash": args[0], "used_at": None, "expires_at": NOW + timedelta(days=1000), "role": "member"}
        return await super().fetchrow(query, *args)


class SetupPool(Pool):
    def __init__(self):
        super().__init__(has_users=False)
        self.fresh = True
        self.connection = SetupConnection(self)


async def test_setup_connects_the_new_user_network_after_the_response(monkeypatch):
    launcher, pool = SlowLauncher(), SetupPool()
    app = make_app(monkeypatch, launcher, pool)
    app.state.setup_code = "setup-code"
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            async with asyncio.timeout(1):
                response = await client.post("/api/setup", json={"email": "a@example.com", "password": "correct horse battery"},
                                             headers={"X-Setup-Code": "setup-code"})
            assert response.status_code == 201, response.text
            await asyncio.wait_for(launcher.entered.wait(), 1)
            launcher.gate.set()
            await wait_for(lambda: launcher.networks)
    assert calls_of(launcher, "ensure_network") == [("ensure_network", str(OTHER))] and launcher.networks == {str(OTHER)}


async def test_a_failing_network_call_is_only_logged_and_ensure_network_stays_the_fallback(monkeypatch, caplog):
    class NoNetwork(FakeLauncherClient):
        async def ensure_network(self, owner_id):
            raise LauncherUnavailable("лаунчер недоступен: ConnectError", code="unavailable")

    launcher, pool = NoNetwork(), SetupPool()
    app = make_app(monkeypatch, launcher, pool)
    app.state.setup_code = "setup-code"
    with caplog.at_level("WARNING"):
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                response = await client.post("/api/setup", json={"email": "a@example.com", "password": "correct horse battery"},
                                             headers={"X-Setup-Code": "setup-code"})
                assert response.status_code == 201
                await wait_for(lambda: any(r.getMessage() == "user_network_prepare_failed" for r in caplog.records))
    created = await launcher.create_bot("scout-aaaa", str(OTHER))  # подстраховка: сеть создаст и сам запуск бота
    assert created.running


async def test_invite_accept_connects_the_network_too_and_local_mode_does_not(monkeypatch):
    launcher, pool = FakeLauncherClient(), SetupPool()
    pool.fresh = False
    app = make_app(monkeypatch, launcher, pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.post("/api/invites/accept", json={"token": "invite-token", "email": "b@example.com",
                                                                      "password": "correct horse battery"})
            assert response.status_code == 201, response.text
            await wait_for(lambda: launcher.networks)
    assert launcher.networks == {str(OTHER)}
    local = FakeLauncherClient()
    pool2 = SetupPool()
    pool2.fresh = False
    app2 = make_app(monkeypatch, local, pool2)
    monkeypatch.setenv("BOTHUB_RUNNER_EXEC", "local")
    async with app2.router.lifespan_context(app2):
        async with client_for(app2) as client:
            response = await client.post("/api/invites/accept", json={"token": "invite-token", "email": "b@example.com",
                                                                      "password": "correct horse battery"})
            assert response.status_code == 201
            await asyncio.sleep(0.05)
    assert calls_of(local, "ensure_network") == []


def test_claim_query_keeps_a_starting_bot_out_of_the_worker():
    source = open(main.__file__, encoding="utf-8").read()
    assert "b.status not in ('starting','error_starting','no_model')" in source


async def test_legacy_ids_replay_returns_the_starting_bot_too(monkeypatch):
    launcher, pool = SlowLauncher(), Pool()
    app = make_app(monkeypatch, launcher, pool, legacy_ids=True)
    owner = headers_for(pool)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            first = await client.post("/api/bots", json=NEW_BOT | {"id": "scout"}, headers=owner)
            again = await client.post("/api/bots", json=NEW_BOT | {"id": "scout"}, headers=owner)
            launcher.gate.set()
            await wait_for(lambda: pool.bots["scout"]["status"] == "idle")
            after = await client.post("/api/bots", json=NEW_BOT | {"id": "scout"}, headers=owner)
    assert (first.status_code, first.json()["id"], first.json()["status"]) == (201, "scout", "starting")
    assert (again.status_code, again.json()["id"], again.json()["status"]) == (200, "scout", "starting")
    assert after.status_code == 409, "готовый бот с занятым id в режиме тестовых id остаётся конфликтом"
    assert pool.inserts == ["scout"]
