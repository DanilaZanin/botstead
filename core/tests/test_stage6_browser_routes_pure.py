"""Browser control HTTP routes with an in-memory pool and real cookie auth."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from bothub import auth
from bothub.main import create_app
from bothub.launcher_client import FakeBot, FakeLauncherClient, LauncherNotFound, LauncherServerError


OWNER_A = uuid.UUID(int=1)
OWNER_B = uuid.UUID(int=2)
CREATED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SESSION_SECRET = "pure-browser-test-secret"


class FakeConnection:
    def __init__(self, pool):
        self.pool = pool
        self.transaction_depth = 0

    @asynccontextmanager
    async def transaction(self):
        bots = {key: dict(value) for key, value in self.pool.bots.items()}
        events = list(self.pool.events)
        event_seq = self.pool.event_seq
        self.transaction_depth += 1
        try:
            yield self
            if self.transaction_depth == 1 and self.pool.fail_next_commit:
                self.pool.fail_next_commit = False
                raise RuntimeError("commit failed")
        except BaseException:
            self.pool.bots = bots
            self.pool.events = events
            self.pool.event_seq = event_seq
            raise
        finally:
            self.transaction_depth -= 1

    async def fetchval(self, query, *args):
        if "select browser_control from bothub.bots" in query:
            if self.pool.reconcile_state is not None:
                return self.pool.reconcile_state
            bot = self.pool.bots.get(args[0])
            return bot["browser_control"] if bot and bot["owner_id"] == args[1] else None
        if "e.kind='browser_step'" in query:
            return self.pool.last_navigate  # the takeover fallback: the bot's last navigate event
        if "bothub.settings" in query:
            return False
        if "update bothub.threads set last_seq" in query:
            self.pool.event_seq += 1
            return self.pool.event_seq
        if "update bothub.sessions set revoked_at=now() where id_hash=$1 and user_id=$2" in query:
            return args[0] if self.pool.sessions.pop(args[0], None) else None
        if "update bothub.sessions" in query:
            return None
        if "from bothub.sessions s join bothub.users u on u.id=s.user_id join bothub.bots b" in query:
            return 1  # the screen proxy re-checks the session
        return None

    async def fetchrow(self, query, *args):
        if "from bothub.sessions s join bothub.users u" in query:
            return self.pool.sessions.get(args[0])
        if "join bothub.sessions s on s.user_id=b.owner_id" in query:
            bot = self.pool.bots.get(args[0])
            return {"owner_id": bot["owner_id"]} if bot else None  # screen websocket auth
        if "need_restart" in query:
            return None
        if "from bothub.turns t join bothub.threads th" in query:
            return dict(self.pool.turn) if self.pool.turn else None
        if "from bothub.bots" in query:
            bot = self.pool.bots.get(args[0])
            if bot is None or len(args) > 1 and bot["owner_id"] != args[1]:
                return None
            return dict(bot)
        if "from bothub.threads where bot_id" in query:
            return {"id": self.pool.thread_id}
        if "insert into bothub.threads" in query:
            return {"id": self.pool.thread_id}
        if "insert into bothub.events" in query:
            event = {
                "id": uuid.uuid4(),
                "thread_id": args[0],
                "seq": args[1],
                "turn_id": args[2],
                "kind": args[3],
                "actor": args[4],
                "client": args[5],
                "payload": json.loads(args[6]),
            }
            self.pool.events.append(event)
            return event
        return None

    async def fetch(self, query, *args):
        if "from bothub.turns" in query:
            return [{'id':self.pool.turn['id']}] if self.pool.turn else []
        return []

    async def execute(self, query, *args):
        if "update bothub.bots set browser_control='returning'" in query:
            bot = self.pool.bots[args[0]]
            if bot["browser_control"] == "human" and bot["owner_id"] == args[1]:
                bot["browser_control"] = "returning"
        elif "update bothub.bots set browser_control" in query:
            self.pool.bots[args[0]]["browser_control"] = args[1]
        return "OK"


class FakePool:
    def __init__(self):
        self.bots = {
            "alpha": {
                "id": "alpha",
                "owner_id": OWNER_A,
                "browser_control": "bot",
                "created_at": CREATED_AT,
            }
        }
        self.thread_id = uuid.UUID(int=3)
        self.events = []
        self.event_seq = 0
        self.fail_next_commit = False
        self.reconcile_state = None
        self.last_navigate = None
        self.connection = FakeConnection(self)
        self.sessions = {}
        self.turn = None

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def no_lifespan(app):
    yield


def _headers(pool, owner_id):
    token = f"session-{owner_id}"
    token_hash = auth.token_hash(token)
    pool.sessions[token_hash] = {
        "user_id": owner_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
        "last_extended_at": datetime.now(timezone.utc),
        "role": "admin",
        "status": "active",
    }
    return {
        "Cookie": f"bothub_session={token}",
        "Origin": "https://testserver",
        "X-CSRF": auth.csrf_token(token, SESSION_SECRET),
    }


def _app(monkeypatch, launcher=None):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", SESSION_SECRET)
    launcher = launcher or FakeLauncherClient()
    launcher.bots["alpha"] = FakeBot("alpha", str(OWNER_A))
    app = create_app(launcher=launcher)
    app.state.pool = FakePool()
    app.router.lifespan_context = no_lifespan
    return app


def _fail_on(launcher, name, exc, *, times=1):
    """Make the next `times` calls of one launcher method fail (FakeLauncherClient.fail_next hits whatever
    method is called first, and takeover now reads the tab before the freeze)."""
    original = getattr(launcher, name)
    left = [times]

    async def failing(*args, **kwargs):
        if left[0] <= 0:
            return await original(*args, **kwargs)
        left[0] -= 1
        launcher.fail_next(exc)
        return await original(*args, **kwargs)

    setattr(launcher, name, failing)


def _calls(launcher, *names):
    return [call for call in launcher.calls if call[0] in names]


@pytest.mark.pure
async def test_browser_since_starts_at_bot_creation_and_stays_stable(monkeypatch):
    app = _app(monkeypatch)
    headers = _headers(app.state.pool, OWNER_A)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            first = await client.get("/api/bots/alpha/browser", headers=headers)
            second = await client.get("/api/bots/alpha/browser", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["since"] == CREATED_AT.isoformat()
    assert second.json()["since"] == first.json()["since"]


@pytest.mark.pure
async def test_secret_input_requires_value(monkeypatch):
    app = _app(monkeypatch)
    headers = _headers(app.state.pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post('/api/bots/alpha/browser/secret-input', json={}, headers=headers)
    assert response.status_code == 400
    assert app.state.launcher.screen_inputs == {}


@pytest.mark.pure
@pytest.mark.parametrize(('status','expected'), [('done',200),('error',502)])
async def test_takeover_handles_turn_finished_after_lookup(monkeypatch,status,expected):
    app = _app(monkeypatch)
    pool = app.state.pool
    pool.turn = {'id':uuid.UUID(int=4),'thread_id':pool.thread_id,'bot_id':'alpha','status':status,'turn_type':'normal'}
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post('/api/bots/alpha/browser/takeover', headers=headers)
    assert response.status_code == expected
    assert pool.bots['alpha']['browser_control'] == 'human'


@pytest.mark.pure
async def test_browser_routes_transition_audit_and_enforce_ownership(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    owner_a = _headers(pool, OWNER_A)
    owner_b = _headers(pool, OWNER_B)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            assert (await client.get("/api/bots/alpha/browser", headers=owner_b)).status_code == 404
            denied_takeover = await client.post(
                "/api/bots/alpha/browser/takeover", headers=owner_b
            )
            assert denied_takeover.status_code == 404
            assert pool.bots["alpha"]["browser_control"] == "bot"
            assert pool.events == []

            initial = await client.get("/api/bots/alpha/browser", headers=owner_a)
            assert initial.status_code == 200
            assert initial.json() == {
                "state": "bot",
                "since": CREATED_AT.isoformat(),
                "by": None,
            }

            takeover = await client.post(
                "/api/bots/alpha/browser/takeover", headers=owner_a
            )
            assert takeover.status_code == 200
            assert takeover.json()["state"] == "human"
            assert takeover.json()["url"] == "about:blank"
            assert ("freeze_bot", "alpha") in app.state.launcher.calls
            assert len(pool.events) == 1
            assert pool.events[0]["kind"] == "browser_control"
            assert pool.events[0]["payload"] == {
                "from": "bot",
                "to": "human",
                "by": str(OWNER_A),
                "reason": "takeover",
                "url": "about:blank",
            }

            denied_return = await client.post(
                "/api/bots/alpha/browser/return", headers=owner_b
            )
            assert denied_return.status_code == 404
            assert pool.bots["alpha"]["browser_control"] == "human"
            assert len(pool.events) == 1

            returned = await client.post(
                "/api/bots/alpha/browser/return", headers=owner_a
            )
            assert returned.status_code == 200
            assert returned.json()["state"] == "returning"
            assert ("unfreeze_bot", "alpha") in app.state.launcher.calls
            assert app.state.launcher.calls.index(("browser_mode", "alpha", "bot", None)) \
                < app.state.launcher.calls.index(("unfreeze_bot", "alpha"))
            assert [event["kind"] for event in pool.events] == [
                "browser_control",
                "browser_control",
            ]
            assert pool.events[1]["payload"] == {
                "from": "human",
                "to": "returning",
                "by": str(OWNER_A),
                "reason": "return",
            }

            denied_takeover = await client.post(
                "/api/bots/alpha/browser/takeover", headers=owner_b
            )
            assert denied_takeover.status_code == 404
            assert pool.bots["alpha"]["browser_control"] == "returning"
            assert len(pool.events) == 2


@pytest.mark.pure
async def test_browser_transition_launcher_failure_keeps_previous_state(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    launcher = app.state.launcher
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        _fail_on(launcher, "freeze_bot", LauncherServerError("freeze failed"))
        failed = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
        assert failed.status_code == 502
        assert failed.json()["error"] == "freeze_failed"
        assert pool.bots["alpha"]["browser_control"] == "bot"
        assert [event["payload"] for event in pool.events] == [
            {"from": "bot", "to": "bot", "by": str(OWNER_A), "reason": "freeze_failed"}]
        assert [call for call in launcher.calls if call[:3] == ("browser_mode", "alpha", "human")] == []

        assert (await client.post("/api/bots/alpha/browser/takeover", headers=headers)).status_code == 200
        _fail_on(launcher, "unfreeze_bot", LauncherServerError("unfreeze failed"))
        failed = await client.post("/api/bots/alpha/browser/return", headers=headers)
        assert failed.status_code == 502
        assert failed.json()["error"] == "launcher_failed"
        assert pool.bots["alpha"]["browser_control"] == "returning", "the state already left the human"
        assert "alpha" in launcher.frozen
        assert len(pool.events) == 3

        retried = await client.post("/api/bots/alpha/browser/return", headers=headers)
        assert retried.status_code == 200
        assert retried.json()["state"] == "returning"
        assert "alpha" not in launcher.frozen
        assert launcher.browser_modes["alpha"] == "bot"
        assert len(pool.events) == 3


@pytest.mark.pure
async def test_browser_return_commit_failure_touches_nothing_and_leaves_the_human_in_charge(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    launcher = app.state.launcher
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        assert (await client.post("/api/bots/alpha/browser/takeover", headers=headers)).status_code == 200
        before = len(launcher.calls)
        pool.fail_next_commit = True
        with pytest.raises(RuntimeError, match="commit failed"):
            await client.post("/api/bots/alpha/browser/return", headers=headers)

    # `returning` is stored before the launcher is asked for anything: if that commit fails, the human's
    # browser and the freeze are exactly as they were, and the audit event of the failed transaction is gone.
    assert launcher.calls[before:] == []
    assert "alpha" in launcher.frozen and launcher.browser_modes["alpha"] == "human"
    assert pool.bots["alpha"]["browser_control"] == "human"
    assert len(pool.events) == 1


@pytest.mark.pure
async def test_takeover_commit_failure_releases_freeze_only_after_fresh_bot_read(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    launcher = app.state.launcher
    pool.fail_next_commit = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        with pytest.raises(RuntimeError, match="commit failed"):
            await client.post("/api/bots/alpha/browser/takeover", headers=headers)
        assert pool.bots["alpha"]["browser_control"] == "bot"
        assert "alpha" not in launcher.frozen
        # The human Chromium was already started: the bot's own comes back first, then the bot runs again.
        assert launcher.calls[-3:] == [("browser_mode", "alpha", "human", "about:blank"),
                                       ("browser_mode", "alpha", "bot", None), ("unfreeze_bot", "alpha")]
        assert launcher.browser_modes["alpha"] == "bot"
        assert pool.events == []

        pool.reconcile_state = "human"
        pool.fail_next_commit = True
        with pytest.raises(RuntimeError, match="commit failed"):
            await client.post("/api/bots/alpha/browser/takeover", headers=headers)
        assert "alpha" in launcher.frozen
        assert launcher.browser_modes["alpha"] == "human"
        assert launcher.calls[-1] == ("browser_mode", "alpha", "human", "about:blank")


@pytest.mark.pure
async def test_takeover_freeze_failure_reconciles_marker(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    launcher = app.state.launcher
    freeze = launcher.freeze_bot

    async def failed_after_marker(bot_id):
        await freeze(bot_id)
        raise LauncherServerError("process termination failed")

    launcher.freeze_bot = failed_after_marker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        failed = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert failed.status_code == 502
    assert pool.bots["alpha"]["browser_control"] == "bot"
    assert "alpha" not in launcher.frozen
    assert launcher.calls[-1] == ("unfreeze_bot", "alpha")


@pytest.mark.pure
async def test_failed_takeover_from_returning_unfreezes_for_snapshot(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    pool.bots['alpha']['browser_control'] = 'returning'
    headers = _headers(pool, OWNER_A)
    launcher = app.state.launcher
    freeze = launcher.freeze_bot

    async def failed_after_marker(bot_id):
        await freeze(bot_id)
        raise LauncherServerError("process termination failed")

    launcher.freeze_bot = failed_after_marker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        failed = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert failed.status_code == 502
    assert pool.bots['alpha']['browser_control'] == 'returning'
    assert 'alpha' not in launcher.frozen
    assert launcher.calls[-1] == ('unfreeze_bot', 'alpha')


@pytest.mark.pure
async def test_cancelled_takeover_reconciles_frozen_marker(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    launcher = app.state.launcher
    freeze = launcher.freeze_bot
    unfreeze = launcher.unfreeze_bot
    entered, released = asyncio.Event(), asyncio.Event()

    async def paused_freeze(bot_id):
        await freeze(bot_id)
        entered.set()
        await asyncio.Event().wait()

    async def observed_unfreeze(bot_id):
        result = await unfreeze(bot_id)
        released.set()
        return result

    launcher.freeze_bot = paused_freeze
    launcher.unfreeze_bot = observed_unfreeze
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        request = asyncio.create_task(client.post("/api/bots/alpha/browser/takeover", headers=headers))
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.wait_for(released.wait(), 1)

    assert pool.bots['alpha']['browser_control'] == 'bot'
    assert 'alpha' not in launcher.frozen


# --- Takeover keeps the human's screen; secret-input answers; restart state ---------------------

class HeldScreenLauncher(FakeLauncherClient):
    """Screen stream that stays open; screen_input can be held to observe the per-bot lock."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.block_input = False
        self.input_entered = threading.Event()
        self.input_release = threading.Event()

    async def screen_output(self, session_id):
        yield b"RFB 003.008\n"
        await asyncio.to_thread(self.gate.wait)

    async def screen_input(self, session_id, data):
        if self.block_input:
            self.block_input = False
            self.input_entered.set()
            await asyncio.to_thread(self.input_release.wait)
        await super().screen_input(session_id, data)


