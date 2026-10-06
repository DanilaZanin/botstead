"""Процедуры (раздел 14): миграция 017, маршруты CRUD, риск, from-turn, импорт и экспорт, запуски (чтение), доступ.
Нужен Postgres. Чистая логика проверки шагов: test_procedures_pure.py."""
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest

from bothub import auth
from bothub.launcher_client import FakeLauncherClient
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}
SENTINEL = 'client-value-4f9a1c'
CLICK = {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}}
PAY = {'id': 's2', 'action': 'click', 'target': {'role': 'button', 'name': 'Pay now'}}
FILL = {'id': 's3', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': '{{email}}'}


@pytest.fixture(autouse=True)
def local_path(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')


@asynccontextmanager
async def client_for(**options):
    app = create_app(**options)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app


async def add_member(client, app, email='member@example.com'):
    async with app.state.pool.acquire() as con:
        user = await con.fetchval('insert into bothub.users(email,password_hash) values($1,$2) returning id',
                                  email, auth.hash_password('long-password'))
    login = await client.post('/api/auth/login', json={'email': email, 'password': 'long-password'})
    assert login.status_code == 200, login.text
    headers = {'Cookie': f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF': login.json()['csrf_token'],
               'Origin': 'https://testserver'}
    return user, headers


def bot_headers(bot_id):
    digest = hmac.new(b'test-secret', bot_id.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'Bearer bot:{bot_id}:{digest}'}


def body(**fields):
    return {'name': 'Login', 'params': [{'name': 'email'}], 'steps': [CLICK, FILL]} | fields


async def create(client, headers=OWNER, **fields):
    response = await client.post('/api/procedures', json=body(**fields), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def owner_id(app):
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select id from bothub.users where email='fixture@example.com'")


async def make_bot(client, bot_id='alpha', headers=OWNER):
    response = await client.post('/api/bots', json={'id': bot_id, 'name': bot_id.title(), 'provider': 'fake', 'model': 'fake'},
                                 headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def make_thread(client, bot_id='alpha', headers=OWNER):
    response = await client.post('/api/threads', json={'bot_id': bot_id}, headers=headers)
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()['id'])


async def make_turn(app, thread, status='done'):
    async with app.state.pool.acquire() as con:
        return await con.fetchval('insert into bothub.turns(thread_id,prompt,status) values($1,$2,$3) returning id',
                                  thread, 'browse', status)


def event(action, **fields):
    return {'action': action, 'target': '', 'url': None, 'value': None, 'result': 'ok', 'role': None, 'name': None} | fields


async def add_events(app, thread, turn, payloads, bot_id='alpha'):
    async with app.state.pool.acquire() as con:
        for payload in payloads:
            seq = await con.fetchval('update bothub.threads set last_seq=last_seq+1 where id=$1 returning last_seq', thread)
            await con.execute("insert into bothub.events(thread_id,seq,turn_id,kind,actor,payload) "
                              "values($1,$2,$3,'browser_step',$4,$5::jsonb)", thread, seq, turn, f'bot:{bot_id}', json.dumps(payload))


async def add_run(app, procedure, status='done', version=1):
    async with app.state.pool.acquire() as con:
        return await con.fetchval('insert into bothub.procedure_runs(procedure_id,procedure_version,status) values($1,$2,$3) returning id',
                                  uuid.UUID(str(procedure)), version, status)


# --- миграция 017 ----------------------------------------------------------------------------------------------

async def test_migration_017_adds_draft_to_the_status_check():
    async with client_for() as (_, app):
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='017_procedure_draft.sql'") == 1
            owner = await owner_id(app)
            for number, status in enumerate(('draft', 'active', 'archived')):
                await con.execute('insert into bothub.procedures(owner_id,name,status) values($1,$2,$3)', owner, f'p{number}', status)
            with pytest.raises(asyncpg.CheckViolationError):
                await con.execute("insert into bothub.procedures(owner_id,name,status) values($1,'bad','bogus')", owner)
            with pytest.raises(asyncpg.CheckViolationError):
                await con.execute("update bothub.procedures set source='robot' where name='p0'")  # прочие проверки на месте


# --- создание, чтение, список ----------------------------------------------------------------------------------

async def test_create_returns_the_normalized_procedure_and_get_reads_it():
    async with client_for() as (client, app):
        created = await create(client, description='d', steps=[CLICK, PAY, FILL])
        assert created['version'] == 1 and created['source'] == 'human' and created['status'] == 'active'
        assert created['bot_id'] is None and created['owner_id'] == str(await owner_id(app))
        assert [s['risk'] for s in created['steps']] == ['none', 'pay', 'none']
        assert created['params'] == [{'name': 'email', 'type': 'string', 'required': True, 'default': None, 'secret': False}]
        assert set(created['steps'][0]) == {'id', 'action', 'target', 'value', 'secret_ref', 'precondition', 'expect',
                                           'safe_to_retry', 'risk', 'computed_risk'}
        assert [s['computed_risk'] for s in created['steps']] == ['none', 'pay', 'none'] and created['last_run'] is None
        got = await client.get(f"/api/procedures/{created['id']}", headers=OWNER)
        assert got.status_code == 200 and got.json() == created


async def test_list_is_own_only_and_filters_by_bot_and_status():
    async with client_for() as (client, app):
        await make_bot(client)
        _, member = await add_member(client, app)
        mine = await create(client, name='A', bot_id='alpha')
        other = await create(client, name='B')
        archived = await create(client, name='C')
        assert (await client.patch(f"/api/procedures/{archived['id']}", json={'status': 'archived'}, headers=OWNER)).status_code == 200
        foreign = await create(client, member, name='A')  # то же имя у другого владельца допустимо
        names = lambda r: sorted(item['name'] for item in r.json())
        assert names(await client.get('/api/procedures', headers=OWNER)) == ['A', 'B', 'C']
        assert names(await client.get('/api/procedures', params={'bot_id': 'alpha'}, headers=OWNER)) == ['A']
        assert names(await client.get('/api/procedures', params={'status': 'archived'}, headers=OWNER)) == ['C']
        assert names(await client.get('/api/procedures', params={'status': 'active', 'bot_id': 'alpha'}, headers=OWNER)) == ['A']
        seen = (await client.get('/api/procedures', headers=member)).json()
        assert [item['id'] for item in seen] == [foreign['id']]
        assert (await client.get('/api/procedures', params={'status': 'bogus'}, headers=OWNER)).status_code == 400
        assert mine['id'] != foreign['id'] and other['id'] not in str(seen)


@pytest.mark.parametrize('payload,status', [
    ({'steps': [{**FILL, 'target': {'role': 'textbox', 'name': 'Password'}, 'value': SENTINEL}]}, 422),
    ({'steps': [{**FILL, 'value': '{{ghost}}'}]}, 422),
    ({'steps': [{**CLICK, 'id': 's1'}, {**CLICK, 'id': 's1'}]}, 422),
    ({'steps': [{**CLICK, 'action': 'drag'}]}, 422),
    ({'steps': [{'id': 's1', 'action': 'navigate', 'target': {'url': 'http://169.254.169.254/'}}]}, 422),
    ({'steps': [{**CLICK, 'expect': {'url_matches': '(a+)+$'}}]}, 422),
    ({'steps': [{**CLICK, 'expect': {'url_matches': 'x' * 301}}]}, 422),
    ({'steps': [{**CLICK, 'secret_ref': 'vault:a'}]}, 422),
    ({'params': [{'name': 'pw', 'secret': True, 'default': SENTINEL}]}, 422),
    ({'params': [{'name': f'p{n}'} for n in range(51)]}, 422),
    ({'steps': [{**CLICK, 'id': f's{n}'} for n in range(201)]}, 422),
    ({'name': 'x' * 121}, 422),
    ({'name': '   '}, 422),
    ({'description': 'd' * 2001}, 422),
    ({'steps': [{**CLICK, 'target': {'role': 'button', 'name': 'x' * 2001}}]}, 422),
    ({'steps': [{**CLICK, 'surprise': 1}]}, 422),
    ({'owner_id': str(uuid.uuid4())}, 400),
    ({'source': 'bot'}, 400),
    ({'status': 'active'}, 400),
    ({'steps': 'nope'}, 400),
    ({'steps': ['click']}, 400),
])
async def test_create_rejects_what_section_14_forbids_without_echoing_values(payload, status):
    async with client_for() as (client, _):
        response = await client.post('/api/procedures', json=body(**payload), headers=OWNER)
        assert response.status_code == status, response.text
        assert set(response.json()) == {'error', 'detail'} and response.json()['error'] == 'invalid'
        assert SENTINEL not in response.text
        assert (await client.get('/api/procedures', headers=OWNER)).json() == []


async def test_name_is_unique_per_owner_and_bot_must_be_own():
    async with client_for() as (client, app):
        await make_bot(client)
        _, member = await add_member(client, app)
        await create(client, name='Same')
        again = await client.post('/api/procedures', json=body(name='Same'), headers=OWNER)
        assert again.status_code == 409 and again.json()['error'] == 'conflict'
        assert (await client.post('/api/procedures', json=body(name='Same'), headers=member)).status_code == 201
        stolen = await client.post('/api/procedures', json=body(name='X', bot_id='alpha'), headers=member)
        assert stolen.status_code == 404 and stolen.json()['error'] == 'not_found'
        assert (await client.post('/api/procedures', json=body(name='Y', bot_id='nobody'), headers=OWNER)).status_code == 404


# --- PATCH -----------------------------------------------------------------------------------------------------

async def test_patch_bumps_the_version_only_when_steps_or_params_change():
    async with client_for() as (client, _):
        created = await create(client)
        url = f"/api/procedures/{created['id']}"
        renamed = (await client.patch(url, json={'name': 'Renamed', 'description': 'new'}, headers=OWNER)).json()
        assert renamed['version'] == 1 and renamed['name'] == 'Renamed' and renamed['description'] == 'new'
        same = (await client.patch(url, json={'steps': created['steps']}, headers=OWNER)).json()
        assert same['version'] == 1
        steps = (await client.patch(url, json={'steps': [CLICK]}, headers=OWNER)).json()
        assert steps['version'] == 2 and [s['id'] for s in steps['steps']] == ['s1']
        params = (await client.patch(url, json={'params': [{'name': 'email'}, {'name': 'city', 'required': False}]}, headers=OWNER)).json()
        assert params['version'] == 3 and [p['name'] for p in params['params']] == ['email', 'city']
        got = (await client.get(url, headers=OWNER)).json()
        assert got['version'] == 3 and got['name'] == 'Renamed'


async def test_patch_cannot_lower_a_risk_and_checks_steps_against_the_new_params():
    async with client_for() as (client, _):
        created = await create(client, steps=[PAY, FILL])
        url = f"/api/procedures/{created['id']}"
        lowered = await client.patch(url, json={'steps': [{**PAY, 'risk': 'none'}, FILL]}, headers=OWNER)
        assert lowered.status_code == 422 and lowered.json()['detail'].startswith('steps[0].risk: risk_below_computed: ')
        assert (await client.get(url, headers=OWNER)).json()['steps'][0]['risk'] == 'pay'
        raised = (await client.patch(url, json={'steps': [{**CLICK, 'risk': 'delete'}]}, headers=OWNER)).json()
        assert raised['steps'][0]['risk'] == 'delete'
        dropped = await client.patch(url, json={'steps': [FILL], 'params': []}, headers=OWNER)
        assert dropped.status_code == 422
        only_params = await client.patch(url, json={'params': []}, headers=OWNER)  # шаги [CLICK] без параметров: можно
        assert only_params.status_code == 200
        broken = await client.patch(url, json={'params': [{'name': 'x'}], 'steps': [FILL]}, headers=OWNER)
        assert broken.status_code == 422
        assert (await client.get(url, headers=OWNER)).json()['version'] == only_params.json()['version']


async def test_patch_rules_name_conflict_empty_body_and_bot_link():
    async with client_for() as (client, _):
        await make_bot(client)
        first, second = await create(client, name='One'), await create(client, name='Two')
        url = f"/api/procedures/{second['id']}"
        assert (await client.patch(url, json={'name': 'One'}, headers=OWNER)).status_code == 409
        assert (await client.patch(url, json={}, headers=OWNER)).status_code == 400
        assert (await client.patch(url, json={'name': None}, headers=OWNER)).status_code == 400
        assert (await client.patch(url, json={'owner_id': str(uuid.uuid4())}, headers=OWNER)).status_code == 400
        assert (await client.patch(url, json={'bot_id': 'nobody'}, headers=OWNER)).status_code == 404
        linked = (await client.patch(url, json={'bot_id': 'alpha'}, headers=OWNER)).json()
        assert linked['bot_id'] == 'alpha' and linked['version'] == 1
        assert (await client.patch(url, json={'bot_id': None}, headers=OWNER)).json()['bot_id'] is None
        assert first['id'] != second['id']


async def test_a_draft_follows_the_unfilled_steps_and_cannot_be_activated_by_hand():
    async with client_for() as (client, _):
        empty = {'id': 's2', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': None, 'needs_value': True}
        draft = await create(client, params=[], steps=[CLICK, empty])
        assert draft['status'] == 'draft' and draft['steps'][1]['needs_value'] is True
        url = f"/api/procedures/{draft['id']}"
        assert (await client.patch(url, json={'status': 'active'}, headers=OWNER)).status_code == 422
        archived = (await client.patch(url, json={'status': 'archived'}, headers=OWNER)).json()
        assert archived['status'] == 'archived'
        still = (await client.patch(url, json={'description': 'x'}, headers=OWNER)).json()
        assert still['status'] == 'archived'
        filled = (await client.patch(url, json={'steps': [CLICK, {**empty, 'value': 'a@b.c', 'needs_value': False}]}, headers=OWNER)).json()
        assert filled['status'] == 'archived' and filled['version'] == 2
        back = (await client.patch(url, json={'status': 'active'}, headers=OWNER)).json()
        assert back['status'] == 'active'
        broken = (await client.patch(url, json={'steps': [CLICK, empty]}, headers=OWNER)).json()
        assert broken['status'] == 'draft'


# --- DELETE ----------------------------------------------------------------------------------------------------

async def test_delete_removes_the_procedure_with_its_finished_runs_but_not_under_an_active_run():
    async with client_for() as (client, app):
        created = await create(client)
        url = f"/api/procedures/{created['id']}"
        done, failed = await add_run(app, created['id'], 'done'), await add_run(app, created['id'], 'failed')
        waiting = await add_run(app, created['id'], 'waiting_approval')
        blocked = await client.delete(url, headers=OWNER)
        assert blocked.status_code == 409 and blocked.json()['error'] == 'conflict'
        assert (await client.get(url, headers=OWNER)).status_code == 200
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.procedure_runs set status='stopped' where id=$1", waiting)
        assert (await client.delete(url, headers=OWNER)).json() == {'ok': True}
        assert (await client.get(url, headers=OWNER)).status_code == 404
        assert (await client.delete(url, headers=OWNER)).status_code == 404
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.procedure_runs') == 0
        assert done != failed


@pytest.mark.parametrize('status', ['queued', 'running', 'waiting_approval', 'waiting_model', 'waiting_human'])
async def test_every_unfinished_run_status_blocks_delete(status):
    async with client_for() as (client, app):
        created = await create(client)
        await add_run(app, created['id'], status)
        assert (await client.delete(f"/api/procedures/{created['id']}", headers=OWNER)).status_code == 409


# --- from-turn -------------------------------------------------------------------------------------------------

RECORDED = [event('navigate', url='https://example.com/login'), event('snapshot'),
            event('fill', target='[redacted]', role='textbox', name='Email', value='[redacted]', secret=False),
            event('fill', target='[redacted]', role='textbox', name='Password', value='[redacted]', secret=True),
            event('click', target='e5', role='button', name='Next', result='error'),
            event('click', target='e5', role='button', name='Next'),
            event('navigate', url='https://example.com/home'), event('navigate', url='https://example.com/home'),
            event('screenshot')]


async def test_from_turn_collects_the_recorded_steps_into_a_draft():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        turn, other_turn = await make_turn(app, thread), await make_turn(app, thread)
        await add_events(app, thread, turn, RECORDED)
        await add_events(app, thread, other_turn, [event('click', role='link', name='Elsewhere')])
        response = await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'turn_id': str(turn), 'name': 'Recorded'},
                                     headers=OWNER)
        assert response.status_code == 201, response.text
        made = response.json()
        assert made['source'] == 'bot' and made['bot_id'] == 'alpha' and made['status'] == 'draft' and made['version'] == 1
        assert [(s['action'], s['target']) for s in made['steps']] == [
            ('navigate', {'url': 'https://example.com/login'}),
            ('fill', {'role': 'textbox', 'name': 'Email'}),
            ('fill', {'role': 'textbox', 'name': 'Password'}),
            ('click', {'role': 'button', 'name': 'Next'}),
            ('navigate', {'url': 'https://example.com/home'})]
        assert [s['id'] for s in made['steps']] == ['s1', 's2', 's3', 's4', 's5']
        fills = [s for s in made['steps'] if s['action'] == 'fill']
        assert all(s['value'] is None and s['secret_ref'] is None and s['needs_value'] is True for s in fills)
        assert 'needs_secret' not in fills[0] and fills[1]['needs_secret'] is True and fills[1]['risk'] == 'login'
        assert 'Elsewhere' not in response.text and '[redacted]' not in str(made['steps'])
        # без turn_id берётся весь тред
        whole = await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'name': 'Whole'}, headers=OWNER)
        assert whole.status_code == 201 and whole.json()['steps'][-1]['target'] == {'role': 'link', 'name': 'Elsewhere'}
        assert len(whole.json()['steps']) == 6


