"""Postgres side of the provider verification: migration 015, SQL of the new routes, turn and gateway refusal.

The route logic itself is covered without a database in test_provider_verify_pure.py."""
import asyncio
import base64
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import httpx
import pytest

from bothub import auth
from bothub.gateway import GatewayProvider
from bothub.main import create_app
from bothub.secrets import decrypt_secret

OWNER = {'Authorization': 'Bearer test-owner'}
KEY = 'sk-good-key-0123456789'
HOSTS = {'llm.lan': '192.168.1.20', 'llm2.lan': '192.168.1.21'}


@pytest.fixture(autouse=True)
def provider_env(monkeypatch):
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.delenv('PROVIDER_PRIVATE_ALLOW', raising=False)
    calls = []

    def upstream(request):
        if not request.headers.get('authorization', '').startswith('Bearer invalid-'):
            calls.append(request)  # считаем пробы с настоящим ключом; запрос с заведомо неверным ключом (key_verified) не в счёт
        if request.headers.get('authorization') == f'Bearer {KEY}' or 'good' in request.headers.get('authorization', ''):
            return httpx.Response(200, json={'data': [{'id': 'm1'}, {'id': 'm2'}]})
        return httpx.Response(401, json={'error': 'bad key'})

    async def resolver(host, port):
        return [(None, None, None, None, (HOSTS.get(host, '8.8.8.8'), port))]

    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    monkeypatch.setattr('bothub.main._resolve_host', resolver)
    monkeypatch.setattr('bothub.main.own_networks', lambda: ())  # результат не зависит от сетей машины, где идут тесты
    return calls


@asynccontextmanager
async def client_for(**options):
    app = create_app(**options)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app


async def add_member(client, app, email='member@example.com'):
    async with app.state.pool.acquire() as con:
        user = await con.fetchval("insert into bothub.users(email,password_hash) values($1,$2) returning id",
                                  email, auth.hash_password('long-password'))
    login = await client.post('/api/auth/login', json={'email': email, 'password': 'long-password'})
    assert login.status_code == 200, login.text
    headers = {'Cookie': f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF': login.json()['csrf_token'],
               'Origin': 'https://testserver'}
    return user, headers


async def row_of(app, provider):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow('select * from bothub.providers where id=$1', uuid.UUID(str(provider)))


async def test_migration_015_columns_defaults_and_status_values():
    async with client_for() as (_, app):
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            for number, status in enumerate(('pending_admin', 'unchecked')):
                row = await con.fetchrow("insert into bothub.providers(owner_id,kind,name,base_url,secret_encrypted,status) "
                                         "values($1,'openai_compatible',$2,'https://x.example',decode('00','hex'),$3) returning *",
                                         owner, f'p{number}', status)
                assert row['allow_private'] is False and row['secret_tail'] is None
            with pytest.raises(asyncpg.CheckViolationError):
                await con.execute("update bothub.providers set status='bogus' where owner_id=$1", owner)
            with pytest.raises(asyncpg.CheckViolationError):
                await con.execute("update bothub.providers set secret_tail='abc' where owner_id=$1", owner)
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='015_provider_verification.sql'") == 1


async def test_create_patch_and_force_against_postgres(provider_env):
    async with client_for() as (client, app):
        created = await client.post('/api/providers', headers=OWNER, json={
            'kind': 'openai_compatible', 'name': 'Local', 'base_url': 'https://api.example', 'secret': KEY})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body['status'] == 'ok' and body['secret_tail'] == '6789' and len(provider_env) == 1
        row = await row_of(app, body['id'])
        assert row['last_check_at'] is not None and row['secret_tail'] == '6789'
        async with app.state.pool.acquire() as con:
            assert {r['name'] for r in await con.fetch('select name from bothub.models where provider_id=$1', row['id'])} == {'m1', 'm2'}
        # неверный ключ ничего не меняет
        refused = await client.patch(f"/api/providers/{body['id']}", headers=OWNER, json={'secret': 'sk-wrong-key-00000000'})
        assert refused.status_code == 422 and refused.json()['error'] == 'key_rejected'
        unchanged = await row_of(app, body['id'])
        assert unchanged['secret_encrypted'] == row['secret_encrypted'] and unchanged['secret_tail'] == '6789'
        assert unchanged['status'] == 'ok'
        # успешная замена: один пробный запрос
        before = len(provider_env)
        replaced = await client.patch(f"/api/providers/{body['id']}", headers=OWNER, json={'secret': 'sk-good-replacement-ABCD'})
        assert replaced.status_code == 200 and replaced.json()['secret_tail'] == 'ABCD'
        assert len(provider_env) == before + 1
        row = await row_of(app, body['id'])
        assert decrypt_secret(bytes(row['secret_encrypted']), row['id'].bytes).decode() == 'sk-good-replacement-ABCD'
        # force: без запроса, статус unchecked
        forced = await client.patch(f"/api/providers/{body['id']}", headers=OWNER, json={'secret': 'sk-wrong-key-00000000', 'force': True})
        assert forced.status_code == 200 and forced.json()['status'] == 'unchecked'
        assert len(provider_env) == before + 1
        assert (await row_of(app, body['id']))['last_check_at'] is None


