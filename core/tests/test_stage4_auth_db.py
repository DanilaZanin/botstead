"""Stage 4 database contract. The orchestrator runs this against Postgres."""
import hashlib
import asyncio
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bothub.main import create_app
from bothub import auth


pytestmark = pytest.mark.empty_users
OWNER = {"Authorization": "Bearer test-owner"}


@pytest.fixture(autouse=True)
def local_cookie_path(monkeypatch):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")


async def _client():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            yield client, app


async def test_setup_adopts_orphans_and_is_one_time():
    async for client, app in _client():
        async with app.state.pool.acquire() as con:
            tables=('bots','threads','memory','schedules','files','push_subscriptions','outbox','mac_status')
            await con.execute("insert into bothub.bots(id,name,provider,model) values('old','Old','fake','fake')")
            thread=await con.fetchval("insert into bothub.threads(bot_id) values('old') returning id")
            await con.execute("insert into bothub.memory(text) values('old')")
            await con.execute("insert into bothub.schedules(bot_id,name,kind,prompt) values('old','old','hook','hello')")
            await con.execute("insert into bothub.files(thread_id,name,origin,size,mime,storage_path) values($1,'old','upload',0,'text/plain','old')",thread)
            await con.execute("insert into bothub.push_subscriptions(endpoint,keys) values('https://push.example','{}')")
            await con.execute("insert into bothub.outbox(kind,payload) values('push','{}')")
            await con.execute("insert into bothub.mac_status(id) values(1)")
        response = await client.post("/api/setup", json={"email": "FIRST@EXAMPLE.COM", "password": "long-password"}, headers=OWNER)
        assert response.status_code == 201, response.text
        async with app.state.pool.acquire() as con:
            for table in tables:
                assert await con.fetchval(f'select count(*) from bothub.{table} where owner_id=$1', uuid.UUID(response.json()['id'])) == 1,table
        assert (await client.post("/api/setup", json={"email": "x@example.com", "password": "long-password"}, headers=OWNER)).status_code == 409


async def test_login_logout_and_password_change_revoke_session():
    async for client, _ in _client():
        await client.post("/api/setup", json={"email": "a@example.com", "password": "long-password"}, headers=OWNER)
        login = await client.post("/api/auth/login", json={"email": "a@example.com", "password": "long-password"})
        assert login.status_code == 200, login.text
        assert "bothub_session" in login.cookies
        assert "HttpOnly" in login.headers["set-cookie"] and "Secure" in login.headers["set-cookie"]
        me = await client.get("/api/auth/me")
        csrf = me.json()["csrf_token"]
        assert me.json()["email"] == "a@example.com"
        headers = {"X-CSRF": csrf, "Origin": "https://testserver"}
        changed = await client.post("/api/auth/password", json={"old_password": "long-password", "new_password": "new-long-password"}, headers=headers)
        assert changed.status_code == 200, changed.text
        assert (await client.get("/api/auth/me")).status_code == 401
        assert (await client.post("/api/auth/login", json={"email": "a@example.com", "password": "new-long-password"})).status_code == 200
        assert (await client.post("/api/auth/logout", headers={"X-CSRF": (await client.get('/api/auth/me')).json()["csrf_token"], "Origin": "https://testserver"})).status_code == 200
        assert (await client.get("/api/auth/me")).status_code == 401


async def test_invite_single_use_and_cross_user_bot_isolation():
    async for client, _ in _client():
        await client.post("/api/setup", json={"email": "a@example.com", "password": "long-password"}, headers=OWNER)
        invite = await client.post("/api/invites", json={"role": "member"}, headers=OWNER)
        assert invite.status_code == 201, invite.text
        token = invite.json()["token"]
        assert (await client.get("/api/invites/check", params={"token": token})).json()["valid"]
        accepted = await client.post("/api/invites/accept", json={"token": token, "email": "b@example.com", "password": "long-password"})
        assert accepted.status_code == 201, accepted.text
        assert (await client.post("/api/invites/accept", json={"token": token, "email": "c@example.com", "password": "long-password"})).status_code == 410
        bot = await client.post("/api/bots", json={"name": "Scout", "provider": "fake", "model": "fake"}, headers=OWNER)
        assert bot.status_code == 200, bot.text
        login = await client.post("/api/auth/login", json={"email": "b@example.com", "password": "long-password"})
        assert login.status_code == 200
        assert (await client.get("/api/bots")).json() == []
        assert (await client.patch(f"/api/bots/{bot.json()['id']}", json={"name": "Taken"}, headers={"X-CSRF": (await client.get('/api/auth/me')).json()["csrf_token"], "Origin": "https://testserver"})).status_code == 404