async def test_a_recorded_draft_is_not_runnable_until_the_values_are_given():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        await add_events(app, thread, turn, [event('fill', role='textbox', name='Email', value='[redacted]')])
        made = (await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'name': 'D'}, headers=OWNER)).json()
        assert made['status'] == 'draft'
        run = await client.post(f"/api/procedures/{made['id']}/run", json={}, headers=OWNER)
        assert run.status_code == 409 and run.json()['error'] == 'not_runnable'
        filled = await client.patch(f"/api/procedures/{made['id']}", json={'steps': [
            {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': 'a@b.c'}]}, headers=OWNER)
        assert filled.json()['status'] == 'active' and filled.json()['version'] == 2


async def test_from_turn_end_to_end_through_the_browser_step_route():
    launcher = FakeLauncherClient()
    async with client_for(launcher=launcher) as (client, app):
        await make_bot(client)
        await launcher.create_bot('alpha', str(await owner_id(app)))
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) "
                                      "values($1,'browse','running',now()+interval '1 hour') returning id", thread)
        base = {'thread_id': str(thread), 'turn_id': str(turn)}
        calls = [{'action': 'navigate', 'url': 'https://example.com/login?next=/a#frag'},
                 {'action': 'snapshot'},
                 {'action': 'fill', 'target': 'e8', 'role': 'textbox', 'name': 'Password'},
                 {'action': 'click', 'target': 'e9', 'role': 'button', 'name': 'Sign in', 'result': 'error'},
                 {'action': 'click', 'target': 'e9', 'role': 'button', 'name': 'Sign in'}]
        for call in calls:
            posted = await client.post('/api/browser/step', json=base | call, headers=bot_headers('alpha'))
            assert posted.status_code == 200, posted.text
        made = await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'turn_id': str(turn), 'name': 'E2E'}, headers=OWNER)
        assert made.status_code == 201, made.text
        steps = made.json()['steps']
        assert [(s['action'], s['risk']) for s in steps] == [('navigate', 'none'), ('fill', 'login'), ('click', 'login')]
        assert steps[0]['target'] == {'url': 'https://example.com/login'}  # без запроса и фрагмента
        assert steps[1]['needs_value'] is True and steps[1]['needs_secret'] is True and steps[1]['value'] is None


