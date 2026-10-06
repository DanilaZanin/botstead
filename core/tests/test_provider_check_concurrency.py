"""Provider checks share one running probe and cache the result."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub.main import create_app
from bothub.launcher_client import ExecExit, FakeLauncherClient


@pytest.mark.pure
@pytest.mark.parametrize('disable_before_release', [False, True])
async def test_provider_check_shared_task_survives_cancelled_waiter(monkeypatch, disable_before_release):
    owner_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    state = {'status': 'unchecked', 'last_error': None, 'last_check_at': None}

    class Connection:
        async def fetchval(self, query, *args):
            return True

        async def fetchrow(self, query, *args):
            if 'setup_user_id' in query:
                return {'id': owner_id, 'role': 'admin', 'status': 'active'}
            return {'id': provider_id, 'owner_id': owner_id, 'kind': 'cli_subscription',
                    'cli': 'claude', 'name': 'Claude', 'base_url': None,
                    'secret_encrypted': None, 'allow_private': False, 'allow_private_ips': [], **state}

        async def execute(self, query, *args):
            if query.startswith('update bothub.providers'):
                state.update(status=args[1], last_error=args[2], last_check_at=datetime.now(timezone.utc))

        @asynccontextmanager
        async def transaction(self):
            yield

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    class Launcher(FakeLauncherClient):
        def __init__(self):
            super().__init__()
            self.opened = asyncio.Event()
            self.release = asyncio.Event()

        async def login_output(self, session_id):
            self.opened.set()
            await self.release.wait()
            yield ExecExit(0, 'exit')

    monkeypatch.setenv('OWNER_TOKEN', 'test-owner')
    monkeypatch.setenv('BOTHUB_LEGACY_AUTH', 'true')
    launcher = Launcher()
    app = create_app(launcher=launcher)
    app.state.pool = Pool()
    url = f'/api/providers/{provider_id}/check'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        first = asyncio.create_task(client.post(url, headers={'Authorization': 'Bearer test-owner'}))
        await asyncio.wait_for(launcher.opened.wait(), 2)
        second = asyncio.create_task(client.post(url, headers={'Authorization': 'Bearer test-owner'}))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        if disable_before_release:
            state['status'] = 'disabled'
        launcher.release.set()
        response = await asyncio.wait_for(second, 2)
        assert response.status_code == 200
        assert response.json()['status'] == ('disabled' if disable_before_release else 'ok')
        again = await client.post(url, headers={'Authorization': 'Bearer test-owner'})
        assert again.status_code == 200
    assert sum(call[0] == 'open_login_session' for call in launcher.calls) == 1


@pytest.mark.pure
async def test_agy_check_uses_ten_minute_cache_after_generic_cooldown(monkeypatch):
    owner_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    checked_at = datetime.now(timezone.utc) - timedelta(seconds=31)

    class Connection:
        async def fetchval(self, query, *args):
            return True

        async def fetchrow(self, query, *args):
            if 'setup_user_id' in query:
                return {'id': owner_id, 'role': 'admin', 'status': 'active'}
            return {'id': provider_id, 'owner_id': owner_id, 'kind': 'cli_subscription',
                    'cli': 'agy', 'name': 'Gemini', 'base_url': None,
                    'secret_encrypted': None, 'allow_private': False, 'allow_private_ips': [], 'status': 'ok', 'last_error': None,
                    'last_check_at': checked_at}

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    monkeypatch.setenv('OWNER_TOKEN', 'test-owner')
    monkeypatch.setenv('BOTHUB_LEGACY_AUTH', 'true')
    launcher = FakeLauncherClient()
    app = create_app(launcher=launcher)
    app.state.pool = Pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/api/providers/{provider_id}/check',
                                     headers={'Authorization': 'Bearer test-owner'})
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
    assert launcher.calls == []


@pytest.mark.pure
async def test_failed_check_uses_thirty_second_cooldown(monkeypatch):
    owner_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    state = {'last_check_at':datetime.now(timezone.utc)-timedelta(seconds=31),
             'status':'error','last_error':'failed'}

    class Connection:
        async def fetchval(self, query, *args): return True
        async def fetchrow(self, query, *args):
            if 'setup_user_id' in query: return {'id':owner_id,'role':'admin','status':'active'}
            return {'id':provider_id,'owner_id':owner_id,'kind':'cli_subscription',
                    'cli':'agy','name':'Gemini','base_url':None,'secret_encrypted':None,'allow_private':False,'allow_private_ips':[],**state}
        async def execute(self, query, *args):
            if query.startswith('update bothub.providers'):
                state.update(status=args[1],last_error=args[2],last_check_at=datetime.now(timezone.utc))
        @asynccontextmanager
        async def transaction(self): yield

    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Connection()

    monkeypatch.setenv('OWNER_TOKEN','test-owner')
    monkeypatch.setenv('BOTHUB_LEGACY_AUTH','true')
    launcher = FakeLauncherClient()
    app = create_app(launcher=launcher)
    app.state.pool = Pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response = await client.post(f'/api/providers/{provider_id}/check',headers={'Authorization':'Bearer test-owner'})
    assert response.status_code == 200
    assert sum(call[0]=='open_login_session' for call in launcher.calls) == 1
