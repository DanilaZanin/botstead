"""Provider checks share one running probe and cache the result."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub.main import SUBSCRIPTION_MODELS, create_app, parse_agy_models
from bothub.launcher_client import ExecChunk, ExecExit, FakeLauncherClient


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


def _subscription_env(monkeypatch, cli, output, *, code=0, state=None):
    """Приложение с одним подписочным провайдером и лаунчером, который отдаёт `output` как stdout команды `<cli>_status`."""
    owner_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    state = state if state is not None else {'status': 'new', 'last_error': None, 'last_check_at': None}
    synced = []

    class Connection:
        async def fetchval(self, query, *args): return True
        async def fetchrow(self, query, *args):
            if 'setup_user_id' in query: return {'id': owner_id, 'role': 'admin', 'status': 'active'}
            return {'id': provider_id, 'owner_id': owner_id, 'kind': 'cli_subscription', 'cli': cli, 'name': 'CLI',
                    'base_url': None, 'secret_encrypted': None, 'allow_private': False, 'allow_private_ips': [], **state}
        async def execute(self, query, *args):
            if query.startswith('update bothub.providers'):
                state.update(status=args[1], last_error=args[2], last_check_at=datetime.now(timezone.utc))
            elif query.startswith('update bothub.models set enabled=false'):
                synced.append(list(args[1]))
        @asynccontextmanager
        async def transaction(self): yield

    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Connection()

    class Launcher(FakeLauncherClient):
        async def login_output(self, session_id):
            for chunk in output if isinstance(output, list) else [output]:
                yield ExecChunk('stdout', chunk)
            yield ExecExit(code, 'exit')

    monkeypatch.setenv('OWNER_TOKEN', 'test-owner')
    monkeypatch.setenv('BOTHUB_LEGACY_AUTH', 'true')
    launcher = Launcher()
    app = create_app(launcher=launcher)
    app.state.pool = Pool()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return client, f'/api/providers/{provider_id}/check', launcher, synced, state


AUTH = {'Authorization': 'Bearer test-owner'}


@pytest.mark.pure
async def test_agy_check_syncs_models_from_cli_output(monkeypatch):
    table = ('gemini-3.8-flash-high\tGemini 3.8 Flash (High)\r\n'
             'gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n'
             'Available models:\n'          # заголовок без таба пропускается
             'Bad_ID\tNot a model id\n'
             'x\ttoo short id\n')
    client, url, launcher, synced, _ = _subscription_env(monkeypatch, 'agy', [table[:30].encode(), table[30:].encode()])
    async with client:
        response = await client.post(url, headers=AUTH)
    assert response.status_code == 200 and response.json()['status'] == 'ok'
    assert synced == [['gemini-3.8-flash-high', 'gemini-3.1-pro-high']]
    assert [call[2] for call in launcher.calls if call[0] == 'open_login_session'] == ['agy_status']


@pytest.mark.pure
@pytest.mark.parametrize('output', [b'', b'no tabs here\njust text\n', b'ok\t\n\tname only\n', b'\xff\xfe\x00\x01'])
async def test_agy_check_without_parsable_models_uses_fallback_table(monkeypatch, output):
    client, url, _, synced, _ = _subscription_env(monkeypatch, 'agy', output)
    async with client:
        response = await client.post(url, headers=AUTH)
    assert response.status_code == 200 and response.json()['status'] == 'ok'
    assert synced == [list(SUBSCRIPTION_MODELS['agy'])]


@pytest.mark.pure
@pytest.mark.parametrize('cli', ['claude', 'codex'])
async def test_other_cli_ignore_output_and_use_table(monkeypatch, cli):
    client, url, _, synced, _ = _subscription_env(monkeypatch, cli, b'some-id\tSome name\n')
    async with client:
        response = await client.post(url, headers=AUTH)
    assert response.json()['status'] == 'ok'
    assert synced == [list(SUBSCRIPTION_MODELS[cli])]


@pytest.mark.pure
async def test_agy_check_with_nonzero_exit_stays_error_even_with_models(monkeypatch):
    client, url, _, synced, _ = _subscription_env(monkeypatch, 'agy', b'gemini-3.1-pro-high\tPro\n', code=1)
    async with client:
        response = await client.post(url, headers=AUTH)
    assert response.json()['status'] == 'error' and response.json()['last_error'] == 'subscription not authenticated'
    assert synced == []


@pytest.mark.pure
def test_parse_agy_models_rules():
    long_name = 'n' * 81
    ok_name = 'n' * 80
    ids = [f'm{i}' for i in range(60)]
    assert parse_agy_models(f'a1\t{ok_name}\na2\t{long_name}\n') == ['a1']
    assert parse_agy_models('\x1b[1mgemini-x.1\tName\x1b[0m\ngemini-x.1\tAgain\n') == ['gemini-x.1']
    assert parse_agy_models('-lead\tN\nUPPER\tN\n' + 'a' * 65 + '\tN\n' + 'a' * 64 + '\tN\n') == ['a' * 64]
    assert parse_agy_models('\n'.join(f'{i}\tN' for i in ids)) == ids[:50]
    assert parse_agy_models('  gemini-a  \t  Name  \n') == ['gemini-a']
    assert parse_agy_models('') == []


@pytest.mark.pure
async def test_force_check_bypasses_cache_but_keeps_short_floor(monkeypatch):
    state = {'status': 'ok', 'last_error': None, 'last_check_at': datetime.now(timezone.utc) - timedelta(seconds=20)}
    client, url, launcher, _, _ = _subscription_env(monkeypatch, 'claude', b'', state=state)
    opened = lambda: sum(call[0] == 'open_login_session' for call in launcher.calls)
    async with client:
        assert (await client.post(url, headers=AUTH)).status_code == 200
        assert opened() == 0                                   # кэш 30 с
        assert (await client.post(url + '?force=1', headers=AUTH)).status_code == 200
        assert opened() == 1                                   # force обходит кэш
        assert (await client.post(url + '?force=1', headers=AUTH)).status_code == 200
        assert opened() == 1                                   # но не чаще раза в 5 с
        assert (await client.post(url + '?force=0', headers=AUTH)).status_code == 200
        assert opened() == 1
