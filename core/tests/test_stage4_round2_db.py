"""Regression tests for the second Stage 4 security pass."""

import asyncio
import logging
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bothub import auth
from bothub.main import create_app


pytestmark = pytest.mark.empty_users
OWNER = {"Authorization": "Bearer test-owner"}
PASSWORD = "long-password"


@pytest.fixture(autouse=True)
def test_environment(monkeypatch):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.delenv("BOTHUB_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("BOTHUB_PROXY_SECRET", raising=False)
    monkeypatch.delenv("BOTHUB_OWNER_USER", raising=False)
    monkeypatch.delenv("BOTHUB_LEGACY_AUTH", raising=False)


async def _client():
    app = create_app()
    async def idle():
        await asyncio.Event().wait()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
                yield client, app
        finally:
            # Setup starts tasks after lifespan enters; its local cleanup list is initially empty.
            for task in app.state.worker_tasks:
                task.cancel()
            await asyncio.gather(*app.state.worker_tasks, return_exceptions=True)


async def _setup(client, headers=OWNER, email="a@example.com"):
    response = await client.post("/api/setup", json={"email": email, "password": PASSWORD}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _invite(client, email, role="member"):
    invitation = await client.post("/api/invites", json={"role": role}, headers=OWNER)
    assert invitation.status_code == 201, invitation.text
    response = await client.post("/api/invites/accept", json={"token": invitation.json()["token"], "email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


async def _bot_thread(client, headers=OWNER, name="Scout"):
    bot = await client.post("/api/bots", json={"name": name, "provider": "fake", "model": "fake"}, headers=headers)
    assert bot.status_code == 200, bot.text
    thread = await client.post("/api/threads", json={"bot_id": bot.json()["id"]}, headers=headers)
    assert thread.status_code == 200, thread.text
    return bot.json(), thread.json()


def _session(login):
    return {"Cookie": f"bothub_session={login.cookies['bothub_session']}"}


def _write_session(login, origin="https://testserver"):
    return _session(login) | {"X-CSRF": login.json()["csrf_token"], "Origin": origin}


async def test_legacy_credentials_stay_bound_to_setup_user_after_demotion(monkeypatch):
    monkeypatch.setenv("BOTHUB_PROXY_SECRET", "proxy-secret")
    monkeypatch.setenv("BOTHUB_OWNER_USER", "configured-owner")
    async for client, app in _client():
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.mac_status(state,info) values('offline','{}'::jsonb)")
        first = await _setup(client)
        second = await _invite(client, "b@example.com", "admin")
        first_bot, first_thread = await _bot_thread(client)
        monkeypatch.setenv("MAC_AGENT_TOKEN", "legacy-env-only-token")
        client.cookies.clear()
        probe = await client.post("/api/files", data={"thread_id": str(uuid.uuid4())},
            files={"file": ("probe.txt", b"x")}, headers={"Authorization": "Bearer legacy-env-only-token"})
        assert probe.status_code == 404, probe.text
        login_b = await client.post("/api/auth/login", json={"email": "b@example.com", "password": PASSWORD})
        assert login_b.status_code == 200
        await _bot_thread(client, _write_session(login_b), "Second")
        demote = await client.patch(f"/api/users/{first['id']}", json={"role": "member"}, headers=_write_session(login_b))
        assert demote.status_code == 200, demote.text
        client.cookies.clear()  # иначе запрос пройдёт по cookie сессии B из банки клиента, а не по legacy-токену
        for headers in (OWNER, {"Authorization": "Bearer legacy-env-only-token"},
                        {"X-Bothub-Proxy": "proxy-secret", "Remote-User": "configured-owner"}):
            assert (await client.get("/api/bots", headers=headers)).status_code == 401
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select value #>> '{}' from bothub.settings where key='setup_user_id'") == first["id"]
        assert second["id"] != first["id"]
        assert first_bot["id"] and first_thread["id"]


async def test_legacy_credentials_have_no_owner_when_setup_user_id_is_unset(monkeypatch):
    monkeypatch.setenv("BOTHUB_PROXY_SECRET", "proxy-secret")
    monkeypatch.setenv("BOTHUB_OWNER_USER", "configured-owner")
    async for client, app in _client():
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.users(email,password_hash,role) values('unconfigured@example.com',$1,'admin')", auth.hash_password(PASSWORD))
        for token in ("test-owner", "test-mac"):
            response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401, response.text
        proxy = await client.get("/api/auth/me", headers={"X-Bothub-Proxy": "proxy-secret", "Remote-User": "configured-owner"})
        assert proxy.status_code == 401, proxy.text


async def test_disabled_setup_admin_does_not_fall_through_to_another_admin():
    async for client, app in _client():
        first = await _setup(client)
        await _invite(client, "b@example.com", "admin")
        assert (await client.patch(f"/api/users/{first['id']}", json={"disabled": True}, headers=OWNER)).status_code == 200
        assert (await client.get("/api/bots", headers=OWNER)).status_code == 401


async def test_malformed_setup_user_id_denies_legacy_access(monkeypatch):
    async for client, app in _client():
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.mac_status(state,info) values('offline','{}'::jsonb)")
        await _setup(client)
        monkeypatch.setenv("MAC_AGENT_TOKEN", "legacy-env-only-token")
        probe = await client.post("/api/files", data={"thread_id": str(uuid.uuid4())},
            files={"file": ("probe.txt", b"x")}, headers={"Authorization": "Bearer legacy-env-only-token"})
        assert probe.status_code == 404, probe.text
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.settings set value='\"not-a-uuid\"'::jsonb where key='setup_user_id'")
        for token in ("test-owner", "legacy-env-only-token"):
            response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401, response.text


def test_legacy_websockets_reject_demoted_setup_user(monkeypatch):
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        async def seed_old_status():
            async with app.state.pool.acquire() as con:
                await con.execute("insert into bothub.mac_status(state,info) values('offline','{}'::jsonb)")
        client.portal.call(seed_old_status)
        first = client.post("/api/setup", json={"email": "a@example.com", "password": PASSWORD}, headers=OWNER).json()
        client.post("/api/mac/token", headers=OWNER)
        monkeypatch.setenv("MAC_AGENT_TOKEN", "legacy-env-only-token")
        with client.websocket_connect("/agent/mac", headers={"Authorization": "Bearer legacy-env-only-token"}) as ws:
            ws.send_json({"type": "hello"})
        bot = client.post("/api/bots", json={"name": "Scout", "provider": "fake", "model": "fake"}, headers=OWNER).json()
        thread = client.post("/api/threads", json={"bot_id": bot["id"]}, headers=OWNER).json()
        invite = client.post("/api/invites", json={"role": "admin"}, headers=OWNER).json()
        client.post("/api/invites/accept", json={"token": invite["token"], "email": "b@example.com", "password": PASSWORD})
        login = client.post("/api/auth/login", json={"email": "b@example.com", "password": PASSWORD})
        assert client.patch(f"/api/users/{first['id']}", json={"role": "member"}, headers=_write_session(login)).status_code == 200
        for path, headers in ((f"/api/ws?thread_id={thread['id']}&token=test-owner", {"Origin": "https://testserver"}),
                              ("/agent/mac", {"Origin": "https://testserver", "Authorization": "Bearer legacy-env-only-token"})):
            with pytest.raises(WebSocketDisconnect) as denied:
                with client.websocket_connect(path, headers=headers) as ws:
                    ws.receive_json()
            assert denied.value.code == 4401


async def test_disabled_owner_turn_scheduler_and_hook_are_inert():
    async for client, app in _client():
        await _setup(client)
        member = await _invite(client, "b@example.com")
        login = await client.post("/api/auth/login", json={"email": "b@example.com", "password": PASSWORD})
        member_headers = _write_session(login)
        bot, thread = await _bot_thread(client, member_headers, "Member")
        queued = await client.post(f"/api/threads/{thread['id']}/turns", json={"prompt": "queued"}, headers=member_headers)
        assert queued.status_code == 200, queued.text
        cron = await client.post("/api/schedules", json={"bot_id": bot["id"], "name": "cron", "kind": "cron", "cron": "* * * * *", "prompt": "tick"}, headers=member_headers)
        manually_paused = await client.post("/api/schedules", json={"bot_id": bot["id"], "name": "paused", "kind": "cron", "cron": "* * * * *", "prompt": "stay paused", "enabled": False}, headers=member_headers)
        hook = await client.post("/api/schedules", json={"bot_id": bot["id"], "name": "hook", "kind": "hook", "prompt": "event"}, headers=member_headers)
        assert cron.status_code == manually_paused.status_code == hook.status_code == 200
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.schedules set next_run_at=now()-interval '1 minute' where id=$1", uuid.UUID(cron.json()["id"]))
            await con.execute("update bothub.schedules set last_turn_id=$2 where id=$1", uuid.UUID(cron.json()["id"]), uuid.UUID(queued.json()["id"]))
        disabled = await client.patch(f"/api/users/{member['id']}", json={"disabled": True}, headers=OWNER)
        assert disabled.status_code == 200, disabled.text
        assert await app.state.claim_turn() is None
        await app.state.run_due_schedules()
        assert (await client.post(f"/hooks/{hook.json()['id']}", params={"token": hook.json()["hook_token"]}, json={})).status_code == 404
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns t join bothub.threads th on th.id=t.thread_id where th.owner_id=$1", uuid.UUID(member["id"])) == 1
            assert await con.fetchval("select enabled from bothub.schedules where id=$1", uuid.UUID(cron.json()["id"])) is False
            assert await con.fetchval("select enabled from bothub.schedules where id=$1", uuid.UUID(hook.json()["id"])) is False
            assert await con.fetchval("select enabled from bothub.schedules where id=$1", uuid.UUID(manually_paused.json()["id"])) is False
            events = await con.fetch("select payload from bothub.events where thread_id=$1", uuid.UUID(thread["id"]))
            assert any("отключ" in str(row["payload"]).lower() or "disabled" in str(row["payload"]).lower() for row in events)
        enabled = await client.patch(f"/api/users/{member['id']}", json={"disabled": False}, headers=OWNER)
        assert enabled.status_code == 200, enabled.text
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select enabled from bothub.schedules where id=$1", uuid.UUID(cron.json()["id"])) is True
            assert await con.fetchval("select enabled from bothub.schedules where id=$1", uuid.UUID(hook.json()["id"])) is True
            assert await con.fetchval("select enabled from bothub.schedules where id=$1", uuid.UUID(manually_paused.json()["id"])) is False


async def test_disable_stops_running_turn_with_status_event():
    async for client, app in _client():
        await _setup(client)
        member = await _invite(client, "b@example.com")
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('member-bot','B','fake','fake',$1)", uuid.UUID(member["id"]))
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('member-bot',$1) returning id", uuid.UUID(member["id"]))
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) values($1,'x','running',now()+interval '1 minute') returning id", thread)
        assert (await client.patch(f"/api/users/{member['id']}", json={"disabled": True}, headers=OWNER)).status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select status from bothub.turns where id=$1", turn) not in ("running", "waiting_approval", "waiting_mac")
            assert await con.fetchval("select 1 from bothub.events where turn_id=$1 and kind='status'", turn)


async def test_setup_recovers_stale_running_turn_before_workers_start():
    async for client, app in _client():
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.bots(id,name,provider,model) values('old','Old','fake','fake')")
            thread = await con.fetchval("insert into bothub.threads(bot_id) values('old') returning id")
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) values($1,'old','running',now()-interval '1 minute') returning id", thread)
        await _setup(client)
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow("select status,error from bothub.turns where id=$1", turn)
        assert row["status"] == "error"
        assert "прервано рестартом" in row["error"]


async def test_proxy_secret_without_owner_user_never_grants_legacy_access(monkeypatch):
    monkeypatch.setenv("BOTHUB_PROXY_SECRET", "proxy-secret")
    async for client, _ in _client():
        await _setup(client)
        response = await client.get("/api/bots", headers={"X-Bothub-Proxy": "proxy-secret", "Remote-User": "anyone"})
        assert response.status_code == 401, response.text


@pytest.mark.parametrize("setup_with_owner,legacy_enabled", [(False, False), (True, True)])
async def test_setup_mode_persists_legacy_auth_and_reports_it(setup_with_owner, legacy_enabled):
    async for client, app in _client():
        headers = OWNER if setup_with_owner else {"X-Setup-Code": app.state.setup_code}
        setup = await _setup(client, headers)
        login = await client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})
        me = await client.get("/api/auth/me", headers=_session(login))
        assert me.status_code == 200
        assert me.json()["id"] == setup["id"]
        assert me.json()["legacy_auth"] is legacy_enabled
        owner_me = await client.get("/api/auth/me", headers=OWNER)
        assert owner_me.status_code == (200 if legacy_enabled else 401)


