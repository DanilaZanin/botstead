"""Пределы длины полей и ответы без повтора входных данных."""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub import auth, delegation, group_chat, procedures, wakeups
from bothub.main import (ApprovalIn, BotIn, BotPatch, BrowserCallIn, DecisionIn, DelegateIn, GroupIn, GroupMessageIn,
                         GroupPatchIn, MemoryIn, ProcedureFromTurnIn, ProcedureImportIn, ProcedureIn,
                         ProcedurePatch, ProcedureRunIn, PushIn, ScheduleIn, ThreadIn, TurnIn, WakeupIn,
                         create_app, parse_email)

pytestmark = pytest.mark.pure

SECRET = "limits-test-secret"
OWNER = uuid.UUID(int=1)
BOT = {"name": "Scout", "provider": "claude", "model": "sonnet"}
MEGABYTE = 900_000  # далеко за пределом любого поля, но меньше общего предела тела (BODY_MAX): иначе ответит 413


class SessionConnection:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, query, *args):
        if "from bothub.sessions s join bothub.users u" in query:
            return self.pool.sessions.get(args[0])
        return None

    async def fetchval(self, query, *args):
        return None


class SessionPool:
    def __init__(self):
        self.sessions = {}
        self.connection = SessionConnection(self)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", SECRET)
    app = create_app()
    app.state.pool = SessionPool()
    return app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as http:
        yield http


def owner_headers(app):
    token = "session-owner"
    app.state.pool.sessions[auth.token_hash(token)] = {
        "user_id": OWNER, "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
        "last_extended_at": datetime.now(timezone.utc), "role": "admin", "status": "active"}
    return {"Cookie": f"bothub_session={token}", "Origin": "https://testserver",
            "X-CSRF": auth.csrf_token(token, SECRET)}


async def post_escaped(client, url, body, field, **kwargs):
    """JSON с непарным суррогатом в поле: httpx такую строку не закодирует, шлём экранированную запись."""
    parts = [f'"{key}": {json.dumps(value)}' for key, value in body.items()] + [f'"{field}": "\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800\\ud800"']
    return await client.post(url, content="{" + ", ".join(parts) + "}", headers={"content-type": "application/json"} | kwargs.get("headers", {}))


def refused(response, detail=None, status=422):
    body = response.json()
    assert response.status_code == status, response.text
    assert body["error"] == "invalid" and body["detail"]
    if detail:
        assert detail in body["detail"], body
    return body


# ---- mcp_allow, auto_allow ----------------------------------------------------------------------------------

@pytest.mark.parametrize("model", [BotIn, BotPatch])
def test_mcp_allow_item_bounds(model):
    base = BOT if model is BotIn else {}
    model.model_validate(base | {"mcp_allow": ["x"]})
    model.model_validate(base | {"mcp_allow": ["x" * 200]})
    model.model_validate(base | {"mcp_allow": ["x"] * 200})
    for bad in ([""], ["x" * 201], ["x"] * 201, ["ok", ""]):
        with pytest.raises(ValueError):
            model.model_validate(base | {"mcp_allow": bad})


@pytest.mark.parametrize("model", [BotIn, BotPatch])
def test_auto_allow_rule_bounds(model):
    base = BOT if model is BotIn else {}
    model.model_validate(base | {"auto_allow": [{"tool": "x"}]})
    model.model_validate(base | {"auto_allow": [{"tool": "x" * 200}]})
    model.model_validate(base | {"auto_allow": [{}] * 200})
    for bad in ([{"tool": ""}], [{"tool": "x" * 201}], [{"tool": 5}], [{}] * 201):
        with pytest.raises(ValueError):
            model.model_validate(base | {"auto_allow": bad})


async def test_list_limits_answer_400(client, app):
    headers = owner_headers(app)
    for field, bad in (("mcp_allow", ["x" * 201]), ("mcp_allow", ["x"] * 201),
                       ("auto_allow", [{"tool": "x" * 201}]), ("auto_allow", [{}] * 201)):
        refused(await client.post("/api/bots", json=BOT | {field: bad}, headers=headers), status=400)
    for field, bad in (("mcp_allow", [""]), ("auto_allow", [{"tool": ""}])):
        refused(await client.post("/api/bots", json=BOT | {field: bad}, headers=headers))


async def test_other_schema_errors_stay_400(client, app):
    response = await client.post("/api/bots", json=BOT | {"mcp_allow": "text"}, headers=owner_headers(app))
    assert response.status_code == 400 and response.json()["error"] == "invalid"
    mixed = await client.post("/api/bots", json=BOT | {"mcp_allow": ["x" * 201], "name": "n" * 81}, headers=owner_headers(app))
    assert mixed.status_code == 400, "a limit error next to an ordinary schema error keeps the old status"


