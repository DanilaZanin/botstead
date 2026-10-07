"""Provider login stays scoped to one owner and one PTY session."""
import asyncio
import logging
import threading
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bothub.launcher_client import ExecChunk, ExecExit, FakeLauncherClient
from bothub.main import create_app


pytestmark = pytest.mark.empty_users
OWNER = {'Authorization': 'Bearer test-owner'}


def wait_bots_idle(client, *bot_ids, timeout=3.0):
    """POST /api/bots в docker-режиме отвечает 201 со статусом starting: контейнер создаёт фон, ждём idle."""
    import time
    deadline = time.monotonic() + timeout
    while True:
        bots = {bot['id']: bot for bot in client.get('/api/bots', headers=OWNER).json()}
        if all(bots.get(bot_id, {}).get('status') == 'idle' for bot_id in bot_ids):
            return bots
        assert time.monotonic() < deadline, bots
        time.sleep(0.02)


class HeldLoginLauncher(FakeLauncherClient):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.active_session = None
        self.last_input = None
        self.last_size = None

    async def open_login_session(self, owner_id, *, command='shell', cols=80, rows=24):
        sid = await super().open_login_session(owner_id, command=command, cols=cols, rows=rows)
        if not command.endswith('_status'):
            self.active_session = sid
        return sid

    async def login_output(self, session_id):
        if session_id == self.active_session:
            yield ExecChunk('stdout', b'private terminal output')
            await self.gate.wait()
            yield ExecExit(0, 'exit')
        else:
            yield ExecExit(0, 'exit')

    async def login_input(self, session_id, data):
        await super().login_input(session_id, data)
        self.last_input = data

    async def login_resize(self, session_id, cols, rows):
        await super().login_resize(session_id, cols, rows)
        self.last_size = (cols, rows)


@pytest.mark.pure
def test_provider_login_close_frame_finishes_before_testclient_exit():
    query_gate = threading.Event()
    checks = 0

    class FakeConnection:
        async def fetchrow(self, query, *args):
            return {'kind':'cli_subscription','cli':'claude','owner_id':uuid.UUID(int=1)}

        async def fetchval(self, query, *args):
            nonlocal checks
            checks += 1
            if checks > 1:
                await asyncio.to_thread(query_gate.wait)
            return 1

    class FakePool:
        @asynccontextmanager
        async def acquire(self):
            yield FakeConnection()

    launcher = HeldLoginLauncher()
    app = create_app(launcher=launcher)
    app.state.pool = FakePool()
    headers = {'Cookie':'bothub_session=test-session','Origin':'https://testserver'}
    client = TestClient(app,base_url='https://testserver')
    url = f'/api/providers/{uuid.uuid4()}/login'
    with client.websocket_connect(url,headers=headers) as ws:
        assert ws.receive_bytes() == b'private terminal output'
        with pytest.raises(WebSocketDisconnect) as duplicate:
            with client.websocket_connect(f'/api/providers/{uuid.uuid4()}/login',headers=headers):
                pass
        assert duplicate.value.code == 4409
        ws.send_json({'t':'close'})
        import time
        deadline = time.monotonic()+.3
        while time.monotonic()<deadline and not any(call[0]=='close_login_session' for call in launcher.calls):
            time.sleep(.01)
        closed = any(call[0]=='close_login_session' for call in launcher.calls)
        query_gate.set()
        assert closed, 'close frame must be handled without waiting for the next session check'
    launcher.gate = asyncio.Event()
    closed_before = sum(call[0]=='close_login_session' for call in launcher.calls)
    with client.websocket_connect(url,headers=headers) as disconnected:
        assert disconnected.receive_bytes() == b'private terminal output'
        disconnected.send({'type':'websocket.disconnect'})
        deadline = time.monotonic()+1
        while time.monotonic()<deadline and sum(call[0]=='close_login_session' for call in launcher.calls)<=closed_before:
            time.sleep(.01)
        assert sum(call[0]=='close_login_session' for call in launcher.calls)>closed_before
    launcher.gate = asyncio.Event()
    closed_before = sum(call[0]=='close_login_session' for call in launcher.calls)
    with client.websocket_connect(url,headers=headers) as retried:
        assert retried.receive_bytes() == b'private terminal output'
        retried.send({'type':'websocket.disconnect'})
        deadline = time.monotonic()+1
        while time.monotonic()<deadline and sum(call[0]=='close_login_session' for call in launcher.calls)<=closed_before:
            time.sleep(.01)
        assert sum(call[0]=='close_login_session' for call in launcher.calls)>closed_before
    launcher.gate = asyncio.Event()
    closed_before = sum(call[0]=='close_login_session' for call in launcher.calls)
    with client.websocket_connect(url,headers=headers) as malformed:
        assert malformed.receive_bytes() == b'private terminal output'
        malformed.send_json(None)
        with pytest.raises(WebSocketDisconnect) as invalid:
            malformed.receive_json()
        assert invalid.value.code == 1003
        deadline = time.monotonic()+1
        while time.monotonic()<deadline and sum(call[0]=='close_login_session' for call in launcher.calls)<=closed_before:
            time.sleep(.01)
        assert sum(call[0]=='close_login_session' for call in launcher.calls)>closed_before
    launcher.gate = asyncio.Event()
    closed_before = sum(call[0]=='close_login_session' for call in launcher.calls)
    with client.websocket_connect(url,headers=headers) as malformed_list:
        assert malformed_list.receive_bytes() == b'private terminal output'
        malformed_list.send_json([1, 2])
        with pytest.raises(WebSocketDisconnect) as invalid:
            malformed_list.receive_json()
        assert invalid.value.code == 1003
    deadline = time.monotonic()+1
    while time.monotonic()<deadline and sum(call[0]=='close_login_session' for call in launcher.calls)<=closed_before:
        time.sleep(.01)
    assert sum(call[0]=='close_login_session' for call in launcher.calls)>closed_before