async def test_from_turn_errors():
    async with client_for() as (client, app):
        await make_bot(client)
        _, member = await add_member(client, app)
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        post = lambda payload, headers=OWNER: client.post('/api/procedures/from-turn', json=payload, headers=headers)
        # нет событий
        empty = await post({'thread_id': str(thread), 'name': 'E'})
        assert empty.status_code == 422 and set(empty.json()) == {'error', 'detail'}
        # событие без роли и имени (записано до расширения): шаг без цели не воспроизвести
        await add_events(app, thread, turn, [event('click', target='e1')])
        unresolved = await post({'thread_id': str(thread), 'name': 'E'})
        assert unresolved.status_code == 422 and 'target' in unresolved.json()['detail']
        # чужой тред, чужой или несуществующий turn
        assert (await post({'thread_id': str(thread), 'name': 'X'}, member)).status_code == 404
        assert (await post({'thread_id': str(uuid.uuid4()), 'name': 'X'})).status_code == 404
        assert (await post({'thread_id': str(thread), 'turn_id': str(uuid.uuid4()), 'name': 'X'})).status_code == 404
        other_thread = await make_thread(client)
        assert (await post({'thread_id': str(other_thread), 'turn_id': str(turn), 'name': 'X'})).status_code == 404
        # имя, тело
        assert (await post({'thread_id': str(thread), 'name': ' '})).status_code == 422
        assert (await post({'thread_id': 'not-a-uuid', 'name': 'X'})).status_code == 400
        assert (await post({'thread_id': str(thread), 'name': 'X', 'extra': 1})).status_code == 400