# ---- client, timezone, device ---------------------------------------------------------------------------------

def test_client_bounds():
    for model, base in ((TurnIn, {"prompt": "hi"}), (DecisionIn, {"decision": "approve"})):
        model.model_validate(base | {"client": "c" * 64})
        model.model_validate(base | {"client": ""})
        with pytest.raises(ValueError):
            model.model_validate(base | {"client": "c" * 65})


def test_timezone_bounds_and_zoneinfo():
    base = {"bot_id": "scout", "name": "n", "kind": "cron", "cron": "0 9 * * *", "prompt": "p"}
    for good in ("Europe/Moscow", "UTC", "America/Argentina/Buenos_Aires"):
        ScheduleIn.model_validate(base | {"timezone": good})
    ScheduleIn.model_validate(base)
    for bad in ("", "x" * 64, "x" * 65, "Mars/Olympus", "../etc/passwd", "Europe", "/etc/passwd", "UTC\x00"):
        with pytest.raises(ValueError):
            ScheduleIn.model_validate(base | {"timezone": bad})


def test_device_bounds():
    PushIn.model_validate({"endpoint": "e", "keys": {}, "device": "d" * 128})
    PushIn.model_validate({"endpoint": "e", "keys": {}})
    with pytest.raises(ValueError):
        PushIn.model_validate({"endpoint": "e", "keys": {}, "device": "d" * 129})


def test_push_subscription_bounds():
    PushIn.model_validate({"endpoint": "e" * 2048, "keys": {"k" * 256: "v" * 256}})
    for body in ({"endpoint": "e" * 2049, "keys": {}},
                 {"endpoint": "e", "keys": {"k" * 257: "v"}},
                 {"endpoint": "e", "keys": {"k": "v" * 257}}):
        with pytest.raises(ValueError):
            PushIn.model_validate(body)


@pytest.mark.parametrize("model,base,field,limit", [
    (BotIn, BOT, "provider", 200), (BotIn, BOT, "model", 200),
    (BotPatch, {}, "provider", 200), (BotPatch, {}, "model", 200),
    (ThreadIn, {}, "bot_id", 200), (ThreadIn, {"bot_id": "b"}, "title", 200),
    (ApprovalIn, {"thread_id": str(OWNER), "title": "t", "tool": "tool", "args": {}}, "risk", 200),
    (ApprovalIn, {"thread_id": str(OWNER), "risk": "r", "tool": "tool", "args": {}}, "title", 200),
    (DecisionIn, {}, "decision", 200),
    (MemoryIn, {"text": "memo"}, "bot_id", 200),
    (ScheduleIn, {"name": "n", "kind": "cron", "prompt": "p"}, "bot_id", 200),
    (ScheduleIn, {"bot_id": "b", "kind": "cron", "prompt": "p"}, "name", 200),
    (ScheduleIn, {"bot_id": "b", "name": "n", "prompt": "p"}, "kind", 64),
    (ScheduleIn, {"bot_id": "b", "name": "n", "kind": "cron", "prompt": "p"}, "cron", 200),
    (WakeupIn, {"prompt": "p"}, "at", 64),
    (DelegateIn, {"task": "t", "turn_id": str(OWNER)}, "bot", 200),
    (GroupIn, {"title": "t", "bot_ids": ["b"], "mode": "round"}, "moderator_bot_id", 200),
    (GroupPatchIn, {}, "moderator_bot_id", 200),
    (BrowserCallIn, {"thread_id": str(OWNER), "turn_id": str(OWNER), "action": "click"}, "name", 200),
    (ProcedureIn, {"name": "n"}, "bot_id", 200),
    (ProcedurePatch, {}, "bot_id", 200),
    (ProcedureImportIn, {"name": "n"}, "format", 200),
    (ProcedureRunIn, {}, "bot_id", 200),
])
def test_free_string_bounds(model, base, field, limit):
    model.model_validate(base | {field: "x" * limit})
    with pytest.raises(ValueError):
        model.model_validate(base | {field: "x" * (limit + 1)})