async def test_invite_accept_rejects_long_email_without_echo():
    async for client, _ in _client():
        email = 'a' * 1_000_000 + '@example.com'
        response = await client.post('/api/invites/accept', json={
            'token': 'unused', 'email': email, 'password': 'long-password'})
        assert response.status_code == 400
        assert response.json() == {'error': 'invalid', 'detail': 'email'}
        assert email not in response.text


async def test_session_hash_only_and_bearer_has_no_csrf():
    async for client, app in _client():
        await client.post("/api/setup", json={"email": "a@example.com", "password": "long-password"}, headers=OWNER)
        login = await client.post("/api/auth/login", json={"email": "a@example.com", "password": "long-password"})
        token = login.cookies["bothub_session"]
        async with app.state.pool.acquire() as con:
            stored = await con.fetchval("select id_hash from bothub.sessions")
        assert stored == hashlib.sha256(token.encode()).hexdigest()
        assert token not in stored
        assert (await client.post("/api/invites", json={}, headers=OWNER)).status_code == 201
        assert (await client.post("/api/invites", json={})).status_code == 403


async def test_cookie_path_defaults_to_bots(monkeypatch):
    monkeypatch.delenv('BOTHUB_BASE_PATH')
    async for client, _ in _client():
        await client.post('/api/setup', json={'email':'first@example.com','password':'long-password'}, headers=OWNER)
        login = await client.post('/api/auth/login', json={'email':'first@example.com','password':'long-password'})
        assert 'Path=/bots/' in login.headers['set-cookie']


async def test_session_cookie_renews_with_database_expiry():
    async for client, app in _client():
        await client.post('/api/setup', json={'email':'first@example.com','password':'long-password'}, headers=OWNER)
        login = await client.post('/api/auth/login', json={'email':'first@example.com','password':'long-password'})
        token = login.cookies['bothub_session']
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.sessions set last_extended_at=now()-interval '2 days' where id_hash=$1", hashlib.sha256(token.encode()).hexdigest())
        me = await client.get('/api/auth/me', headers={'Cookie':f'bothub_session={token}'})
        assert me.status_code == 200, me.text
        assert 'Max-Age=2592000' in me.headers['set-cookie']
        again = await client.get('/api/auth/me', headers={'Cookie':f'bothub_session={token}'})
        assert 'set-cookie' not in again.headers


async def test_revoke_own_session_preserves_other_session():
    async for client, _ in _client():
        await client.post('/api/setup', json={'email':'a@example.com','password':'long-password'}, headers=OWNER)
        first = await client.post('/api/auth/login', json={'email':'a@example.com','password':'long-password'})
        second = await client.post('/api/auth/login', json={'email':'a@example.com','password':'long-password'})
        first_token = first.cookies['bothub_session']
        second_token = second.cookies['bothub_session']
        first_id = hashlib.sha256(first_token.encode()).hexdigest()
        headers = {'Cookie':f'bothub_session={first_token}', 'X-CSRF':first.json()['csrf_token'], 'Origin':'https://testserver'}
        sessions = await client.get('/api/sessions', headers={'Cookie':f'bothub_session={first_token}'})
        assert first_id in {row['id_hash'] for row in sessions.json()}
        assert (await client.delete('/api/sessions/'+first_id, headers=headers)).status_code == 200
        assert (await client.get('/api/auth/me', headers={'Cookie':f'bothub_session={first_token}'})).status_code == 401
        assert (await client.get('/api/auth/me', headers={'Cookie':f'bothub_session={second_token}'})).status_code == 200


