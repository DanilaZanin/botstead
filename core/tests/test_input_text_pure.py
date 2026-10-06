"""Текст на входе: NUL и непарные суррогаты (пп. 2, 3, 5 правок Opus): 4xx вместо 500, значение не уходит клиенту,
общий предел тела 1 МиБ."""
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub import auth
from bothub.main import (BODY_MAX, Body, BotIn, BotPatch, MacCallIn, PushIn, ScheduleIn, TurnIn, create_app)

pytestmark = pytest.mark.pure

SECRET = "text-test-secret"
OWNER = uuid.UUID(int=1)
BOT = {"name": "Scout", "provider": "claude", "model": "sonnet"}
SURROGATE = '"\\ud800"'  # JSON-запись непарного суррогата: httpx такую строку не закодирует
LEAK = "LEAKED-VALUE-0451"


class RecordingConnection:
    """Фиксирует каждый параметр запроса: строка с NUL или суррогатом в базе даёт 500 из asyncpg."""

    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, query, *args):
        self.pool.calls.append(args)
        if "from bothub.sessions s join bothub.users u" in query:
            return self.pool.sessions.get(args[0])
        return None

    async def fetchval(self, query, *args):
        self.pool.calls.append(args)
        return None

    async def fetch(self, query, *args):
        self.pool.calls.append(args)
        return []

    async def execute(self, query, *args):
        self.pool.calls.append(args)
        return "OK"


class RecordingPool:
    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.connection = RecordingConnection(self)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    def dirty_params(self):
        return [arg for args in self.calls for arg in args if isinstance(arg, str) and ("\x00" in arg or not clean(arg))]


def clean(text):
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("OWNER_TOKEN", "owner-secret")
    app = create_app()
    app.state.pool = RecordingPool()
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


def raw_json(pairs):
    """Тело JSON из готовых фрагментов значений (значение уже в JSON-записи)."""
    return "{" + ", ".join(f'{json.dumps(key)}: {value}' for key, value in pairs.items()) + "}"


async def send(client, method, url, pairs, **kwargs):
    return await client.request(method, url, content=raw_json(pairs),
                                headers={"content-type": "application/json"} | kwargs.get("headers", {}))


def refused(response, status=422):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == "invalid" and body["detail"], body
    assert LEAK not in response.text
    return body


# ---- п. 2: auth.py ---------------------------------------------------------------------------------------------

def test_token_hash_never_raises_on_unencodable_text():
    for value in ("\ud800", "a\udfffb", "a\x00b", "\ud800" * 1000):
        digest = auth.token_hash(value)
        assert len(digest) == 64 and digest != auth.token_hash("")
    assert auth.token_hash("abc") == auth.token_hash("abc")
    assert auth.token_hash("\ud800") != auth.token_hash("\ud801")


def test_token_equals_is_total():
    assert auth.token_equals("abc", "abc") is True
    assert auth.token_equals("abc", "abd") is False
    assert auth.token_equals("\ud800", "abc") is False
    assert auth.token_equals("\ud800", "\ud800") is True, "equal text stays equal, the comparison must not raise"
    assert auth.token_equals("abc", None) is False
    assert auth.token_equals("", "") is False, "an empty secret never matches"


def test_csrf_token_accepts_unencodable_session_text():
    assert len(auth.csrf_token("\ud800", "secret")) == 64


def test_is_clean_text():
    assert auth.is_clean_text("обычный текст\nс переводом строки\tи табом")
    for bad in ("a\x00", "\ud800", "x\udfffy"):
        assert not auth.is_clean_text(bad)


# ---- п. 2: маршруты ----------------------------------------------------------------------------------------------

async def test_invite_accept_with_a_lone_surrogate_token_is_410(client, app):
    for token in (SURROGATE, '"a\\u0000b"'):
        response = await send(client, "POST", "/api/invites/accept",
                              {"token": token, "email": '"a@example.com"', "password": '"long-password-1"'})
        assert response.status_code == 410 and response.json()["error"] == "invite_invalid", response.text
    assert app.state.pool.dirty_params() == []