async def test_password_change_revokes_mac_token():
    async for client, app in _client():
        setup = await _setup(client)
        login = await client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})
        mac = await client.post("/api/mac/token", headers=_write_session(login))
        assert mac.status_code == 201, mac.text
        changed = await client.post("/api/auth/password", json={"old_password": PASSWORD, "new_password": "new-long-password"}, headers=_write_session(login))
        assert changed.status_code == 200, changed.text
        async with app.state.pool.acquire() as con:
            stored_hash = await con.fetchval("select token_hash from bothub.macs where owner_id=$1", uuid.UUID(setup["id"]))
            assert stored_hash != auth.token_hash(mac.json()["token"])
        assert (await client.get("/api/mac/status", headers={"Authorization": "Bearer " + mac.json()["token"]})).status_code == 401


def test_password_change_closes_connected_mac_socket():
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/setup", json={"email": "a@example.com", "password": PASSWORD}, headers=OWNER)
        login = client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})
        token = client.post("/api/mac/token", headers=_write_session(login)).json()["token"]
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/agent/mac", headers={"Authorization": "Bearer " + token}) as ws:
                changed = client.post("/api/auth/password", json={"old_password": PASSWORD, "new_password": "new-long-password"}, headers=_write_session(login))
                assert changed.status_code == 200, changed.text
                ws.receive_json()
        assert closed.value.code == 4401


