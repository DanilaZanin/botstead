"""Stage 4 authorization and isolation checks for the HTTP route surface."""

import hashlib
import uuid

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount

from bothub.auth import hash_password
from bothub.main import create_app


pytestmark = pytest.mark.empty_users
OWNER = {"Authorization": "Bearer test-owner"}
PASSWORD = "long-password"


@pytest.fixture(autouse=True)
def local_cookie_path(monkeypatch):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")


async def _client():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            yield client, app


async def _setup(client):
    response = await client.post("/api/setup", json={"email": "a@example.com", "password": PASSWORD}, headers=OWNER)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _login(client, email):
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.cookies["bothub_session"], response.json()["csrf_token"]


def _cookie(token, csrf=None):
    headers = {"Cookie": f"bothub_session={token}"}
    if csrf:
        headers.update({"X-CSRF": csrf, "Origin": "https://testserver"})
    return headers


@pytest.mark.pure
def test_route_authorization_inventory():
    app = create_app()
    public = {
        ("GET", "/api/health"),
        ("GET", "/api/setup/status"),
        ("POST", "/api/setup"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/invites/check"),
        ("POST", "/api/invites/accept"),
        ("POST", "/hooks/{id}"),
        ("POST", "/hooks/{id}/github"),
        ("POST", "/hooks/{id}/slack"),
    }
    protected = {
        ("GET", "/api/auth/me"), ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/password"), ("POST", "/api/invites"),
        ("GET", "/api/invites"), ("DELETE", "/api/invites/{id}"),
        ("GET", "/api/users"), ("PATCH", "/api/users/{id}"),
        ("GET", "/api/sessions"), ("DELETE", "/api/sessions/{id}"),
        ("POST", "/api/mac/token"), ("DELETE", "/api/mac/token"),
        ("GET", "/api/bots"), ("POST", "/api/bots/draft"),
        ("POST", "/api/bots"), ("PATCH", "/api/bots/{id}"),
        ("GET", "/api/threads"), ("POST", "/api/threads"),
        ("GET", "/api/threads/{id}"), ("POST", "/api/threads/{id}/compact"),
        ("PATCH", "/api/threads/{id}"), ("POST", "/api/threads/{id}/turns"),
        ("POST", "/api/turns/{id}/stop"), ("GET", "/api/threads/{id}/events"),
        ("GET", "/api/approvals"), ("POST", "/api/approvals"),
        ("POST", "/api/approvals/{id}/decide"), ("GET", "/api/approvals/{id}/wait"),
        ("GET", "/api/memory"), ("POST", "/api/memory"),
        ("PATCH", "/api/memory/{id}"), ("DELETE", "/api/memory/{id}"),
        ("GET", "/api/usage/summary"),
        ("POST", "/api/usage"), ("GET", "/api/schedules"),
        ("POST", "/api/schedules"), ("PATCH", "/api/schedules/{id}"),
        ("POST", "/api/schedules/{id}/run"), ("POST", "/api/files"),
        ("GET", "/api/files/{id}"), ("GET", "/api/mac/status"),
        ("POST", "/api/mac/call"), ("POST", "/api/push/subscribe"),
        ("GET", "/api/providers"), ("POST", "/api/providers"),
        ("PATCH", "/api/providers/{id}"), ("DELETE", "/api/providers/{id}"),
        ("POST", "/api/providers/{id}/check"),
        ("PATCH", "/api/providers/{id}/allow-private"), ("GET", "/api/admin/provider-requests"),
        ("GET", "/api/models"), ("PATCH", "/api/models/{id}"),
        ("POST", "/api/models/refresh"),
        ("DELETE", "/api/bots/{id}"), ("POST", "/api/bots/{id}/recreate"),
        ("POST", "/api/bots/import"), ("GET", "/api/bots/{id}/export"),
        ("GET", "/api/bots/{id}/browser"),
        ("POST", "/api/bots/{id}/browser/takeover"),
        ("POST", "/api/bots/{id}/browser/return"),
        ("POST", "/api/bots/{id}/browser/secret-input"),
        ("POST", "/api/browser/authorize"), ("POST", "/api/browser/step"),
        ("GET", "/api/procedures"), ("POST", "/api/procedures"),
        ("POST", "/api/procedures/from-turn"), ("POST", "/api/procedures/import"),
        ("GET", "/api/procedures/{id}"), ("PATCH", "/api/procedures/{id}"), ("DELETE", "/api/procedures/{id}"),
        ("GET", "/api/procedures/{id}/export"), ("GET", "/api/procedures/{id}/runs"),
        ("POST", "/api/procedures/{id}/run"), ("GET", "/api/procedure-runs/{id}"),
        ("POST", "/api/procedure-runs/{id}/stop"), ("POST", "/api/procedure-runs/{id}/decide"),
        ("GET", "/api/secrets"),
        ("GET", "/api/activity"), ("POST", "/api/bots/{id}/pause"), ("POST", "/api/bots/{id}/resume"),
        ("POST", "/api/bots/pause-all"), ("POST", "/api/bots/resume-all"),
        ("POST", "/api/bots/wakeups"), ("GET", "/api/bots/{id}/wakeups"), ("DELETE", "/api/wakeups/{id}"),
        ("POST", "/api/bots/delegations"), ("GET", "/api/bots/delegations/{turn_id}"),
    }
    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield from ((method, route.path) for method in route.methods)
            elif isinstance(route, Mount):
                yield from walk(route.routes)
            elif hasattr(route, 'original_router'):
                yield from walk(route.original_router.routes)
    actual = {(method, path) for method, path in walk(app.routes) if not path.startswith('/gateway/')}
    assert actual == public | protected
    assert any(isinstance(route, APIWebSocketRoute) and route.path == '/api/bots/{id}/screen'
               for route in app.routes)


async def test_logout_revokes_only_present_session():
    async for client, _ in _client():
        await _setup(client)
        first, first_csrf = await _login(client, "a@example.com")
        second, _ = await _login(client, "a@example.com")
        response = await client.post("/api/auth/logout", headers=_cookie(first, first_csrf))
        assert response.status_code == 200, response.text
        assert (await client.get("/api/auth/me", headers=_cookie(first))).status_code == 401
        assert (await client.get("/api/auth/me", headers=_cookie(second))).status_code == 200


async def test_cookie_csrf_and_origin_matrix():
    async for client, _ in _client():
        await _setup(client)
        token, csrf = await _login(client, "a@example.com")
        cases = [
            ({}, 403),
            ({"X-CSRF": csrf}, 403),
            ({"Origin": "https://testserver"}, 403),
            ({"X-CSRF": "bad", "Origin": "https://testserver"}, 403),
            ({"X-CSRF": csrf, "Origin": "https://evil.example"}, 403),
            ({"X-CSRF": csrf, "Referer": "https://testserver/page"}, 201),
        ]
        for extra, expected in cases:
            response = await client.post("/api/invites", json={}, headers=_cookie(token) | extra)
            assert response.status_code == expected, (extra, response.text)
        assert (await client.post("/api/invites", json={}, headers=OWNER)).status_code == 201


async def test_activity_and_pause_routes_enforce_csrf_and_refuse_bot_tokens():
    import hmac
    async for client, _ in _client():
        await _setup(client)
        bot = await client.post("/api/bots", json={"id": "alpha", "name": "Alpha", "provider": "fake", "model": "fake"}, headers=OWNER)
        assert bot.status_code == 200, bot.text
        token, csrf = await _login(client, "a@example.com")
        digest = hmac.new(b"test-secret", b"alpha", hashlib.sha256).hexdigest()
        as_bot = {"Authorization": f"Bearer bot:alpha:{digest}"}
        writes = ["/api/bots/alpha/pause", "/api/bots/alpha/resume", "/api/bots/pause-all", "/api/bots/resume-all"]
        for path in writes:
            for extra, expected in (({}, 403), ({"X-CSRF": csrf}, 403), ({"X-CSRF": "bad", "Origin": "https://testserver"}, 403),
                                    ({"X-CSRF": csrf, "Origin": "https://evil.example"}, 403),
                                    ({"X-CSRF": csrf, "Origin": "https://testserver"}, 200)):
                response = await client.post(path, json={}, headers=_cookie(token) | extra)
                assert response.status_code == expected, (path, extra, response.text)
            assert (await client.post(path, json={}, headers=as_bot)).status_code == 403, path
            client.cookies.clear()  # вход оставил cookie сессии в клиенте: без очистки запрос не анонимный
            assert (await client.post(path, json={})).status_code == 401, path
        assert (await client.get("/api/activity", headers=_cookie(token))).status_code == 200  # чтение без CSRF
        assert (await client.get("/api/activity", headers=as_bot)).status_code == 403
        client.cookies.clear()
        assert (await client.get("/api/activity")).status_code == 401


async def test_last_active_admin_cannot_be_disabled_or_demoted():
    async for client, app in _client():
        owner = await _setup(client)
        async with app.state.pool.acquire() as con:
            member = await con.fetchval(
                "insert into bothub.users(email,password_hash) values('b@example.com',$1) returning id",
                hash_password(PASSWORD),
            )
        assert (await client.patch(f"/api/users/{owner}", json={"disabled": True}, headers=OWNER)).status_code == 409
        assert (await client.patch(f"/api/users/{owner}", json={"role": "member"}, headers=OWNER)).status_code == 409
        assert (await client.patch(f"/api/users/{member}", json={"role": "admin"}, headers=OWNER)).status_code == 200
        assert (await client.patch(f"/api/users/{owner}", json={"disabled": True}, headers=OWNER)).status_code == 200


async def test_login_rate_limit_by_ip_and_login():
    async for client, app in _client():
        await _setup(client)
        codes = []
        for _ in range(9):
            response = await client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
            codes.append(response.status_code)
        assert codes[:8] == [401] * 8
        assert codes[8] == 429
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("198.51.100.2", 1234)),
            base_url="https://testserver",
        ) as other_ip:
            response = await other_ip.post(
                "/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"}
            )
            assert response.status_code == 401, response.text


