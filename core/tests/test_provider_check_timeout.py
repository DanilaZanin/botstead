import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub import main
from bothub.launcher_client import FakeLauncherClient


@pytest.mark.pure
@pytest.mark.asyncio
async def test_subscription_check_timeout_closes_login_session(monkeypatch):
    owner_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    state = {'status': 'unchecked', 'last_error': None, 'last_check_at': None}
    closed = asyncio.Event()

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
                state.update(status=args[1], last_error=args[2])

        @asynccontextmanager
        async def transaction(self):
            yield

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    class Launcher(FakeLauncherClient):
        async def login_output(self, session_id):
            await asyncio.Event().wait()
            yield

        async def close_login_session(self, session_id):
            await asyncio.sleep(0)
            closed.set()

    monkeypatch.setenv('OWNER_TOKEN', 'test-owner')
    monkeypatch.setattr(main, 'PROVIDER_CHECK_TIMEOUT', .01)
    app = main.create_app(launcher=Launcher())
    app.state.pool = Pool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await asyncio.wait_for(
            client.post(f'/api/providers/{provider_id}/check', headers={'Authorization': 'Bearer test-owner'}),
            timeout=2,
        )
    assert response.status_code == 200, response.text
    assert state['status'] == 'error'
    assert closed.is_set()