async def test_member_private_address_flow_against_postgres(provider_env):
    async with client_for() as (client, app):
        member, headers = await add_member(client, app)
        created = await client.post('/api/providers', headers=headers, json={
            'kind': 'openai_compatible', 'name': 'Home LLM', 'base_url': 'https://llm.lan', 'secret': KEY})
        assert created.status_code == 201, created.text
        assert created.json()['status'] == 'pending_admin' and not provider_env
        provider = created.json()['id']
        denied = await client.patch(f'/api/providers/{provider}/allow-private', headers=headers, json={'allow': True})
        assert denied.status_code == 403
        listed = await client.get('/api/admin/provider-requests', headers=OWNER)
        assert listed.status_code == 200
        assert listed.json() == [{'id': provider, 'name': 'Home LLM', 'base_url': 'https://llm.lan',
                                  'email': 'member@example.com', 'created_at': listed.json()[0]['created_at'],
                                  'allow_private': False, 'approved_ips': [], 'resolved_ips': ['192.168.1.20'],
                                  'reapproval': False}]
        without_ips = await client.patch(f'/api/providers/{provider}/allow-private', headers=OWNER,
                                         json={'allow': True, 'base_url': 'https://llm.lan'})
        assert without_ips.status_code == 400 and set(without_ips.json()) == {'error', 'detail'}
        approved = await client.patch(f'/api/providers/{provider}/allow-private', headers=OWNER,
                                      json={'allow': True, 'base_url': 'https://llm.lan', 'ips': ['192.168.1.20']})
        assert approved.status_code == 200 and approved.json()['status'] == 'ok', approved.text
        assert approved.json()['allow_private_ips'] == ['192.168.1.20']
        row = await row_of(app, provider)
        assert row['allow_private'] is True and row['status'] == 'ok' and len(provider_env) == 1
        assert row['allow_private_ips'] == ['192.168.1.20']
        assert (await client.get('/api/admin/provider-requests', headers=OWNER)).json() == []
        moved = await client.patch(f'/api/providers/{provider}', headers=headers, json={'secret': KEY, 'base_url': 'https://llm2.lan'})
        assert moved.status_code == 200 and moved.json()['status'] == 'pending_admin' and moved.json()['allow_private'] is False
        assert len(provider_env) == 1
        assert [r['name'] for r in (await client.get('/api/admin/provider-requests', headers=OWNER)).json()] == ['Home LLM']


async def test_pending_admin_provider_is_refused_by_gateway_and_turn():
    started = []

    class Runner:
        async def run(self, turn):
            started.append(turn)
            return
            yield

        async def stop(self, turn_id):
            pass

    async with client_for(runner_factory=lambda provider: Runner()) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            provider = await con.fetchval(
                "insert into bothub.providers(owner_id,kind,name,base_url,secret_encrypted,status) "
                "values($1,'openai_compatible','Pending','https://llm.lan',decode('00','hex'),'ok') returning id", owner)
            model = await con.fetchval("insert into bothub.models(provider_id,name) values($1,'gpt-test') returning id", provider)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,provider_id,model_id) "
                              "values('pend','Pend','codex','gpt-test',$1,$2,$3)", owner, provider, model)
        thread = (await client.post('/api/threads', json={'bot_id': 'pend'}, headers=OWNER)).json()
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.providers set status='pending_admin' where id=$1", provider)
            running = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) "
                                         "values($1,'probe','running',now()+interval '1 hour') returning id", uuid.UUID(thread['id']))
        token = app.state.gateway.issue_token('pend', str(provider), turn_id=str(running))
        refused = await client.get(f'/gateway/{provider}/v1/models', headers={'Authorization': f'Bearer {token}'})
        assert refused.status_code == 403 and refused.json()['detail'] == 'provider unavailable to bot', refused.text
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='done' where id=$1", running)
        turn = await client.post(f"/api/threads/{thread['id']}/turns", json={'prompt': 'go'}, headers=OWNER)
        assert turn.status_code in (200, 201, 202), turn.text
        async with asyncio.timeout(10):
            while True:
                async with app.state.pool.acquire() as con:
                    statuses = await con.fetch("select status from bothub.turns where thread_id=$1 and prompt='go'", uuid.UUID(thread['id']))
                if statuses and statuses[0]['status'] == 'error':
                    break
                await asyncio.sleep(.05)
        assert not started


