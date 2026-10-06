"""Лента активности, пауза бота, пауза триггеров (docs/contracts.md, раздел 16). Нужен Postgres из conftest.py.
Чистая логика (курсор, слияние, решения про пропуски): test_activity_pure.py."""
import asyncio
import hashlib
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub import auth
from bothub.launcher_client import FakeLauncherClient, LauncherUnavailable
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}
SENTINEL = 'client-value-4f9a1c'


@pytest.fixture(autouse=True)
def quiet_loops(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_OUTBOX_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_SCHEDULER_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_WORKER_INTERVAL', '3600')


@asynccontextmanager
async def client_for(manual=True, **options):
    """manual: worker, scheduler и outbox заменены пустыми циклами, проходы гоняются руками через app.state."""
    app = create_app(**options)
    if manual:
        async def idle():
            await asyncio.Event().wait()
        app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
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


async def owner_id(app):
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select id from bothub.users where email='fixture@example.com'")


async def make_bot(client, bot_id='alpha', headers=OWNER, **fields):
    response = await client.post('/api/bots', json={'id': bot_id, 'name': bot_id.title(), 'provider': 'fake', 'model': 'fake'} | fields,
                                 headers=headers)
    assert response.status_code in (200, 201), response.text  # 201: контейнерный бот в docker-режиме запускается фоном
    return response.json()


async def wait_bot_idle(client, bot_id='alpha', timeout=3.0):
    async with asyncio.timeout(timeout):
        while True:
            bots = {bot['id']: bot for bot in (await client.get('/api/bots', headers=OWNER)).json()}
            if bots[bot_id]['status'] == 'idle':
                return
            await asyncio.sleep(0.02)


async def make_thread(client, bot_id='alpha', headers=OWNER, title='T'):
    response = await client.post('/api/threads', json={'bot_id': bot_id, 'title': title}, headers=headers)
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()['id'])


async def feed(client, headers=OWNER, **params):
    response = await client.get('/api/activity', params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def feed_codes(client, **params):
    return [(item['kind'], item['title']['code']) for item in (await feed(client, **params))['items']]


async def add_event(con, thread, kind, payload, turn=None):
    seq = await con.fetchval('update bothub.threads set last_seq=last_seq+1 where id=$1 returning last_seq', thread)
    await con.execute('insert into bothub.events(thread_id,seq,turn_id,kind,actor,payload) values($1,$2,$3,$4,$5,$6::jsonb)',
                      thread, seq, turn, kind, 'system', json.dumps(payload))


async def seed(app, owner, bot_id):
    """Одно событие каждого вида у бота bot_id; возвращает идентификаторы для проверок."""
    async with app.state.pool.acquire() as con:
        thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id,title) values($1,$2,'Диалог') returning id", bot_id, owner)
        routine = await con.fetchval("insert into bothub.threads(bot_id,owner_id,title,kind) values($1,$2,'Утро','routine') returning id", bot_id, owner)
        done = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,client,started_at,finished_at) "
                                  "values($1,'p','done','iphone',now()-interval '50 minutes',now()-interval '49 minutes') returning id", thread)
        failed = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,client,started_at,finished_at,error) "
                                    "values($1,'p','error','api',now()-interval '40 minutes',now()-interval '39 minutes','boom ' || $2) returning id", thread, SENTINEL)
        scheduled = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,client,created_at) "
                                       "values($1,'tick','queued','schedule',now()-interval '30 minutes') returning id", routine)
        approval = await con.fetchval("insert into bothub.approvals(thread_id,turn_id,bot_id,risk,title,tool,args,args_hash,status,expires_at,created_at,decided_at) "
                                      "values($1,$2,$3,'pay','Оплатить заказ','mcp__bothub__mac_shell',$4::jsonb,'h','approved',now()+interval '1 hour',"
                                      "now()-interval '20 minutes',now()-interval '19 minutes') returning id", thread, done, bot_id, json.dumps({'cmd': 'pay ' + SENTINEL}))
        await add_event(con, thread, 'browser_step', {'action': 'fill', 'target': '[redacted]', 'url': None, 'value': SENTINEL, 'result': 'ok'}, done)
        await add_event(con, thread, 'browser_step', {'action': 'navigate', 'target': '', 'url': 'https://shop.example/cart', 'value': None, 'result': 'ok'}, done)
        await add_event(con, thread, 'browser_control', {'from': 'bot', 'to': 'human', 'by': str(owner), 'reason': 'takeover'})
        memory = await con.fetchval("insert into bothub.memory(text,bot_id,source,status,owner_id) values('Любит чай','"
                                    + bot_id + "','bot:" + bot_id + "','proposed',$1) returning id", owner)
        procedure = await con.fetchval("insert into bothub.procedures(owner_id,bot_id,name) values($1,$2,$3) returning id", owner, bot_id,
                                       # имя уникально на владельца: у второго бота своё
                                       'Вход в кабинет' if bot_id == 'alpha' else f'Вход в кабинет ({bot_id})')
        run = await con.fetchval("insert into bothub.procedure_runs(procedure_id,procedure_version,bot_id,status,params,started_at,finished_at) "
                                 "values($1,1,$2,'done',$3::jsonb,now()-interval '10 minutes',now()-interval '9 minutes') returning id",
                                 procedure, bot_id, json.dumps({'password': SENTINEL}))
    return {'thread': thread, 'routine': routine, 'done': done, 'failed': failed, 'scheduled': scheduled,
            'approval': approval, 'memory': memory, 'procedure': procedure, 'run': run}