def test_websocket_header_and_subprotocol_tokens_and_query_warning(caplog):
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/setup", json={"email": "a@example.com", "password": PASSWORD}, headers=OWNER)
        bot = client.post("/api/bots", json={"name": "Scout", "provider": "fake", "model": "fake"}, headers=OWNER).json()
        thread = client.post("/api/threads", json={"bot_id": bot["id"]}, headers=OWNER).json()
        for headers, subprotocols in ((OWNER | {"Origin": "https://testserver"}, None), ({"Origin": "https://testserver"}, ["bearer", "test-owner"])):
            with client.websocket_connect(f"/api/ws?thread_id={thread['id']}", headers=headers, subprotocols=subprotocols) as ws:
                assert ws is not None
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="bothub"):
            with client.websocket_connect(f"/api/ws?thread_id={thread['id']}&token=wrong", headers=OWNER | {"Origin": "https://testserver"}):
                pass
        assert not any("deprecated" in record.getMessage().lower() for record in caplog.records)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="bothub"):
            with client.websocket_connect(f"/api/ws?thread_id={thread['id']}&token=test-owner", headers={"Origin": "https://testserver"}):
                pass
        assert any("deprecated" in record.getMessage().lower() for record in caplog.records)


def test_mac_websocket_header_token_and_query_rejection():
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/setup", json={"email": "a@example.com", "password": PASSWORD}, headers=OWNER)
        login = client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})
        token = client.post("/api/mac/token", headers=_write_session(login)).json()["token"]
        with client.websocket_connect("/agent/mac", headers={"Authorization": "Bearer " + token}) as ws:
            ws.send_json({"type": "hello"})
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/agent/mac?token=wrong", headers={"Authorization": "Bearer " + token}) as ws:
                ws.send_json({"type": "hello"})
        assert denied.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/agent/mac?token=" + token) as ws:
                ws.send_json({"type": "hello"})
        assert denied.value.code == 4401


