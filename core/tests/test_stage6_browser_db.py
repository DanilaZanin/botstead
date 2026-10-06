"""Stage 6 browser state, ownership, and audit checks (requires Postgres)."""
import hashlib
import hmac
import base64
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bothub.launcher_client import FakeLauncherClient
from bothub.auth import hash_password
from bothub.main import create_app
from bothub.secrets import decrypt_secret


pytestmark = pytest.mark.empty_users
OWNER = {'Authorization': 'Bearer test-owner'}


async def _setup(client):
    response = await client.post('/api/setup', json={'email':'a@example.com','password':'long-password'},headers=OWNER)
    assert response.status_code == 201
    return uuid.UUID(response.json()['id'])


def _bot_headers(bot_id):
    signature = hmac.new(b'test-secret',bot_id.encode(),hashlib.sha256).hexdigest()
    return {'Authorization':f'Bearer bot:{bot_id}:{signature}'}


async def test_browser_takeover_return_snapshot_and_owner_matrix(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH','/')
    launcher = FakeLauncherClient()
    app = create_app(launcher=launcher)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://testserver') as client:
            owner = await _setup(client)
            response = await client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake'},headers=OWNER)
            assert response.status_code == 200, response.text
            await launcher.create_bot('alpha', str(owner))
            async with app.state.pool.acquire() as con:
                await con.execute("insert into bothub.users(email,password_hash) values('b@example.com',$1)",hash_password('long-password'))
                thread = await con.fetchval('insert into bothub.threads(bot_id,owner_id) values($1,$2) returning id','alpha',owner)
                turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) "
                                          "values($1,'browse','running',now()+interval '1 hour') returning id",thread)
            state = await client.get('/api/bots/alpha/browser',headers=OWNER)
            assert state.status_code == 200 and state.json()['state'] == 'bot'
            takeover = await client.post('/api/bots/alpha/browser/takeover',headers=OWNER)
            assert takeover.status_code == 200 and takeover.json()['state'] == 'human'
            async with app.state.pool.acquire() as con:
                assert await con.fetchval('select status from bothub.turns where id=$1',turn) == 'stopped'
                human_turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) "
                                                "values($1,'browse','running',now()+interval '1 hour') returning id",thread)
            payload = {'thread_id':str(thread),'turn_id':str(human_turn),'action':'snapshot'}
            blocked = await client.post('/api/browser/authorize',json=payload,headers=_bot_headers('alpha'))
            assert blocked.status_code == 409 and blocked.json()['error'] == 'human_in_control'
            returning = await client.post('/api/bots/alpha/browser/return',headers=OWNER)
            assert returning.status_code == 200 and returning.json()['state'] == 'returning'
            stale = await client.post('/api/browser/authorize',json=payload | {'action':'click'},headers=_bot_headers('alpha'))
            assert stale.status_code == 409 and stale.json()['error'] == 'browser_stale'
            missing = await client.post('/api/browser/step',json=payload,headers=_bot_headers('alpha'))
            assert missing.status_code == 409 and missing.json()['error'] == 'browser_authorization_required'
            authorized = await client.post('/api/browser/authorize',json=payload,headers=_bot_headers('alpha'))
            assert authorized.status_code == 200
            wrong = await client.post('/api/browser/step',json=payload | {
                'authorization_id':str(uuid.uuid4())},headers=_bot_headers('alpha'))
            assert wrong.status_code == 409
            failed = await client.post('/api/browser/step',json=payload | {
                'authorization_id': authorized.json()['authorization_id'],'result':'error'},headers=_bot_headers('alpha'))
            assert failed.status_code == 200
            replay = await client.post('/api/browser/step',json=payload | {
                'authorization_id': authorized.json()['authorization_id']},headers=_bot_headers('alpha'))
            assert replay.status_code == 409
            authorized = await client.post('/api/browser/authorize',json=payload,headers=_bot_headers('alpha'))
            snapshot = await client.post('/api/browser/step',json=payload | {
                'authorization_id': authorized.json()['authorization_id']},headers=_bot_headers('alpha'))
            assert snapshot.status_code == 200
            assert (await client.get('/api/bots/alpha/browser',headers=OWNER)).json()['state'] == 'bot'
            async with app.state.pool.acquire() as con:
                kinds = [row['kind'] for row in await con.fetch('select kind from bothub.events where thread_id=$1 order by seq',thread)]
            assert kinds.count('browser_control') == 3 and kinds.count('browser_step') == 2
            # A second owner cannot see or take over A's browser.
            login = await client.post('/api/auth/login',json={'email':'b@example.com','password':'long-password'})
            assert login.status_code == 200
            b_cookie = login.cookies['bothub_session']
            b_headers = {'Cookie':f'bothub_session={b_cookie}',
                         'Origin':'https://testserver','X-CSRF':login.json()['csrf_token']}
            assert (await client.get('/api/bots/alpha/browser',headers=b_headers)).status_code == 404
            assert (await client.post('/api/bots/alpha/browser/takeover',headers=b_headers)).status_code == 404