@pytest.mark.parametrize("model,base,field,limit,validator,code", [
    (WakeupIn, {}, "prompt", 2000, wakeups.clean_prompt, "prompt_too_long"),
    (WakeupIn, {"prompt": "p"}, "reason", 200, wakeups.clean_reason, "reason_too_long"),
    (DelegateIn, {"bot": "b", "turn_id": str(OWNER)}, "task", 4000, delegation.clean_task, "task_too_long"),
    (GroupIn, {"bot_ids": ["b"], "mode": "round"}, "title", 120, group_chat.clean_title, "title_too_long"),
    (GroupPatchIn, {}, "title", 120, group_chat.clean_title, "title_too_long"),
    (GroupMessageIn, {}, "text", 8000, group_chat.clean_message, "text_too_long"),
    (ProcedureIn, {}, "name", 120, procedures.check_name, "too_long"),
    (ProcedureIn, {"name": "n"}, "description", 2000, procedures.check_description, "too_long"),
    (ProcedurePatch, {}, "name", 120, procedures.check_name, "too_long"),
    (ProcedurePatch, {}, "description", 2000, procedures.check_description, "too_long"),
    (ProcedureImportIn, {}, "name", 120, procedures.check_name, "too_long"),
    (ProcedureImportIn, {"name": "n"}, "description", 2000, procedures.check_description, "too_long"),
    (ProcedureFromTurnIn, {"thread_id": str(OWNER)}, "name", 120, procedures.check_name, "too_long"),
])
def test_manual_validation_fields_reach_route(model, base, field, limit, validator, code):
    value = "x" * (limit + 1)
    model.model_validate(base | {field: value})
    with pytest.raises(ValueError) as caught:
        validator(value)
    assert caught.value.code == code
    if hasattr(caught.value, "status"):
        assert caught.value.status == 422


def test_group_bot_id_item_bound():
    base = {"title": "t", "mode": "round"}
    GroupIn.model_validate(base | {"bot_ids": ["x" * 200]})
    with pytest.raises(ValueError):
        GroupIn.model_validate(base | {"bot_ids": ["x" * 201]})


async def test_turn_client_limit_answers_400(client, app):
    url = f"/api/threads/{uuid.uuid4()}/turns"
    refused(await client.post(url, json={"prompt": "hi", "client": "c" * 65}, headers=owner_headers(app)), "client", 400)
    ok = await client.post(url, json={"prompt": "hi", "client": "c" * 64})
    assert ok.status_code == 401, "the boundary value passed validation and stopped at the authentication"


async def test_decision_client_limit_answers_400(client, app):
    url = f"/api/approvals/{uuid.uuid4()}/decide"
    refused(await client.post(url, json={"decision": "approve", "client": "c" * 65}, headers=owner_headers(app)), "client", 400)


async def test_schedule_timezone_limits_answer_400(client, app):
    headers = owner_headers(app)
    base = {"bot_id": "scout", "name": "n", "kind": "cron", "cron": "0 9 * * *", "prompt": "p"}
    refused(await client.post("/api/schedules", json=base | {"timezone": "x" * 65}, headers=headers), "timezone", 400)
    refused(await client.post("/api/schedules", json=base | {"timezone": "Mars/Olympus"}, headers=headers), "time zone")
    refused(await client.post("/api/schedules", json=base | {"timezone": "../../etc/passwd"}, headers=headers), "time zone")
    ok = await client.post("/api/schedules", json=base | {"timezone": "Europe/Moscow"})
    assert ok.status_code == 401, "a known zone passed validation and stopped at the authentication"


async def test_bot_schedule_timezone_limits_answer_422(client, app):
    headers = owner_headers(app)
    schedule = {"cron": "0 9 * * *", "prompt": "p"}
    refused(await client.post("/api/bots", json=BOT | {"schedule": schedule | {"timezone": "x" * 65}}, headers=headers), "timezone")
    refused(await client.post("/api/bots", json=BOT | {"schedule": schedule | {"timezone": "Mars/Olympus"}}, headers=headers), "timezone")
    wrong_type = await client.post("/api/bots", json=BOT | {"schedule": schedule | {"timezone": 5}}, headers=headers)
    assert wrong_type.status_code == 400


async def test_push_device_limit_answers_400(client, app):
    headers = owner_headers(app)
    refused(await client.post("/api/push/subscribe", json={"endpoint": "e", "keys": {}, "device": "d" * 129}, headers=headers), "device", 400)
    ok = await client.post("/api/push/subscribe", json={"endpoint": "e", "keys": {}, "device": "d" * 128})
    assert ok.status_code == 401, "the boundary value passed validation and stopped at the authentication"


# ---- email and the public routes ------------------------------------------------------------------------------

def test_email_check_bounds():
    local = "a" * 64
    exact = local + "@" + "b" * (254 - len(local) - 1)
    assert len(exact) == 254 and parse_email(exact) == exact
    assert parse_email("  A@Example.COM ") == "a@example.com"
    for bad in (exact + "b", "x" * MEGABYTE + "@x.com", "", " ", "no-at", "@x.com", "a@", "a@@b", "a@b@c", "a b@x.com",
                "a@x\x00.com", "a\n@x.com", "a@\ud800.com"):
        with pytest.raises(Exception) as caught:
            parse_email(bad)
        assert caught.value.status_code == (400 if len(bad.strip()) > 254 else 422)