async def test_invite_expiry_revoke_and_concurrent_accept():
    async for client, app in _client():
        await client.post('/api/setup', json={'email':'a@example.com','password':'long-password'}, headers=OWNER)
        expired=(await client.post('/api/invites',json={},headers=OWNER)).json()
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.invites set expires_at=now()-interval '1 second' where token_hash=$1",expired['token_hash'])
        assert (await client.post('/api/invites/accept',json={'token':expired['token'],'email':'x@example.com','password':'long-password'})).status_code==410
        revoked=(await client.post('/api/invites',json={},headers=OWNER)).json()
        assert (await client.delete('/api/invites/'+revoked['token_hash'],headers=OWNER)).status_code==200
        assert (await client.get('/api/invites/check',params={'token':revoked['token']})).json()=={'valid':False}
        active=(await client.post('/api/invites',json={},headers=OWNER)).json()
        async def accept(email):
            return await client.post('/api/invites/accept',json={'token':active['token'],'email':email,'password':'long-password'})
        results=await asyncio.gather(accept('b@example.com'),accept('c@example.com'))
        assert sorted(r.status_code for r in results)==[201,410]


async def test_csrf_origin_bearer_and_last_admin():
    async for client, _ in _client():
        setup=(await client.post('/api/setup',json={'email':'a@example.com','password':'long-password'},headers=OWNER)).json()
        login=await client.post('/api/auth/login',json={'email':'a@example.com','password':'long-password'})
        assert login.status_code==200
        assert (await client.post('/api/invites',json={})).status_code==403
        csrf=(await client.get('/api/auth/me')).json()['csrf_token']
        assert (await client.post('/api/invites',json={},headers={'X-CSRF':csrf,'Origin':'https://evil.example'})).status_code==403
        assert (await client.post('/api/invites',json={},headers={'X-CSRF':csrf,'Origin':'https://testserver'})).status_code==201
        assert (await client.post('/api/invites',json={},headers=OWNER)).status_code==201
        response=await client.patch('/api/users/'+setup['id'],json={'disabled':True},headers=OWNER)
        assert response.status_code==409 and response.json()['detail']=='last_admin'


async def test_login_rate_limit_and_owner_token_maps_first_admin():
    async for client, _ in _client():
        setup=(await client.post('/api/setup',json={'email':'a@example.com','password':'long-password'},headers=OWNER)).json()
        assert (await client.get('/api/auth/me',headers=OWNER)).json()['id']==setup['id']
        wrong=[]
        for _ in range(9):
            wrong.append((await client.post('/api/auth/login',json={'email':'missing@example.com','password':'long-password'})).status_code)
        assert wrong[:8]==[401]*8 and wrong[8]==429


async def test_missing_user_and_wrong_password_share_response_and_hash_work(monkeypatch):
    async for client, _ in _client():
        await client.post('/api/setup',json={'email':'a@example.com','password':'long-password'},headers=OWNER)
        called=[]
        original=auth.verify_password
        def spy(digest,password):
            called.append(digest)
            return original(digest,password)
        monkeypatch.setattr(auth,'verify_password',spy)
        missing=await client.post('/api/auth/login',json={'email':'missing@example.com','password':'wrong-password'})
        wrong=await client.post('/api/auth/login',json={'email':'a@example.com','password':'wrong-password'})
        assert (missing.status_code,missing.json())==(wrong.status_code,wrong.json())
        assert len(called)==2 and all(value.startswith('$argon2id$') for value in called)


async def test_cookie_identity_takes_priority_over_trusted_proxy(monkeypatch):
    monkeypatch.setenv('BOTHUB_PROXY_SECRET','proxy-secret')
    async for client, _ in _client():
        await client.post('/api/setup',json={'email':'a@example.com','password':'long-password'},headers=OWNER)
        invite=(await client.post('/api/invites',json={},headers=OWNER)).json()
        await client.post('/api/invites/accept',json={'token':invite['token'],'email':'b@example.com','password':'long-password'})
        await client.post('/api/bots',json={'name':'A','provider':'fake','model':'fake'},headers=OWNER)
        login=await client.post('/api/auth/login',json={'email':'b@example.com','password':'long-password'})
        assert login.status_code==200
        headers={'X-Bothub-Proxy':'proxy-secret','Remote-User':'owner'}
        assert (await client.get('/api/bots',headers=headers)).json()==[]