# ---- лента ----

async def test_feed_lists_every_source_newest_first_with_codes_and_links():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        await make_bot(client)
        ids = await seed(app, owner, 'alpha')
        body = await feed(client)
        assert set(body) == {'items', 'next'} and body['next'] is None
        items = body['items']
        assert [item['at'] for item in items] == sorted((item['at'] for item in items), reverse=True)
        assert len({item['id'] for item in items}) == len(items)
        codes = {(item['kind'], item['title']['code']) for item in items}
        assert codes == {('turn', 'turn_started'), ('turn', 'turn_done'), ('turn', 'turn_error'),
                         ('approval', 'approval_requested'), ('approval', 'approval_approved'),
                         ('browser', 'browser_step'), ('takeover', 'takeover_started'), ('schedule', 'schedule_run'),
                         ('procedure', 'procedure_started'), ('procedure', 'procedure_finished'), ('memory', 'memory_proposed')}
        by_code = {}
        for item in items:
            by_code.setdefault(item['title']['code'], []).append(item)
        requested = by_code['approval_requested'][0]
        assert requested['risk'] == 'pay' and requested['bot_id'] == 'alpha' and requested['thread_id'] == str(ids['thread'])
        assert requested['turn_id'] == str(ids['done']) and requested['detail'] == 'Оплатить заказ'
        assert by_code['turn_done'][0]['status'] == 'done' and by_code['turn_error'][0]['status'] == 'error'
        assert by_code['schedule_run'][0]['thread_id'] == str(ids['routine']) and by_code['schedule_run'][0]['title']['params']['name'] == 'Утро'
        run = by_code['procedure_finished'][0]
        assert run['title']['params']['run_id'] == str(ids['run']) and run['title']['params']['procedure_id'] == str(ids['procedure'])
        assert run['title']['params']['name'] == 'Вход в кабинет' and run['status'] == 'done'
        assert by_code['memory_proposed'][0]['detail'] == 'Любит чай' and by_code['memory_proposed'][0]['status'] == 'proposed'
        actions = sorted(item['title']['params']['action'] for item in by_code['browser_step'])
        assert actions == ['fill', 'navigate']
        navigate = next(item for item in by_code['browser_step'] if item['title']['params']['action'] == 'navigate')
        assert navigate['title']['params']['url'] == 'https://shop.example/cart'
        assert SENTINEL not in json.dumps(body), 'значения аргументов, параметров, ввода и текст ошибки в ленту не попадают'