async def test_hook_accepts_header_token_and_warns_on_query(caplog):
    async for client, _ in _client():
        await _setup(client)
        bot, _ = await _bot_thread(client)
        schedule = await client.post("/api/schedules", json={"bot_id": bot["id"], "name": "hook", "kind": "hook", "prompt": "x"}, headers=OWNER)
        assert schedule.status_code == 200
        route = f"/hooks/{schedule.json()['id']}"
        token = schedule.json()["hook_token"]
        assert (await client.post(route, headers={"X-Hook-Token": token}, json={})).status_code == 202
        with caplog.at_level(logging.WARNING, logger="bothub"):
            assert (await client.post(route, params={"token": token}, json={})).status_code == 202
        assert any("deprecated" in record.getMessage().lower() for record in caplog.records)


async def test_unicode_bearer_is_unauthorized_instead_of_server_error():
    async for client, _ in _client():
        await _setup(client)
        response = await client.get("/api/auth/me", headers={b"Authorization": "Bearer 中文".encode()})
        assert response.status_code == 401, response.text


async def test_login_rate_limit_is_scoped_to_email_and_ip():
    async for client, app in _client():
        await _setup(client)
        for _ in range(8):
            assert (await client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})).status_code == 401
        assert (await client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})).status_code == 429
        transport = httpx.ASGITransport(app=app, client=("198.51.100.2", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as other:
            assert (await other.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})).status_code == 200