def test_provider_login_frames_isolation_and_recreate(monkeypatch, caplog):
    monkeypatch.setattr('bothub.main.BOT_START_DELAY', 0)
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_RUNNER_EXEC', 'docker')
    monkeypatch.setenv('BOTHUB_GATEWAY_TOKEN_SECRET', 'test-gateway-secret')
    launcher = HeldLoginLauncher()
    app = create_app(launcher=launcher)
    with TestClient(app, base_url='https://testserver') as client:
        setup = client.post('/api/setup', json={'email':'a@example.com','password':'long-password'}, headers=OWNER)
        assert setup.status_code == 201, setup.text
        login = client.post('/api/auth/login', json={'email':'a@example.com','password':'long-password'})
        cookie = login.cookies['bothub_session']
        csrf = login.json()['csrf_token']
        headers = {'Cookie':f'bothub_session={cookie}', 'X-CSRF':csrf, 'Origin':'https://testserver'}
        # бот без привязки создаётся, пока реестр владельца пуст (режим совместимости); провайдер добавляется после
        bot = client.post('/api/bots', json={'id':'scout','name':'Scout','provider':'claude','model':'claude-sonnet-5'}, headers=OWNER)
        assert bot.status_code == 201 and bot.json()['status'] == 'starting', bot.text
        wait_bots_idle(client, 'scout')
        provider = client.post('/api/providers', json={'kind':'cli_subscription','cli':'claude','name':'Claude'}, headers=headers)
        assert provider.status_code == 201, provider.text
        provider_id = provider.json()['id']
        invite = client.post('/api/invites', json={}, headers=OWNER).json()['token']
        accepted = client.post('/api/invites/accept', json={'token':invite,'email':'b@example.com','password':'long-password'})
        assert accepted.status_code == 201, accepted.text
        foreign = client.post('/api/auth/login', json={'email':'b@example.com','password':'long-password'})
        foreign_headers = {'Cookie':f"bothub_session={foreign.cookies['bothub_session']}", 'Origin':'https://testserver'}
        url = f'/api/providers/{provider_id}/login'
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(url, headers=foreign_headers):
                pass
        assert denied.value.code == 4404
        with caplog.at_level(logging.INFO):
            with client.websocket_connect(url, headers=headers) as ws:
                assert ws.receive_bytes() == b'private terminal output'
                ws.send_bytes(b'auth code\r')
                ws.send_json({'t':'resize','cols':120,'rows':35})
                with pytest.raises(WebSocketDisconnect) as duplicate:
                    with client.websocket_connect(url, headers=headers):
                        pass
                assert duplicate.value.code == 4409
                client.portal.call(launcher.gate.set)
                assert ws.receive_json() == {'t':'exit','code':0}
        assert launcher.last_input == b'auth code\r'
        assert launcher.last_size == (120, 35)
        assert 'private terminal output' not in caplog.text
        async def leaked_events():
            async with app.state.pool.acquire() as con:
                return await con.fetchval("select count(*) from bothub.events where payload::text like '%private terminal output%'")
        assert client.portal.call(leaked_events) == 0
        assert not any(call[0] == 'recreate_bot' for call in launcher.calls)
        recreated = client.post('/api/bots/scout/recreate', headers=headers)
        assert recreated.status_code == 200, recreated.text
        assert any(call[0] == 'recreate_bot' for call in launcher.calls)
        launcher.gate = asyncio.Event()
        monkeypatch.setattr('bothub.main.LOGIN_IDLE_TIMEOUT', 0)
        with client.websocket_connect(url, headers=headers) as timed_out:
            with pytest.raises(WebSocketDisconnect) as timeout:
                timed_out.receive_json()
        assert timeout.value.code == 1001
        monkeypatch.setattr('bothub.main.LOGIN_IDLE_TIMEOUT', 600)
        closed_before = sum(call[0] == 'close_login_session' for call in launcher.calls)
        with client.websocket_connect(url, headers=headers) as stopped:
            assert stopped.receive_bytes() == b'private terminal output'
            stopped.send_json({'t':'close'})
            import time
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and sum(call[0] == 'close_login_session' for call in launcher.calls) <= closed_before:
                time.sleep(0.05)
            assert sum(call[0] == 'close_login_session' for call in launcher.calls) > closed_before