async def test_feed_never_shows_foreign_events():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        member, member_headers = await add_member(client, app)
        await make_bot(client)
        await make_bot(client, 'beta', member_headers)
        mine = await seed(app, owner, 'alpha')
        theirs = await seed(app, member, 'beta')
        await client.post('/api/bots/beta/pause', json={'reason': 'чужая пауза'}, headers=member_headers)
        own = await feed(client)
        foreign = await feed(client, member_headers)
        own_text, foreign_text = json.dumps(own), json.dumps(foreign)
        assert 'beta' not in own_text and 'чужая пауза' not in own_text
        assert all(str(value) not in own_text for value in theirs.values())
        assert 'alpha' not in foreign_text and all(str(value) not in foreign_text for value in mine.values())
        assert own['items'] and foreign['items']
        asked = await feed(client, bot_id='beta')
        assert asked == {'items': [], 'next': None}  # чужой bot_id не открывает ничего


async def test_feed_filters_by_bot_and_kind():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        await make_bot(client)
        await make_bot(client, 'beta')
        await seed(app, owner, 'alpha')
        await seed(app, owner, 'beta')
        everything = await feed(client)
        assert {item['bot_id'] for item in everything['items']} == {'alpha', 'beta'}
        only = await feed(client, bot_id='alpha')
        assert only['items'] and {item['bot_id'] for item in only['items']} == {'alpha'}
        assert {item['kind'] for item in (await feed(client, kind='approval'))['items']} == {'approval'}
        pair = await feed(client, kind='turn,memory')
        assert {item['kind'] for item in pair['items']} == {'turn', 'memory'}
        both = await feed(client, kind='takeover', bot_id='beta')
        assert [(item['kind'], item['bot_id']) for item in both['items']] == [('takeover', 'beta')]
        for kind in ('turn', 'approval', 'browser', 'takeover', 'schedule', 'procedure', 'memory', 'pause'):
            assert (await client.get('/api/activity', params={'kind': kind}, headers=OWNER)).status_code == 200


async def test_feed_validates_parameters_with_error_format_and_no_echo():
    async with client_for() as (client, app):
        for params, detail in (({'kind': 'secret-kind-xyz'}, 'kind'), ({'kind': 'turn,nope'}, 'kind'), ({'limit': '0'}, 'limit'),
                               ({'limit': '101'}, 'limit'), ({'limit': 'abc'}, 'limit'), ({'before': 'garbage-cursor-xyz'}, 'before')):
            response = await client.get('/api/activity', params=params, headers=OWNER)
            assert response.status_code == 422, (params, response.text)
            assert response.json() == {'error': 'invalid', 'detail': detail}, response.text
        assert (await client.get('/api/activity', params={'limit': '100'}, headers=OWNER)).status_code == 200
        assert (await client.get('/api/activity', params={'limit': '1'}, headers=OWNER)).status_code == 200


async def test_feed_pages_by_cursor_without_duplicates_when_times_are_equal():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        await make_bot(client)
        async with app.state.pool.acquire() as con:
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('alpha',$1) returning id", owner)
            await con.execute("insert into bothub.turns(thread_id,prompt,status,started_at,finished_at) "
                              "select $1,'p','done','2026-10-01T10:00:00Z','2026-10-01T10:00:00Z' from generate_series(1,30)", thread)
        seen, cursor, pages = [], None, 0
        while True:
            params = {'limit': 7} | ({'before': cursor} if cursor else {})
            body = await feed(client, **params)
            assert len(body['items']) <= 7
            seen += [item['id'] for item in body['items']]
            pages += 1
            assert pages < 20
            cursor = body['next']
            if cursor is None:
                break
        assert len(seen) == 60 and len(set(seen)) == 60 and pages == 9
        assert seen == sorted(seen, reverse=True)  # одно время у всех: порядок по id
        assert (await feed(client, limit=100))['next'] is None
        assert [item['id'] for item in (await feed(client, limit=100))['items']] == seen


async def test_feed_default_limit_is_50():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        await make_bot(client)
        async with app.state.pool.acquire() as con:
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('alpha',$1) returning id", owner)
            await con.execute("insert into bothub.turns(thread_id,prompt,status,started_at) select $1,'p','running',now()-g*interval '1 second' from generate_series(1,60) g", thread)
        body = await feed(client)
        assert len(body['items']) == 50 and body['next']


