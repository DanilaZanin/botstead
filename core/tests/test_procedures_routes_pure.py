"""Маршруты процедур поверх памяти вместо Postgres: логика ответов, версий, статусов и доступа.

SQL здесь подменён простой моделью, поэтому сами запросы проверяют DB-тесты (test_procedures_db.py); этот файл ловит ошибки
маршрутов там, где базы нет."""
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from bothub import auth
from bothub.launcher_client import FakeLauncherClient
from bothub.main import create_app
from bothub.procedure_runner import ProcedureRunner
from procedure_fakes import MemStore

pytestmark = pytest.mark.pure
SECRET = 'procedures-pure-secret'
A, B = uuid.UUID(int=1), uuid.UUID(int=2)
THREAD, TURN = uuid.UUID(int=3), uuid.UUID(int=4)
NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)
CLICK = {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}}
PAY = {'id': 's2', 'action': 'click', 'target': {'role': 'button', 'name': 'Pay now'}}
FILL = {'id': 's3', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': '{{email}}'}
EMPTY = {'id': 's4', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': None, 'needs_value': True}
ACTIVE = ('queued', 'running', 'waiting_approval', 'waiting_model', 'waiting_human')


class Con:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        yield self

    def _owned(self, pid, owner):
        row = self.pool.rows.get(pid)
        return row if row and row['owner_id'] == owner else None

    async def fetchval(self, query, *args):
        if 'select 1 from bothub.bots where id=$1 and owner_id=$2' in query:
            return 1 if self.pool.bots.get(args[0]) == args[1] else None
        if 'select 1 from bothub.turns where id=$1 and thread_id=$2' in query:
            return 1 if args == (TURN, THREAD) else None
        if 'select exists(select 1 from bothub.procedure_runs' in query:
            return any(r['procedure_id'] == args[0] and r['status'] in args[1] for r in self.pool.runs.values())
        if 'bothub.settings' in query:
            return False
        return None

    async def fetchrow(self, query, *args):
        if 'from bothub.sessions s join bothub.users u' in query:
            return self.pool.sessions.get(args[0])
        if 'from bothub.bots b join bothub.users u' in query:
            return {'owner_id': A} if args[0] == 'alpha' else None
        if 'from bothub.threads where id=$1 and owner_id=$2' in query:
            return {'id': THREAD, 'bot_id': 'alpha', 'owner_id': A} if args == (THREAD, A) else None
        if 'select * from bothub.procedures where id=$1 and owner_id=$2' in query:
            row = self._owned(*args)
            return dict(row) if row else None
        if 'select r.* from bothub.procedure_runs r join bothub.procedures p' in query:
            run = self.pool.runs.get(args[0])
            return dict(run) if run and self._owned(run['procedure_id'], args[1]) else None
        if 'insert into bothub.procedures' in query:
            owner, bot_id, name, description, params, steps, source, status = args
            if any(r['owner_id'] == owner and r['name'] == name for r in self.pool.rows.values()):
                raise asyncpg.UniqueViolationError()
            row = {'id': uuid.uuid4(), 'owner_id': owner, 'bot_id': bot_id, 'name': name, 'description': description,
                   'params': json.loads(params), 'steps': json.loads(steps), 'source': source, 'status': status,
                   'version': 1, 'created_at': NOW, 'updated_at': NOW}
            self.pool.rows[row['id']] = row
            return dict(row)
        if 'update bothub.procedures set name=$2' in query:
            pid, name, description, bot_id, params, steps, status, bump = args
            if any(r['owner_id'] == self.pool.rows[pid]['owner_id'] and r['name'] == name and r['id'] != pid
                   for r in self.pool.rows.values()):
                raise asyncpg.UniqueViolationError()
            row = self.pool.rows[pid]
            row.update(name=name, description=description, bot_id=bot_id, params=json.loads(params), steps=json.loads(steps),
                       status=status, version=row['version'] + bump)
            return dict(row)
        raise AssertionError(f'unexpected SQL: {query}')

    async def fetch(self, query, *args):
        if 'select * from bothub.procedures where owner_id=$1' in query:
            owner, bot_id, status = args
            return [dict(r) for r in self.pool.rows.values()
                    if r['owner_id'] == owner and bot_id in (None, r['bot_id']) and status in (None, r['status'])]
        if "from bothub.events where thread_id=$1 and kind='browser_step'" in query:
            return [{'payload': p} for p in self.pool.events]
        if 'select * from bothub.procedure_runs where procedure_id=$1' in query:
            return [dict(r) for r in self.pool.runs.values() if r['procedure_id'] == args[0]]
        if 'select distinct on (procedure_id)' in query and 'from bothub.procedure_runs' in query:
            latest = {}
            for run in sorted(self.pool.runs.values(), key=lambda r: (r.get('created_at', NOW), str(r['id']))):
                if run['procedure_id'] in args[0]:
                    latest[run['procedure_id']] = run
            return [{'procedure_id': r['procedure_id'], 'id': r['id'], 'status': r['status'], 'started_at': r.get('started_at'),
                     'finished_at': r.get('finished_at'), 'error': r.get('error')} for r in latest.values()]
        if 'from bothub.secrets where owner_id=$1' in query:
            return [{'name': r['name'], 'bot_id': r['bot_id']} for r in self.pool.secrets if r['owner_id'] == args[0]]
        raise AssertionError(f'unexpected SQL: {query}')

    async def execute(self, query, *args):
        if 'delete from bothub.procedures where id=$1' in query:
            self.pool.rows.pop(args[0])
            self.pool.runs = {k: r for k, r in self.pool.runs.items() if r['procedure_id'] != args[0]}
            return 'DELETE 1'
        raise AssertionError(f'unexpected SQL: {query}')


class Pool:
    def __init__(self):
        self.rows, self.runs, self.events, self.sessions, self.secrets = {}, {}, [], {}, []
        self.bots = {'alpha': A}
        self.connection = Con(self)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def no_lifespan(app):
    yield


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_LEGACY_AUTH', 'false')
    monkeypatch.setenv('BOT_TOKEN_SECRET', SECRET)
    application = create_app()
    application.state.pool = Pool()
    application.router.lifespan_context = no_lifespan
    return application


def headers(app, owner):
    token = f'session-{owner}'
    app.state.pool.sessions[auth.token_hash(token)] = {
        'user_id': owner, 'expires_at': datetime.now(timezone.utc) + timedelta(days=1),
        'last_extended_at': datetime.now(timezone.utc), 'role': 'member', 'status': 'active'}
    return {'Cookie': f'bothub_session={token}', 'Origin': 'https://testserver', 'X-CSRF': auth.csrf_token(token, SECRET)}


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as http:
        http.a, http.b = headers(app, A), headers(app, B)
        yield http


def proc(**fields):
    return {'name': 'Login', 'params': [{'name': 'email'}], 'steps': [CLICK, FILL]} | fields


async def make(client, who=None, **fields):
    response = await client.post('/api/procedures', json=proc(**fields), headers=who or client.a)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_normalizes_computes_risk_and_reads_back(client):
    made = await make(client, steps=[CLICK, PAY, FILL])
    assert made['version'] == 1 and made['source'] == 'human' and made['status'] == 'active' and made['bot_id'] is None
    assert [s['risk'] for s in made['steps']] == ['none', 'pay', 'none']
    assert (await client.get(f"/api/procedures/{made['id']}", headers=client.a)).json() == made
    assert (await client.get(f"/api/procedures/{made['id']}", headers=client.b)).status_code == 404


async def test_validation_errors_are_422_with_the_top_level_shape_and_no_echo(client):
    sentinel = 'client-value-4f9a1c'
    bad = [{**FILL, 'target': {'role': 'textbox', 'name': 'Password'}, 'value': sentinel}]
    response = await client.post('/api/procedures', json=proc(steps=bad), headers=client.a)
    assert response.status_code == 422 and set(response.json()) == {'error', 'detail'} and sentinel not in response.text
    assert (await client.post('/api/procedures', json=proc(owner_id=str(A)), headers=client.a)).status_code == 400
    assert (await client.post('/api/procedures', json=proc(name=' '), headers=client.a)).status_code == 422
    assert (await client.get('/api/procedures', headers=client.a)).json() == []


async def test_name_conflict_bot_ownership_and_filters(client):
    one = await make(client, name='One', bot_id='alpha')
    await make(client, name='Two')
    assert (await client.post('/api/procedures', json=proc(name='One'), headers=client.a)).status_code == 409
    assert (await client.post('/api/procedures', json=proc(name='X', bot_id='alpha'), headers=client.b)).status_code == 404
    assert [p['id'] for p in (await client.get('/api/procedures?bot_id=alpha', headers=client.a)).json()] == [one['id']]
    assert (await client.get('/api/procedures?status=bogus', headers=client.a)).status_code == 400
    assert (await client.get('/api/procedures', headers=client.b)).json() == []


async def test_patch_version_risk_and_status_rules(client):
    made = await make(client, steps=[PAY, FILL])
    url = f"/api/procedures/{made['id']}"
    assert (await client.patch(url, json={'name': 'R'}, headers=client.a)).json()['version'] == 1
    assert (await client.patch(url, json={'steps': made['steps']}, headers=client.a)).json()['version'] == 1
    lowered = await client.patch(url, json={'steps': [{**PAY, 'risk': 'none'}, FILL]}, headers=client.a)
    assert lowered.status_code == 422 and lowered.json()['detail'].startswith('steps[0].risk: risk_below_computed: ')
    assert (await client.get(url, headers=client.a)).json()['steps'][0]['risk'] == 'pay'
    changed = (await client.patch(url, json={'steps': [CLICK]}, headers=client.a)).json()
    assert changed['version'] == 2
    assert (await client.patch(url, json={'steps': [FILL], 'params': []}, headers=client.a)).status_code == 422
    assert (await client.patch(url, json={}, headers=client.a)).status_code == 400
    assert (await client.patch(url, json={'name': None}, headers=client.a)).status_code == 400
    assert (await client.patch(url, json={'bot_id': 'nobody'}, headers=client.a)).status_code == 404
    assert (await client.patch(url, json={'bot_id': 'alpha'}, headers=client.a)).json()['bot_id'] == 'alpha'
    assert (await client.patch(url, json={'bot_id': None}, headers=client.a)).json()['bot_id'] is None
    assert (await client.patch(url, json={'name': 'x'}, headers=client.b)).status_code == 404


async def test_draft_status_follows_needs_value(client):
    draft = await make(client, params=[], steps=[CLICK, EMPTY])
    url = f"/api/procedures/{draft['id']}"
    assert draft['status'] == 'draft'
    assert (await client.patch(url, json={'status': 'active'}, headers=client.a)).status_code == 422
    assert (await client.patch(url, json={'status': 'archived'}, headers=client.a)).json()['status'] == 'archived'
    filled = (await client.patch(url, json={'steps': [CLICK, {**EMPTY, 'value': 'a@b.c', 'needs_value': False}]}, headers=client.a)).json()
    assert filled['status'] == 'archived' and filled['version'] == 2
    assert (await client.patch(url, json={'status': 'active'}, headers=client.a)).json()['status'] == 'active'
    assert (await client.patch(url, json={'steps': [CLICK, EMPTY]}, headers=client.a)).json()['status'] == 'draft'


async def test_delete_is_blocked_by_an_unfinished_run_and_cascades_the_rest(client, app):
    made = await make(client)
    pid, runs = uuid.UUID(made['id']), app.state.pool.runs
    for status in ('done', 'failed'):
        runs[uuid.uuid4()] = {'id': uuid.uuid4(), 'procedure_id': pid, 'status': status}
    blocker = uuid.uuid4()
    runs[blocker] = {'id': blocker, 'procedure_id': pid, 'status': 'waiting_human'}
    url = f'/api/procedures/{pid}'
    assert (await client.delete(url, headers=client.b)).status_code == 404
    blocked = await client.delete(url, headers=client.a)
    assert blocked.status_code == 409 and blocked.json()['error'] == 'conflict'
    runs[blocker]['status'] = 'stopped'
    assert (await client.delete(url, headers=client.a)).json() == {'ok': True}
    assert app.state.pool.runs == {} and (await client.get(url, headers=client.a)).status_code == 404


async def test_from_turn_builds_a_draft_from_the_recorded_events(client, app):
    def ev(action, **extra):
        return {'action': action, 'target': '', 'url': None, 'value': None, 'result': 'ok', 'role': None, 'name': None} | extra

    app.state.pool.events = [ev('navigate', url='https://example.com/'), ev('snapshot'),
                             ev('fill', role='textbox', name='Password', value='[redacted]', secret=True),
                             ev('click', role='button', name='Go', result='error'), ev('click', role='button', name='Go')]
    response = await client.post('/api/procedures/from-turn', json={'thread_id': str(THREAD), 'turn_id': str(TURN), 'name': 'Rec'},
                                 headers=client.a)
    assert response.status_code == 201, response.text
    made = response.json()
    assert made['source'] == 'bot' and made['bot_id'] == 'alpha' and made['status'] == 'draft'
    assert [s['action'] for s in made['steps']] == ['navigate', 'fill', 'click']
    assert made['steps'][1]['needs_value'] is True and made['steps'][1]['needs_secret'] is True and made['steps'][1]['value'] is None
    assert made['steps'][1]['risk'] == 'login'
    assert (await client.post('/api/procedures/from-turn', json={'thread_id': str(THREAD), 'name': 'Rec'}, headers=client.a)).status_code == 409
    other = {'thread_id': str(THREAD), 'name': 'X'}
    assert (await client.post('/api/procedures/from-turn', json=other, headers=client.b)).status_code == 404
    assert (await client.post('/api/procedures/from-turn', json=other | {'turn_id': str(uuid.uuid4())}, headers=client.a)).status_code == 404
    app.state.pool.events = []
    assert (await client.post('/api/procedures/from-turn', json=other, headers=client.a)).status_code == 422
    app.state.pool.events = [ev('click', target='e1')]
    unresolved = await client.post('/api/procedures/from-turn', json=other, headers=client.a)
    assert unresolved.status_code == 422 and 'target' in unresolved.json()['detail']


async def test_export_import_roundtrip_and_recomputed_risk(client):
    made = await make(client, steps=[PAY, FILL], description='about', bot_id='alpha')
    exported = await client.get(f"/api/procedures/{made['id']}/export", headers=client.a)
    doc = exported.json()
    assert set(doc) == {'format', 'name', 'description', 'params', 'steps'}
    assert made['id'] not in exported.text and str(A) not in exported.text and 'alpha' not in exported.text
    copy = await client.post('/api/procedures/import', json=doc | {'name': 'Copy'}, headers=client.a)
    assert copy.status_code == 201 and copy.json()['source'] == 'import' and copy.json()['bot_id'] is None
    assert copy.json()['steps'] == made['steps']
    lowered = await client.post('/api/procedures/import', json={'name': 'L', 'steps': [{**PAY, 'risk': 'none'}]}, headers=client.a)
    assert lowered.json()['steps'][0]['risk'] == 'pay'
    for payload, status in (({'name': 'a', 'owner_id': str(A)}, 400), ({'name': 'a', 'bot_id': 'alpha'}, 400),
                            ({'name': 'a', 'format': 'x/1'}, 422), ({'name': 'Login'}, 409),
                            ({'name': 'a', 'steps': [{'id': 's1', 'action': 'navigate', 'target': {'url': 'http://10.0.0.1/'}}]}, 422)):
        assert (await client.post('/api/procedures/import', json=payload, headers=client.a)).status_code == status, payload
    assert (await client.get(f"/api/procedures/{made['id']}/export", headers=client.b)).status_code == 404


@pytest.fixture
def replay(app):
    """Воспроизведение поверх памяти: те же строки процедур и запусков, что у пула маршрутов; лаунчер на месте и сверен."""
    pool = app.state.pool
    store = MemStore(pool.runs)
    store.procedures = pool.rows
    store.add_bot('alpha', owner_id=A)
    app.state.launcher, app.state.launcher_ready = FakeLauncherClient(), True
    app.state.procedure_runner = ProcedureRunner(store, lambda: app.state.launcher)
    return store


def run_row(pid, status, **fields):
    return {'id': uuid.uuid4(), 'procedure_id': pid, 'status': status, 'steps': [CLICK], 'step_log': [], 'next_step': 0,
            'approval_id': None, 'reason': None, 'in_flight': False, 'attempt': 0, 'finished_at': None, 'error': None} | fields


async def test_runs_are_read_through_the_owner_of_the_procedure(client, app):
    made = await make(client)
    pid = uuid.UUID(made['id'])
    run_id = uuid.uuid4()
    app.state.pool.runs[run_id] = {'id': run_id, 'procedure_id': pid, 'status': 'running'}
    assert [r['id'] for r in (await client.get(f'/api/procedures/{pid}/runs', headers=client.a)).json()] == [str(run_id)]
    assert (await client.get(f'/api/procedure-runs/{run_id}', headers=client.a)).json()['status'] == 'running'
    assert (await client.get(f'/api/procedure-runs/{run_id}', headers=client.b)).status_code == 404


async def test_run_creates_a_queued_run_with_a_snapshot_and_a_thread(client, app, replay):
    made = await make(client, bot_id='alpha')
    pid = made['id']
    started = await client.post(f'/api/procedures/{pid}/run', json={'params': {'email': 'a@b.c'}}, headers=client.a)
    assert started.status_code == 201, started.text
    run = started.json()
    assert (run['status'], run['procedure_id'], run['procedure_version'], run['bot_id']) == ('queued', pid, 1, 'alpha')
    assert (run['next_step'], run['step_log'], run['error'], run['reason'], run['started_at'], run['finished_at'], run['turn_id']) \
        == (0, [], None, None, None, None, None)
    assert run['params'] == {'email': 'a@b.c'} and run['thread_id'] and [s['id'] for s in run['steps']] == ['s1', 's3']
    assert (await client.get(f"/api/procedure-runs/{run['id']}", headers=client.a)).json() == run
    assert [r['id'] for r in (await client.get(f'/api/procedures/{pid}/runs', headers=client.a)).json()] == [run['id']]
    # снимок: правка процедуры после запуска шаги запуска не меняет
    await client.patch(f'/api/procedures/{pid}', json={'steps': [CLICK]}, headers=client.a)
    assert [s['id'] for s in (await client.get(f"/api/procedure-runs/{run['id']}", headers=client.a)).json()['steps']] == ['s1', 's3']


async def test_run_takes_bot_and_thread_from_the_body_and_checks_them(client, app, replay):
    made = await make(client)
    url = f"/api/procedures/{made['id']}/run"
    own_thread = replay._thread('alpha')
    ok = await client.post(url, json={'bot_id': 'alpha', 'thread_id': str(own_thread), 'params': {'email': 'x'}}, headers=client.a)
    assert ok.status_code == 201 and ok.json()['thread_id'] == str(own_thread)
    for body in ({'bot_id': 'nobody', 'params': {'email': 'x'}},
                 {'bot_id': 'alpha', 'thread_id': str(uuid.uuid4()), 'params': {'email': 'x'}},
                 {'bot_id': 'alpha', 'params': {}}, {'bot_id': 'alpha', 'params': {'email': 1}},
                 {'bot_id': 'alpha', 'params': {'email': 'x', 'unknown': 1}}):
        response = await client.post(url, json=body, headers=client.a)
        assert response.status_code == 400 and response.json()['error'] == 'invalid', body
    for bad in ({'params': 'x'}, {'thread_id': 'nope'}, {'bot_id': 5}, {'surprise': 1}):
        assert (await client.post(url, json=bad, headers=client.a)).status_code == 400, bad
    replay.add_bot('macbot', owner_id=A, executor='mac')
    assert (await client.post(url, json={'bot_id': 'macbot', 'params': {'email': 'x'}}, headers=client.a)).status_code == 400


async def test_run_without_any_bot_is_recorded_as_failed_and_needs_no_launcher(client, app, replay):
    app.state.launcher = None
    made = await make(client)
    started = await client.post(f"/api/procedures/{made['id']}/run", json={'params': {'email': 'x'}}, headers=client.a)
    assert started.status_code == 201
    assert (started.json()['status'], started.json()['error'], started.json()['thread_id']) == ('failed', 'no_bot', None)
    assert app.state.pool.runs[uuid.UUID(started.json()['id'])]['status'] == 'failed'


async def test_run_is_refused_for_drafts_archived_and_foreign_procedures_and_without_a_launcher(client, app, replay):
    made = await make(client, bot_id='alpha')
    url = f"/api/procedures/{made['id']}/run"
    assert (await client.post(url, json={'params': {'email': 'x'}}, headers=client.b)).status_code == 404
    draft = await make(client, name='D', params=[], steps=[EMPTY])
    refused = await client.post(f"/api/procedures/{draft['id']}/run", json={}, headers=client.a)
    assert refused.status_code == 409 and refused.json()['error'] == 'not_runnable'
    await client.patch(f"/api/procedures/{made['id']}", json={'status': 'archived'}, headers=client.a)
    assert (await client.post(url, json={'params': {'email': 'x'}}, headers=client.a)).json()['error'] == 'not_runnable'
    await client.patch(f"/api/procedures/{made['id']}", json={'status': 'active'}, headers=client.a)
    app.state.launcher_ready = False  # лаунчер ещё не сверен (раздел 13): 503 после проверок ресурса и тела
    unavailable = await client.post(url, json={'params': {'email': 'x'}}, headers=client.a)
    assert unavailable.status_code == 503 and unavailable.json()['error'] == 'launcher_unavailable'
    assert (await client.post(url, json={'surprise': 1}, headers=client.a)).status_code == 400
    assert (await client.post(url, json={}, headers=client.b)).status_code == 404
    app.state.launcher_ready, app.state.launcher = True, None
    assert (await client.post(url, json={'params': {'email': 'x'}}, headers=client.a)).status_code == 503
    assert not [r for r in app.state.pool.runs.values()]


async def test_stop_answers_with_the_stopped_run_and_conflicts_when_it_is_over(client, app, replay):
    made = await make(client)
    pid = uuid.UUID(made['id'])
    row = run_row(pid, 'running')
    app.state.pool.runs[row['id']] = row
    url = f"/api/procedure-runs/{row['id']}/stop"
    assert (await client.post(url, headers=client.b)).status_code == 404
    assert (await client.post(f'/api/procedure-runs/{uuid.uuid4()}/stop', headers=client.a)).status_code == 404
    stopped = await client.post(url, headers=client.a)
    assert stopped.status_code == 200 and stopped.json()['status'] == 'stopped' and stopped.json()['finished_at']
    assert stopped.json() == (await client.get(f"/api/procedure-runs/{row['id']}", headers=client.a)).json()
    again = await client.post(url, headers=client.a)
    assert again.status_code == 409 and again.json()['error'] == 'conflict'
    for status in ('queued', 'waiting_approval', 'waiting_human'):
        other = run_row(pid, status)
        app.state.pool.runs[other['id']] = other
        assert (await client.post(f"/api/procedure-runs/{other['id']}/stop", headers=client.a)).json()['status'] == 'stopped'
    for status in ('done', 'failed'):
        over = run_row(pid, status)
        app.state.pool.runs[over['id']] = over
        assert (await client.post(f"/api/procedure-runs/{over['id']}/stop", headers=client.a)).status_code == 409


async def test_bot_token_is_403_anonymous_401_and_cookie_needs_csrf(client, app):
    made = await make(client)
    pid = made['id']
    digest = hmac.new(SECRET.encode(), b'alpha', hashlib.sha256).hexdigest()
    bot = {'Authorization': f'Bearer bot:alpha:{digest}'}
    routes = [('GET', '/api/procedures', None), ('POST', '/api/procedures', proc(name='B')),
              ('POST', '/api/procedures/from-turn', {'thread_id': str(THREAD), 'name': 'B'}),
              ('POST', '/api/procedures/import', {'name': 'B'}), ('GET', f'/api/procedures/{pid}', None),
              ('PATCH', f'/api/procedures/{pid}', {'name': 'B'}), ('DELETE', f'/api/procedures/{pid}', None),
              ('GET', f'/api/procedures/{pid}/export', None), ('GET', f'/api/procedures/{pid}/runs', None),
              ('POST', f'/api/procedures/{pid}/run', {}), ('GET', f'/api/procedure-runs/{uuid.uuid4()}', None),
              ('POST', f'/api/procedure-runs/{uuid.uuid4()}/stop', None),
              ('POST', f'/api/procedure-runs/{uuid.uuid4()}/decide', {'action': 'retry'})]
    for method, path, payload in routes:
        kwargs = {} if payload is None else {'json': payload}
        assert (await client.request(method, path, headers=bot, **kwargs)).status_code == 403, (method, path)
        assert (await client.request(method, path, **kwargs)).status_code == 401, (method, path)
        if method != 'GET':
            no_csrf = {'Cookie': client.a['Cookie']}
            response = await client.request(method, path, headers=no_csrf, **kwargs)
            assert response.status_code == 403 and response.json()['detail'] == 'csrf', (method, path)
    assert (await client.get(f'/api/procedures/{pid}', headers=client.a)).status_code == 200


# --- правки по ревью Opus: computed_risk, last_run, формат ошибок, секреты, decide, снимок шагов ----------------------

async def test_responses_carry_computed_risk_and_last_run(client, app):
    made = await make(client, steps=[CLICK, PAY, FILL])
    assert [s['computed_risk'] for s in made['steps']] == ['none', 'pay', 'none'] and made['last_run'] is None
    pid = uuid.UUID(made['id'])
    old, new = uuid.uuid4(), uuid.uuid4()
    later = NOW + timedelta(hours=1)
    app.state.pool.runs[old] = {'id': old, 'procedure_id': pid, 'status': 'done', 'created_at': NOW, 'started_at': NOW,
                                'finished_at': NOW, 'error': None}
    app.state.pool.runs[new] = {'id': new, 'procedure_id': pid, 'status': 'failed', 'created_at': later, 'started_at': later,
                                'finished_at': None, 'error': 'expect_failed', 'params': {'secret': 'not-for-the-list'}}
    expected = {'id': str(new), 'status': 'failed', 'started_at': later.isoformat(), 'finished_at': None, 'error': 'expect_failed'}
    got = (await client.get(f'/api/procedures/{pid}', headers=client.a)).json()
    assert got['last_run'] == expected and got['steps'][1]['computed_risk'] == 'pay'
    listed = (await client.get('/api/procedures', headers=client.a)).json()
    assert listed[0]['last_run'] == expected and listed[0]['steps'][1]['computed_risk'] == 'pay'
    other = await make(client, name='Other')
    assert {p['name']: p['last_run'] is None for p in (await client.get('/api/procedures', headers=client.a)).json()} == {
        'Login': False, 'Other': True} and other['last_run'] is None


async def test_computed_risk_is_a_floor_and_never_stored_or_exported(client):
    made = await make(client, steps=[{**CLICK, 'risk': 'delete'}, PAY, FILL])
    assert [s['risk'] for s in made['steps']] == ['delete', 'pay', 'none']
    assert [s['computed_risk'] for s in made['steps']] == ['none', 'pay', 'none']
    url = f"/api/procedures/{made['id']}"
    same = await client.patch(url, json={'steps': made['steps']}, headers=client.a)  # то, что клиент получил, уходит обратно
    assert same.status_code == 200 and same.json()['version'] == 1 and same.json()['steps'] == made['steps']
    exported = (await client.get(f'{url}/export', headers=client.a)).text
    assert 'computed_risk' not in exported and 'last_run' not in exported


async def test_lowering_the_risk_below_computed_is_422_with_the_path_on_create_and_patch(client):
    steps = [CLICK, {**PAY, 'risk': 'other'}]
    response = await client.post('/api/procedures', json=proc(steps=steps), headers=client.a)
    assert response.status_code == 422 and set(response.json()) == {'error', 'detail'}
    assert response.json()['detail'].startswith('steps[1].risk: risk_below_computed: ')
    made = await make(client, steps=[PAY])
    patched = await client.patch(f"/api/procedures/{made['id']}", json={'steps': [{**PAY, 'risk': 'none'}]}, headers=client.a)
    assert patched.status_code == 422 and patched.json()['detail'].startswith('steps[0].risk: risk_below_computed: ')


async def test_import_and_from_turn_recompute_the_risk_without_complaint(client, app):
    lowered = await client.post('/api/procedures/import', json={'name': 'L', 'steps': [{**PAY, 'risk': 'none'}]}, headers=client.a)
    assert lowered.status_code == 201 and lowered.json()['steps'][0]['risk'] == 'pay'
    assert lowered.json()['steps'][0]['computed_risk'] == 'pay'
    app.state.pool.events = [{'action': 'click', 'target': '', 'url': None, 'value': None, 'result': 'ok', 'role': 'button',
                              'name': 'Pay now'}]
    recorded = await client.post('/api/procedures/from-turn', json={'thread_id': str(THREAD), 'name': 'R'}, headers=client.a)
    assert recorded.status_code == 201 and recorded.json()['steps'][0]['risk'] == 'pay'


async def test_validation_details_start_with_the_path_and_a_reason_code(client, app):
    sentinel = 'client-value-4f9a1c'
    password = {'role': 'textbox', 'name': 'Password'}
    cases = [
        (proc(steps=[{**FILL, 'target': password, 'value': sentinel}]), 'steps[0].value: secret_required: '),
        (proc(steps=[{**FILL, 'target': {'role': 'textbox', 'name': 'CVV'}, 'value': sentinel}]), 'steps[0].value: secret_required: '),
        (proc(steps=[CLICK, {**PAY, 'risk': 'none'}]), 'steps[1].risk: risk_below_computed: '),
        (proc(params=[{'name': '1bad ' + sentinel}]), 'params[0].name: invalid: '),
        (proc(name=' '), 'name: empty: '),
        (proc(steps=[{**CLICK, 'target': {'role': 'button', 'name': 'a\u202eb' + sentinel}}]), 'steps[0].target.name: control_chars: '),
        (proc(steps=[{**CLICK, 'target': {'selector': '#{{email}}'}}]), 'steps[0].target.selector: param_in_selector: '),
        (proc(steps=[{**CLICK, 'expect': {'url_matches': '(a+)+'}}]), 'steps[0].expect.url_matches: regex_nested: '),
        (proc(steps=[{**CLICK, 'target': {'role': 'button', 'name': '{{nope}}'}}]), 'steps[0].target.name: param_undeclared: '),
    ]
    for body, prefix in cases:
        response = await client.post('/api/procedures', json=body, headers=client.a)
        assert response.status_code == 422, (prefix, response.text)
        assert response.json()['detail'].startswith(prefix), response.json()['detail']
        assert sentinel not in response.text
    app.state.pool.events = []
    empty = await client.post('/api/procedures/from-turn', json={'thread_id': str(THREAD), 'name': 'X'}, headers=client.a)
    assert empty.status_code == 422 and empty.json()['detail'].startswith('steps: empty: ')
    app.state.pool.events = [{'action': 'click', 'target': 'e1', 'result': 'ok'}]
    unresolved = await client.post('/api/procedures/from-turn', json={'thread_id': str(THREAD), 'name': 'X'}, headers=client.a)
    assert unresolved.json()['detail'].startswith('events[0].target: target_missing: ')
    app.state.pool.events = [{'action': 'snapshot', 'result': 'ok'}] * 5001
    many = await client.post('/api/procedures/from-turn', json={'thread_id': str(THREAD), 'name': 'X'}, headers=client.a)
    assert many.status_code == 422 and many.json()['detail'].startswith('steps: too_many: ')
    draft = await make(client, name='Draft', params=[], steps=[CLICK, EMPTY])
    activate = await client.patch(f"/api/procedures/{draft['id']}", json={'status': 'active'}, headers=client.a)
    assert activate.status_code == 422 and activate.json()['detail'].startswith('status: steps_need_values: ')
    bad_import = await client.post('/api/procedures/import', json={'name': 'a', 'format': 'x/1'}, headers=client.a)
    assert bad_import.json()['detail'].startswith('format: unsupported: ')


async def test_a_selector_with_a_param_and_a_hidden_name_are_not_saved(client):
    for target in ({'selector': '#{{email}}'}, {'role': 'button', 'name': 'Pay\u200b'}):
        response = await client.post('/api/procedures', json=proc(steps=[{**CLICK, 'target': target}]), headers=client.a)
        assert response.status_code == 422, target
    assert (await client.get('/api/procedures', headers=client.a)).json() == []


async def test_a_param_in_the_name_raises_the_risk_to_other(client):
    made = await make(client, steps=[{**CLICK, 'target': {'role': 'button', 'name': '{{email}}'}}])
    assert made['steps'][0]['risk'] == 'other' and made['steps'][0]['computed_risk'] == 'other'


async def test_press_after_a_payment_field_inherits_pay_and_keeps_flags(client):
    card = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Card number'}, 'secret_ref': 'vault:card',
            'precondition': {'url_matches': r'^https://shop\.example/'}}
    enter = {'id': 's2', 'action': 'press', 'value': 'Enter'}
    made = await make(client, params=[], steps=[card, enter])
    assert [s['risk'] for s in made['steps']] == ['pay', 'pay'] and made['steps'][1]['flags'] == ['login']
    assert made['steps'][1]['computed_risk'] == 'pay'


async def test_secrets_list_has_only_names_of_the_current_users_secrets(client, app):
    secret = b'ciphertext-must-not-leak'
    app.state.pool.secrets = [
        {'owner_id': A, 'name': 'gmail', 'bot_id': None, 'value_encrypted': secret, 'id': uuid.uuid4()},
        {'owner_id': A, 'name': 'jobs', 'bot_id': 'alpha', 'value_encrypted': secret, 'id': uuid.uuid4()},
        {'owner_id': B, 'name': 'theirs', 'bot_id': None, 'value_encrypted': secret, 'id': uuid.uuid4()}]
    mine = await client.get('/api/secrets', headers=client.a)
    assert mine.status_code == 200
    assert mine.json() == [{'name': 'gmail', 'bot_id': None}, {'name': 'jobs', 'bot_id': 'alpha'}]
    assert 'theirs' not in mine.text and 'ciphertext' not in mine.text and 'value' not in mine.text
    assert (await client.get('/api/secrets', headers=client.b)).json() == [{'name': 'theirs', 'bot_id': None}]
    digest = hmac.new(SECRET.encode(), b'alpha', hashlib.sha256).hexdigest()
    assert (await client.get('/api/secrets', headers={'Authorization': f'Bearer bot:alpha:{digest}'})).status_code == 403
    assert (await client.get('/api/secrets')).status_code == 401


async def test_run_reading_returns_the_steps_snapshot(client, app):
    made = await make(client)
    pid, run_id = uuid.UUID(made['id']), uuid.uuid4()
    snapshot = [{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Old name'}, 'risk': 'none'}]
    app.state.pool.runs[run_id] = {'id': run_id, 'procedure_id': pid, 'status': 'running', 'steps': snapshot}
    assert (await client.get(f'/api/procedure-runs/{run_id}', headers=client.a)).json()['steps'] == snapshot
    assert (await client.get(f'/api/procedures/{pid}/runs', headers=client.a)).json()[0]['steps'] == snapshot


async def test_decide_applies_the_owner_choice_to_a_run_waiting_for_a_human(client, app, replay):
    made = await make(client)
    pid = uuid.UUID(made['id'])
    steps = [CLICK, {**CLICK, 'id': 's2'}]
    waiting = run_row(pid, 'waiting_human', steps=steps, reason='element_not_found')
    app.state.pool.runs[waiting['id']] = waiting
    url = f"/api/procedure-runs/{waiting['id']}/decide"
    skipped = await client.post(url, json={'action': 'skip'}, headers=client.a)
    assert skipped.status_code == 200
    assert (skipped.json()['status'], skipped.json()['next_step'], skipped.json()['reason']) == ('running', 1, None)
    assert skipped.json()['step_log'][0]['status'] == 'skipped' and skipped.json()['step_log'][0]['step_id'] == 's1'
    assert (await client.post(url, json={'action': 'retry'}, headers=client.a)).status_code == 409  # уже не ждёт
    waiting['status'], waiting['reason'] = 'waiting_human', 'unknown_outcome'
    retried = await client.post(url, json={'action': 'retry'}, headers=client.a)
    assert (retried.json()['status'], retried.json()['next_step'], retried.json()['reason']) == ('running', 1, None)
    waiting['status'] = 'waiting_human'
    assert (await client.post(url, json={'action': 'skip'}, headers=client.a)).json()['status'] == 'done'
    third = run_row(pid, 'waiting_human')
    app.state.pool.runs[third['id']] = third
    stopped = await client.post(f"/api/procedure-runs/{third['id']}/decide", json={'action': 'stop'}, headers=client.a)
    assert stopped.json()['status'] == 'stopped'
    running = run_row(pid, 'running')
    app.state.pool.runs[running['id']] = running
    conflict = await client.post(f"/api/procedure-runs/{running['id']}/decide", json={'action': 'retry'}, headers=client.a)
    assert conflict.status_code == 409 and conflict.json()['error'] == 'conflict'


async def test_decide_checks_access_and_body_first(client, app, replay):
    made = await make(client)
    pid = uuid.UUID(made['id'])
    run = run_row(pid, 'waiting_human')
    app.state.pool.runs[run['id']] = run
    url = f"/api/procedure-runs/{run['id']}/decide"
    for bad in ({}, {'action': 'boom'}, {'action': None}, {'action': 'retry', 'extra': 1}, {'action': ['retry']}):
        assert (await client.post(url, json=bad, headers=client.a)).status_code == 400, bad
    assert (await client.post(url, json={'action': 'retry'}, headers=client.b)).status_code == 404
    unknown = f'/api/procedure-runs/{uuid.uuid4()}/decide'
    assert (await client.post(unknown, json={'action': 'retry'}, headers=client.a)).status_code == 404
    assert app.state.pool.runs[run['id']]['status'] == 'waiting_human'
    digest = hmac.new(SECRET.encode(), b'alpha', hashlib.sha256).hexdigest()
    assert (await client.post(url, json={'action': 'retry'}, headers={'Authorization': f'Bearer bot:alpha:{digest}'})).status_code == 403
    assert (await client.post(url, json={'action': 'retry'})).status_code == 401


async def test_decide_without_csrf_is_403_for_a_cookie_session_and_nothing_changes(client, app, replay):
    made = await make(client)
    run = run_row(uuid.UUID(made['id']), 'waiting_human')
    app.state.pool.runs[run['id']] = run
    url = f"/api/procedure-runs/{run['id']}/decide"
    refused = await client.post(url, json={'action': 'stop'}, headers={'Cookie': client.a['Cookie']})
    assert refused.status_code == 403 and refused.json()['detail'] == 'csrf'
    forged = await client.post(url, json={'action': 'stop'}, headers={**client.a, 'X-CSRF': 'forged'})
    assert forged.status_code == 403 and app.state.pool.runs[run['id']]['status'] == 'waiting_human'
    assert (await client.post(url, json={'action': 'stop'}, headers=client.a)).status_code == 200


# --- правки по ревью Opus движка воспроизведения: конкуренция за браузер ----------------------------------------------------

async def test_a_second_run_for_the_same_bot_is_409_conflict_through_the_route(client, app, replay):
    made = await make(client, bot_id='alpha')
    url = f"/api/procedures/{made['id']}/run"
    first = await client.post(url, json={'params': {'email': 'a@b.c'}}, headers=client.a)
    assert first.status_code == 201
    second = await client.post(url, json={'params': {'email': 'a@b.c'}}, headers=client.a)
    assert second.status_code == 409 and second.json()['error'] == 'conflict'
    assert len(app.state.pool.runs) == 1
    app.state.pool.runs[uuid.UUID(first.json()['id'])]['status'] = 'done'
    assert (await client.post(url, json={'params': {'email': 'a@b.c'}}, headers=client.a)).status_code == 201


async def test_runs_are_limited_to_20_per_hour_per_user(client, app, replay):
    mine = await make(client, name='Mine')  # без бота: запуск сразу failed no_bot, бота не занимает
    theirs = await make(client, who=client.b, name='Theirs')
    url, body = f"/api/procedures/{mine['id']}/run", {'params': {'email': 'a@b.c'}}
    for _ in range(20):
        assert (await client.post(url, json=body, headers=client.a)).status_code == 201
    limited = await client.post(url, json=body, headers=client.a)
    assert limited.status_code == 429 and limited.json()['error'] == 'rate_limited'
    assert len([r for r in app.state.pool.runs.values() if r['status'] == 'failed']) == 20  # 21-й не создан
    assert (await client.post(f"/api/procedures/{theirs['id']}/run", json=body, headers=client.b)).status_code == 201
    assert (await client.post(f"/api/procedures/{mine['id']}/run", json={'surprise': 1}, headers=client.a)).status_code in (400, 429)