async def test_login_unknown_email_has_same_response_as_wrong_password():
    async for client, _ in _client():
        await _setup(client)
        wrong = await client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"}
        )
        absent = await client.post(
            "/api/auth/login", json={"email": "absent@example.com", "password": "wrong-password"}
        )
        assert wrong.status_code == absent.status_code == 401
        assert wrong.json() == absent.json()


async def test_setup_requires_secret_even_when_users_empty():
    async for client, app in _client():
        body = {"email": "first@example.com", "password": PASSWORD}
        assert (await client.post("/api/setup", json=body)).status_code == 401
        response = await client.post("/api/setup", json=body, headers={"X-Setup-Code": app.state.setup_code})
        assert response.status_code == 201, response.text
        assert (await client.post("/api/setup", json=body, headers=OWNER)).status_code == 409


async def test_invite_rejects_invalid_types_without_server_error():
    async for client, _ in _client():
        await _setup(client)
        bad_days = await client.post("/api/invites", json={"days": 10**20}, headers=OWNER)
        assert bad_days.status_code == 400, bad_days.text
        bad_token = await client.post(
            "/api/invites/accept",
            json={"token": None, "email": "b@example.com", "password": PASSWORD},
        )
        assert bad_token.status_code == 410, bad_token.text


async def test_cross_owner_resource_matrix():
    async for client, app in _client():
        owner_a = await _setup(client)
        await _login(client, "a@example.com")
        async with app.state.pool.acquire() as con:
            owner_b = await con.fetchval(
                "insert into bothub.users(email,password_hash) values('b@example.com',$1) returning id",
                hash_password(PASSWORD),
            )
            await con.execute(
                "insert into bothub.bots(id,name,provider,model,owner_id) values('private-bot','Private','fake','fake',$1)",
                owner_a,
            )
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('private-bot',$1) returning id", owner_a)
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,status) values($1,'private','done') returning id", thread)
            approval = await con.fetchval(
                "insert into bothub.approvals(thread_id,turn_id,bot_id,risk,title,tool,args,args_hash,expires_at) "
                "values($1,$2,'private-bot','other','Private','Read','{}','private-hash',now()+interval '1 day') returning id",
                thread, turn,
            )
            memory = await con.fetchval("insert into bothub.memory(text,owner_id) values('private',$1) returning id", owner_a)
            schedule = await con.fetchval(
                "insert into bothub.schedules(bot_id,name,kind,prompt,owner_id) values('private-bot','Private','hook','private',$1) returning id",
                owner_a,
            )
            file_id = await con.fetchval(
                "insert into bothub.files(thread_id,name,origin,size,mime,storage_path,owner_id) "
                "values($1,'private','upload',0,'text/plain','private',$2) returning id",
                thread, owner_a,
            )
            session_a = await con.fetchval("select id_hash from bothub.sessions where user_id=$1 limit 1", owner_a)
            provider_a = await con.fetchval(
                "insert into bothub.providers(owner_id,kind,cli,name,status) "
                "values($1,'cli_subscription','claude','A CLI','ok') returning id", owner_a
            )
            model_a = await con.fetchval(
                "insert into bothub.models(provider_id,name) values($1,'claude-test') returning id", provider_a
            )
        token_b, csrf_b = await _login(client, "b@example.com")
        foreign_binding = await client.post(
            "/api/bots",
            json={"name": "Foreign binding", "provider": "claude", "model": "claude-test",
                  "provider_id": str(provider_a), "model_id": str(model_a)},
            headers=_cookie(token_b, csrf_b),
        )
        assert foreign_binding.status_code == 400, foreign_binding.text
        own_bot = await client.post(
            "/api/bots", json={"name": "Own", "provider": "fake", "model": "fake"},
            headers=_cookie(token_b, csrf_b),
        )
        assert own_bot.status_code == 200, own_bot.text
        foreign_patch = await client.patch(
            f"/api/bots/{own_bot.json()['id']}",
            json={"provider_id": str(provider_a), "model_id": str(model_a)},
            headers=_cookie(token_b, csrf_b),
        )
        assert foreign_patch.status_code == 400, foreign_patch.text
        removed = await client.delete(f"/api/bots/{own_bot.json()['id']}", headers=_cookie(token_b, csrf_b))
        assert removed.status_code == 200, removed.text
        mac_response = await client.post("/api/mac/token", headers=_cookie(token_b, csrf_b))
        assert mac_response.status_code == 201, mac_response.text
        mac_b = mac_response.json()["token"]
        reads = [
            ("/api/bots", []), ("/api/threads", []), ("/api/memory", []),
            ("/api/schedules", []), ("/api/approvals", []),
        ]
        for path, expected in reads:
            response = await client.get(path, headers=_cookie(token_b))
            assert response.status_code == 200 and response.json() == expected, (path, response.text)
        for path in (
            f"/api/threads/{thread}", f"/api/threads/{thread}/events", f"/api/files/{file_id}",
            "/api/bots/private-bot/browser",
        ):
            response = await client.get(path, headers=_cookie(token_b))
            assert response.status_code == 404, (path, response.text)
        mutations = [
            ("PATCH", "/api/bots/private-bot", {"name": "Taken"}),
            ("POST", "/api/threads", {"bot_id": "private-bot"}),
            ("PATCH", f"/api/threads/{thread}", {"title": "Taken"}),
            ("POST", f"/api/threads/{thread}/turns", {"prompt": "Taken"}),
            ("POST", f"/api/threads/{thread}/compact", {}),
            ("POST", f"/api/turns/{turn}/stop", {}),
            ("POST", f"/api/approvals/{approval}/decide", {"decision": "reject"}),
            ("PATCH", f"/api/memory/{memory}", {"text": "Taken"}),
            ("PATCH", f"/api/schedules/{schedule}", {"prompt": "Taken"}),
            ("POST", f"/api/schedules/{schedule}/run", {}),
            ("DELETE", f"/api/sessions/{session_a}", {}),
            ("POST", "/api/bots/private-bot/browser/takeover", {}),
            ("POST", "/api/bots/private-bot/browser/return", {}),
            ("POST", "/api/bots/private-bot/browser/secret-input", {"value": "secret"}),
        ]
        for method, path, body in mutations:
            response = await client.request(method, path, json=body, headers=_cookie(token_b, csrf_b))
            assert response.status_code == 404, (method, path, response.text)
        mac_upload = await client.post(
            "/api/files",
            data={"thread_id": str(thread)},
            files={"file": ("foreign.txt", b"secret")},
            headers={"Authorization": f"Bearer {mac_b}"},
        )
        assert mac_upload.status_code == 404, mac_upload.text
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select name from bothub.bots where id='private-bot'") == "Private"
            assert await con.fetchval("select count(*) from bothub.users where id=$1", owner_b) == 1