async def test_from_turn_name_conflict_and_too_many_steps():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        await add_events(app, thread, turn, [event('click', role='button', name='Ok')])
        first = await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'name': 'N'}, headers=OWNER)
        assert first.status_code == 201
        again = await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'name': 'N'}, headers=OWNER)
        assert again.status_code == 409
        await add_events(app, thread, turn, [event('click', role='button', name=f'b{n}') for n in range(200)])
        many = await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'name': 'Many'}, headers=OWNER)
        assert many.status_code == 422 and '200' in many.json()['detail']


# --- экспорт и импорт ------------------------------------------------------------------------------------------

async def test_export_has_no_ids_and_import_creates_an_import_procedure():
    async with client_for() as (client, app):
        await make_bot(client)
        _, member = await add_member(client, app)
        created = await create(client, bot_id='alpha', steps=[PAY, FILL], description='about')
        exported = await client.get(f"/api/procedures/{created['id']}/export", headers=OWNER)
        assert exported.status_code == 200
        doc = exported.json()
        assert set(doc) == {'format', 'name', 'description', 'params', 'steps'}
        for leaked in (created['id'], created['owner_id'], 'alpha', 'owner_id', 'bot_id'):
            assert leaked not in exported.text
        imported = await client.post('/api/procedures/import', json=doc | {'name': 'Copy'}, headers=OWNER)
        assert imported.status_code == 201, imported.text
        copy = imported.json()
        assert copy['source'] == 'import' and copy['bot_id'] is None and copy['version'] == 1 and copy['id'] != created['id']
        assert copy['steps'] == created['steps'] and copy['params'] == created['params'] and copy['description'] == 'about'
        # чужой владелец принимает тот же файл под своим именем
        theirs = await client.post('/api/procedures/import', json=doc, headers=member)
        assert theirs.status_code == 201 and theirs.json()['owner_id'] != created['owner_id']
        again = await client.post('/api/procedures/import', json=doc, headers=OWNER)
        assert again.status_code == 409


