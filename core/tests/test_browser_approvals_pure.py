"""Browser approvals, the core gate and the audit event, over HTTP with an in-memory pool (no Postgres).

The values typed into the bot's browser must not be stored, hashed or echoed; forbidden addresses are refused
without a question to the owner; reading the page needs no question."""
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest

from bothub.launcher_client import FakeBot, FakeLauncherClient
from bothub.main import canonical, create_app
from bothub.risk import remember_rule

pytestmark = pytest.mark.pure
OWNER = uuid.UUID(int=1)
THREAD = uuid.UUID(int=3)
TURN = uuid.UUID(int=4)
SECRET = "hunter2-Very-Private"
BROWSER = "mcp__bothub__browser"


class Con:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, query, *args):
        if "from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and t.thread_id=$2" in query:
            return 1
        if "update bothub.threads set last_seq" in query:
            self.pool.seq += 1
            return self.pool.seq
        return None

    async def fetchrow(self, query, *args):
        if "from bothub.bots b join bothub.users u" in query:
            return {"owner_id": OWNER}
        if "from bothub.threads where id=$1 and owner_id=$2" in query:
            return {"id": THREAD, "bot_id": "alpha", "owner_id": OWNER}
        if "select * from bothub.bots where id=$1 and owner_id=$2" in query:
            return {"id": "alpha", "owner_id": OWNER, "auto_allow": self.pool.auto_allow, "mac_full_control": False}
        if "select b.browser_control,b.provider,b.executor from bothub.turns t" in query:
            return {"browser_control": self.pool.control, "provider": "claude", "executor": "container"}
        if "insert into bothub.approvals" in query:
            keys = ("thread_id", "turn_id", "bot_id", "risk", "title", "tool", "args", "args_hash", "op_hash", "status", "expires_at")
            row = dict(zip(keys, args)) | {"id": uuid.uuid4(), "args": json.loads(args[6]), "remember": False,
                                            "created_at": datetime.now(timezone.utc)}
            self.pool.approvals.append(row)
            return row
        if "insert into bothub.events" in query:
            event = {"id": uuid.uuid4(), "thread_id": args[0], "seq": args[1], "turn_id": args[2], "kind": args[3],
                     "actor": args[4], "client": args[5], "payload": json.loads(args[6]),
                     "created_at": datetime.now(timezone.utc)}
            self.pool.events.append(event)
            return event
        raise AssertionError(f"unexpected SQL: {query}")

    async def fetch(self, query, *args):
        raise AssertionError(f"unexpected SQL: {query}")

    async def execute(self, query, *args):
        return "OK"


class Pool:
    def __init__(self):
        self.approvals, self.events, self.seq = [], [], 0
        self.auto_allow, self.control = [], "bot"
        self.connection = Con(self)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def no_lifespan(app):
    yield


def _app(monkeypatch):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", "approvals-test-secret")
    launcher = FakeLauncherClient()
    launcher.bots["alpha"] = FakeBot("alpha", str(OWNER))
    app = create_app(launcher=launcher)
    app.state.pool = Pool()
    app.router.lifespan_context = no_lifespan
    return app