async def test_feed_is_owner_only_and_bot_tokens_are_forbidden():
    async with client_for() as (client, app):
        await make_bot(client)
        assert (await client.get('/api/activity', headers=bot_headers('alpha'))).status_code == 403
        assert (await client.get('/api/activity')).status_code == 401


# ---- пауза бота ----

async def test_pause_and_resume_flip_the_bot_and_write_feed_events():
    async with client_for() as (client, app):
        await make_bot(client)
        paused = await client.post('/api/bots/alpha/pause', json={'reason': 'отпуск'}, headers=OWNER)
        assert paused.status_code == 200, paused.text
        body = paused.json()
        assert body['bot_id'] == 'alpha' and body['paused'] is True and body['paused_reason'] == 'отпуск'
        assert body['paused_at'] and body['running_turn'] is None
        listed = next(bot for bot in (await client.get('/api/bots', headers=OWNER)).json() if bot['id'] == 'alpha')
        assert listed['paused'] is True and listed['paused_reason'] == 'отпуск' and listed['paused_at']
        again = await client.post('/api/bots/alpha/pause', json={}, headers=OWNER)
        assert again.status_code == 200 and again.json()['paused_reason'] == 'отпуск'
        resumed = await client.post('/api/bots/alpha/resume', headers=OWNER)
        assert resumed.status_code == 200 and resumed.json()['paused'] is False and resumed.json()['paused_at'] is None
        assert (await client.post('/api/bots/alpha/resume', headers=OWNER)).status_code == 200
        items = (await feed(client, kind='pause'))['items']
        assert [item['title']['code'] for item in items] == ['bot_resumed', 'bot_paused']  # повторы события не пишут
        assert items[1]['title']['params'] == {'reason': 'отпуск'} and items[0]['bot_id'] == 'alpha' and items[0]['kind'] == 'pause'


async def test_pause_validates_the_body_and_reports_the_running_turn():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            running = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,started_at,lease_until) "
                                         "values($1,'p','running',now(),now()+interval '1 hour') returning id", thread)
        for body in ({'reason': 'x' * 201}, {'reason': 5}, {'unknown': 1}):
            response = await client.post('/api/bots/alpha/pause', json=body, headers=OWNER)
            assert response.status_code in (400, 422), (body, response.text)
            assert set(response.json()) == {'error', 'detail'}
        paused = await client.post('/api/bots/alpha/pause', headers=OWNER)  # тело необязательно
        assert paused.status_code == 200 and paused.json()['running_turn'] == str(running)
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select status from bothub.turns where id=$1', running) == 'running'  # пауза не убивает


async def test_message_to_paused_bot_waits_with_one_system_event_and_runs_after_resume(monkeypatch):
    monkeypatch.setenv('BOTHUB_WORKER_INTERVAL', '0.05')
    async with client_for(manual=False) as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        assert (await client.post('/api/bots/alpha/pause', json={'reason': 'ждите'}, headers=OWNER)).status_code == 200
        accepted = await client.post(f'/api/threads/{thread}/turns', json={'prompt': 'сделай'}, headers=OWNER)
        assert accepted.status_code == 200, accepted.text
        turn_id = uuid.UUID(accepted.json()['id'])
        await asyncio.sleep(0.6)  # worker крутится каждые 50 мс и обязан обойти turn
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select status from bothub.turns where id=$1', turn_id) == 'queued'
        events = (await client.get(f'/api/threads/{thread}/events', headers=OWNER)).json()
        notes = [event for event in events if event['kind'] == 'system' and event['payload'].get('code') == 'bot_paused']
        assert len(notes) == 1 and notes[0]['turn_id'] == str(turn_id) and 'на паузе' in notes[0]['payload']['text']
        assert (await client.post('/api/bots/alpha/resume', headers=OWNER)).status_code == 200
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            async with app.state.pool.acquire() as con:
                status = await con.fetchval('select status from bothub.turns where id=$1', turn_id)
            if status == 'done':
                break
            await asyncio.sleep(0.05)
        assert status == 'done'
        events = (await client.get(f'/api/threads/{thread}/events', headers=OWNER)).json()
        assert len([event for event in events if event['kind'] == 'system' and event['payload'].get('code') == 'bot_paused']) == 1