async def test_import_runs_the_full_validation_and_recomputes_the_risk():
    async with client_for() as (client, _):
        lowered = {'name': 'Imp', 'steps': [{**PAY, 'risk': 'none'}]}
        made = await client.post('/api/procedures/import', json=lowered, headers=OWNER)
        assert made.status_code == 201 and made.json()['steps'][0]['risk'] == 'pay'
        bad = {'steps': [{**FILL, 'target': {'role': 'textbox', 'name': 'Password'}, 'value': SENTINEL}], 'params': [{'name': 'email'}]}
        for name, payload, status in (('B1', bad, 422),
                                      ('B2', {'steps': [{'id': 's1', 'action': 'navigate', 'target': {'url': 'http://127.0.0.1/'}}]}, 422),
                                      ('B3', {'steps': [{**CLICK, 'expect': {'url_matches': '(a|aa)+$'}}]}, 422),
                                      ('B4', {'format': 'other/9'}, 422),
                                      ('B5', {'owner_id': str(uuid.uuid4())}, 400),
                                      ('B6', {'bot_id': 'alpha'}, 400),
                                      ('B7', {'id': str(uuid.uuid4())}, 400)):
            response = await client.post('/api/procedures/import', json={'name': name} | payload, headers=OWNER)
            assert response.status_code == status, (name, response.text)
            assert SENTINEL not in response.text and set(response.json()) == {'error', 'detail'}
        assert [p['name'] for p in (await client.get('/api/procedures', headers=OWNER)).json()] == ['Imp']


async def test_import_keeps_a_draft_a_draft():
    async with client_for() as (client, _):
        doc = {'name': 'Draft', 'steps': [{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'},
                                           'value': None, 'needs_value': True}]}
        made = await client.post('/api/procedures/import', json=doc, headers=OWNER)
        assert made.status_code == 201 and made.json()['status'] == 'draft' and made.json()['source'] == 'import'


# --- запуски: чтение и заглушки --------------------------------------------------------------------------------

async def test_runs_list_and_run_reading():
    async with client_for() as (client, app):
        _, member = await add_member(client, app)
        created = await create(client)
        url = f"/api/procedures/{created['id']}/runs"
        assert (await client.get(url, headers=OWNER)).json() == []
        first, second = await add_run(app, created['id'], 'done'), await add_run(app, created['id'], 'failed')
        listed = (await client.get(url, headers=OWNER)).json()
        assert [r['id'] for r in listed] == [str(second), str(first)]  # новые сверху
        assert set(listed[0]) >= {'id', 'procedure_id', 'procedure_version', 'status', 'next_step', 'step_log', 'params', 'error',
                                  'started_at', 'finished_at', 'created_at', 'bot_id', 'thread_id', 'turn_id'}
        one = await client.get(f'/api/procedure-runs/{first}', headers=OWNER)
        assert one.status_code == 200 and one.json()['id'] == str(first) and one.json()['status'] == 'done'
        assert (await client.get(url, headers=member)).status_code == 404
        assert (await client.get(f'/api/procedure-runs/{first}', headers=member)).status_code == 404
        assert (await client.get(f'/api/procedure-runs/{uuid.uuid4()}', headers=OWNER)).status_code == 404
        assert (await client.get(f'/api/procedures/{uuid.uuid4()}/runs', headers=OWNER)).status_code == 404