async def test_cross_owner_sessions_are_hidden():
    async for client, app in _client():
        await _setup(client)
        token_a, _ = await _login(client, "a@example.com")
        async with app.state.pool.acquire() as con:
            await con.execute(
                "insert into bothub.users(email,password_hash) values('b@example.com',$1)", hash_password(PASSWORD)
            )
        token_b, _ = await _login(client, "b@example.com")
        own = hashlib.sha256(token_b.encode()).hexdigest()
        foreign = hashlib.sha256(token_a.encode()).hexdigest()
        response = await client.get("/api/sessions", headers=_cookie(token_b))
        assert response.status_code == 200, response.text
        assert [session["id_hash"] for session in response.json()] == [own]
        assert foreign not in response.text


async def test_setup_invite_users_and_sessions_response_fields():
    async for client, app in _client():
        assert (await client.get('/api/setup/status')).json() == {'needs_setup': True}
        owner = await _setup(client)
        assert (await client.get('/api/setup/status')).json() == {'needs_setup': False}
        invite = (await client.post('/api/invites', json={'role':'member'}, headers=OWNER)).json()
        checked = (await client.get('/api/invites/check', params={'token':invite['token']})).json()
        assert checked['valid'] is True
        assert checked['role'] == 'member'
        assert checked['expires_at'] == invite['expires_at']
        assert checked['invited_by'] == 'a@example.com'
        bot = await client.post('/api/bots', json={'id':'owner-bot','name':'Owner','provider':'fake','model':'fake'}, headers=OWNER)
        assert bot.status_code == 200, bot.text
        users = (await client.get('/api/users', headers=OWNER)).json()
        assert users[0]['id'] == str(owner) and users[0]['bots_count'] == 1
        assert users[0]['last_seen_at'] is None
        token, _ = await _login(client, 'a@example.com')
        sessions = (await client.get('/api/sessions', headers=_cookie(token))).json()
        assert len(sessions) == 1 and sessions[0]['current'] is True
        users = (await client.get('/api/users', headers=OWNER)).json()
        assert users[0]['last_seen_at'] is not None