async def test_secret_input_never_enters_events_or_log_without_screen(caplog):
    launcher = FakeLauncherClient()
    app = create_app(launcher=launcher)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://testserver') as client:
            owner = await _setup(client)
            assert (await client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake'},headers=OWNER)).status_code == 200
            await launcher.create_bot('alpha', str(owner))
            assert (await client.post('/api/bots/alpha/browser/takeover',headers=OWNER)).status_code == 200
            password = 'UniquePrivatePassword123'
            response = await client.post('/api/bots/alpha/browser/secret-input',json={'value':password},headers=OWNER)
            assert response.status_code == 409 and response.json()['error'] == 'screen_required'
            async with app.state.pool.acquire() as con:
                events = await con.fetch('select payload from bothub.events')
            assert password not in str(events)
            assert password not in caplog.text


async def test_codex_runner_cannot_call_browser_tool():
    app=create_app(launcher=FakeLauncherClient())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://testserver') as client:
            owner=await _setup(client)
            async with app.state.pool.acquire() as con:
                await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) "
                                  "values('codex-bot','Codex','codex','test',$1)",owner)
                thread=await con.fetchval('insert into bothub.threads(bot_id,owner_id) values($1,$2) returning id','codex-bot',owner)
                turn=await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) "
                                        "values($1,'browse','running',now()+interval '1 hour') returning id",thread)
            response=await client.post('/api/browser/authorize',json={'thread_id':str(thread),'turn_id':str(turn),'action':'snapshot'},
                                       headers=_bot_headers('codex-bot'))
            assert response.status_code==409 and response.json()['error']=='browser_unavailable'
            async with app.state.pool.acquire() as con:
                await con.execute("update bothub.bots set provider='claude',executor='mac' where id='codex-bot'")
            response=await client.post('/api/browser/authorize',json={'thread_id':str(thread),'turn_id':str(turn),'action':'snapshot'},
                                       headers=_bot_headers('codex-bot'))
            assert response.status_code==409 and response.json()['error']=='browser_unavailable'