async def test_paused_bot_is_skipped_by_claim_turn_but_other_bots_are_not():
    async with client_for() as (client, app):
        await make_bot(client)
        await make_bot(client, 'beta')
        first, second = await make_thread(client), await make_thread(client, 'beta')
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        await client.post(f'/api/threads/{first}/turns', json={'prompt': 'a'}, headers=OWNER)
        other = await client.post(f'/api/threads/{second}/turns', json={'prompt': 'b'}, headers=OWNER)
        claimed = await app.state.claim_turn()
        assert claimed is not None and str(claimed['id']) == other.json()['id']
        assert await app.state.claim_turn() is None
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        again = await app.state.claim_turn()
        assert again is not None and again['thread_id'] == first


async def test_run_now_and_procedure_run_are_rejected_on_a_paused_bot():
    # проверка паузы запуска процедуры теперь внутри транзакции создания запуска (после require_launcher): лаунчер нужен
    async with client_for(launcher=FakeLauncherClient()) as (client, app):
        await make_bot(client)
        schedule = (await client.post('/api/schedules', json={'bot_id': 'alpha', 'name': 'S', 'kind': 'cron', 'cron': '0 9 * * *', 'prompt': 'go'}, headers=OWNER)).json()
        procedure = await client.post('/api/procedures', json={'name': 'P', 'bot_id': 'alpha', 'params': [],
                                                              'steps': [{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}}]}, headers=OWNER)
        assert procedure.status_code == 201, procedure.text
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        run_now = await client.post(f"/api/schedules/{schedule['id']}/run", headers=OWNER)
        assert run_now.status_code == 409 and run_now.json() == {'error': 'bot_paused', 'detail': 'bot_paused'}
        run = await client.post(f"/api/procedures/{procedure.json()['id']}/run", json={}, headers=OWNER)
        assert run.status_code == 409 and run.json() == {'error': 'bot_paused', 'detail': 'bot_paused'}
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        assert (await client.post(f"/api/schedules/{schedule['id']}/run", headers=OWNER)).status_code == 200
        assert (await client.post(f"/api/procedures/{procedure.json()['id']}/run", json={}, headers=OWNER)).status_code != 409