HANDSHAKE = b"RFB 003.008\n\x01\x01"
KEY_A = b"\x04\x01\x00\x00\x00\x00\x00A"
FRAMEBUFFER_REQUEST = b"\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00"


@contextmanager
def _screen(client, launcher, ws_headers):
    """Open the screen socket, read the server banner, and always let the held stream end."""
    with client.websocket_connect("/api/bots/alpha/screen", headers=ws_headers) as ws:
        try:
            assert ws.receive_bytes() == b"RFB 003.008\n"
            yield ws
            ws.send({"type": "websocket.disconnect"})
        finally:
            launcher.gate.set()


def _wait(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.01)
    return condition()


def _running_turn(pool):
    pool.turn = {"id": uuid.UUID(int=4), "thread_id": pool.thread_id, "bot_id": "alpha", "status": "running", "turn_type": "normal"}


def _screen_inputs(launcher):
    return next(iter(launcher.screen_inputs.values()), None)


@pytest.mark.pure
def test_takeover_with_running_turn_keeps_the_screen_open_and_input_reaches_the_bot_browser(monkeypatch):
    launcher = HeldScreenLauncher()
    app = _app(monkeypatch, launcher)
    pool = app.state.pool
    _running_turn(pool)
    headers = _headers(pool, OWNER_A)
    ws_headers = {"Origin": "https://testserver", "Cookie": headers["Cookie"]}
    try:
        with TestClient(app, base_url="https://testserver") as client:
            with _screen(client, launcher, ws_headers) as ws:
                ws.send_bytes(HANDSHAKE + KEY_A)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE])
                time.sleep(0.05)
                assert _screen_inputs(launcher) == [HANDSHAKE], "the bot has control: human keys are dropped"

                response = client.post("/api/bots/alpha/browser/takeover", headers=headers)
                assert response.status_code == 200 and response.json()["state"] == "human"
                assert "interrupted" in [event["kind"] for event in pool.events], "the turn was stopped"
                assert launcher.frozen == {"alpha"}

                ws.send_bytes(KEY_A)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE, KEY_A]), "human input reaches the browser"
                assert launcher.screen_inputs, "the screen session was not closed"
                ws.send_bytes(FRAMEBUFFER_REQUEST)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE, KEY_A, FRAMEBUFFER_REQUEST])
    finally:
        launcher.gate.set()