MIGRATION_016 = Path(__file__).parent.parent / 'bothub' / 'migrations' / '016_provider_approved_ips.sql'


async def test_migration_016_column_default_and_backfill():
    async with client_for() as (_, app):
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='016_provider_approved_ips.sql'") == 1
            assert await con.fetchval("select udt_name from information_schema.columns where table_schema='bothub' "
                                      "and table_name='providers' and column_name='allow_private_ips'") == '_text'
            plain = await con.fetchrow("insert into bothub.providers(owner_id,kind,name,base_url,secret_encrypted) "
                                       "values($1,'openai_compatible','plain','https://x.example',decode('00','hex')) returning *", owner)
            assert plain['allow_private_ips'] == []
            with pytest.raises(asyncpg.NotNullViolationError):
                await con.execute("update bothub.providers set allow_private_ips=null where id=$1", plain['id'])
            # строки, одобренные до миграции (флаг есть, набора нет), уходят на повторное одобрение; отключённые и без флага остаются
            ids = {}
            for name, flag, status in (('flagged-ok', True, 'ok'), ('flagged-off', True, 'disabled'), ('unflagged', False, 'ok')):
                ids[name] = await con.fetchval(
                    "insert into bothub.providers(owner_id,kind,name,base_url,secret_encrypted,allow_private,status,last_check_at) "
                    "values($1,'openai_compatible',$2,'https://llm.lan',decode('00','hex'),$3,$4,now()) returning id", owner, name, flag, status)
            sql = MIGRATION_016.read_text()
            await con.execute(sql[sql.index('update bothub.providers'):])
            rows = {r['name']: r for r in await con.fetch("select * from bothub.providers where name=any($1::text[])", list(ids))}
            assert rows['flagged-ok']['status'] == 'pending_admin' and rows['flagged-ok']['allow_private'] is True
            assert rows['flagged-ok']['last_check_at'] is None and 'повторное одобрение' in rows['flagged-ok']['last_error']
            assert rows['flagged-off']['status'] == 'disabled' and rows['unflagged']['status'] == 'ok'


async def test_rebinding_after_approval_against_postgres(provider_env, monkeypatch):
    async with client_for() as (client, app):
        member, headers = await add_member(client, app)
        created = await client.post('/api/providers', headers=headers, json={
            'kind': 'openai_compatible', 'name': 'Home LLM', 'base_url': 'https://llm.lan', 'secret': KEY})
        provider = created.json()['id']
        approved = await client.patch(f'/api/providers/{provider}/allow-private', headers=OWNER,
                                      json={'allow': True, 'base_url': 'https://llm.lan', 'ips': ['192.168.1.20']})
        assert approved.status_code == 200 and approved.json()['status'] == 'ok', approved.text
        assert 'allow_private_ips' not in (await client.get('/api/providers', headers=headers)).json()[0]
        probes = len(provider_env)
        monkeypatch.setitem(HOSTS, 'llm.lan', '172.18.0.2')  # DNS переключили на сеть Docker
        async with app.state.pool.acquire() as con:
            await con.execute('update bothub.providers set last_check_at=null where id=$1', uuid.UUID(provider))
        checked = await client.post(f'/api/providers/{provider}/check', headers=headers)
        assert checked.status_code == 422 and checked.json()['error'] == 'invalid_base_url'
        assert 'повторное одобрение' in checked.json()['detail']
        row = await row_of(app, provider)
        assert row['status'] == 'pending_admin' and row['allow_private'] is True and row['allow_private_ips'] == ['192.168.1.20']
        patched = await client.patch(f'/api/providers/{provider}', headers=headers, json={'secret': 'sk-good-replacement-ABCD'})
        assert patched.status_code == 422 and (await row_of(app, provider))['secret_encrypted'] == row['secret_encrypted']
        assert len(provider_env) == probes  # на 172.18.0.2 ничего не ушло
        listed = (await client.get('/api/admin/provider-requests', headers=OWNER)).json()
        assert listed[0]['reapproval'] is True and listed[0]['approved_ips'] == ['192.168.1.20']
        assert listed[0]['resolved_ips'] == ['172.18.0.2']
        # шлюз: отчёт о смене переводит провайдера в ожидание, даже если /check не звали
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.providers set status='ok' where id=$1", uuid.UUID(provider))
        await app.state.provider_address_changed(GatewayProvider(provider, 'openai_compatible', 'https://llm.lan', 'k', True, [],
                                                                 allow_private=True, allow_private_ips=('192.168.1.20',)))
        assert (await row_of(app, provider))['status'] == 'pending_admin'
        # админ видит новый адрес и одобряет его
        stale = await client.patch(f'/api/providers/{provider}/allow-private', headers=OWNER,
                                   json={'allow': True, 'base_url': 'https://llm.lan', 'ips': ['192.168.1.20']})
        assert stale.status_code == 409  # админ видел 192.168.1.20, имя теперь указывает на 172.18.0.2
        monkeypatch.setitem(HOSTS, 'llm.lan', '192.168.1.30')
        again = await client.patch(f'/api/providers/{provider}/allow-private', headers=OWNER,
                                   json={'allow': True, 'base_url': 'https://llm.lan', 'ips': ['192.168.1.30']})
        assert again.status_code == 200 and again.json()['allow_private_ips'] == ['192.168.1.30'], again.text
        assert (await row_of(app, provider))['status'] == 'ok'


