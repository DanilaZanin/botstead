"""Key verification before saving and administrator approval of private addresses.

Routes run against an in-memory stand-in for the providers tables, so these tests cover the control flow (status codes,
probe count, flag rules, leaks) without Postgres; SQL and the migration are covered by test_provider_verify_db.py."""
import asyncio
import base64
import ipaddress
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
import pytest

from bothub import auth
from bothub.main import create_app
from bothub.secrets import decrypt_secret

pytestmark = pytest.mark.pure

ADMIN_ID = uuid.UUID(int=1)
MEMBER_ID = uuid.UUID(int=2)
ADMIN = {'Authorization': 'Bearer test-owner'}
GOOD_KEY = 'sk-good-key-0123456789'
CANARY = 'sk-leak-canary-aaaa1111'
HOSTS = {'llm.lan': '192.168.1.20', 'llm2.lan': '192.168.1.21', 'loop.lan': '127.0.0.1', 'meta.lan': '169.254.169.254'}


class FakeDB:
    """Just enough of the providers/models/users tables for the routes under test."""

    def __init__(self):
        self.providers: dict[uuid.UUID, dict] = {}
        self.models: dict[uuid.UUID, set[str]] = {}
        self.emails = {ADMIN_ID: 'admin@example.com', MEMBER_ID: 'member@example.com'}

    def add(self, **fields):
        row = {'id': uuid.uuid4(), 'owner_id': ADMIN_ID, 'kind': 'openai_compatible', 'cli': None, 'name': 'P',
               'base_url': 'https://api.example', 'secret_encrypted': b'old', 'secret_tail': None, 'allow_private': False,
               'allow_private_ips': [],
               'status': 'ok', 'last_check_at': None, 'last_error': None, 'created_at': datetime.now(timezone.utc)}
        row.update(fields)
        self.providers[row['id']] = row
        return row

    def only(self):
        assert len(self.providers) == 1, self.providers
        return next(iter(self.providers.values()))


class Conn:
    def __init__(self, db):
        self.db = db

    def _own(self, args):
        row = self.db.providers.get(args[0])
        return dict(row) if row and (len(args) < 2 or row['owner_id'] == args[1]) else None

    async def fetchval(self, query, *args):
        if 'legacy_auth_enabled' in query:
            return True
        raise AssertionError(f'unexpected fetchval: {query}')

    async def fetchrow(self, query, *args):
        db = self.db
        if 'setup_user_id' in query:
            return {'id': ADMIN_ID, 'role': 'admin', 'status': 'active'}
        if 'from bothub.sessions s join bothub.users u' in query:
            now = datetime.now(timezone.utc)
            return {'user_id': MEMBER_ID, 'expires_at': now + timedelta(days=1), 'last_extended_at': now,
                    'role': 'member', 'status': 'active'}
        if query.startswith('select * from bothub.providers where id=$1'):
            return self._own(args)
        if query.startswith('select p.*,u.email from bothub.providers p'):
            row = self._own(args)
            return row and {**row, 'email': db.emails[row['owner_id']]}
        if query.startswith('insert into bothub.providers('):
            keys = ('id', 'owner_id', 'kind', 'name', 'base_url', 'secret_encrypted', 'secret_tail', 'allow_private',
                    'allow_private_ips', 'status')
            row = db.add(**dict(zip(keys, args)))
            row['last_check_at'] = datetime.now(timezone.utc) if row['status'] == 'ok' else None
            return dict(row)
        if query.startswith('update bothub.providers set allow_private=$2,status=$3'):
            row = db.providers[args[0]]
            if row['base_url'] != args[3] or row['secret_encrypted'] != args[4]:
                return None
            if row['status'] != args[2]:
                row['last_check_at'] = None
            row.update(allow_private=args[1], status=args[2], allow_private_ips=list(args[5]), last_error=args[6])
            return {'id': row['id']}
        if query.startswith('update bothub.providers set ') and 'returning *' in query:
            row = db.providers[args[0]]
            for column, index in re.findall(r'(\w+)=\$(\d+)', query.split(' where ')[0]):
                row[column] = args[int(index) - 1]
            return dict(row)
        raise AssertionError(f'unexpected fetchrow: {query}')

    async def fetch(self, query, *args):
        if 'where p.status=\'pending_admin\'' in query:
            rows = [r for r in self.db.providers.values() if r['status'] == 'pending_admin']
            return [{**r, 'email': self.db.emails[r['owner_id']]} for r in sorted(rows, key=lambda r: r['created_at'])]
        if query.startswith('select id from bothub.providers where owner_id=$1'):
            return [{'id': r['id']} for r in self.db.providers.values() if r['owner_id'] == args[0]]
        if query.startswith('select * from bothub.providers where owner_id=$1'):
            return [dict(r) for r in self.db.providers.values() if r['owner_id'] == args[0]]
        raise AssertionError(f'unexpected fetch: {query}')

    async def execute(self, query, *args):
        if query.startswith('insert into bothub.models'):
            self.db.models.setdefault(args[0], set()).add(args[1])
        elif query.startswith('update bothub.models set enabled=false'):
            self.db.models[args[0]] = {n for n in self.db.models.get(args[0], set()) if n in args[1]}
        elif query.startswith('update bothub.providers set status=$2,last_check_at=now(),last_error=$3'):
            self.db.providers[args[0]].update(status=args[1], last_error=args[2], last_check_at=datetime.now(timezone.utc))
        elif query.startswith("update bothub.providers set status='pending_admin'"):
            row = self.db.providers[args[0]]
            if row['allow_private'] and row['status'] in ('ok', 'new', 'error', 'unchecked'):
                row.update(status='pending_admin', last_error=args[1], last_check_at=None)
        else:
            raise AssertionError(f'unexpected execute: {query}')

    @asynccontextmanager
    async def transaction(self):
        yield