@pytest.mark.pure
def test_failed_freeze_answers_502_keeps_bot_control_and_drops_human_input(monkeypatch):
    launcher = HeldScreenLauncher()
    app = _app(monkeypatch, launcher)
    pool = app.state.pool
    _running_turn(pool)
    headers = _headers(pool, OWNER_A)
    ws_headers = {"Origin": "https://testserver", "Cookie": headers["Cookie"]}
    try:
        with TestClient(app, base_url="https://testserver") as client:
            with _screen(client, launcher, ws_headers) as ws:
                ws.send_bytes(HANDSHAKE)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE])

                _fail_on(launcher, "freeze_bot", LauncherServerError("freeze failed"))
                response = client.post("/api/bots/alpha/browser/takeover", headers=headers)
                assert response.status_code == 502
                assert response.json()["error"] == "freeze_failed"
                assert pool.bots["alpha"]["browser_control"] == "bot"
                assert [event["payload"]["reason"] for event in pool.events] == ["freeze_failed"]
                assert "interrupted" not in [event["kind"] for event in pool.events], "the turn keeps running"

                ws.send_bytes(KEY_A)
                time.sleep(0.2)
                assert _screen_inputs(launcher) == [HANDSHAKE], "no input path until the bot is frozen"

                assert client.post("/api/bots/alpha/browser/takeover", headers=headers).status_code == 200
                ws.send_bytes(KEY_A)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE, KEY_A])
    finally:
        launcher.gate.set()


SECRET_VALUE = "Zq9-UniqueSecret-"