async def test_empty_base_url_patch_is_a_400_against_postgres():
    async with client_for() as (client, app):
        created = await client.post('/api/providers', headers=OWNER, json={
            'kind': 'openai_api', 'name': 'OpenAI', 'secret': KEY})
        assert created.status_code == 201, created.text
        for extra in ({}, {'force': True}):
            response = await client.patch(f"/api/providers/{created.json()['id']}", headers=OWNER,
                                          json={'secret': KEY, 'base_url': '', **extra})
            assert response.status_code == 400 and response.json()['error'] == 'invalid_base_url', response.text
        assert (await row_of(app, created.json()['id']))['base_url'] is None


async def test_migration_024_adds_nullable_key_verified():
    async with client_for() as (_, app):
        async with app.state.pool.acquire() as con:
            column = await con.fetchrow("select data_type, is_nullable from information_schema.columns "
                                        "where table_schema='bothub' and table_name='providers' and column_name='key_verified'")
            assert column['data_type'] == 'boolean' and column['is_nullable'] == 'YES'
            owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            row = await con.fetchrow("insert into bothub.providers(owner_id,kind,name,base_url,secret_encrypted,status) "
                                     "values($1,'openai_compatible','old','https://x.example',decode('00','hex'),'ok') returning *", owner)
            assert row['key_verified'] is None  # строки до миграции и подписочные провайдеры: не определено
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='024_provider_key_verified.sql'") == 1


async def test_key_verified_is_stored_on_create_check_and_patch(monkeypatch):
    async with client_for() as (client, app):
        created = await client.post('/api/providers', headers=OWNER, json={
            'kind': 'openai_compatible', 'name': 'Strict', 'base_url': 'https://api.example', 'secret': KEY})
        assert created.status_code == 201, created.text
        assert created.json()['key_verified'] is True  # неверный ключ сервер отверг (401)
        assert (await row_of(app, created.json()['id']))['key_verified'] is True
        # сервер, который отвечает 200 на любой ключ (как OpenRouter): статус ok, ключ не проверен
        monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(lambda request: httpx.Response(200, json={'data': [{'id': 'm1'}]})))
        loose = await client.post('/api/providers', headers=OWNER, json={
            'kind': 'openai_compatible', 'name': 'Loose', 'base_url': 'https://api.example', 'secret': KEY})
        assert loose.status_code == 201 and loose.json()['status'] == 'ok' and loose.json()['key_verified'] is False
        assert (await row_of(app, loose.json()['id']))['key_verified'] is False
        listed = {item['name']: item['key_verified'] for item in (await client.get('/api/providers', headers=OWNER)).json()}
        assert listed == {'Strict': True, 'Loose': False}
        # force: проба не шла, значение сбрасывается в null
        forced = await client.patch(f"/api/providers/{loose.json()['id']}", headers=OWNER, json={'secret': KEY, 'force': True})
        assert forced.status_code == 200 and forced.json()['key_verified'] is None
        assert (await row_of(app, loose.json()['id']))['key_verified'] is None
        # повторная проверка (после force провайдер unchecked, кэша нет) снова определяет значение
        checked = await client.post(f"/api/providers/{loose.json()['id']}/check", headers=OWNER)
        assert checked.status_code == 200 and checked.json()['status'] == 'ok' and checked.json()['key_verified'] is False