async def test_many_ips_on_one_email_slow_down_without_lockout(monkeypatch):
    async for client, app in _client():
        await _setup(client)
        delays = []
        real_sleep = asyncio.sleep

        async def observed_sleep(seconds):
            delays.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr("bothub.main.asyncio.sleep", observed_sleep)
        for index in range(8):
            transport = httpx.ASGITransport(app=app, client=(f"198.51.100.{index + 10}", 1234))
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as remote:
                response = await remote.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
                assert response.status_code == 401, response.text
        assert delays and 0 < max(delays) <= 2


async def test_public_origin_controls_cookie_csrf_with_different_host(monkeypatch):
    monkeypatch.setenv("BOTHUB_PUBLIC_ORIGIN", "https://example.com")
    async for client, _ in _client():
        await _setup(client)
        login = await client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})
        accepted = await client.post("/api/invites", json={}, headers=_write_session(login, "https://example.com"))
        assert accepted.status_code == 201, accepted.text
        denied = await client.post("/api/invites", json={}, headers=_write_session(login, "https://testserver"))
        assert denied.status_code == 403


def test_public_origin_controls_websocket_with_different_host(monkeypatch):
    monkeypatch.setenv("BOTHUB_PUBLIC_ORIGIN", "https://example.com")
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/setup", json={"email": "a@example.com", "password": PASSWORD}, headers=OWNER)
        bot = client.post("/api/bots", json={"name": "Scout", "provider": "fake", "model": "fake"}, headers=OWNER).json()
        thread = client.post("/api/threads", json={"bot_id": bot["id"]}, headers=OWNER).json()
        with client.websocket_connect(f"/api/ws?thread_id={thread['id']}&token=test-owner", headers={"Origin": "https://example.com"}) as ws:
            assert ws is not None
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(f"/api/ws?thread_id={thread['id']}&token=test-owner", headers={"Origin": "https://testserver"}) as ws:
                ws.receive_json()
        assert denied.value.code == 4401