@pytest.mark.pure
async def test_secret_input_400_never_echoes_the_value(monkeypatch):
    app = _app(monkeypatch)
    headers = _headers(app.state.pool, OWNER_A)
    bodies = [
        {"value": SECRET_VALUE * 400},                                  # too long
        {"value": 12345678},                                            # wrong type
        {"value": SECRET_VALUE, "save_as": "1 bad name"},               # bad name next to a real value
        {"value": SECRET_VALUE, "password": "PasswordInExtraField"},    # unknown field
        {"value": SECRET_VALUE, "token": {"nested": "TokenInExtraField"}},
        {"secret": SECRET_VALUE},
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        for body in bodies:
            response = await client.post("/api/bots/alpha/browser/secret-input", json=body, headers=headers)
            assert response.status_code == 400, body
            payload = response.json()
            assert set(payload) == {"error", "detail"} and payload["error"] == "invalid"
            for leaked in ("Zq9-", "12345678", "PasswordInExtraField", "TokenInExtraField", "input"):
                assert leaked not in response.text, (leaked, response.text)
    assert app.state.launcher.screen_inputs == {}


@pytest.mark.pure
def test_secret_input_with_control_characters_is_400_without_the_value(monkeypatch):
    launcher = HeldScreenLauncher()
    app = _app(monkeypatch, launcher)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    ws_headers = {"Origin": "https://testserver", "Cookie": headers["Cookie"]}
    try:
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/api/bots/alpha/browser/takeover", headers=headers).status_code == 200
            with _screen(client, launcher, ws_headers) as ws:
                ws.send_bytes(HANDSHAKE + FRAMEBUFFER_REQUEST)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE + FRAMEBUFFER_REQUEST])
                response = client.post("/api/bots/alpha/browser/secret-input",
                                       json={"value": SECRET_VALUE + "\x07"}, headers=headers)
                assert response.status_code == 400 and response.json()["error"] == "invalid"
                assert "Zq9-" not in response.text
                assert _screen_inputs(launcher) == [HANDSHAKE + FRAMEBUFFER_REQUEST]
    finally:
        launcher.gate.set()


@pytest.mark.pure
def test_parallel_secret_inputs_are_typed_one_after_another(monkeypatch):
    launcher = HeldScreenLauncher()
    app = _app(monkeypatch, launcher)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    ws_headers = {"Origin": "https://testserver", "Cookie": headers["Cookie"]}
    try:
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/api/bots/alpha/browser/takeover", headers=headers).status_code == 200
            with _screen(client, launcher, ws_headers) as ws:
                ws.send_bytes(HANDSHAKE + FRAMEBUFFER_REQUEST)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE + FRAMEBUFFER_REQUEST])
                launcher.block_input = True
                results = {}

                def post(name):
                    results[name] = client.post("/api/bots/alpha/browser/secret-input",
                                                json={"value": name}, headers=headers)

                first = threading.Thread(target=post, args=("first",))
                second = threading.Thread(target=post, args=("second",))
                first.start()
                try:
                    assert launcher.input_entered.wait(2), "the first input is being typed"
                    second.start()
                    time.sleep(0.2)
                    assert "second" not in results, "the second input waits for the bot lock"
                    assert len(_screen_inputs(launcher)) == 1, "nothing was typed while the first is held"
                finally:
                    launcher.input_release.set()
                first.join(5)
                second.join(5)
                assert results["first"].status_code == 200 and results["second"].status_code == 200
                typed = b"".join(_screen_inputs(launcher)[1:])
                one, two = (b"".join(bytes([4, d, 0, 0, 0, 0, 0, ord(c)]) for c in word for d in (1, 0)) for word in ("first", "second"))
                assert typed == one + two, "no interleaving of keystrokes"
    finally:
        launcher.gate.set()


class RestartConnection:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch(self, query, *args):
        if "select id from bothub.bots where executor='container' and provider='claude'" in query:
            return [{"id": key} for key, bot in self.pool.bots.items()
                    if bot["executor"] == "container" and bot["provider"] == "claude"]
        if query == "select id from bothub.bots where executor='container'":
            return [{"id": key} for key, bot in self.pool.bots.items() if bot["executor"] == "container"]
        return []

    async def fetchval(self, query, *args):
        if "select browser_control from bothub.bots where id=$1 for update" in query:
            return self.pool.bots[args[0]]["browser_control"]
        return None

    async def execute(self, query, *args):
        if "update bothub.bots set browser_control='returning' where id=any($1" in query:
            for key in args[0]:
                self.pool.bots[key]["browser_control"] = "returning"
        elif "update bothub.bots set browser_control='bot' where browser_control<>'bot' and not (id=any($1" in query:
            for key, bot in self.pool.bots.items():
                if key not in args[0]:
                    bot["browser_control"] = "bot"
        return "OK"


class RestartPool:
    def __init__(self, bots):
        self.bots = bots
        self.connection = RestartConnection(self)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def close(self):
        pass


class QuietGateway:
    async def startup(self):
        pass

    async def shutdown(self):
        pass


@pytest.mark.pure
async def test_restart_puts_only_container_claude_bots_into_returning(monkeypatch):
    import bothub.main as main

    def bot(executor, provider, state):
        return {"executor": executor, "provider": provider, "browser_control": state}

    pool = RestartPool({
        "claude-human": bot("container", "claude", "human"),
        "claude-bot": bot("container", "claude", "bot"),
        "codex": bot("container", "codex", "returning"),
        "gemini": bot("container", "gemini", "human"),
        "fake": bot("container", "fake", "returning"),
        "mac-claude": bot("mac", "claude", "human"),
    })

    async def open_pool():
        return pool

    monkeypatch.setattr(main, "open_pool", open_pool)
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    launcher = FakeLauncherClient()
    for key in pool.bots:
        launcher.bots[key] = FakeBot(key, str(OWNER_A))
    app = create_app(launcher=launcher)
    app.state.gateway = QuietGateway()
    async with app.router.lifespan_context(app):
        await _synced(app)
    assert {key: item["browser_control"] for key, item in pool.bots.items()} == {
        "claude-human": "returning", "claude-bot": "returning", "codex": "bot", "gemini": "bot",
        "fake": "bot", "mac-claude": "bot"}
    # The freeze marker outlives the core and does not depend on the provider: every container bot is unfrozen.
    assert sorted(call[1] for call in launcher.calls if call[0] == "unfreeze_bot") == [
        "claude-bot", "claude-human", "codex", "fake", "gemini"]


async def _synced(app, timeout=2.0):
    """Стартовая сверка с лаунчером идёт фоновой задачей: ждём, пока маршруты лаунчера откроются."""
    async with asyncio.timeout(timeout):
        while not app.state.launcher_ready:
            await asyncio.sleep(0.005)


def _startup_app(monkeypatch, bots, launcher):
    import bothub.main as main

    pool = RestartPool(bots)

    async def open_pool():
        return pool

    monkeypatch.setattr(main, "open_pool", open_pool)
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    for key in bots:
        launcher.bots[key] = FakeBot(key, str(OWNER_A))
    app = create_app(launcher=launcher)
    app.state.gateway = QuietGateway()
    return app, pool


