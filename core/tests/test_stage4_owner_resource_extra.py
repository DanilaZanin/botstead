"""Cross-owner cases that need bot credentials or a multipart body."""
import hashlib
import hmac
import asyncio

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from bothub.auth import hash_password
from bothub.main import create_app


@pytest.mark.asyncio
async def test_bot_and_upload_routes_reject_foreign_resources(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    app = create_app()
    async with app.router.lifespan_context(app):
        async with app.state.pool.acquire() as con:
            owner_a = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            await con.execute("insert into bothub.users(email,password_hash) values('b@example.com',$1)", hash_password('long-password'))
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('a-bot','A','fake','fake',$1)", owner_a)
            owner_b = await con.fetchval("select id from bothub.users where email='b@example.com'")
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('b-bot','B','fake','fake',$1)", owner_b)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('a-bot',$1) returning id", owner_a)
            own_thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('b-bot',$1) returning id", owner_b)
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,status) values($1,'A','done') returning id", thread)
            approval = await con.fetchval("insert into bothub.approvals(thread_id,bot_id,risk,title,tool,args,args_hash,expires_at) values($1,'a-bot','other','A','x','{}','x',now()+interval '1 hour') returning id", thread)
            mismatched_approval = await con.fetchval("insert into bothub.approvals(thread_id,bot_id,risk,title,tool,args,args_hash,expires_at) values($1,'b-bot','other','A','x','{}','x',now()+interval '1 hour') returning id", thread)
            file_id = await con.fetchval("insert into bothub.files(thread_id,name,origin,size,mime,storage_path,owner_id) values($1,'a.txt','upload',0,'text/plain','/tmp/a.txt',$2) returning id", thread, owner_a)
            await con.execute("insert into bothub.push_subscriptions(endpoint,keys,owner_id) values('https://push.example/a','{}',$1)", owner_a)
        digest = hmac.new(b'test-secret', b'b-bot', hashlib.sha256).hexdigest()
        bot = {'Authorization': f'Bearer bot:b-bot:{digest}'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            login = await client.post('/api/auth/login', json={'email':'b@example.com','password':'long-password'})
            user = {'Cookie':f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF':login.json()['csrf_token'], 'Origin':'https://testserver'}
            cases = [
                ('POST', '/api/approvals', {'thread_id':str(thread),'risk':'other','title':'x','tool':'x','args':{}}, bot),
                ('GET', f'/api/approvals/{approval}/wait?timeout=0', None, bot),
                ('GET', f'/api/approvals/{mismatched_approval}/wait?timeout=0', None, bot),
                ('POST', f'/api/approvals/{approval}/decide', {'decision':'reject'}, user),
                ('POST', '/api/memory', {'text':'x','bot_id':'a-bot'}, user),
                ('POST', '/api/memory', {'text':'x','bot_id':'a-bot'}, bot),
                ('POST', '/api/usage', {'thread_id':str(thread),'turn_id':str(turn),'provider':'fake','model':'fake'}, bot),
                ('POST', '/api/schedules', {'bot_id':'a-bot','name':'x','kind':'hook','prompt':'x'}, user),
                ('POST', '/api/mac/call', {'thread_id':str(thread),'turn_id':str(turn),'tool':'x','args':{}}, bot),
                ('POST', '/api/mac/call', {'thread_id':str(own_thread),'turn_id':str(turn),'tool':'x','args':{}}, bot),
                ('POST', '/api/push/subscribe', {'endpoint':'https://push.example/a','keys':{}}, user),
                ('GET', f'/api/files/{file_id}', None, user),
            ]
            for method, path, body, headers in cases:
                response = await client.request(method, path, json=body, headers=headers)
                assert response.status_code == 404, (method, path, response.text)
            upload = await client.post('/api/files', data={'thread_id':str(thread)}, files={'file':('x.txt',b'x','text/plain')}, headers=user)
            assert upload.status_code == 404, upload.text


def test_websocket_rejects_another_owners_thread(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    app = create_app()
    with TestClient(app) as client:
        bot = client.post('/api/bots', json={'name':'A','provider':'fake','model':'fake'}, headers={'Authorization':'Bearer test-owner'})
        assert bot.status_code == 200, bot.text
        thread = client.post('/api/threads', json={'bot_id':bot.json()['id']}, headers={'Authorization':'Bearer test-owner'})
        assert thread.status_code == 200, thread.text

        async def seed_b():
            con = await asyncpg.connect(__import__('os').environ['DATABASE_URL'])
            try:
                await con.execute("insert into bothub.users(email,password_hash) values('b@example.com',$1)", hash_password('long-password'))
            finally:
                await con.close()
        asyncio.run(seed_b())
        login = client.post('/api/auth/login', json={'email':'b@example.com','password':'long-password'})
        assert login.status_code == 200, login.text
        headers = {'Origin':'https://testserver', 'Cookie':f"bothub_session={login.cookies['bothub_session']}"}
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/api/ws?thread_id={thread.json()['id']}", headers=headers):
                pass
        assert exc.value.code == 4404


@pytest.mark.asyncio
async def test_delete_bot_preserves_creation_schedule_and_removes_empty_bot():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            scheduled = await client.post('/api/bots', json={
                'id':'scheduled', 'name':'Scheduled', 'provider':'fake', 'model':'fake',
                'schedule':{'name':'Daily', 'cron':'0 9 * * *', 'timezone':'Europe/Moscow', 'prompt':'Run daily'},
            }, headers={'Authorization':'Bearer test-owner'})
            assert scheduled.status_code == 200, scheduled.text
            empty = await client.post('/api/bots', json={
                'id':'empty', 'name':'Empty', 'provider':'fake', 'model':'fake',
            }, headers={'Authorization':'Bearer test-owner'})
            assert empty.status_code == 200, empty.text
            conflict = await client.delete('/api/bots/scheduled', headers={'Authorization':'Bearer test-owner'})
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()['error'] == 'conflict'
            deleted = await client.delete('/api/bots/empty', headers={'Authorization':'Bearer test-owner'})
            assert deleted.status_code == 200 and deleted.json() == {'ok':True}
            bots = (await client.get('/api/bots', headers={'Authorization':'Bearer test-owner'})).json()
            assert [bot['id'] for bot in bots] == ['scheduled']
            schedules = (await client.get('/api/schedules', headers={'Authorization':'Bearer test-owner'})).json()
            assert len(schedules) == 1 and schedules[0]['bot_id'] == 'scheduled'


@pytest.mark.pure
def test_non_http_app_routes_are_in_inventory():
    routes = create_app().routes
    assert {route.path for route in routes if isinstance(route, WebSocketRoute)} == {'/api/ws', '/agent/mac', '/api/providers/{id}/login', '/api/bots/{id}/screen'}
    assert {route.path for route in routes if isinstance(route, Mount)} == {'/'}
    assert {route.path for route in routes if type(route) is Route} == {'/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc'}