async def test_pause_all_and_resume_all_touch_only_own_bots():
    async with client_for() as (client, app):
        member, member_headers = await add_member(client, app)
        await make_bot(client)
        await make_bot(client, 'beta')
        await make_bot(client, 'gamma', member_headers)
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            running = await con.fetchval("insert into bothub.turns(thread_id,prompt,status,started_at,lease_until) "
                                         "values($1,'p','running',now(),now()+interval '1 hour') returning id", thread)
        paused = await client.post('/api/bots/pause-all', json={'reason': 'ночь'}, headers=OWNER)
        assert paused.status_code == 200, paused.text
        by_bot = {item['bot_id']: item for item in paused.json()['bots']}
        assert set(by_bot) == {'alpha', 'beta'} and all(item['paused'] for item in by_bot.values())
        assert by_bot['alpha']['running_turn'] == str(running) and by_bot['beta']['running_turn'] is None
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select paused from bothub.bots where id='gamma'") is False
        assert len((await feed(client, kind='pause'))['items']) == 2
        resumed = await client.post('/api/bots/resume-all', headers=OWNER)
        assert resumed.status_code == 200 and {item['bot_id'] for item in resumed.json()['bots']} == {'alpha', 'beta'}
        assert not any(item['paused'] for item in resumed.json()['bots'])
        assert len((await feed(client, kind='pause'))['items']) == 4
        assert (await client.post('/api/bots/pause-all', headers=member_headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select paused from bothub.bots where id='gamma'") is True
            assert await con.fetchval("select paused from bothub.bots where id in ('alpha','beta') and paused") is None


async def test_pause_routes_reject_bot_tokens_and_foreign_owners():
    async with client_for() as (client, app):
        member, member_headers = await add_member(client, app)
        await make_bot(client)
        for path in ('/api/bots/alpha/pause', '/api/bots/alpha/resume', '/api/bots/pause-all', '/api/bots/resume-all'):
            response = await client.post(path, json={}, headers=bot_headers('alpha'))
            assert response.status_code == 403, (path, response.text)
            client.cookies.clear()  # вход участника оставил cookie сессии в клиенте: без очистки запрос не анонимный
            assert (await client.post(path, json={})).status_code == 401
        for path in ('/api/bots/alpha/pause', '/api/bots/alpha/resume'):
            response = await client.post(path, json={}, headers=member_headers)
            assert response.status_code == 404 and response.json()['error'] == 'not_found', (path, response.text)
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select paused from bothub.bots where id='alpha'") is False


# ---- пауза триггеров ----

async def cron_schedule(client, bot_id='alpha', **extra):
    response = await client.post('/api/schedules', json={'bot_id': bot_id, 'name': 'Утро', 'kind': 'cron', 'cron': '*/5 * * * *', 'prompt': 'go'} | extra, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


async def make_due(app, schedule_id, **fields):
    async with app.state.pool.acquire() as con:
        await con.execute("update bothub.schedules set next_run_at=now()-interval '1 minute' where id=$1", uuid.UUID(schedule_id))
        for key, value in fields.items():
            await con.execute(f'update bothub.schedules set {key}=$2 where id=$1', uuid.UUID(schedule_id), value)


async def state_of(app, schedule_id):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow('select s.*, (select count(*) from bothub.turns t join bothub.threads th on th.id=t.thread_id '
                                  "where th.bot_id=s.bot_id and t.client='schedule') as turns, now() as db_now from bothub.schedules s where s.id=$1",
                                  uuid.UUID(schedule_id))


async def tick(app, schedule_id, **fields):
    await make_due(app, schedule_id, **fields)
    await app.state.run_due_schedules()
    return await state_of(app, schedule_id)


async def test_schedule_on_a_paused_bot_creates_no_turn_and_records_the_skip():
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client)
        first = await client.post(f"/api/schedules/{schedule['id']}/run", headers=OWNER)  # тред расписания появился
        thread = first.json()['thread_id']
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        row = await tick(app, schedule['id'])
        assert row['turns'] == 1 and row['skipped_count'] == 1 and row['last_skip_reason'] == 'bot_paused'
        assert row['last_skipped_at'] is not None and row['paused_by_unavailable'] is False
        assert row['next_run_at'] > row['db_now']  # cron сдвинут: пропуск не крутится каждые 30 секунд
        items = (await feed(client, kind='schedule'))['items']
        skipped = [item for item in items if item['title']['code'] == 'schedule_skipped']
        assert len(skipped) == 1 and skipped[0]['bot_id'] == 'alpha' and skipped[0]['thread_id'] == thread
        assert skipped[0]['title']['params'] == {'reason': 'bot_paused', 'count': 1, 'paused': False, 'schedule_id': schedule['id'], 'name': 'Утро'}
        events = (await client.get(f'/api/threads/{thread}/events', headers=OWNER)).json()
        notes = [event for event in events if event['payload'].get('code') == 'schedule_skipped']
        assert len(notes) == 1 and notes[0]['kind'] == 'system' and notes[0]['payload']['reason'] == 'bot_paused'
        listed = next(item for item in (await client.get('/api/schedules', headers=OWNER)).json() if item['id'] == schedule['id'])
        assert listed['skipped_count'] == 1 and listed['last_skip_reason'] == 'bot_paused' and listed['catch_up'] is False
        assert listed['paused_by_unavailable'] is False and listed['last_skipped_at']


async def test_schedule_skips_when_the_mac_executor_is_offline():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,executor) values('mac','Mac','fake','fake',$1,'mac')", owner)
        schedule = await cron_schedule(client, 'mac')
        row = await tick(app, schedule['id'])
        assert row['turns'] == 0 and row['skipped_count'] == 1 and row['last_skip_reason'] == 'executor_unavailable'


async def test_schedule_skips_when_the_provider_is_not_usable():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            provider = await con.fetchval("insert into bothub.providers(owner_id,kind,name,status,secret_encrypted) "
                                          "values($1,'openai_api','API','pending_admin',decode('00','hex')) returning id", owner)
            model = await con.fetchval("insert into bothub.models(provider_id,name) values($1,'gpt-test') returning id", provider)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,provider_id,model_id) values('bound','Bound','codex','gpt-test',$1,$2,$3)",
                              owner, provider, model)
        schedule = await cron_schedule(client, 'bound')
        row = await tick(app, schedule['id'])
        assert row['turns'] == 0 and row['skipped_count'] == 1 and row['last_skip_reason'] == 'provider_unavailable'
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.providers set status='ok' where id=$1", provider)
        row = await tick(app, schedule['id'])
        assert row['turns'] == 1 and row['skipped_count'] == 0