def test_email_must_be_a_string():
    for bad in (None, 5, ["a@b.c"], {"a": 1}):
        with pytest.raises(Exception) as caught:
            parse_email(bad)
        assert caught.value.status_code == 400


async def test_invite_accept_with_a_huge_email_answers_400(client):
    base = {"token": "t" * 43, "password": "long-password"}
    for email in ("a" * MEGABYTE + "@example.com", "a" * 255 + "@x.com", "a" * 249 + "@x.com", "nope", "a@b@c.d", "a\x00@b.c", ""):
        response = await client.post("/api/invites/accept", json=base | {"email": email})
        refused(response, "email", 400 if len(email.strip()) > 254 else 422)
        assert email not in response.text or not email


async def test_invite_accept_limits_on_the_other_fields(client):
    good_email = {"email": "a@example.com"}
    refused(await client.post("/api/invites/accept", json={"token": "t", "password": "p" * (MEGABYTE), **good_email}), "password")
    refused(await client.post("/api/invites/accept", json={"token": "t", "password": "p" * 513, **good_email}), "password")
    refused(await post_escaped(client, "/api/invites/accept", {"token": "t", **good_email}, "password"), "password")
    gone = await client.post("/api/invites/accept", json={"token": "t" * MEGABYTE, "password": "long-password", **good_email})
    assert gone.status_code == 410 and gone.json()["error"] == "invite_invalid"
    assert (await client.post("/api/invites/accept", json={"token": "t", "email": 5, "password": "long-password"})).status_code == 400
    short = await client.post("/api/invites/accept", json={"token": "t", "password": "short", **good_email})
    assert short.status_code == 400, "a short password keeps its old answer"


async def test_invite_check_with_a_huge_token_is_not_an_error(client):
    response = await client.get("/api/invites/check", params={"token": "t" * 60000})
    assert response.status_code == 200 and response.json() == {"valid": False}


async def test_setup_limits_answer_422_before_the_database(client):
    base = {"email": "a@example.com", "password": "long-password"}
    refused(await client.post("/api/setup", json=base | {"email": "a" * MEGABYTE + "@x.com"}), "email", 400)
    refused(await client.post("/api/setup", json=base | {"email": "a" * 255 + "@x.com"}), "email", 400)
    refused(await client.post("/api/setup", json=base | {"email": "nope"}), "email")
    refused(await client.post("/api/setup", json=base | {"password": "p" * MEGABYTE}), "password")
    refused(await client.post("/api/setup", json=base | {"password": "p" * 513}), "password")
    refused(await post_escaped(client, "/api/setup", base, "password"), "password")
    for body in ({"email": 5, "password": "long-password"}, {"email": "a@b.c"}, {"password": "long-password"}):
        assert (await client.post("/api/setup", json=body)).status_code == 400


async def test_login_limits_answer_422_before_the_database(client):
    base = {"email": "a@example.com", "password": "long-password"}
    refused(await client.post("/api/auth/login", json=base | {"email": "a" * MEGABYTE + "@x.com"}), "email", 400)
    refused(await client.post("/api/auth/login", json=base | {"email": "a" * 255 + "@x.com"}), "email", 400)
    refused(await client.post("/api/auth/login", json=base | {"email": "a\x00@x.com"}), "email")
    refused(await client.post("/api/auth/login", json=base | {"password": "p" * MEGABYTE}), "password")
    refused(await client.post("/api/auth/login", json=base | {"password": "p" * 513}), "password")
    refused(await post_escaped(client, "/api/auth/login", base, "password"), "password")
    assert (await client.post("/api/auth/login", json={"email": "a@example.com"})).status_code == 400


async def test_password_change_limits_answer_422(client, app):
    headers = owner_headers(app)
    ok_old = {"old_password": "old-password-1", "new_password": "new-password-1"}
    refused(await client.post("/api/auth/password", json=ok_old | {"new_password": "p" * MEGABYTE}, headers=headers), "new_password")
    refused(await client.post("/api/auth/password", json=ok_old | {"new_password": "p" * 513}, headers=headers), "new_password")
    refused(await client.post("/api/auth/password", json=ok_old | {"old_password": "p" * MEGABYTE}, headers=headers), "old_password")
    refused(await post_escaped(client, "/api/auth/password", ok_old, "old_password", headers=headers), "old_password")
    short = await client.post("/api/auth/password", json=ok_old | {"new_password": "short"}, headers=headers)
    assert short.status_code == 400


async def test_hook_body_in_another_encoding_is_400_not_500(client):
    raw = '{"a": 1}'.encode("utf-16")
    response = await client.post(f"/hooks/{uuid.uuid4()}", content=raw, headers={"x-hook-token": "t"})
    assert response.status_code == 400 and response.json()["error"] == "invalid"