async def test_invite_check_with_nul_token_is_not_an_error(client, app):
    response = await client.get("/api/invites/check?token=a%00b")
    assert response.status_code == 200 and response.json() == {"valid": False}
    assert app.state.pool.dirty_params() == []


async def test_nul_in_cookie_is_401_without_a_database_call(client, app):
    for header in ("bothub_session=a\x00b", "bothub_session=\x00"):
        response = await client.get("/api/auth/me", headers={"Cookie": header})
        assert response.status_code == 401, response.text
    assert app.state.pool.calls == []


async def test_nul_in_bearer_is_401_without_a_database_call(client, app):
    for token in ("a\x00b", "bot:a\x00b:signature", "bot:scout:sig\x00"):
        response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, (token, response.text)
    assert app.state.pool.dirty_params() == []


async def test_bot_token_with_a_nul_id_never_reaches_the_database(client, app):
    bot_id = "a\x00b"
    signature = hmac.new(SECRET.encode(), bot_id.encode(), hashlib.sha256).hexdigest()
    response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer bot:{bot_id}:{signature}"})
    assert response.status_code == 401, response.text
    assert app.state.pool.dirty_params() == []


async def test_setup_code_with_nul_is_401(client, app):
    response = await send(client, "POST", "/api/setup", {"email": '"a@example.com"', "password": '"long-password-1"'},
                          headers={"x-setup-code": "a\x00b"})
    assert response.status_code == 401, response.text


async def test_nul_path_ids_are_404_without_a_database_call(client, app):
    headers = owner_headers(app)
    app.state.pool.calls.clear()
    for method, url in (("DELETE", "/api/invites/a%00b"), ("DELETE", "/api/sessions/a%00b")):
        response = await client.request(method, url, headers=headers)
        assert response.status_code == 404, (url, response.text)
    assert app.state.pool.dirty_params() == []


# ---- п. 3: ValidationError без значения --------------------------------------------------------------------------

async def test_patch_bot_with_a_long_mcp_allow_item_is_422_without_the_value(client, app):
    headers = owner_headers(app)
    response = await client.patch("/api/bots/scout", json={"mcp_allow": [LEAK + "x" * 300]}, headers=headers)
    assert "mcp_allow" in refused(response)["detail"]