@pytest.mark.pure
async def test_startup_unfreeze_failure_does_not_stop_the_core_and_leaves_exec_refused(monkeypatch, caplog):
    from bothub.main import launcher_failure_detail

    class FailingUnfreeze(FakeLauncherClient):
        async def unfreeze_bot(self, bot_id):
            if bot_id == "bad":
                self.frozen.add(bot_id)
                raise ValueError("launcher answered garbage")  # not even a LauncherError
            if bot_id == "ghost":
                raise LauncherNotFound("no container", code="not_found", status=404)
            return await super().unfreeze_bot(bot_id)

    bots = {key: {"executor": "container", "provider": "claude", "browser_control": "human"}
            for key in ("bad", "good", "ghost")}
    launcher = FailingUnfreeze()
    app, pool = _startup_app(monkeypatch, bots, launcher)
    launcher.frozen.update({"good", "bad"})
    with caplog.at_level("WARNING"):
        async with app.router.lifespan_context(app):
            await _synced(app)
    assert launcher.frozen == {"bad"}, "the others are unfrozen, the failed one stays frozen"
    failed = [record for record in caplog.records if record.getMessage() == "browser_startup_unfreeze_failed"]
    assert [record.bot_id for record in failed] == ["bad"], "journal: the failed bot, nothing for a bot without container"
    assert "garbage" in failed[0].error
    with pytest.raises(Exception) as refused:
        async for _ in launcher.exec("bad", ["true"]):
            pass
    code, detail = launcher_failure_detail(refused.value)
    assert code == "bot_frozen" and "пересоздайте" in detail
    assert launcher_failure_detail(RuntimeError("x")) is None
    assert launcher_failure_detail(LauncherServerError("boom", code="docker_error")) is None


@pytest.mark.pure
async def test_startup_unfreezes_container_bots_that_are_not_in_human_state_whatever_the_provider(monkeypatch):
    bots = {"claude": {"executor": "container", "provider": "claude", "browser_control": "human"},
            "fake": {"executor": "container", "provider": "fake", "browser_control": "human"},
            "mac": {"executor": "mac", "provider": "claude", "browser_control": "human"}}
    launcher = FakeLauncherClient()
    app, pool = _startup_app(monkeypatch, bots, launcher)
    launcher.frozen.update(bots)
    async with app.router.lifespan_context(app):
        await _synced(app)
    assert launcher.frozen == {"mac"}, "bots without a container are not the launcher's business"
    assert {key: item["browser_control"] for key, item in pool.bots.items()} == {
        "claude": "returning", "fake": "bot", "mac": "bot"}


# --- The screen stream closes at once, the 5-second session check is only a safety net -------------------

def _closed(launcher):
    return any(call[0] == "close_screen_session" for call in launcher.calls)


def _expect_screen_closed(ws, launcher, code):
    try:
        assert _wait(lambda: _closed(launcher), timeout=2.0), "the stream was not closed (the 5 s check is too late)"
    finally:
        launcher.gate.set()  # the held fake stream must end before the client shuts down
    message = ws.receive()
    assert message["type"] == "websocket.close" and message["code"] == code


def _screen_case(monkeypatch):
    launcher = HeldScreenLauncher()
    app = _app(monkeypatch, launcher)
    headers = _headers(app.state.pool, OWNER_A)
    ws_headers = {"Origin": "https://testserver", "Cookie": headers["Cookie"]}
    return launcher, app, headers, ws_headers


@pytest.mark.pure
def test_logout_closes_the_screen_stream_at_once(monkeypatch):
    launcher, app, headers, ws_headers = _screen_case(monkeypatch)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            with client.websocket_connect("/api/bots/alpha/screen", headers=ws_headers) as ws:
                assert ws.receive_bytes() == b"RFB 003.008\n"
                assert client.post("/api/auth/logout", headers=headers).status_code == 200
                _expect_screen_closed(ws, launcher, 4401)
    finally:
        launcher.gate.set()


@pytest.mark.pure
def test_deleting_the_session_closes_the_screen_stream_at_once(monkeypatch):
    launcher, app, headers, ws_headers = _screen_case(monkeypatch)
    session_hash = auth.token_hash(f"session-{OWNER_A}")
    try:
        with TestClient(app, base_url="https://testserver") as client:
            with client.websocket_connect("/api/bots/alpha/screen", headers=ws_headers) as ws:
                assert ws.receive_bytes() == b"RFB 003.008\n"
                assert client.delete(f"/api/sessions/{session_hash}", headers=headers).status_code == 200
                _expect_screen_closed(ws, launcher, 4401)
    finally:
        launcher.gate.set()


@pytest.mark.pure
def test_deleting_the_bot_closes_the_screen_stream_at_once(monkeypatch):
    launcher, app, headers, ws_headers = _screen_case(monkeypatch)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            with client.websocket_connect("/api/bots/alpha/screen", headers=ws_headers) as ws:
                assert ws.receive_bytes() == b"RFB 003.008\n"
                response = client.delete("/api/bots/alpha", headers=headers)
                assert response.status_code == 200, response.text
                _expect_screen_closed(ws, launcher, 4410)
    finally:
        launcher.gate.set()


@pytest.mark.pure
def test_recreating_the_bot_closes_the_screen_stream_at_once(monkeypatch):
    launcher, app, headers, ws_headers = _screen_case(monkeypatch)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            with client.websocket_connect("/api/bots/alpha/screen", headers=ws_headers) as ws:
                assert ws.receive_bytes() == b"RFB 003.008\n"
                response = client.post("/api/bots/alpha/recreate", headers=headers)
                assert response.status_code == 200, response.text
                _expect_screen_closed(ws, launcher, 4410)
    finally:
        launcher.gate.set()


# --- The human works in a clean Chromium: takeover and return drive browser_mode -------------------------

def _mode_calls(launcher):
    return _calls(launcher, "browser_tab", "freeze_bot", "browser_mode", "unfreeze_bot")


def _trace_state(launcher, pool, *names):
    """Record the stored browser_control at the moment each named launcher method is entered."""
    seen = []
    for name in names:
        original = getattr(launcher, name)

        async def traced(*args, _name=name, _original=original, **kwargs):
            seen.append((_name, pool.bots["alpha"]["browser_control"]))
            return await _original(*args, **kwargs)

        setattr(launcher, name, traced)
    return seen