def test_provider_login_disconnect_without_close_releases_session(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_RUNNER_EXEC', 'docker')
    monkeypatch.setenv('BOTHUB_GATEWAY_TOKEN_SECRET', 'test-gateway-secret')
    launcher = HeldLoginLauncher()
    app = create_app(launcher=launcher)
    with TestClient(app, base_url='https://testserver') as client:
        setup = client.post('/api/setup', json={'email':'a@example.com','password':'long-password'}, headers=OWNER)
        assert setup.status_code == 201, setup.text
        login = client.post('/api/auth/login', json={'email':'a@example.com','password':'long-password'})
        headers = {'Cookie': f"bothub_session={login.cookies['bothub_session']}",
                   'X-CSRF': login.json()['csrf_token'], 'Origin': 'https://testserver'}
        provider = client.post('/api/providers', json={'kind':'cli_subscription','cli':'claude','name':'Claude'}, headers=headers)
        assert provider.status_code == 201, provider.text
        url = f"/api/providers/{provider.json()['id']}/login"
        closed_before = sum(call[0] == 'close_login_session' for call in launcher.calls)
        with client.websocket_connect(url, headers=headers) as disconnected:
            assert disconnected.receive_bytes() == b'private terminal output'
            disconnected.send({'type':'websocket.disconnect'})
            import time
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and sum(call[0] == 'close_login_session' for call in launcher.calls) <= closed_before:
                time.sleep(0.05)
            assert sum(call[0] == 'close_login_session' for call in launcher.calls) > closed_before
        with client.websocket_connect(url, headers=headers) as retried:
            assert retried.receive_bytes() == b'private terminal output'


def test_subscription_login_recreates_idle_bot_after_active_turn(monkeypatch):
    monkeypatch.setattr('bothub.main.BOT_START_DELAY', 0)
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_RUNNER_EXEC', 'docker')
    monkeypatch.setenv('BOTHUB_GATEWAY_TOKEN_SECRET', 'test-gateway-secret')
    launcher = HeldLoginLauncher()
    app = create_app(launcher=launcher)
    with TestClient(app, base_url='https://testserver') as client:
        setup = client.post('/api/setup', json={'email':'a@example.com','password':'long-password'}, headers=OWNER)
        assert setup.status_code == 201, setup.text
        login = client.post('/api/auth/login', json={'email':'a@example.com','password':'long-password'})
        headers = {'Cookie': f"bothub_session={login.cookies['bothub_session']}",
                   'X-CSRF': login.json()['csrf_token'], 'Origin': 'https://testserver'}
        provider = client.post('/api/providers', json={'kind':'cli_subscription','cli':'claude','name':'Claude'}, headers=headers)
        assert provider.status_code == 201, provider.text
        provider_id = provider.json()['id']
        model = next(m for m in client.get('/api/models',headers=OWNER).json() if m['provider_id']==provider_id)
        for bot_id in ('idle-bot','busy-bot'):
            result = client.post('/api/bots', json={'id':bot_id,'name':bot_id,'provider':'claude',
                'model':model['name'],'provider_id':provider_id,'model_id':model['id']}, headers=OWNER)
            assert result.status_code == 201, result.text
        wait_bots_idle(client, 'idle-bot', 'busy-bot')

        async def expire_provider():
            async with app.state.pool.acquire() as con:
                await con.execute("update bothub.providers set status='needs_login' where id=$1", uuid.UUID(provider_id))
        client.portal.call(expire_provider)

        async def start_busy_turn():
            async with app.state.pool.acquire() as con:
                thread_id = await con.fetchval("insert into bothub.threads(bot_id,owner_id) select id,owner_id from bothub.bots where id='busy-bot' returning id")
                return await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) values($1,'busy','running',now()+interval '1 hour') returning id",thread_id)
        turn_id = client.portal.call(start_busy_turn)
        with client.websocket_connect(f'/api/providers/{provider_id}/login',headers=headers) as ws:
            assert ws.receive_bytes() == b'private terminal output'
            client.portal.call(launcher.gate.set)
            assert ws.receive_json() == {'t':'exit','code':0}
            import time
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                bots = {bot['id']:bot for bot in client.get('/api/bots',headers=OWNER).json()}
                if bots['busy-bot']['need_restart'] and not bots['idle-bot']['need_restart']:
                    break
                time.sleep(0.05)
        assert not bots['idle-bot']['need_restart']
        assert bots['busy-bot']['need_restart']
        assert client.get('/api/providers', headers=OWNER).json()[0]['status'] == 'ok'
        assert any(call[0]=='recreate_bot' and call[1]=='idle-bot' for call in launcher.calls)
        assert not any(call[0]=='recreate_bot' and call[1]=='busy-bot' for call in launcher.calls)

        stopped = client.post(f'/api/turns/{turn_id}/stop',headers=OWNER)
        assert stopped.status_code == 200, stopped.text
        bots = {bot['id']:bot for bot in client.get('/api/bots',headers=OWNER).json()}
        assert not bots['busy-bot']['need_restart']
        assert any(call[0]=='recreate_bot' and call[1]=='busy-bot' for call in launcher.calls)