async def test_restart_unfreezes_fake_bot_and_expires_browser_approval():
    app=create_app(launcher=FakeLauncherClient())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://testserver') as client:
            owner=await _setup(client)
            assert (await client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake'},headers=OWNER)).status_code==200
            async with app.state.pool.acquire() as con:
                thread=await con.fetchval('insert into bothub.threads(bot_id,owner_id) values($1,$2) returning id','alpha',owner)
                approval=await con.fetchval("insert into bothub.approvals(thread_id,bot_id,risk,title,tool,args,args_hash,status,expires_at) "
                                            "values($1,'alpha','other','Browser','mcp__bothub__browser','{}','hash','approved',now()+interval '1 hour') returning id",thread)
    restarted_launcher=FakeLauncherClient()
    await restarted_launcher.create_bot('alpha',str(owner))
    restarted_launcher.frozen.add('alpha')
    restarted=create_app(launcher=restarted_launcher)
    async with restarted.router.lifespan_context(restarted):
        async with restarted.state.pool.acquire() as con:
            assert await con.fetchval("select browser_control from bothub.bots where id='alpha'")=='bot'
            assert await con.fetchval('select status from bothub.approvals where id=$1',approval)=='expired'
        # Бот не в human размораживается на старте независимо от провайдера: иначе каждый exec падает с frozen.
        assert 'alpha' not in restarted_launcher.frozen


class HeldScreenLauncher(FakeLauncherClient):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.block_input = False
        self.input_entered = threading.Event()
        self.input_release = threading.Event()

    async def screen_output(self, session_id):
        yield b'RFB 003.008\n'
        import asyncio
        await asyncio.to_thread(self.gate.wait)

    async def screen_input(self, session_id, data):
        if self.block_input:
            self.block_input = False
            self.input_entered.set()
            import asyncio
            await asyncio.to_thread(self.input_release.wait)
        await super().screen_input(session_id,data)


def test_secret_input_encrypted_and_stop_turn_releases_screen(monkeypatch,caplog):
    monkeypatch.setenv('BOTHUB_BASE_PATH','/')
    monkeypatch.setenv('BOTHUB_SECRET_KEYS','1:'+base64.b64encode(b'k'*32).decode())
    launcher=HeldScreenLauncher()
    app=create_app(launcher=launcher)
    with TestClient(app,base_url='https://testserver') as client:
        owner = uuid.UUID(client.post('/api/setup',json={'email':'a@example.com','password':'long-password'},headers=OWNER).json()['id'])
        assert client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake'},headers=OWNER).status_code==200
        # тесты идут в режиме BOTHUB_RUNNER_EXEC=local, где ядро не создаёт контейнер: заводим его в поддельном лаунчере явно
        if 'alpha' not in launcher.bots:
            client.portal.call(launcher.create_bot, 'alpha', str(owner))
        assert client.post('/api/bots/alpha/browser/takeover',headers=OWNER).status_code==200

        async def make_turn():
            async with app.state.pool.acquire() as con:
                thread=await con.fetchval('insert into bothub.threads(bot_id,owner_id) values($1,$2) returning id','alpha',owner)
                turn=await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) "
                                        "values($1,'browse','running',now()+interval '1 hour') returning id",thread)
                return turn

        turn=client.portal.call(make_turn)
        try:
            with client.websocket_connect('/api/bots/alpha/screen',headers={'Origin':'https://testserver',
                                                                            'Cookie':'bothub_session=unused'}) as ws:
                # This connection is deliberately unauthorized; owner cookie is obtained below.
                pytest.fail('unauthorized screen accepted')
        except WebSocketDisconnect as exc:
            assert exc.code in (4401,4404)
        login=client.post('/api/auth/login',json={'email':'a@example.com','password':'long-password'})
        cookie=login.cookies['bothub_session']
        try:
            with client.websocket_connect('/api/bots/alpha/screen',headers={'Origin':'https://testserver',
                                                                            'Cookie':f'bothub_session={cookie}'}) as ws:
                try:
                    assert ws.receive_bytes()==b'RFB 003.008\n'
                    ws.send_bytes(b'RFB 003.008\n\x01\x01' + b'\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                    deadline=time.monotonic()+1
                    while time.monotonic()<deadline and not any(launcher.screen_inputs.values()):
                        time.sleep(.01)
                    assert any(launcher.screen_inputs.values())
                    password='VisibleOnlyToHuman123'
                    caplog.set_level(logging.INFO)
                    response=client.post('/api/bots/alpha/browser/secret-input',json={'value':password,'save_as':'login'},headers=OWNER)
                    assert response.status_code==200,response.text

                    async def secret_row():
                        async with app.state.pool.acquire() as con:
                            row=await con.fetchrow("select id,value_encrypted from bothub.secrets where bot_id='alpha' and name='login'")
                            events=await con.fetch('select payload from bothub.events')
                        return row,events

                    row,events=client.portal.call(secret_row)
                    assert decrypt_secret(bytes(row['value_encrypted']),row['id'].bytes).decode()==password
                    assert password not in str(events) and password not in caplog.text
                    launcher.block_input=True
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        first=executor.submit(client.post,'/api/bots/alpha/browser/secret-input',
                                              json={'value':'first'},headers=OWNER)
                        try:
                            assert launcher.input_entered.wait(1)
                            second=executor.submit(client.post,'/api/bots/alpha/browser/secret-input',
                                                   json={'value':'second'},headers=OWNER)
                            time.sleep(.05)
                            assert not second.done(), 'second secret bypassed the bot lock'
                        finally:
                            launcher.input_release.set()
                        assert first.result(timeout=2).status_code==200
                        assert second.result(timeout=2).status_code==200
                    launcher.input_entered.clear()
                    launcher.input_release.clear()
                    launcher.block_input=True
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        secret_future=executor.submit(client.post,'/api/bots/alpha/browser/secret-input',
                                                      json={'value':'second-secret'},headers=OWNER)
                        try:
                            assert launcher.input_entered.wait(1)
                            return_future=executor.submit(client.post,'/api/bots/alpha/browser/return',headers=OWNER)
                            time.sleep(.05)
                            assert not return_future.done(), 'return changed state during secret injection'
                        finally:
                            launcher.input_release.set()
                        assert secret_future.result(timeout=2).status_code==200
                        assert return_future.result(timeout=2).status_code==200
                    assert client.post(f'/api/turns/{turn}/stop',headers=OWNER).status_code==200
                    with pytest.raises(WebSocketDisconnect) as closed:
                        ws.receive_bytes()
                    assert closed.value.code==4410
                finally:
                    launcher.gate.set()
        finally:
            launcher.gate.set()
        assert not launcher.screen_inputs