def _bot_headers():
    signature = hmac.new(b"approvals-test-secret", b"alpha", hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer bot:alpha:{signature}"}


async def _post(app, path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        return await client.post(path, json=body, headers=_bot_headers())


def _approval(args, tool=BROWSER):
    return {"thread_id": str(THREAD), "turn_id": str(TURN), "risk": "other", "title": tool, "tool": tool, "args": args}


async def test_fill_value_is_not_stored_hashed_or_echoed(monkeypatch):
    app = _app(monkeypatch)
    args = {"action": "fill", "target": "e8", "element": "Search", "value": SECRET, "origin": "https://example.com"}
    response = await _post(app, "/api/approvals", _approval(args))
    pool = app.state.pool
    assert response.status_code == 200 and response.json()["status"] == "pending"
    assert SECRET not in response.text
    stored = pool.approvals[0]
    assert stored["args"] == {"action": "fill", "target": "e8", "element": "Search", "value": "[redacted]",
                              "origin": "https://example.com"}
    masked = args | {"value": "[redacted]"}
    assert stored["args_hash"] == hashlib.sha256(canonical(masked).encode()).hexdigest()
    assert SECRET not in json.dumps(stored, default=str) + json.dumps(pool.events, default=str)


async def test_navigate_url_is_reduced_to_scheme_host_path(monkeypatch):
    app = _app(monkeypatch)
    response = await _post(app, "/api/approvals", _approval(
        {"action": "navigate", "url": "https://user:pw@example.com/in?token=" + SECRET + "#x"}))
    assert response.status_code == 200
    assert app.state.pool.approvals[0]["args"] == {"action": "navigate", "url": "https://example.com/in"}
    assert SECRET not in response.text


@pytest.mark.parametrize("url", [
    "file:///home/bot/.claude/.credentials.json",
    "http://169.254.169.254/",
    "http://core:8080/gateway/",
    "chrome://settings/passwords",
    "view-source:file:///proc/self/environ",
    "http://localhost:8000/", "http://10.0.0.7/", "http://[fd00::1]/", "javascript:alert(1)",
])
async def test_forbidden_navigation_is_rejected_at_once_without_asking_the_owner(monkeypatch, url):
    app = _app(monkeypatch)
    pool = app.state.pool
    pool.auto_allow = [{"tool": BROWSER}]
    response = await _post(app, "/api/approvals", _approval({"action": "navigate", "url": url}))
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert [event["kind"] for event in pool.events] == ["approval_dec"]
    assert pool.events[0]["actor"] == "system" and pool.events[0]["payload"]["decision"] == "rejected"
    assert pool.events[0]["payload"]["reason"] in {"scheme_forbidden", "private_address", "internal_host", "loopback_host"}
    assert "approval_req" not in [event["kind"] for event in pool.events]


@pytest.mark.parametrize("action", ["snapshot", "screenshot"])
async def test_reading_the_page_is_approved_without_a_question(monkeypatch, action):
    app = _app(monkeypatch)
    response = await _post(app, "/api/approvals", _approval({"action": action}))
    assert response.status_code == 200 and response.json()["status"] == "approved"
    assert app.state.pool.events == []


async def test_pay_now_click_waits_for_the_owner_even_with_rules(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    pool.auto_allow = [{"tool": BROWSER},
                       {"tool": BROWSER, "scope": "browser_origin",
                        "match": {"action": "click", "origin": "https://shop.example.com"}}]
    args = {"action": "click", "target": "e12", "element": "Pay now", "origin": "https://shop.example.com"}
    response = await _post(app, "/api/approvals", _approval(args))
    assert response.status_code == 200 and response.json()["status"] == "pending" and response.json()["risk"] == "pay"
    assert "approval_req" in [event["kind"] for event in pool.events]


async def test_remembered_origin_rule_covers_navigation_to_that_origin_only(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    pool.auto_allow = [remember_rule(BROWSER, {"action": "navigate", "url": "https://example.com/"})]
    same = await _post(app, "/api/approvals", _approval({"action": "navigate", "url": "https://example.com/deep/page"}))
    other = await _post(app, "/api/approvals", _approval({"action": "navigate", "url": "https://example.org/"}))
    private = await _post(app, "/api/approvals", _approval({"action": "navigate", "url": "http://127.0.0.1/"}))
    assert [r.json()["status"] for r in (same, other, private)] == ["approved", "pending", "rejected"]


async def test_other_tools_keep_their_arguments(monkeypatch):
    app = _app(monkeypatch)
    response = await _post(app, "/api/approvals", _approval(
        {"command": "ls", "value": "kept", "url": "https://a.com/?q=1"}, tool="Bash"))
    assert response.status_code == 200
    assert app.state.pool.approvals[0]["args"] == {"command": "ls", "value": "kept", "url": "https://a.com/?q=1"}


# --- /api/browser/authorize and /api/browser/step ---------------------------------------------

def _call(action, **extra):
    return {"thread_id": str(THREAD), "turn_id": str(TURN), "action": action} | extra


@pytest.mark.parametrize("url", [
    "file:///home/bot/.claude/.credentials.json", "http://169.254.169.254/", "http://core:8080/gateway/",
    "chrome://settings/passwords", "view-source:file:///proc/self/environ", None,
])
async def test_core_gate_refuses_forbidden_navigation(monkeypatch, url):
    app = _app(monkeypatch)
    body = _call("navigate") | ({"url": url} if url else {})
    response = await _post(app, "/api/browser/authorize", body)
    assert response.status_code == 403 and response.json()["error"] == "url_forbidden"
    assert ("ensure_browser", "alpha") not in app.state.launcher.calls


async def test_core_gate_lets_public_navigation_and_reading_through(monkeypatch):
    app = _app(monkeypatch)
    assert (await _post(app, "/api/browser/authorize", _call("navigate", url="https://example.com/"))).status_code == 200
    assert (await _post(app, "/api/browser/authorize", _call("snapshot"))).status_code == 200
    assert (await _post(app, "/api/browser/authorize", _call("navigate", url="about:blank"))).status_code == 200


async def test_step_event_has_no_typed_text_and_a_reduced_url(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    fill = await _post(app, "/api/browser/step", _call("fill", target="password field " + SECRET[:4]))
    nav = await _post(app, "/api/browser/step", _call("navigate", url="https://a.com/p?token=" + SECRET))
    assert fill.status_code == nav.status_code == 200
    payloads = [event["payload"] for event in pool.events]
    assert payloads[0]["value"] == "[redacted]" and payloads[0]["target"] == "[redacted]"
    assert payloads[1]["url"] == "https://a.com/p"
    assert SECRET not in json.dumps(payloads)


# --- роль и имя элемента в событии: сырьё для процедур (раздел 14) -----------------------------------------------

async def test_fill_event_keeps_the_element_role_and_name_but_never_the_value(monkeypatch):
    app = _app(monkeypatch)
    pool = app.state.pool
    refused = await _post(app, "/api/browser/step", _call("fill", target="e8", role="textbox", name="Email", value=SECRET))
    assert refused.status_code == 400 and SECRET not in refused.text  # у вызова нет поля для значения
    response = await _post(app, "/api/browser/step", _call("fill", target="e8", role="textbox", name="Email"))
    assert response.status_code == 200
    payload = pool.events[0]["payload"]
    assert payload["role"] == "textbox" and payload["name"] == "Email"
    assert payload["value"] == "[redacted]" and payload["target"] == "[redacted]" and payload["secret"] is False
    assert SECRET not in json.dumps(pool.events, default=str)


async def test_click_event_keeps_the_element_role_and_name(monkeypatch):
    app = _app(monkeypatch)
    await _post(app, "/api/browser/step", _call("click", target="e12", role="button", name="Pay now"))
    payload = app.state.pool.events[0]["payload"]
    assert payload["role"] == "button" and payload["name"] == "Pay now" and payload["target"] == "e12"
    assert "secret" not in payload or payload["secret"] is False


@pytest.mark.parametrize("name", ["Password", "Пароль", "API key", "One-time code"])
async def test_a_password_like_field_is_marked_secret_in_the_event(monkeypatch, name):
    app = _app(monkeypatch)
    await _post(app, "/api/browser/step", _call("fill", target="e8", role="textbox", name=name))
    assert app.state.pool.events[0]["payload"]["secret"] is True


async def test_element_name_goes_through_the_same_masking_as_other_browser_text(monkeypatch):
    app = _app(monkeypatch)
    name = "Open https://user:pw@shop.example/cart?token=" + SECRET + "#x now"
    await _post(app, "/api/browser/step", _call("click", target="e1", role="link", name=name))
    stored = app.state.pool.events[0]["payload"]["name"]
    assert SECRET not in stored and "pw@" not in stored and "https://shop.example/cart" in stored


async def test_element_name_is_cut_and_role_is_checked(monkeypatch):
    app = _app(monkeypatch)
    too_long = await _post(app, "/api/browser/step", _call("click", target="e1", role="button", name="x" * 201))
    bad_role = await _post(app, "/api/browser/step", _call("click", target="e1", role="Button Bar", name="x"))
    assert too_long.status_code == 400 and bad_role.status_code in (400, 422)
    assert SECRET not in too_long.text + bad_role.text
    assert app.state.pool.events == []


async def test_events_without_role_and_name_stay_as_before(monkeypatch):
    app = _app(monkeypatch)
    await _post(app, "/api/browser/step", _call("click", target="e1"))
    payload = app.state.pool.events[0]["payload"]
    assert payload["role"] is None and payload["name"] is None and payload["target"] == "e1"


@pytest.mark.parametrize("char", ["\u202e", "\u200b", "\ufeff", "\u2066", "\x07", "\n", "\t", "\u00ad"])
async def test_control_and_invisible_characters_are_cleaned_from_the_element_name(monkeypatch, char):
    app = _app(monkeypatch)
    response = await _post(app, "/api/browser/step", _call("click", target="e1", role="button", name=f"Pay{char} now"))
    assert response.status_code == 200
    assert app.state.pool.events[0]["payload"]["name"] == "Pay now"


async def test_a_hidden_character_does_not_hide_a_secret_field_and_is_cleaned_from_the_role_too(monkeypatch):
    app = _app(monkeypatch)
    await _post(app, "/api/browser/step", _call("fill", target="e8", role="textbox", name="Pass\u202eword"))
    payload = app.state.pool.events[0]["payload"]
    assert payload["secret"] is True and payload["name"] == "Password"
    cleaned = await _post(app, "/api/browser/step", _call("click", target="e1", role="but\u202e\u200bton", name="x"))
    assert cleaned.status_code == 200 and app.state.pool.events[1]["payload"]["role"] == "button"
    refused = await _post(app, "/api/browser/step", _call("click", target="e1", role="Button Bar", name="x"))  # не роль и после очистки
    assert refused.status_code in (400, 422) and len(app.state.pool.events) == 2