async def test_container_executor_is_checked_through_the_launcher(monkeypatch):
    monkeypatch.setattr('bothub.main.BOT_START_DELAY', 0)
    monkeypatch.setenv('BOTHUB_RUNNER_EXEC', 'docker')
    monkeypatch.setenv('BOTHUB_GATEWAY_TOKEN_SECRET', 'test-gateway-secret')
    launcher = FakeLauncherClient()
    async with client_for(launcher=launcher) as (client, app):
        bot = await make_bot(client)
        assert (bot['container'], bot['status']) == ('starting', 'starting')
        await wait_bot_idle(client)
        assert launcher.bots['alpha'].running
        schedule = await cron_schedule(client)
        launcher.bots['alpha'].running = False
        row = await tick(app, schedule['id'])
        assert row['turns'] == 0 and row['skipped_count'] == 1 and row['last_skip_reason'] == 'executor_unavailable'
        launcher.fail_next(LauncherUnavailable('launcher down'))  # недоступный лаунчер тоже даёт пропуск, а не ошибку прохода
        row = await tick(app, schedule['id'])
        assert row['turns'] == 0 and row['skipped_count'] == 2 and row['last_skip_reason'] == 'executor_unavailable'
        launcher.bots['alpha'].running = True
        row = await tick(app, schedule['id'])
        assert row['turns'] == 1 and row['skipped_count'] == 0


async def test_skip_event_is_throttled_to_one_per_hour_and_five_skips_pause_the_schedule():
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        for number in range(1, 6):
            row = await tick(app, schedule['id'])
            assert row['skipped_count'] == number
        assert row['paused_by_unavailable'] is True
        assert (row['next_run_at'] - row['db_now']).total_seconds() > 14 * 60  # в паузе проверка не чаще раза в 15 минут
        skipped = [item for item in (await feed(client, kind='schedule'))['items'] if item['title']['code'] == 'schedule_skipped']
        assert len(skipped) == 1 and skipped[0]['title']['params']['count'] == 1
        before = await state_of(app, schedule['id'])
        await app.state.run_due_schedules()  # срок ещё не настал: счётчик не растёт
        assert (await state_of(app, schedule['id']))['skipped_count'] == before['skipped_count']
        row = await tick(app, schedule['id'], last_skip_event_at=None)
        assert row['skipped_count'] == 6
        skipped = [item for item in (await feed(client, kind='schedule'))['items'] if item['title']['code'] == 'schedule_skipped']
        assert len(skipped) == 2 and skipped[0]['title']['params']['paused'] is True and skipped[0]['title']['params']['count'] == 6
        listed = next(item for item in (await client.get('/api/schedules', headers=OWNER)).json() if item['id'] == schedule['id'])
        assert listed['paused_by_unavailable'] is True and listed['skipped_count'] == 6


async def test_paused_schedule_resumes_by_itself_without_replaying_missed_runs():
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        for _ in range(5):
            await tick(app, schedule['id'])
        assert (await state_of(app, schedule['id']))['paused_by_unavailable'] is True
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        row = await tick(app, schedule['id'])
        assert row['turns'] == 0, 'пропущенные запуски не догоняются, а catch_up выключен'
        assert row['skipped_count'] == 0 and row['paused_by_unavailable'] is False and row['next_run_at'] > row['db_now']
        resumed = [item for item in (await feed(client, kind='schedule'))['items'] if item['title']['code'] == 'schedule_resumed']
        assert len(resumed) == 1 and resumed[0]['title']['params']['schedule_id'] == schedule['id']
        row = await tick(app, schedule['id'])
        assert row['turns'] == 1  # дальше обычная жизнь: один запуск на срок