async def test_patch_bot_other_schema_errors_are_400_without_the_value(client, app):
    headers = owner_headers(app)
    for payload in ({"mac_full_control": LEAK}, {"name": LEAK * 10}, {"max_turn_seconds": LEAK}, {"unknown_" + LEAK: 1}):
        response = await client.patch("/api/bots/scout", json=payload, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid" and LEAK not in response.text, response.text
        assert "ValidationError" not in response.text and "validation error for" not in response.text


async def test_patch_bot_mixed_errors_keep_400(client, app):
    response = await client.patch("/api/bots/scout", json={"mcp_allow": ["x" * 201], "name": "n" * 81}, headers=owner_headers(app))
    assert response.status_code == 400


# ---- п. 5: общая проверка текста ---------------------------------------------------------------------------------

BAD_VALUES = ("a\x00b", "a\ud800b", "\udfff")


def test_every_request_model_rejects_nul_and_surrogates():
    models = []

    def collect(cls):
        for sub in cls.__subclasses__():
            models.append(sub)
            collect(sub)

    collect(Body)
    assert len(models) >= 14, "the check must cover all request models"
    for model in models:
        for bad in BAD_VALUES:
            with pytest.raises(ValueError) as caught:
                model.model_validate({"anything": bad})
            assert bad not in str(caught.value), model


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_models_reject_bad_text_in_nested_fields(bad):
    cases = (
        (BotIn, BOT | {"name": bad}),
        (BotIn, BOT | {"mcp_allow": [bad]}),
        (BotIn, BOT | {"auto_allow": [{"tool": bad}]}),
        (BotIn, BOT | {"auto_allow": [{"tool": "x", "match": {"key": bad}}]}),
        (BotIn, BOT | {"auto_allow": [{"tool": "x", "match": {bad: "v"}}]}),
        (BotIn, BOT | {"schedule": {"prompt": [bad]}}),
        (BotPatch, {"instructions": bad}),
        (BotPatch, {"mcp_allow": [bad]}),
        (TurnIn, {"prompt": bad}),
        (TurnIn, {"prompt": "hi", "client": bad}),
        (PushIn, {"endpoint": "e", "keys": {}, "device": bad}),
        (PushIn, {"endpoint": "e", "keys": {bad: "v"}}),
        (MacCallIn, {"thread_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()), "tool": "t", "args": {"a": [{"b": bad}]}}),
        (ScheduleIn, {"bot_id": "scout", "name": bad, "kind": "cron", "prompt": "p"}),
    )
    for model, payload in cases:
        with pytest.raises(ValueError):
            model.model_validate(payload)


def test_ordinary_text_is_unchanged():
    text = "Привет,\nмир!\t  emoji \U0001F600 \x1f"
    assert TurnIn.model_validate({"prompt": text, "client": "pwa"}).prompt == text
    assert BotIn.model_validate(BOT | {"mcp_allow": ["mcp__github__x"]}).mcp_allow == ["mcp__github__x"]


def test_deeply_nested_json_is_rejected_not_a_recursion_error():
    value = leaf = {}
    for _ in range(500):
        leaf["a"] = {}
        leaf = leaf["a"]
    with pytest.raises(ValueError):
        MacCallIn.model_validate({"thread_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()), "tool": "t", "args": value})


async def test_bad_text_in_models_is_422_not_500(client, app):
    headers = owner_headers(app)
    cases = (
        ("POST", "/api/bots", BOT | {"name": '"a\\u0000b"'}),
        ("POST", "/api/bots", BOT | {"mcp_allow": f"[{SURROGATE}]"}),
        ("POST", "/api/bots", BOT | {"auto_allow": f'[{{"tool": {SURROGATE}}}]'}),
        ("PATCH", "/api/bots/scout", {"mcp_allow": f"[{SURROGATE}]"}),
        ("POST", f"/api/threads/{uuid.uuid4()}/turns", {"prompt": '"a\\u0000b"'}),
        ("POST", f"/api/threads/{uuid.uuid4()}/turns", {"prompt": '"hi"', "client": SURROGATE}),
        ("POST", "/api/push/subscribe", {"endpoint": '"e"', "keys": "{}", "device": SURROGATE}),
        ("POST", "/api/push/subscribe", {"endpoint": '"e"', "keys": "{}", "device": '"a\\u0000"'}),
    )
    for method, url, body in cases:
        pairs = {key: value if isinstance(value, str) and value.startswith(('[', '{', '"')) else json.dumps(value)
                 for key, value in body.items()}
        response = await send(client, method, url, pairs, headers=headers)
        assert response.status_code == 422, (url, body, response.text)
        assert response.json()["error"] == "invalid"
    assert app.state.pool.dirty_params() == []


async def test_surrogate_key_does_not_break_the_error_response(client, app):
    headers = owner_headers(app)
    body = '{"name": "x", "\\ud800": 1}'
    for method, url in (("POST", "/api/bots"), ("PATCH", "/api/bots/scout"), ("PATCH", f"/api/users/{uuid.uuid4()}")):
        response = await client.request(method, url, content=body, headers={"content-type": "application/json"} | headers)
        assert response.status_code == 422, response.text


async def test_dict_body_routes_reject_bad_text(client, app):
    headers = owner_headers(app)
    base = {"email": '"a@example.com"', "password": '"long-password-1"'}
    refused(await send(client, "POST", "/api/auth/login", base | {"password": '"long-passw\\u0000ord"'}))
    refused(await send(client, "POST", "/api/setup", base | {"password": '"long-passw\\u0000ord"'}))
    refused(await send(client, "PATCH", f"/api/users/{uuid.uuid4()}", {"role": SURROGATE}, headers=headers))
    refused(await send(client, "POST", "/api/providers", {"kind": '"api_key"', "name": '"a\\u0000b"'}, headers=headers))
    refused(await send(client, "PATCH", "/api/bots/scout", {"name": '"a\\u0000b"'}, headers=headers))
    assert app.state.pool.dirty_params() == []


# ---- п. 5: поля auto_allow ------------------------------------------------------------------------------------

def test_auto_allow_field_limits():
    def rule(**fields):
        return BotIn.model_validate(BOT | {"auto_allow": [{"tool": "x"} | fields]})

    rule(pattern="p" * 500)
    rule(match={"k": "v" * 500}, op_hash="a" * 64, scope="browser_origin")
    with pytest.raises(ValueError):
        rule(pattern="p" * 501)
    for bad in ({"op_hash": "a" * 129}, {"scope": "s" * 65}, {"match": {"k" * 201: "v"}},
                {"match": {f"k{i}": "v" for i in range(65)}}, {"match": {"k": "v" * 4097}}, {"match": ["x"]},
                {"op_hash": 5}, {"scope": []}, {"pattern": 5}):
        with pytest.raises(ValueError):
            rule(**bad)


async def test_auto_allow_pattern_limit_is_422(client, app):
    response = await client.post("/api/bots", json=BOT | {"auto_allow": [{"tool": "x", "pattern": "p" * 501}]},
                                 headers=owner_headers(app))
    refused(response)


# ---- п. 5: предел тела --------------------------------------------------------------------------------------------

def padded(size):
    """JSON-тело ровно в size байт."""
    prefix, suffix = '{"email": "a@example.com", "password": "', '"}'
    return (prefix + "p" * (size - len(prefix) - len(suffix)) + suffix).encode()


async def test_body_above_the_limit_is_413_in_the_error_format(client):
    response = await client.post("/api/auth/login", content=padded(BODY_MAX + 1), headers={"content-type": "application/json"})
    assert response.status_code == 413
    body = response.json()
    assert body["error"] == "invalid" and "1 MiB" in body["detail"], body


async def test_body_at_the_limit_reaches_the_handler(client):
    response = await client.post("/api/auth/login", content=padded(BODY_MAX), headers={"content-type": "application/json"})
    assert response.status_code == 422, "the password is longer than 512: the handler answered, not the limit"


async def test_streamed_body_without_content_length_is_limited_too(client):
    chunk = b"x" * 65536

    async def stream():
        for _ in range(BODY_MAX // len(chunk) + 2):
            yield chunk

    response = await client.post("/api/auth/login", content=stream(), headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json()["error"] == "invalid"


async def test_multipart_upload_is_not_limited_by_the_json_cap(client):
    files = {"file": ("big.bin", b"x" * (BODY_MAX + 1024), "application/octet-stream")}
    response = await client.post("/api/files", files=files, data={"thread_id": str(uuid.uuid4())})
    assert response.status_code == 401, "an upload goes to its own handler, which asks for authentication"


async def test_gateway_keeps_its_own_body_limit(client):
    """Промпты CLI идут через /gateway: у шлюза свой предел (32 МиБ), общий 1 МиБ его не режет."""
    big = b'{"messages": "' + b"x" * (2 * BODY_MAX) + b'"}'
    response = await client.post(f"/gateway/{uuid.uuid4()}/v1/messages", content=big, headers={"content-type": "application/json"})
    assert response.status_code == 401, "the gateway answered (no token), not the body limit"


async def test_hook_keeps_its_own_limit_and_raw_text(client):
    response = await client.post(f"/hooks/{uuid.uuid4()}", content=b'{"a": "' + b"x" * 70000 + b'"}', headers={"x-hook-token": "t"})
    assert response.status_code == 413


# ---- страховка: текст, который всё же дошёл до базы -------------------------------------------------------------

async def test_database_text_rejection_is_400_not_500(client, app, monkeypatch):
    import asyncpg

    original = RecordingConnection.fetchrow

    async def picky(self, query, *args):
        if any(isinstance(arg, str) and "\x00" in arg for arg in args):
            raise asyncpg.CharacterNotInRepertoireError("invalid byte sequence for encoding UTF8: 0x00")
        return await original(self, query, *args)

    monkeypatch.setattr(RecordingConnection, "fetchrow", picky)
    response = await client.patch("/api/bots/a%00b", json={"name": "x"}, headers=owner_headers(app))
    assert response.status_code == 400 and response.json() == {"error": "invalid", "detail": "invalid"}