class Pool:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def acquire(self):
        yield Conn(self.db)


class Upstream:
    """Provider API stand-in: counts requests and answers by the bearer key."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.behaviour = None  # callable(request) -> Response, or raises

    def __call__(self, request):
        self.requests.append(request)
        if self.behaviour:
            return self.behaviour(request)  # may be a coroutine: httpx.MockTransport awaits it
        if request.headers.get('authorization') == f'Bearer {GOOD_KEY}' or 'good' in request.headers.get('authorization', ''):
            return httpx.Response(200, json={'data': [{'id': 'm1'}, {'id': 'm2'}]})
        return httpx.Response(401, json={'error': {'message': 'bad key ' + request.headers.get('authorization', '')}})


class Env:
    def __init__(self, client, db, upstream):
        self.client, self.db, self.upstream = client, db, upstream
        self.member = {'Cookie': 'bothub_session=member-session', 'Origin': 'https://testserver',
                       'X-CSRF': auth.csrf_token('member-session', 'test-secret')}

    async def create(self, headers=ADMIN, **body):
        body = {'kind': 'openai_compatible', 'name': 'Local', 'base_url': 'https://api.example', 'secret': GOOD_KEY, **body}
        return await self.client.post('/api/providers', json=body, headers=headers)

    async def patch(self, provider, headers=ADMIN, **body):
        return await self.client.patch(f'/api/providers/{provider["id"]}', json=body, headers=headers)

    async def allow(self, provider, allow, **extra):
        """Approval as the PWA sends it: the address and the IPs the admin saw in the list (resolved_ips = DNS now)."""
        body = {'allow': allow, **extra}
        if allow:
            host = urlsplit(provider['base_url'] or '').hostname
            body = {'base_url': provider['base_url'], **({'ips': [self.hosts[host]]} if host in self.hosts else {}), **body}
        return await self.client.patch(f'/api/providers/{provider["id"]}/allow-private', json=body, headers=ADMIN)


@asynccontextmanager
async def make_env(monkeypatch, own=(), forbidden_cidrs=None):
    """App over the in-memory tables. `own` stands in for the subnets of the core's own interfaces."""
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())
    monkeypatch.setenv('BOT_TOKEN_SECRET', 'test-secret')
    monkeypatch.delenv('PROVIDER_PRIVATE_ALLOW', raising=False)
    monkeypatch.delenv('PROVIDER_FORBIDDEN_CIDRS', raising=False)
    if forbidden_cidrs:
        monkeypatch.setenv('PROVIDER_FORBIDDEN_CIDRS', forbidden_cidrs)
    monkeypatch.delenv('BOTHUB_LEGACY_AUTH', raising=False)
    monkeypatch.setattr('bothub.main.own_networks', lambda: tuple(ipaddress.ip_network(n) for n in own))
    upstream = Upstream()
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    hosts = dict(HOSTS)
    dns_calls = []

    async def resolver(host, port):
        dns_calls.append(host)
        if host == 'nowhere.example':
            return []
        if host == 'slow.example':
            await asyncio.sleep(3600)
        return [(None, None, None, None, (hosts.get(host, '8.8.8.8'), port))]

    monkeypatch.setattr('bothub.main._resolve_host', resolver)
    db = FakeDB()
    app = create_app()
    app.state.pool = Pool(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
        result = Env(client, db, upstream)
        result.hosts, result.dns_calls, result.app = hosts, dns_calls, app
        yield result


@pytest.fixture
async def env(monkeypatch):
    async with make_env(monkeypatch) as result:
        yield result


# ---- пункт 1: проверка ключа до сохранения ------------------------------------------------------------------

async def test_create_verifies_the_key_once_and_stores_models_and_tail(env):
    response = await env.create()
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['status'] == 'ok' and body['has_secret'] is True and body['secret_tail'] == '6789'
    assert 'secret_encrypted' not in body and GOOD_KEY not in response.text
    assert len(env.upstream.requests) == 1
    request = env.upstream.requests[0]
    assert request.headers['authorization'] == f'Bearer {GOOD_KEY}' and request.headers['host'] == 'api.example'
    assert request.url.host == '8.8.8.8' and request.url.path == '/v1/models'
    row = env.db.only()
    assert env.db.models[row['id']] == {'m1', 'm2'} and row['last_check_at'] is not None
    assert decrypt_secret(bytes(row['secret_encrypted']), row['id'].bytes).decode() == GOOD_KEY
    # проверка сразу после сохранения укладывается в cooldown: второго пробного запроса нет
    checked = await env.client.post(f'/api/providers/{row["id"]}/check', headers=ADMIN)
    assert checked.status_code == 200 and checked.json()['status'] == 'ok'
    assert len(env.upstream.requests) == 1


@pytest.mark.parametrize('kind,probe_path,header', [
    ('anthropic_api', '/v1/models', 'x-api-key'), ('openai_api', '/v1/models', 'authorization'),
    ('google_api', '/v1beta/models', 'x-goog-api-key')])
async def test_every_provider_kind_is_probed_with_its_own_auth(env, kind, probe_path, header):
    env.upstream.behaviour = lambda request: httpx.Response(200, json={'models': [{'name': 'models/g1'}]} if kind == 'google_api'
                                                            else {'data': [{'id': 'm1'}]})
    body = {'kind': kind, 'name': kind, 'secret': GOOD_KEY}
    response = await env.client.post('/api/providers', json=body, headers=ADMIN)
    assert response.status_code == 201, response.text
    request = env.upstream.requests[0]
    assert request.url.path == probe_path and GOOD_KEY in request.headers[header]
    assert request.url.host == '8.8.8.8' and len(env.upstream.requests) == 1


@pytest.mark.parametrize('secret,tail', [('a' * 11, None), ('abcdefghijkl', 'ijkl'), ('x' * 40 + 'wxyz', 'wxyz'), ('short', None)])
async def test_secret_tail_is_the_last_four_characters_from_twelve(env, secret, tail):
    env.upstream.behaviour = lambda request: httpx.Response(200, json={'data': [{'id': 'm1'}]})
    response = await env.create(secret=secret)
    assert response.status_code == 201 and response.json()['secret_tail'] == tail
    assert env.db.only()['secret_tail'] == tail
    listed = await env.client.get('/api/providers', headers=ADMIN)
    assert listed.json()[0]['secret_tail'] == tail


def reject(status):
    return lambda request: httpx.Response(status, json={'error': 'x'})


def explode(error):
    def behaviour(request):
        raise error
    return behaviour


CODES = [
    ('key_rejected', reject(401), {}), ('key_rejected', reject(403), {}),
    ('unreachable', explode(httpx.ConnectError('refused')), {}), ('unreachable', explode(httpx.ReadTimeout('slow')), {}),
    ('unreachable', reject(500), {}), ('unreachable', reject(503), {}), ('unreachable', reject(429), {}),
    ('unreachable', None, {'base_url': 'https://nowhere.example'}),
    ('incompatible', reject(404), {}), ('incompatible', reject(400), {}),
    ('incompatible', lambda r: httpx.Response(200, text='not json'), {}),
    ('incompatible', lambda r: httpx.Response(200, json={'data': []}), {}),
    ('incompatible', lambda r: httpx.Response(200, json={'unexpected': True}), {}),
    ('incompatible', lambda r: httpx.Response(200, json=['a']), {}),
    ('incompatible', lambda r: httpx.Response(302, headers={'location': 'https://elsewhere.example/'}), {}),
    ('invalid_base_url', None, {'base_url': 'https://loop.lan'}),
    ('invalid_base_url', None, {'base_url': 'https://meta.lan'}),
    ('invalid_base_url', None, {'base_url': 'http://api.example'}),
    ('invalid_base_url', None, {'base_url': 'https://user:pw@api.example'}),
]


@pytest.mark.parametrize('code,behaviour,extra', CODES)
async def test_create_refusal_codes_store_nothing(env, code, behaviour, extra):
    env.upstream.behaviour = behaviour
    response = await env.create(secret=CANARY, **extra)
    assert response.status_code == 422, response.text
    assert response.json()['error'] == code
    assert CANARY not in response.text
    assert not env.db.providers and not env.db.models
    if code == 'invalid_base_url':
        assert not env.upstream.requests  # адрес отклонён до того, как ключ ушёл по сети


@pytest.mark.parametrize('code,behaviour,extra', CODES)
async def test_wrong_key_does_not_overwrite_the_old_one(env, code, behaviour, extra):
    created = await env.create(secret=GOOD_KEY)
    assert created.status_code == 201
    before = dict(env.db.only())
    env.upstream.behaviour = behaviour
    patch = {'secret': CANARY, **({'base_url': extra['base_url']} if 'base_url' in extra else {})}
    response = await env.patch(before, **patch)
    assert response.status_code == 422, response.text
    assert response.json()['error'] == code and CANARY not in response.text
    assert env.db.only() == before  # ключ, хвост, адрес, статус и список моделей остались прежними


async def test_patch_replaces_the_key_after_one_successful_probe(env):
    await env.create(secret=GOOD_KEY)
    provider = dict(env.db.only())
    env.upstream.requests.clear()
    new_key = 'sk-good-replacement-ABCD'
    response = await env.patch(provider, secret=new_key)
    assert response.status_code == 200, response.text
    assert response.json()['secret_tail'] == 'ABCD' and response.json()['status'] == 'ok'
    row = env.db.only()
    assert decrypt_secret(bytes(row['secret_encrypted']), row['id'].bytes).decode() == new_key
    assert len(env.upstream.requests) == 1
    assert env.upstream.requests[0].headers['authorization'] == f'Bearer {new_key}'


async def test_patch_without_secret_does_not_probe(env):
    await env.create()
    env.upstream.requests.clear()
    response = await env.patch(env.db.only(), name='Renamed')
    assert response.status_code == 200 and response.json()['name'] == 'Renamed'
    assert not env.upstream.requests


async def test_force_saves_without_probing(env):
    env.upstream.behaviour = reject(401)
    created = await env.create(secret=CANARY, force=True)
    assert created.status_code == 201, created.text
    assert created.json()['status'] == 'unchecked' and created.json()['secret_tail'] == '1111'
    assert not env.upstream.requests and not env.db.models
    assert env.db.only()['last_check_at'] is None
    # force заменяет ключ у рабочего провайдера и тоже не ходит по сети
    env.db.only().update(status='ok')
    before = len(env.upstream.requests)
    replaced = await env.patch(env.db.only(), secret='another-unverified-key', force=True)
    assert replaced.status_code == 200 and replaced.json()['status'] == 'unchecked'
    assert len(env.upstream.requests) == before and env.db.only()['last_check_at'] is None


async def test_force_does_not_bypass_the_address_rules(env):
    response = await env.create(base_url='https://loop.lan', force=True)
    assert response.status_code == 422 and response.json()['error'] == 'invalid_base_url'
    assert not env.db.providers and not env.upstream.requests


@pytest.mark.parametrize('field,value', [('force', 'yes'), ('force', 1), ('allow_private', 'yes')])
async def test_flags_must_be_booleans(env, field, value):
    response = await env.create(**{field: value})
    assert response.status_code == 400 and not env.db.providers


async def test_force_without_secret_and_unknown_fields_are_refused(env):
    await env.create()
    assert (await env.patch(env.db.only(), force=True)).status_code == 400
    assert (await env.patch(env.db.only(), secret=GOOD_KEY, unknown=1)).status_code == 400


async def test_disabled_provider_stays_disabled_after_key_replacement(env):
    await env.create()
    env.db.only().update(status='disabled')
    response = await env.patch(env.db.only(), secret='sk-good-replacement-ABCD')
    assert response.status_code == 200 and response.json()['status'] == 'disabled'


async def test_secret_never_reaches_logs_responses_or_stored_errors(env, caplog, capsys):
    caplog.set_level('DEBUG')
    texts = []
    for behaviour in (reject(401), explode(httpx.ConnectError('refused')), reject(500),
                      lambda r: httpx.Response(200, text='Bearer ' + r.headers['authorization'])):
        env.upstream.behaviour = behaviour
        texts.append((await env.create(secret=CANARY)).text)
    env.upstream.behaviour = None
    await env.create(secret=CANARY, force=True, name='forced')
    created = await env.create(secret=GOOD_KEY, name='second')
    texts.append(created.text)
    texts.append((await env.patch(env.db.providers[uuid.UUID(created.json()['id'])], secret=CANARY)).text)
    texts.append((await env.client.get('/api/providers', headers=ADMIN)).text)
    out = capsys.readouterr()
    haystack = '\n'.join(texts + [caplog.text, out.out, out.err, repr([r['last_error'] for r in env.db.providers.values()])])
    assert CANARY not in haystack and CANARY.encode() not in b''.join(bytes(r['secret_encrypted']) for r in env.db.providers.values())


# ---- пункт 2: приватные адреса через администратора ------------------------------------------------------------

async def test_member_private_address_waits_for_admin_without_any_probe(env):
    response = await env.create(headers=env.member, base_url='https://llm.lan')
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['status'] == 'pending_admin' and body['allow_private'] is False and body['secret_tail'] == '6789'
    assert not env.upstream.requests and not env.db.models
    row = env.db.only()
    assert row['owner_id'] == MEMBER_ID and row['secret_encrypted'] and row['last_check_at'] is None
    # проверка вручную тоже не ходит по приватному адресу
    checked = await env.client.post(f'/api/providers/{row["id"]}/check', headers=env.member)
    assert checked.status_code == 200 and checked.json()['status'] == 'pending_admin'
    refreshed = await env.client.post('/api/models/refresh', headers=env.member)
    assert refreshed.status_code == 200 and not env.upstream.requests


@pytest.mark.parametrize('flag', [True, False])
async def test_member_cannot_set_the_flag_anywhere(env, flag):
    created = await env.create(headers=env.member, base_url='https://llm.lan', allow_private=flag)
    assert created.status_code == 403 and not env.db.providers
    own = env.db.add(owner_id=MEMBER_ID, base_url='https://llm.lan', status='pending_admin')
    patched = await env.patch(own, headers=env.member, allow_private=flag)
    assert patched.status_code == 403 and env.db.providers[own['id']]['allow_private'] is False
    patched = await env.patch(own, headers=env.member, secret=GOOD_KEY, allow_private=flag)
    assert patched.status_code == 403
    route = await env.client.patch(f'/api/providers/{own["id"]}/allow-private', json={'allow': flag}, headers=env.member)
    assert route.status_code == 403 and env.db.providers[own['id']]['allow_private'] is False
    assert (await env.client.get('/api/admin/provider-requests', headers=env.member)).status_code == 403
    assert not env.upstream.requests


async def test_admin_can_create_a_private_provider_with_the_flag_at_once(env):
    response = await env.create(base_url='https://llm.lan', allow_private=True)
    assert response.status_code == 201, response.text
    assert response.json()['status'] == 'ok' and response.json()['allow_private'] is True
    assert len(env.upstream.requests) == 1 and env.upstream.requests[0].url.host == '192.168.1.20'
    without = await env.create(base_url='https://llm2.lan', name='No flag')
    assert without.status_code == 201 and without.json()['status'] == 'pending_admin'
    assert len(env.upstream.requests) == 1


async def test_admin_approval_runs_one_check_and_sets_the_status(env):
    await env.create(headers=env.member, base_url='https://llm.lan')
    provider = dict(env.db.only())
    response = await env.allow(provider, True, base_url='https://llm.lan')
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {'id', 'name', 'base_url', 'created_at', 'email', 'status', 'allow_private', 'allow_private_ips'}
    assert body['status'] == 'ok' and body['allow_private'] is True and body['email'] == 'member@example.com'
    assert GOOD_KEY not in response.text and 'secret' not in response.text and 'tail' not in response.text
    assert len(env.upstream.requests) == 1 and env.upstream.requests[0].url.host == '192.168.1.20'
    assert env.db.models[provider['id']] == {'m1', 'm2'}
    # отказ после одобрения возвращает провайдера в ожидание, сеть не трогаем
    revoked = await env.allow(provider, False)
    assert revoked.status_code == 200 and revoked.json()['status'] == 'pending_admin' and revoked.json()['allow_private'] is False
    assert len(env.upstream.requests) == 1
    assert env.db.only()['allow_private'] is False and env.db.only()['status'] == 'pending_admin'


async def test_approval_with_a_wrong_key_ends_in_error_not_ok(env):
    await env.create(headers=env.member, base_url='https://llm.lan', secret=CANARY)
    env.upstream.behaviour = reject(401)
    response = await env.allow(env.db.only(), True)
    assert response.status_code == 200 and response.json()['status'] == 'error'
    assert CANARY not in response.text and CANARY not in str(env.db.only()['last_error'])


async def test_admin_list_has_only_the_pending_requests_without_secrets_or_models(env):
    await env.create(headers=env.member, base_url='https://llm.lan', name='Member LLM', secret=CANARY)
    await env.create(base_url='https://llm2.lan', name='Admin LLM', secret=CANARY)
    await env.create(base_url='https://api.example', name='Public')
    response = await env.client.get('/api/admin/provider-requests', headers=ADMIN)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {r['name'] for r in rows} == {'Member LLM', 'Admin LLM'}
    for row in rows:
        assert set(row) == {'id', 'name', 'base_url', 'email', 'created_at', 'allow_private', 'approved_ips', 'resolved_ips',
                            'reapproval'}
    assert {r['email'] for r in rows} == {'member@example.com', 'admin@example.com'}
    for hidden in (CANARY, 'secret', 'models', 'tail', 'status'):
        assert hidden not in response.text.lower()


async def test_forbidden_addresses_stay_forbidden_even_with_the_flag(env):
    response = await env.create(base_url='https://loop.lan', allow_private=True)
    assert response.status_code == 422 and response.json()['error'] == 'invalid_base_url'
    assert (await env.create(base_url='https://meta.lan', allow_private=True)).status_code == 422
    # адрес стал запрещённым уже после создания: флаг не включается
    await env.create(headers=env.member, base_url='https://llm.lan')
    provider = dict(env.db.only())
    env.hosts['llm.lan'] = '127.0.0.1'
    refused = await env.allow(provider, True)
    assert refused.status_code == 422 and refused.json()['error'] == 'invalid_base_url'
    assert env.db.only()['allow_private'] is False and env.db.only()['status'] == 'pending_admin'
    assert not env.upstream.requests


async def test_changing_the_address_resets_the_flag(env):
    await env.create(headers=env.member, base_url='https://llm.lan')
    provider = dict(env.db.only())
    assert (await env.allow(provider, True)).json()['status'] == 'ok'
    probes = len(env.upstream.requests)
    # тот же адрес: флаг сохраняется, ключ проверяется
    same = await env.patch(provider, headers=env.member, secret=GOOD_KEY, base_url='https://llm.lan')
    assert same.status_code == 200 and same.json()['allow_private'] is True and same.json()['status'] == 'ok'
    assert len(env.upstream.requests) == probes + 1
    # другой приватный адрес: флаг сброшен, ждём администратора, запроса нет
    moved = await env.patch(provider, headers=env.member, secret=GOOD_KEY, base_url='https://llm2.lan')
    assert moved.status_code == 200
    assert moved.json()['allow_private'] is False and moved.json()['status'] == 'pending_admin'
    assert env.db.only()['base_url'] == 'https://llm2.lan' and len(env.upstream.requests) == probes + 1
    # публичный адрес: флаг сброшен, проверка проходит обычным путём
    public = await env.patch(provider, headers=env.member, secret=GOOD_KEY, base_url='https://api.example')
    assert public.json()['allow_private'] is False and public.json()['status'] == 'ok'
    assert len(env.upstream.requests) == probes + 2


async def test_admin_patch_flag_applies_to_the_new_address_and_alone(env):
    own = await env.create(base_url='https://api.example')
    provider = dict(env.db.only())
    assert own.status_code == 201
    moved = await env.patch(provider, secret=GOOD_KEY, base_url='https://llm.lan', allow_private=True)
    assert moved.status_code == 200 and moved.json()['status'] == 'ok' and moved.json()['allow_private'] is True
    unflagged = await env.patch(provider, secret=GOOD_KEY, base_url='https://llm2.lan')
    assert unflagged.json()['status'] == 'pending_admin' and unflagged.json()['allow_private'] is False
    probes = len(env.upstream.requests)
    approved = await env.patch(provider, allow_private=True)
    assert approved.status_code == 200 and approved.json()['allow_private'] is True and approved.json()['status'] == 'ok'
    assert len(env.upstream.requests) == probes + 1


async def test_allow_private_route_input_checks(env):
    await env.create(headers=env.member, base_url='https://llm.lan')
    provider = dict(env.db.only())
    for body in ({}, {'allow': 'yes'}, {'allow': 1}, {'allow': True, 'extra': 1}, {'base_url': 'https://llm.lan'},
                 {'allow': True}, {'allow': True, 'base_url': 5}):
        response = await env.client.patch(f'/api/providers/{provider["id"]}/allow-private', json=body, headers=ADMIN)
        assert response.status_code == 400, body
    missing = await env.client.patch(f'/api/providers/{uuid.uuid4()}/allow-private', json={'allow': True}, headers=ADMIN)
    assert missing.status_code == 404
    cli = env.db.add(kind='cli_subscription', cli='claude', base_url=None, secret_encrypted=None, name='CLI')
    assert (await env.allow(cli, True)).status_code == 400
    # участник успел сменить адрес после того, как администратор открыл список
    stale = await env.allow(provider, True, base_url='https://old.example')
    assert stale.status_code == 409
    assert env.db.providers[provider['id']]['allow_private'] is False and not env.upstream.requests


async def test_global_env_allowance_remains_a_working_but_deprecated_path(env, monkeypatch):
    monkeypatch.setenv('PROVIDER_PRIVATE_ALLOW', 'llm.lan')
    response = await env.create(headers=env.member, base_url='https://llm.lan')
    assert response.status_code == 201 and response.json()['status'] == 'ok' and response.json()['allow_private'] is False
    other = await env.create(headers=env.member, base_url='https://llm2.lan', name='Other')
    assert other.json()['status'] == 'pending_admin'