def test_websocket_origin_and_session_revoke_close_4401():
    app=create_app()
    with TestClient(app,base_url='https://testserver') as client:
        assert client.post('/api/setup',json={'email':'a@example.com','password':'long-password'},headers=OWNER).status_code==201
        bot=client.post('/api/bots',json={'name':'Scout','provider':'fake','model':'fake'},headers=OWNER).json()
        thread=client.post('/api/threads',json={'bot_id':bot['id']},headers=OWNER).json()
        login=client.post('/api/auth/login',json={'email':'a@example.com','password':'long-password'})
        token=login.cookies['bothub_session']
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect('/api/ws?thread_id='+thread['id'],headers={'Cookie':f'bothub_session={token}','Origin':'http://evil.example'}) as ws:
                ws.receive_json()
        assert rejected.value.code==4401
        # сервер закрывает сокет сам: исключение должно выйти из блока with, иначе TestClient падает на закрытии
        with pytest.raises(WebSocketDisconnect) as revoked:
            with client.websocket_connect('/api/ws?thread_id='+thread['id'],headers={'Cookie':f'bothub_session={token}','Origin':'https://testserver'}) as ws:
                csrf=client.get('/api/auth/me').json()['csrf_token']
                assert client.post('/api/auth/logout',headers={'X-CSRF':csrf,'Origin':'https://testserver'}).status_code==200
                ws.receive_json()
            assert revoked.value.code==4401


async def test_schedule_does_not_reuse_another_owners_thread():
    from bothub.auth import hash_password
    async for client, app in _client():
        await client.post('/api/setup', json={'email':'a@example.com','password':'long-password'}, headers=OWNER)
        async with app.state.pool.acquire() as con:
            owner_a = await con.fetchval("select id from bothub.users where email='a@example.com'")
            owner_b = await con.fetchval("insert into bothub.users(email,password_hash) values('b@example.com',$1) returning id", hash_password('long-password'))
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('bot-a','A','fake','fake',$1),('bot-b','B','fake','fake',$2)",owner_a,owner_b)
            thread_a = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('bot-a',$1) returning id",owner_a)
            turn_a = await con.fetchval("insert into bothub.turns(thread_id,prompt,status) values($1,'a','done') returning id",thread_a)
            schedule_b = await con.fetchval("insert into bothub.schedules(bot_id,name,kind,prompt,last_turn_id,owner_id) values('bot-b','B','hook','b',$1,$2) returning id",turn_a,owner_b)
        login = await client.post('/api/auth/login',json={'email':'b@example.com','password':'long-password'})
        token = login.cookies['bothub_session']
        run = await client.post(f'/api/schedules/{schedule_b}/run',headers={'Cookie':f'bothub_session={token}','X-CSRF':login.json()['csrf_token'],'Origin':'https://testserver'})
        assert run.status_code == 200, run.text
        async with app.state.pool.acquire() as con:
            thread_b = await con.fetchval('select thread_id from bothub.turns where id=$1',run.json()['id'])
            assert thread_b != thread_a
            assert await con.fetchval('select owner_id from bothub.threads where id=$1',thread_b) == owner_b


async def _network_for(launcher, owner_id):
    for _ in range(100):
        if str(owner_id) in launcher.networks:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_setup_and_invite_prepare_the_user_network_in_background(monkeypatch):
    from bothub.launcher_client import FakeLauncherClient, LauncherUnavailable
    monkeypatch.setenv('BOTHUB_RUNNER_EXEC', 'docker')
    monkeypatch.setenv('BOTHUB_GATEWAY_TOKEN_SECRET', 'test-gateway-secret')
    launcher = FakeLauncherClient()
    app = create_app(launcher=launcher)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            admin = await client.post("/api/setup", json={"email": "a@example.com", "password": "long-password"}, headers=OWNER)
            assert admin.status_code == 201, admin.text
            assert await _network_for(launcher, admin.json()["id"])
            invite = (await client.post("/api/invites", json={"role": "member"}, headers=OWNER)).json()
            launcher.fail_next(LauncherUnavailable('launcher down'))  # отказ лаунчера не ломает приём приглашения
            accepted = await client.post("/api/invites/accept", json={"token": invite["token"], "email": "b@example.com", "password": "long-password"})
            assert accepted.status_code == 201, accepted.text
            await asyncio.sleep(0.1)
            assert accepted.json()["id"] not in launcher.networks