async def test_run_and_stop_check_access_and_body_first_then_act():
    async with client_for() as (client, app):
        _, member = await add_member(client, app)
        created = await create(client)
        run = await add_run(app, created['id'], 'running')
        start = f"/api/procedures/{created['id']}/run"
        stop = f'/api/procedure-runs/{run}/stop'
        # доступ и тело проверяются раньше действия
        assert (await client.post(start, json={}, headers=member)).status_code == 404
        assert (await client.post(stop, headers=member)).status_code == 404
        assert (await client.post(f'/api/procedure-runs/{uuid.uuid4()}/stop', headers=OWNER)).status_code == 404
        for bad in ({'params': 'x'}, {'thread_id': 'nope'}, {'bot_id': 5}, {'surprise': 1}):
            assert (await client.post(start, json=bad, headers=OWNER)).status_code == 400
        assert (await client.post(start, json={}, headers=bot_headers('alpha'))).status_code in (401, 403)
        archived = await create(client, name='Old')
        await client.patch(f"/api/procedures/{archived['id']}", json={'status': 'archived'}, headers=OWNER)
        refused = await client.post(f"/api/procedures/{archived['id']}/run", json={}, headers=OWNER)
        assert refused.status_code == 409 and refused.json()['error'] == 'not_runnable'
        async with app.state.pool.acquire() as con:  # отказы ничего не создают и не меняют
            assert await con.fetchval('select count(*) from bothub.procedure_runs') == 1
            assert await con.fetchval("select status from bothub.procedure_runs where id=$1", run) == 'running'
        # процедура без бота: запуск записывается сразу как failed no_bot (контейнера не нужно)
        started = await client.post(start, json={'params': {'email': 'a@b.c'}}, headers=OWNER)
        assert started.status_code == 201 and started.json()['status'] == 'failed' and started.json()['error'] == 'no_bot'
        stopped = await client.post(stop, headers=OWNER)
        assert stopped.status_code == 200 and stopped.json()['status'] == 'stopped'
        assert (await client.post(stop, headers=OWNER)).status_code == 409
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.procedure_runs') == 2


async def test_decide_checks_access_and_body_then_applies_the_choice():
    async with client_for() as (client, app):
        _, member = await add_member(client, app)
        created = await create(client)
        run = await add_run(app, created['id'], 'waiting_human')
        url = f'/api/procedure-runs/{run}/decide'
        for bad in ({}, {'action': 'boom'}, {'action': None}, {'action': 'retry', 'extra': 1}):
            assert (await client.post(url, json=bad, headers=OWNER)).status_code == 400, bad
        assert (await client.post(url, json={'action': 'retry'}, headers=member)).status_code == 404
        assert (await client.post(f'/api/procedure-runs/{uuid.uuid4()}/decide', json={'action': 'retry'}, headers=OWNER)).status_code == 404
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select status from bothub.procedure_runs where id=$1', run) == 'waiting_human'
        expected = {'retry': 'running', 'skip': 'done', 'stop': 'stopped'}  # у запуска без снимка шагов skip закрывает его
        for action, status in expected.items():
            target = await add_run(app, created['id'], 'waiting_human')
            response = await client.post(f'/api/procedure-runs/{target}/decide', json={'action': action}, headers=OWNER)
            assert response.status_code == 200 and response.json()['status'] == status, (action, response.text)
            again = await client.post(f'/api/procedure-runs/{target}/decide', json={'action': action}, headers=OWNER)
            assert again.status_code == 409  # после решения запуск уже не ждёт человека