async def test_catch_up_runs_exactly_one_turn_after_resume():
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client, catch_up=True)
        assert schedule['catch_up'] is True
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        for _ in range(7):
            await tick(app, schedule['id'])
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        row = await tick(app, schedule['id'])
        assert row['turns'] == 1 and row['skipped_count'] == 0 and row['paused_by_unavailable'] is False
        await app.state.run_due_schedules()
        assert (await state_of(app, schedule['id']))['turns'] == 1


async def test_short_outage_runs_normally_when_the_executor_returns_and_reports_the_resume():
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        await tick(app, schedule['id'])
        await tick(app, schedule['id'])
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        row = await tick(app, schedule['id'])
        assert row['turns'] == 1 and row['skipped_count'] == 0 and row['last_skip_reason'] == 'bot_paused'
        assert [item['title']['code'] for item in (await feed(client, kind='schedule'))['items']].count('schedule_resumed') == 1


async def test_disabled_schedules_are_not_probed():
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client, enabled=False)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        row = await tick(app, schedule['id'])
        assert row['skipped_count'] == 0 and row['turns'] == 0


async def hook_for(client, bot_id='alpha'):
    response = await client.post('/api/schedules', json={'bot_id': bot_id, 'name': 'Хук', 'kind': 'hook', 'prompt': 'event'}, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


async def test_hook_answers_202_without_a_turn_when_the_bot_is_paused_and_resumes_later():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        route, token = f"/hooks/{hook['id']}", {'X-Hook-Token': hook['hook_token']}
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        skipped = await client.post(route, headers=token, json={'order': 7})
        assert skipped.status_code == 202 and skipped.json() == {'status': 'accepted'}, skipped.text
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 0
            assert await con.fetchval("select count(*) from bothub.threads where title='Хук'") == 0
        row = await state_of(app, hook['id'])
        assert row['skipped_count'] == 1 and row['last_skip_reason'] == 'bot_paused' and row['last_skipped_at']
        assert (await client.post(route, headers={'X-Hook-Token': 'wrong'}, json={})).status_code == 403
        assert (await state_of(app, hook['id']))['skipped_count'] == 1  # неверный токен пропуск не пишет
        await client.post(route, headers=token, json={})
        skipped_items = [item for item in (await feed(client, kind='schedule'))['items'] if item['title']['code'] == 'schedule_skipped']
        assert len(skipped_items) == 1  # не чаще одного события в час
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        accepted = await client.post(route, headers=token, json={'order': 8})
        assert accepted.status_code == 202 and accepted.json() == {'status': 'accepted'}  # тело то же, turn в ответе не отдаётся
        assert accepted.content == skipped.content
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 1
        row = await state_of(app, hook['id'])
        assert row['skipped_count'] == 0 and row['paused_by_unavailable'] is False
        assert [item['title']['code'] for item in (await feed(client, kind='schedule'))['items']].count('schedule_resumed') == 1


async def test_hook_marks_the_schedule_after_five_skips_and_skips_for_an_offline_mac():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,executor) values('mac','Mac','fake','fake',$1,'mac')", owner)
        hook = await hook_for(client, 'mac')
        for _ in range(5):
            assert (await client.post(f"/hooks/{hook['id']}", headers={'X-Hook-Token': hook['hook_token']}, json={})).status_code == 202
        row = await state_of(app, hook['id'])
        assert row['skipped_count'] == 5 and row['paused_by_unavailable'] is True and row['last_skip_reason'] == 'executor_unavailable'
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.turns') == 0


async def test_schedule_catch_up_is_settable_and_defaults_to_false():
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client)
        assert schedule['catch_up'] is False and schedule['skipped_count'] == 0 and schedule['paused_by_unavailable'] is False
        assert schedule['last_skipped_at'] is None and schedule['last_skip_reason'] is None
        patched = await client.patch(f"/api/schedules/{schedule['id']}", json={'catch_up': True}, headers=OWNER)
        assert patched.status_code == 200 and patched.json()['catch_up'] is True
        bad = await client.patch(f"/api/schedules/{schedule['id']}", json={'catch_up': 'yes'}, headers=OWNER)
        assert bad.status_code == 400 and set(bad.json()) == {'error', 'detail'}