@pytest.mark.pure
async def test_takeover_reads_the_tab_then_freezes_then_opens_the_clean_browser_then_stores_human(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    launcher.tab_urls["alpha"] = "https://example.com/inbox?token=SECRET#frag"
    headers = _headers(pool, OWNER_A)
    seen = _trace_state(launcher, pool, "browser_tab", "freeze_bot", "browser_mode")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "human"
    assert _mode_calls(launcher) == [
        ("browser_tab", "alpha"), ("freeze_bot", "alpha"),
        ("browser_mode", "alpha", "human", "https://example.com/inbox?token=SECRET#frag")]
    assert seen == [("browser_tab", "bot"), ("freeze_bot", "bot"), ("browser_mode", "bot")], \
        "the state is stored only after the clean browser is up"
    assert pool.bots["alpha"]["browser_control"] == "human"
    assert response.json()["url"] == "https://example.com/inbox?token=SECRET#frag"
    # The audit event keeps only scheme://host/path: query and fragment can carry tokens.
    assert [event["payload"] for event in pool.events] == [
        {"from": "bot", "to": "human", "by": str(OWNER_A), "reason": "takeover", "url": "https://example.com/inbox"}]
    assert "SECRET" not in json.dumps(pool.events, default=str)


@pytest.mark.pure
@pytest.mark.parametrize("address", ["http://127.0.0.1/", "file:///x", "http://10.0.0.5/admin",
                                     "javascript:alert(1)", "chrome://settings", "http://user:pw@example.com/"])
async def test_takeover_opens_about_blank_for_a_forbidden_tab_address(monkeypatch, address):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    launcher.tab_urls["alpha"] = address
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 200 and response.json()["url"] == "about:blank"
    assert ("browser_mode", "alpha", "human", "about:blank") in launcher.calls
    assert pool.events[0]["payload"]["url"] == "about:blank"
    assert pool.bots["alpha"]["browser_control"] == "human", "a forbidden address never blocks the takeover"


@pytest.mark.pure
async def test_takeover_without_any_address_opens_about_blank(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 200 and response.json()["url"] == "about:blank"
    assert ("browser_mode", "alpha", "human", "about:blank") in launcher.calls


@pytest.mark.pure
@pytest.mark.parametrize("tab_fails", [False, True])
async def test_takeover_falls_back_to_the_last_navigate_event(monkeypatch, tab_fails):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    pool.last_navigate = "https://example.com/from/event"
    if tab_fails:
        _fail_on(launcher, "browser_tab", LauncherServerError("cdp down"))  # not fatal: the address is just unknown
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 200 and response.json()["url"] == "https://example.com/from/event"
    assert ("browser_mode", "alpha", "human", "https://example.com/from/event") in launcher.calls
    assert _mode_calls(launcher)[:2] == [("browser_tab", "alpha"), ("freeze_bot", "alpha")]


@pytest.mark.pure
@pytest.mark.parametrize("hidden", ["\u202e", "\u2066", "\u200b", "\ufeff"])
@pytest.mark.parametrize("source", ["tab", "event"])
async def test_takeover_gives_the_human_a_blank_page_for_an_address_with_hidden_characters(monkeypatch, hidden, source):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    address = f"https://bank.example/login{hidden}/gnp.exe"
    if source == "tab":
        launcher.tab_urls["alpha"] = address
    else:
        pool.last_navigate = address
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 200 and response.json()["url"] == "about:blank"
    assert ("browser_mode", "alpha", "human", "about:blank") in launcher.calls
    assert pool.events[0]["payload"]["url"] == "about:blank"


@pytest.mark.pure
async def test_takeover_answers_and_opens_the_host_as_punycode(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    launcher.tab_urls["alpha"] = "https://\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444/in?next=1"
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 200
    assert response.json()["url"] == "https://xn--e1afmkfd.xn--p1ai/in?next=1"
    assert ("browser_mode", "alpha", "human", "https://xn--e1afmkfd.xn--p1ai/in?next=1") in launcher.calls
    assert pool.events[0]["payload"]["url"] == "https://xn--e1afmkfd.xn--p1ai/in", "the audit keeps no query"


@pytest.mark.pure
@pytest.mark.parametrize("stored", ["[redacted]", "http://127.0.0.1/x", "file:///etc/passwd"])
async def test_takeover_ignores_a_forbidden_or_masked_event_address(monkeypatch, stored):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    pool.last_navigate = stored
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 200 and response.json()["url"] == "about:blank"


@pytest.mark.pure
async def test_takeover_prefers_the_live_tab_over_the_event(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    pool.last_navigate = "https://example.com/old"
    launcher.tab_urls["alpha"] = "https://example.com/live"
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.json()["url"] == "https://example.com/live"


@pytest.mark.pure
async def test_takeover_freeze_failure_never_opens_the_human_browser(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    launcher.tab_urls["alpha"] = "https://example.com/x"
    _fail_on(launcher, "freeze_bot", LauncherServerError("freeze failed"))
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 502 and response.json()["error"] == "freeze_failed"
    assert [call for call in launcher.calls if call[0] == "browser_mode"] == []
    assert _mode_calls(launcher) == [("browser_tab", "alpha"), ("freeze_bot", "alpha"), ("unfreeze_bot", "alpha")]
    assert "alpha" not in launcher.frozen and pool.bots["alpha"]["browser_control"] == "bot"
    assert [event["payload"]["reason"] for event in pool.events] == ["freeze_failed"]


@pytest.mark.pure
@pytest.mark.parametrize("start", ["bot", "returning"])
async def test_takeover_browser_mode_failure_rolls_back_mode_then_freeze(monkeypatch, start):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    pool.bots["alpha"]["browser_control"] = start
    _fail_on(launcher, "browser_mode", LauncherServerError("chromium did not start"))
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 502 and response.json()["error"] == "browser_mode_failed"
    assert _mode_calls(launcher) == [
        ("browser_tab", "alpha"), ("freeze_bot", "alpha"), ("browser_mode", "alpha", "human", "about:blank"),
        ("browser_mode", "alpha", "bot", None), ("unfreeze_bot", "alpha")]
    assert "alpha" not in launcher.frozen and launcher.browser_modes.get("alpha", "bot") == "bot"
    assert pool.bots["alpha"]["browser_control"] == start
    assert [event["payload"] for event in pool.events] == [
        {"from": start, "to": start, "by": str(OWNER_A), "reason": "browser_mode_failed"}]


@pytest.mark.pure
async def test_takeover_rollback_that_cannot_restore_the_bot_browser_leaves_the_bot_frozen(monkeypatch, caplog):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    _fail_on(launcher, "browser_mode", LauncherServerError("chromium did not start"), times=2)
    headers = _headers(pool, OWNER_A)
    with caplog.at_level("ERROR"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            response = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert response.status_code == 502 and response.json()["error"] == "browser_mode_failed"
    assert ("unfreeze_bot", "alpha") not in launcher.calls, "no CDP browser: the bot must not run"
    assert "alpha" in launcher.frozen and pool.bots["alpha"]["browser_control"] == "bot"
    assert any(record.getMessage() == "browser_mode_reconcile_failed" for record in caplog.records)


@pytest.mark.pure
def test_failed_browser_mode_answers_502_and_human_input_stays_dropped(monkeypatch):
    launcher = HeldScreenLauncher()
    app = _app(monkeypatch, launcher)
    pool = app.state.pool
    _running_turn(pool)
    headers = _headers(pool, OWNER_A)
    ws_headers = {"Origin": "https://testserver", "Cookie": headers["Cookie"]}
    try:
        with TestClient(app, base_url="https://testserver") as client:
            with _screen(client, launcher, ws_headers) as ws:
                ws.send_bytes(HANDSHAKE)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE])
                _fail_on(launcher, "browser_mode", LauncherServerError("chromium did not start"))
                response = client.post("/api/bots/alpha/browser/takeover", headers=headers)
                assert response.status_code == 502 and response.json()["error"] == "browser_mode_failed"
                assert pool.bots["alpha"]["browser_control"] == "bot"
                assert "interrupted" not in [event["kind"] for event in pool.events], "the turn keeps running"
                assert launcher.frozen == set()

                ws.send_bytes(KEY_A)
                time.sleep(0.2)
                assert _screen_inputs(launcher) == [HANDSHAKE], "no input path while the bot has control"

                assert client.post("/api/bots/alpha/browser/takeover", headers=headers).status_code == 200
                ws.send_bytes(KEY_A)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE, KEY_A])
    finally:
        launcher.gate.set()


@pytest.mark.pure
async def test_return_stores_returning_before_the_launcher_calls_then_switches_back_and_unfreezes(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        assert (await client.post("/api/bots/alpha/browser/takeover", headers=headers)).status_code == 200
        before = len(launcher.calls)
        seen = _trace_state(launcher, pool, "browser_mode", "unfreeze_bot")
        response = await client.post("/api/bots/alpha/browser/return", headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "returning"
    assert "url" not in response.json()
    assert launcher.calls[before:] == [("browser_mode", "alpha", "bot", None), ("unfreeze_bot", "alpha")]
    assert seen == [("browser_mode", "returning"), ("unfreeze_bot", "returning")], \
        "the state leaves human before the human's browser is touched"
    assert pool.bots["alpha"]["browser_control"] == "returning"
    assert launcher.browser_modes["alpha"] == "bot" and "alpha" not in launcher.frozen
    assert pool.events[-1]["payload"] == {"from": "human", "to": "returning", "by": str(OWNER_A), "reason": "return"}


@pytest.mark.pure
async def test_return_browser_mode_failure_stores_returning_keeps_the_bot_frozen_and_a_retry_finishes(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        assert (await client.post("/api/bots/alpha/browser/takeover", headers=headers)).status_code == 200
        before = len(launcher.calls)
        _fail_on(launcher, "browser_mode", LauncherServerError("cannot stop the human chromium"))
        response = await client.post("/api/bots/alpha/browser/return", headers=headers)
        assert response.status_code == 502 and response.json()["error"] == "launcher_failed"
        assert launcher.calls[before:] == [("browser_mode", "alpha", "bot", None)], "unfreeze_bot is not called"
        assert "alpha" in launcher.frozen and launcher.browser_modes["alpha"] == "human"
        assert pool.bots["alpha"]["browser_control"] == "returning", "the human no longer has the state"
        assert len(pool.events) == 2

        retried = await client.post("/api/bots/alpha/browser/return", headers=headers)
    assert retried.status_code == 200 and retried.json()["state"] == "returning"
    assert "alpha" not in launcher.frozen and launcher.browser_modes["alpha"] == "bot"
    assert pool.bots["alpha"]["browser_control"] == "returning"
    assert len(pool.events) == 2, "the retry writes no second audit event"


@pytest.mark.pure
async def test_return_unfreeze_failure_stores_returning_and_a_retry_finishes_it(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        assert (await client.post("/api/bots/alpha/browser/takeover", headers=headers)).status_code == 200
        before = len(launcher.calls)
        _fail_on(launcher, "unfreeze_bot", LauncherServerError("unfreeze failed"))
        response = await client.post("/api/bots/alpha/browser/return", headers=headers)
        assert response.status_code == 502 and response.json()["error"] == "launcher_failed"
        assert launcher.calls[before:] == [("browser_mode", "alpha", "bot", None), ("unfreeze_bot", "alpha")]
        assert pool.bots["alpha"]["browser_control"] == "returning" and "alpha" in launcher.frozen
        assert launcher.browser_modes["alpha"] == "bot"
        assert len(pool.events) == 2

        retried = await client.post("/api/bots/alpha/browser/return", headers=headers)
    assert retried.status_code == 200 and retried.json()["state"] == "returning"
    assert launcher.calls[before + 2:] == [("browser_mode", "alpha", "bot", None), ("unfreeze_bot", "alpha")]
    assert "alpha" not in launcher.frozen and pool.bots["alpha"]["browser_control"] == "returning"
    assert len(pool.events) == 2


@pytest.mark.pure
async def test_failed_return_does_not_go_back_to_human_without_a_new_freeze(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        assert (await client.post("/api/bots/alpha/browser/takeover", headers=headers)).status_code == 200
        _fail_on(launcher, "unfreeze_bot", LauncherServerError("unfreeze failed"))
        assert (await client.post("/api/bots/alpha/browser/return", headers=headers)).status_code == 502
        launcher.frozen.discard("alpha")  # something thawed the bot by hand
        before = len(launcher.calls)
        _fail_on(launcher, "freeze_bot", LauncherServerError("freeze failed"))
        refused = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
        assert refused.status_code == 502 and refused.json()["error"] == "freeze_failed"
        assert pool.bots["alpha"]["browser_control"] == "returning", "no human state without a successful freeze"
        assert ("freeze_bot", "alpha") in launcher.calls[before:]
        again = await client.post("/api/bots/alpha/browser/takeover", headers=headers)
    assert again.status_code == 200 and again.json()["state"] == "human"
    assert "alpha" in launcher.frozen


@pytest.mark.pure
def test_failed_return_leaves_the_screen_without_an_input_path(monkeypatch):
    launcher = HeldScreenLauncher()
    app = _app(monkeypatch, launcher)
    pool = app.state.pool
    headers = _headers(pool, OWNER_A)
    ws_headers = {"Origin": "https://testserver", "Cookie": headers["Cookie"]}
    try:
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/api/bots/alpha/browser/takeover", headers=headers).status_code == 200
            with _screen(client, launcher, ws_headers) as ws:
                ws.send_bytes(HANDSHAKE)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE])
                ws.send_bytes(KEY_A)
                assert _wait(lambda: _screen_inputs(launcher) == [HANDSHAKE, KEY_A]), "the human types before return"

                _fail_on(launcher, "browser_mode", LauncherServerError("cannot stop the human chromium"))
                assert client.post("/api/bots/alpha/browser/return", headers=headers).status_code == 502
                assert pool.bots["alpha"]["browser_control"] == "returning"
                ws.send_bytes(KEY_A)
                time.sleep(0.2)
                assert _screen_inputs(launcher) == [HANDSHAKE, KEY_A], "returning accepts no pointer or key input"
                secret = client.post("/api/bots/alpha/browser/secret-input",
                                     json={"value": SECRET_VALUE}, headers=headers)
                assert secret.status_code == 409 and secret.json()["error"] == "human_required"
                assert _screen_inputs(launcher) == [HANDSHAKE, KEY_A]
    finally:
        launcher.gate.set()


@pytest.mark.pure
async def test_return_retry_in_returning_repeats_both_calls_idempotently(monkeypatch):
    app = _app(monkeypatch)
    pool, launcher = app.state.pool, app.state.launcher
    pool.bots["alpha"]["browser_control"] = "returning"
    launcher.frozen.add("alpha")
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        first = await client.post("/api/bots/alpha/browser/return", headers=headers)
        second = await client.post("/api/bots/alpha/browser/return", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["state"] == second.json()["state"] == "returning"
    assert launcher.calls == [("browser_mode", "alpha", "bot", None), ("unfreeze_bot", "alpha")] * 2
    assert "alpha" not in launcher.frozen and pool.bots["alpha"]["browser_control"] == "returning"
    assert pool.events == [], "a retry writes no new audit event"


@pytest.mark.pure
async def test_return_without_a_launcher_is_503_and_changes_nothing(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    pool.bots["alpha"]["browser_control"] = "human"
    app.state.launcher = None
    headers = _headers(pool, OWNER_A)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/bots/alpha/browser/return", headers=headers)
    assert response.status_code == 503 and response.json()["error"] == "launcher_unavailable"
    assert pool.bots["alpha"]["browser_control"] == "human" and pool.events == []


# --- Startup: the bot's own Chromium first, then the thaw ---------------------------------------------------

@pytest.mark.pure
async def test_startup_restores_the_bot_browser_before_unfreezing_and_leaves_humans_alone(monkeypatch):
    bots = {"one": {"executor": "container", "provider": "claude", "browser_control": "human"},
            "two": {"executor": "container", "provider": "fake", "browser_control": "human"},
            "mac": {"executor": "mac", "provider": "claude", "browser_control": "human"}}
    launcher = FakeLauncherClient()
    app, pool = _startup_app(monkeypatch, bots, launcher)
    launcher.frozen.update(bots)
    async with app.router.lifespan_context(app):
        pass
    for key in ("one", "two"):
        assert launcher.calls.index(("browser_mode", key, "bot", None)) < launcher.calls.index(("unfreeze_bot", key))
    assert launcher.browser_modes == {"one": "bot", "two": "bot"}
    assert not [call for call in launcher.calls if call[0] == "browser_mode" and call[1] == "mac"]


@pytest.mark.pure
async def test_startup_does_not_touch_a_bot_whose_state_stays_human(monkeypatch):
    bots = {"calm": {"executor": "container", "provider": "claude", "browser_control": "bot"}}
    launcher = FakeLauncherClient()
    app, pool = _startup_app(monkeypatch, bots, launcher)
    # A bot that reads as human at the moment of the check (state read under the row lock) gets neither call.
    original = pool.connection.fetchval

    async def human(query, *args):
        if "select browser_control from bothub.bots where id=$1 for update" in query:
            return "human"
        return await original(query, *args)

    pool.connection.fetchval = human
    launcher.frozen.add("calm")
    launcher.browser_modes["calm"] = "human"
    async with app.router.lifespan_context(app):
        pass
    assert [call for call in launcher.calls if call[0] in ("browser_mode", "unfreeze_bot")] == []
    assert "calm" in launcher.frozen and launcher.browser_modes["calm"] == "human"


@pytest.mark.pure
async def test_startup_unfreezes_a_bot_whatever_the_outcome_of_its_browser_mode_call(monkeypatch, caplog):
    from bothub.launcher_client import LauncherConflict

    class FailingMode(FakeLauncherClient):
        async def browser_mode(self, bot_id, mode, *, url=None):
            self.calls.append(("browser_mode", bot_id, mode, url))
            if bot_id == "bad":
                raise ValueError("launcher answered garbage")  # not even a LauncherError
            if bot_id == "stopped":
                raise LauncherConflict("container is not running", code="conflict", status=409)
            if bot_id == "ghost":
                raise LauncherNotFound("no container", code="not_found", status=404)
            return await super().browser_mode(bot_id, mode, url=url)

    bots = {key: {"executor": "container", "provider": "claude", "browser_control": "human"}
            for key in ("bad", "good", "ghost", "stopped")}
    launcher = FailingMode()
    app, pool = _startup_app(monkeypatch, bots, launcher)
    launcher.frozen.update(bots)
    with caplog.at_level("WARNING"):
        async with app.router.lifespan_context(app):
            pass
    assert launcher.frozen == set(), "a failed browser_mode must not leave the bot frozen"
    for key in bots:
        assert ("unfreeze_bot", key) in launcher.calls, key
        assert launcher.calls.index(("browser_mode", key, "bot", None)) < launcher.calls.index(("unfreeze_bot", key))
    failed = [record for record in caplog.records if record.getMessage() == "browser_startup_mode_failed"]
    assert sorted(record.bot_id for record in failed) == ["bad", "stopped"], \
        "journal: failed bots only, nothing for a bot without container"
    assert "garbage" in next(record.error for record in failed if record.bot_id == "bad")


@pytest.mark.pure
async def test_startup_keeps_a_bot_frozen_when_the_launcher_refuses_the_unfreeze_next_to_a_human(monkeypatch, caplog):
    from bothub.launcher_client import LauncherConflict

    class HumanNextToIt(FakeLauncherClient):
        async def browser_mode(self, bot_id, mode, *, url=None):
            self.calls.append(("browser_mode", bot_id, mode, url))
            raise LauncherServerError("browser mode switch timed out")

        async def unfreeze_bot(self, bot_id):
            self.calls.append(("unfreeze_bot", bot_id))
            raise LauncherConflict("browser is in human mode", code="conflict", status=409)

    bots = {"calm": {"executor": "container", "provider": "claude", "browser_control": "human"}}
    launcher = HumanNextToIt()
    app, pool = _startup_app(monkeypatch, bots, launcher)
    launcher.frozen.add("calm")
    with caplog.at_level("WARNING"):
        async with app.router.lifespan_context(app):
            pass
    assert launcher.frozen == {"calm"}
    assert [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("browser_startup_")] == [
        "browser_startup_mode_failed", "browser_startup_unfreeze_failed"]