async def test_migration_018_gives_a_run_a_steps_snapshot_that_reading_returns():
    async with client_for() as (client, app):
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='018_procedure_run_steps.sql'") == 1
        created = await create(client)
        plain = await add_run(app, created['id'])
        assert (await client.get(f'/api/procedure-runs/{plain}', headers=OWNER)).json()['steps'] == []  # по умолчанию пустой массив
        snapshot = [{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Old name'}, 'risk': 'none'}]
        async with app.state.pool.acquire() as con:
            run = await con.fetchval('insert into bothub.procedure_runs(procedure_id,procedure_version,steps) values($1,1,$2::jsonb) returning id',
                                     uuid.UUID(created['id']), json.dumps(snapshot))
            with pytest.raises(asyncpg.CheckViolationError):
                await con.execute("insert into bothub.procedure_runs(procedure_id,procedure_version,steps) values($1,1,'{}'::jsonb)",
                                  uuid.UUID(created['id']))
            with pytest.raises(asyncpg.NotNullViolationError):
                await con.execute("insert into bothub.procedure_runs(procedure_id,procedure_version,steps) values($1,1,null)",
                                  uuid.UUID(created['id']))
        # правка процедуры после запуска снимок не меняет
        await client.patch(f"/api/procedures/{created['id']}", json={'steps': [PAY, FILL]}, headers=OWNER)
        got = (await client.get(f'/api/procedure-runs/{run}', headers=OWNER)).json()
        assert got['steps'] == snapshot and got['procedure_version'] == 1
        assert [r['steps'] for r in (await client.get(f"/api/procedures/{created['id']}/runs", headers=OWNER)).json()
                if r['id'] == str(run)] == [snapshot]


async def test_get_and_list_carry_last_run_and_computed_risk():
    async with client_for() as (client, app):
        created = await create(client, steps=[CLICK, PAY, FILL])
        other = await create(client, name='Never run')
        assert created['last_run'] is None
        first, second = await add_run(app, created['id'], 'done'), await add_run(app, created['id'], 'failed')
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.procedure_runs set error='expect_failed', started_at=now() where id=$1", second)
        got = (await client.get(f"/api/procedures/{created['id']}", headers=OWNER)).json()
        assert set(got['last_run']) == {'id', 'status', 'started_at', 'finished_at', 'error'}
        assert got['last_run']['id'] == str(second) and got['last_run']['status'] == 'failed' and got['last_run']['error'] == 'expect_failed'
        assert [s['computed_risk'] for s in got['steps']] == ['none', 'pay', 'none']
        listed = {p['id']: p for p in (await client.get('/api/procedures', headers=OWNER)).json()}
        assert listed[created['id']]['last_run']['id'] == str(second) and listed[other['id']]['last_run'] is None
        exported = (await client.get(f"/api/procedures/{created['id']}/export", headers=OWNER)).text
        assert 'last_run' not in exported and 'computed_risk' not in exported and first != second


async def test_secrets_list_has_names_only_and_only_the_users_own():
    async with client_for() as (client, app):
        await make_bot(client)
        member_id, member = await add_member(client, app)
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            for user, bot, name in ((owner, None, 'gmail'), (owner, 'alpha', 'jobs'), (member_id, None, 'theirs')):
                await con.execute('insert into bothub.secrets(owner_id,bot_id,name,value_encrypted) values($1,$2,$3,$4)',
                                  user, bot, name, b'ciphertext-must-not-leak')
        mine = await client.get('/api/secrets', headers=OWNER)
        assert mine.status_code == 200 and mine.json() == [{'name': 'gmail', 'bot_id': None}, {'name': 'jobs', 'bot_id': 'alpha'}]
        assert 'theirs' not in mine.text and 'ciphertext' not in mine.text
        assert (await client.get('/api/secrets', headers=member)).json() == [{'name': 'theirs', 'bot_id': None}]
        assert (await client.get('/api/secrets', headers=bot_headers('alpha'))).status_code == 403
        client.cookies.clear()  # вход участника оставил cookie сессии в клиенте: без очистки запрос не анонимный
        assert (await client.get('/api/secrets')).status_code == 401


async def test_import_and_from_turn_recompute_the_risk_while_patch_refuses_to_lower_it():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        await add_events(app, thread, turn, [event('click', role='button', name='Pay now')])
        recorded = await client.post('/api/procedures/from-turn', json={'thread_id': str(thread), 'name': 'R'}, headers=OWNER)
        assert recorded.status_code == 201 and recorded.json()['steps'][0]['risk'] == 'pay'
        imported = await client.post('/api/procedures/import', json={'name': 'I', 'steps': [{**PAY, 'risk': 'none'}]}, headers=OWNER)
        assert imported.status_code == 201 and imported.json()['steps'][0]['risk'] == 'pay'
        patched = await client.patch(f"/api/procedures/{recorded.json()['id']}", json={'steps': [{**PAY, 'risk': 'other'}]}, headers=OWNER)
        assert patched.status_code == 422 and patched.json()['detail'].startswith('steps[0].risk: risk_below_computed: ')


# --- доступ ----------------------------------------------------------------------------------------------------

async def test_bot_token_gets_403_and_anonymous_401_on_every_procedure_route():
    async with client_for() as (client, app):
        await make_bot(client)
        created = await create(client)
        run = await add_run(app, created['id'])
        routes = [('GET', '/api/secrets', None), ('POST', f'/api/procedure-runs/{run}/decide', {'action': 'retry'}),
                  ('GET', '/api/procedures', None), ('POST', '/api/procedures', body(name='B')),
                  ('POST', '/api/procedures/from-turn', {'thread_id': str(uuid.uuid4()), 'name': 'B'}),
                  ('POST', '/api/procedures/import', {'name': 'B'}),
                  ('GET', f"/api/procedures/{created['id']}", None), ('PATCH', f"/api/procedures/{created['id']}", {'name': 'B'}),
                  ('DELETE', f"/api/procedures/{created['id']}", None), ('GET', f"/api/procedures/{created['id']}/export", None),
                  ('GET', f"/api/procedures/{created['id']}/runs", None), ('POST', f"/api/procedures/{created['id']}/run", {}),
                  ('GET', f'/api/procedure-runs/{run}', None), ('POST', f'/api/procedure-runs/{run}/stop', None)]
        for method, path, payload in routes:
            kwargs = {} if payload is None else {'json': payload}
            as_bot = await client.request(method, path, headers=bot_headers('alpha'), **kwargs)
            anonymous = await client.request(method, path, **kwargs)
            assert as_bot.status_code == 403, (method, path, as_bot.status_code, as_bot.text)
            assert anonymous.status_code == 401, (method, path, anonymous.status_code)
        assert (await client.get(f"/api/procedures/{created['id']}", headers=OWNER)).status_code == 200


async def test_cookie_session_needs_csrf_on_every_changing_procedure_route():
    async with client_for() as (client, app):
        _, member = await add_member(client, app)
        created = await create(client, member, name='Mine')
        run = await add_run(app, created['id'])
        no_csrf = {'Cookie': member['Cookie']}
        changing = [('POST', '/api/procedures', body(name='B')), ('POST', '/api/procedures/import', {'name': 'B'}),
                    ('POST', '/api/procedures/from-turn', {'thread_id': str(uuid.uuid4()), 'name': 'B'}),
                    ('PATCH', f"/api/procedures/{created['id']}", {'name': 'B'}),
                    ('DELETE', f"/api/procedures/{created['id']}", None),
                    ('POST', f"/api/procedures/{created['id']}/run", {}), ('POST', f'/api/procedure-runs/{run}/stop', None),
                    ('POST', f'/api/procedure-runs/{run}/decide', {'action': 'retry'})]
        for method, path, payload in changing:
            kwargs = {} if payload is None else {'json': payload}
            response = await client.request(method, path, headers=no_csrf, **kwargs)
            assert response.status_code == 403 and response.json()['detail'] == 'csrf', (method, path, response.text)
        assert (await client.get(f"/api/procedures/{created['id']}", headers=no_csrf)).status_code == 200


async def test_a_foreign_owner_gets_404_everywhere_and_cannot_change_anything():
    async with client_for() as (client, app):
        _, member = await add_member(client, app)
        created = await create(client)
        run = await add_run(app, created['id'])
        pid = created['id']
        for method, path, payload in [('GET', f'/api/procedures/{pid}', None), ('PATCH', f'/api/procedures/{pid}', {'name': 'Stolen'}),
                                      ('DELETE', f'/api/procedures/{pid}', None), ('GET', f'/api/procedures/{pid}/export', None),
                                      ('GET', f'/api/procedures/{pid}/runs', None), ('POST', f'/api/procedures/{pid}/run', {}),
                                      ('GET', f'/api/procedure-runs/{run}', None), ('POST', f'/api/procedure-runs/{run}/stop', None),
                                      ('POST', f'/api/procedure-runs/{run}/decide', {'action': 'retry'})]:
            kwargs = {} if payload is None else {'json': payload}
            response = await client.request(method, path, headers=member, **kwargs)
            assert response.status_code == 404 and response.json()['error'] == 'not_found', (method, path, response.text)
        assert (await client.get(f'/api/procedures/{pid}', headers=OWNER)).json()['name'] == 'Login'


# --- бот и пользователь ----------------------------------------------------------------------------------------

async def test_procedures_survive_the_deletion_of_their_bot():
    async with client_for() as (client, app):
        await make_bot(client)
        created = await create(client, bot_id='alpha')
        async with app.state.pool.acquire() as con:
            run = await con.fetchval("insert into bothub.procedure_runs(procedure_id,procedure_version,bot_id,status) "
                                     "values($1,1,'alpha','done') returning id", uuid.UUID(created['id']))
        removed = await client.delete('/api/bots/alpha', headers=OWNER)
        assert removed.status_code == 200, removed.text
        after = (await client.get(f"/api/procedures/{created['id']}", headers=OWNER)).json()
        assert after['bot_id'] is None and after['steps'] == created['steps'] and after['version'] == 1
        assert (await client.get('/api/procedures', params={'bot_id': 'alpha'}, headers=OWNER)).json() == []
        assert (await client.get(f'/api/procedure-runs/{run}', headers=OWNER)).json()['bot_id'] is None


async def test_a_disabled_user_cannot_read_or_change_procedures_and_they_stay_in_place():
    async with client_for() as (client, app):
        member_id, member = await add_member(client, app)
        created = await create(client, member, name='Mine')
        assert (await client.get('/api/procedures', headers=member)).status_code == 200
        disabled = await client.patch(f'/api/users/{member_id}', json={'disabled': True}, headers=OWNER)
        assert disabled.status_code == 200, disabled.text
        for method, path in (('GET', '/api/procedures'), ('GET', f"/api/procedures/{created['id']}"),
                             ('GET', f"/api/procedures/{created['id']}/export"), ('DELETE', f"/api/procedures/{created['id']}")):
            response = await client.request(method, path, headers=member)
            assert response.status_code == 401, (method, path, response.text)
        relogin = await client.post('/api/auth/login', json={'email': 'member@example.com', 'password': 'long-password'})
        assert relogin.status_code in (401, 403)
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.procedures where owner_id=$1', member_id) == 1
        _, other = await add_member(client, app, 'second@example.com')
        assert (await client.get('/api/procedures', headers=other)).json() == []  # чужих процедур у другого пользователя нет


async def test_a_user_with_procedures_cannot_be_deleted_from_the_database():
    async with client_for() as (client, app):
        member_id, member = await add_member(client, app)
        await create(client, member, name='Mine')
        async with app.state.pool.acquire() as con:
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await con.execute('delete from bothub.users where id=$1', member_id)
