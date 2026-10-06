"""Правки по ревью этапа 8, часть с Postgres (нужен conftest.py). Здесь пробы оценщика (s8r), превращённые в постоянные тесты:
изоляция владельцев по всем источникам ленты, курсор и limit, маскирование, ответ hook, запуск процедуры на паузе, память
(`LIKE` и срок), usage, план выборок ленты на чужих данных. Те же правила без БД: test_stage8_review_*_pure.py."""
import base64
import json
import re

import pytest

from bothub import activity
from test_activity_db import (OWNER, add_event, add_member, bot_headers, client_for, cron_schedule, feed, hook_for, make_bot,
                              make_due, owner_id, seed, state_of)


def forged_cursor(text):
    return base64.urlsafe_b64encode(text.encode()).rstrip(b'=').decode()


# ---- изоляция владельцев ----

async def test_feed_never_shows_another_owners_rows_from_any_source():
    async with client_for() as (client, app):
        await make_bot(client, 'alpha')
        member, member_headers = await add_member(client, app)
        await make_bot(client, 'beta', headers=member_headers)
        owner = await owner_id(app)
        await seed(app, owner, 'alpha')
        await seed(app, member, 'beta')
        async with app.state.pool.acquire() as con:
            # процедура без бота и общая память чужого владельца, предложенная его ботом
            procedure = await con.fetchval("insert into bothub.procedures(owner_id,bot_id,name) values($1,null,'NoBotB') returning id", member)
            await con.execute("insert into bothub.procedure_runs(procedure_id,procedure_version,status,params) values($1,1,'queued','{}'::jsonb)", procedure)
            await con.execute("insert into bothub.memory(text,bot_id,source,status,owner_id) values('B shared',null,'bot:beta','proposed',$1)", member)
        await client.post('/api/bots/beta/pause', headers=member_headers, json={'reason': 'B secret reason'})
        mine = (await feed(client, limit='100'))['items']
        leak = [item for item in mine if item['bot_id'] == 'beta' or any(marker in json.dumps(item) for marker in ('NoBotB', 'B shared', 'B secret'))]
        assert not leak and mine
        theirs = (await feed(client, headers=member_headers, limit='100'))['items']
        assert theirs and not [item for item in theirs if item['bot_id'] == 'alpha']
        assert await feed(client, bot_id='beta') == await feed(client, bot_id='nonexistent')  # чужой бот не отличить от несуществующего


# ---- курсор и limit ----

@pytest.mark.parametrize('stamp', ['0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00', '2026-01-01T00:00:00', 'garbage'])
async def test_cursor_that_cannot_be_a_utc_time_is_422_not_500(stamp):
    async with client_for() as (client, app):
        await make_bot(client)
        response = await client.get('/api/activity', params={'before': forged_cursor(f'{stamp}|log:x')}, headers=OWNER)
        assert response.status_code == 422 and response.json() == {'error': 'invalid', 'detail': 'before'}


@pytest.mark.parametrize('value', ['zzz', "x' or 1=1--", 'A' * 5000, forged_cursor("2026-01-01T00:00:00+00:00|x' or '1'='1"),
                                   forged_cursor('2026-01-01T00:00:00+00:00|a\x00b')], ids=['garbage', 'sql', 'huge', 'b64sql', 'nul'])
async def test_other_broken_cursors_are_422(value):
    async with client_for() as (client, app):
        response = await client.get('/api/activity', params={'before': value}, headers=OWNER)
        assert response.status_code == 422, response.text


async def test_a_cursor_far_in_the_future_is_valid_and_returns_the_whole_feed():
    async with client_for() as (client, app):
        await make_bot(client)
        await seed(app, await owner_id(app), 'alpha')
        everything = (await feed(client))['items']
        future = await feed(client, before=forged_cursor('2999-01-01T00:00:00+00:00|log:zzz'))
        assert future['items'] == everything


async def test_limit_is_bounded_and_pages_do_not_lose_or_repeat_rows():
    async with client_for() as (client, app):
        await make_bot(client)
        owner = await owner_id(app)
        for value in ('0', '101', '-1', 'abc', '1e2', '５', ''):
            assert (await client.get('/api/activity', params={'limit': value}, headers=OWNER)).status_code == 422, value
        for value in ('1', '100'):
            assert (await client.get('/api/activity', params={'limit': value}, headers=OWNER)).status_code == 200
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.activity_log(owner_id,bot_id,kind,code,params,at) "
                              "select $1,'alpha','pause','bot_paused','{}'::jsonb,'2026-10-01T00:00:00Z' from generate_series(1,237)", owner)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id,title) values('alpha',$1,'T') returning id", owner)
            await con.execute("insert into bothub.turns(thread_id,prompt,status,client,started_at,finished_at) "
                              "select $1,'p','done','api','2026-10-01T00:00:00Z','2026-10-01T00:00:00Z' from generate_series(1,61)", thread)
        seen, before = [], None
        while True:
            page = await feed(client, **({'limit': '7'} | ({'before': before} if before else {})))
            seen += [item['id'] for item in page['items']]
            before = page['next']
            if not before:
                break
        assert len(seen) == len(set(seen)) == 237 + 61 * 2


# ---- что в ленту не попадает ----

async def test_feed_masks_urls_values_and_arguments_and_keeps_text_short():
    async with client_for() as (client, app):
        await make_bot(client)
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id,title) values('alpha',$1,'T') returning id", owner)
            await add_event(con, thread, 'browser_step', {'action': 'navigate', 'target': 'input[name=password] value=hunter2',
                                                          'url': 'https://u:pw@shop.example/cb?token=SECRET123#frag', 'value': 'VALUESECRET', 'result': 'ok'})
            await add_event(con, thread, 'browser_step', {'action': 'navigate', 'target': '', 'url': 'javascript:alert(1)//?t=SECRETJS', 'result': 'ok'})
            await con.execute("insert into bothub.approvals(thread_id,bot_id,risk,title,tool,args,args_hash,status,expires_at) "
                              "values($1,'alpha','pay','Pay https://bank.example/p?otp=OTPSECRET now','t',$2::jsonb,'h','pending',now()+interval '1 hour')",
                              thread, json.dumps({'card': 'ARGSECRET'}))
            await con.execute("insert into bothub.memory(text,bot_id,source,status,owner_id) values($1,'alpha','bot:alpha','proposed',$2)", 'm' * 5000, owner)
            await con.execute("insert into bothub.activity_log(owner_id,bot_id,kind,code,params) values($1,'alpha','pause','bot_paused',$2::jsonb)",
                              owner, json.dumps({'reason': '<img src=x onerror=alert(1)>', 'extra': 'NOTALLOWED', 'name': {'nested': 'obj'}}))
        body = await feed(client)
        dump = json.dumps(body, ensure_ascii=False)
        for marker in ('hunter2', 'SECRET123', 'VALUESECRET', 'OTPSECRET', 'ARGSECRET', 'pw@', 'NOTALLOWED', 'SECRETJS'):
            assert marker not in dump, marker
        assert all(len(item.get('detail') or '') <= activity.TEXT_MAX for item in body['items'])


# ---- hook ----

async def test_hook_answers_identically_whether_it_started_a_turn_or_skipped():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        route, token = f"/hooks/{hook['id']}", {'X-Hook-Token': hook['hook_token']}
        started = await client.post(route, headers=token, json={'a': 1})
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        skipped = await client.post(route, headers=token, json={'a': 1})
        assert (started.status_code, started.json()) == (skipped.status_code, skipped.json()) == (202, {'status': 'accepted'})
        assert started.content == skipped.content
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 1  # первый создал turn, второй нет


# ---- запуск процедуры и пауза ----

async def queued_run(app, bot_id='alpha'):
    owner = await owner_id(app)
    async with app.state.pool.acquire() as con:
        procedure = await con.fetchval("insert into bothub.procedures(owner_id,bot_id,name,steps) values($1,$2,'P',$3::jsonb) returning id", owner, bot_id,
                                       json.dumps([{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}, 'safe_to_retry': False}]))
        thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id,title,kind) values($1,$2,'P','routine') returning id", bot_id, owner)
        return await con.fetchval("insert into bothub.procedure_runs(procedure_id,procedure_version,bot_id,thread_id,status,params,steps) "
                                  "values($1,1,$2,$3,'queued','{}'::jsonb,$4::jsonb) returning id", procedure, bot_id, thread,
                                  json.dumps([{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}, 'safe_to_retry': False}]))


async def run_row(app, run_id):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow('select * from bothub.procedure_runs where id=$1', run_id)


async def test_a_queued_procedure_run_of_a_paused_bot_is_not_taken_and_shows_bot_paused():
    async with client_for() as (client, app):
        await make_bot(client)
        run = await queued_run(app)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        runner = app.state.procedure_runner
        store = runner.store
        active = [row for row in await store.active_runs() if row['id'] == run]
        assert active and active[0]['bot_paused'] is True  # движок видит паузу в самом списке
        assert await store.begin(run) is False  # и queued не переходит в running
        await runner.tick()
        assert not runner._tasks  # шаг не начат
        row = await run_row(app, run)
        assert (row['status'], row['reason'], row['started_at']) == ('queued', 'bot_paused', None)
        view = await client.get(f'/api/procedure-runs/{run}', headers=OWNER)
        assert view.status_code == 200 and view.json()['status'] == 'queued' and view.json()['reason'] == 'bot_paused'
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        await runner.tick()  # возобновили: причина снята, запуск идёт дальше (шаг дойдёт до лаунчера, которого в тесте нет)
        await runner.shutdown()
        row = await run_row(app, run)
        assert row['reason'] != 'bot_paused'


async def test_resume_gives_waiting_runs_the_time_they_spent_paused():
    async with client_for() as (client, app):
        await make_bot(client)
        run = await queued_run(app)
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.procedure_runs set status='waiting_human', reason='element_not_found', "
                              "waiting_since=now()-interval '20 hours' where id=$1", run)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set paused_at=now()-interval '100 hours' where id='alpha'")  # пауза долгая
            await con.execute("update bothub.procedure_runs set waiting_since=now()-interval '120 hours' where id=$1", run)
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        async with app.state.pool.acquire() as con:
            waited = await con.fetchval('select now() - waiting_since from bothub.procedure_runs where id=$1', run)
        assert 19 <= waited.total_seconds() / 3600 <= 21  # 120 ч ожидания минус 100 ч паузы
        assert (await run_row(app, run))['status'] == 'waiting_human'  # и срок 24 ч его не остановил


async def test_run_procedure_on_a_paused_bot_is_409_from_inside_the_creating_transaction():
    from bothub.launcher_client import FakeLauncherClient
    async with client_for(launcher=FakeLauncherClient()) as (client, app):
        await make_bot(client)
        procedure = await client.post('/api/procedures', json={'name': 'P', 'bot_id': 'alpha', 'params': [],
                                                              'steps': [{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}}]}, headers=OWNER)
        assert procedure.status_code == 201, procedure.text
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        run = await client.post(f"/api/procedures/{procedure.json()['id']}/run", json={}, headers=OWNER)
        assert run.status_code == 409 and run.json() == {'error': 'bot_paused', 'detail': 'bot_paused'}
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.procedure_runs') == 0  # ни запуска, ни треда
            assert await con.fetchval("select count(*) from bothub.threads where kind='routine'") == 0


# ---- расписание: сбой проверки исполнителя ----

async def test_a_failing_executor_check_skips_the_schedule_with_check_failed_and_moves_it(monkeypatch):
    async with client_for() as (client, app):
        await make_bot(client)
        schedule = await cron_schedule(client)
        await make_due(app, schedule['id'])

        def broken(*args, **kwargs):
            raise RuntimeError('check exploded')

        monkeypatch.setattr(activity, 'skip_reason', broken)
        await app.state.run_due_schedules()
        row = await state_of(app, schedule['id'])
        assert row['turns'] == 0 and row['skipped_count'] == 1 and row['last_skip_reason'] == 'check_failed'
        assert row['next_run_at'] > row['db_now']  # сдвинуто на следующий срок: цикл раз в 30 секунд разорван
        await app.state.run_due_schedules()
        assert (await state_of(app, schedule['id']))['skipped_count'] == 1  # срок не настал: повторного пропуска нет


# ---- память ----

@pytest.mark.parametrize('stamp', ['0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00'])
async def test_memory_expiry_outside_utc_is_400_not_500(stamp):
    async with client_for() as (client, app):
        created = await client.post('/api/memory', json={'text': 'fact'}, headers=OWNER)
        assert created.status_code == 200, created.text
        patched = await client.patch(f"/api/memory/{created.json()['id']}", json={'expires_at': stamp}, headers=OWNER)
        assert patched.status_code == 400 and patched.json()['detail'] == 'expires_at'
        posted = await client.post('/api/memory', json={'text': 'fact 2', 'expires_at': stamp}, headers=OWNER)
        assert posted.status_code in (400, 422), posted.text


async def test_memory_search_treats_like_characters_literally_and_scopes_by_owner():
    async with client_for() as (client, app):
        await make_bot(client, 'alpha')
        member, member_headers = await add_member(client, app)
        await make_bot(client, 'beta', headers=member_headers)
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            for text in ['100% sure', 'a_b', 'axb', 'back\\slash', 'plain']:
                await con.execute("insert into bothub.memory(text,status,source,owner_id) values($1,'active','owner',$2)", text, owner)
            await con.execute("insert into bothub.memory(text,status,source,owner_id) values('FOREIGN shared','active','owner',$1)", member)
        for query, expected in (('%', ['100% sure']), ('_', ['a_b']), ('\\', ['back\\slash']), ('a_b', ['a_b']), ('100%', ['100% sure'])):
            response = await client.get('/api/memory', params={'q': query}, headers=OWNER)
            assert response.status_code == 200 and [item['text'] for item in response.json()] == expected, query
        assert 'FOREIGN shared' not in json.dumps((await client.get('/api/memory', headers=bot_headers('alpha'))).json())
        first = (await client.get('/api/memory', headers=OWNER)).json()[0]['id']
        assert (await client.patch(f'/api/memory/{first}', headers=OWNER, json={'bot_id': 'beta'})).status_code == 404  # чужой бот
        assert (await client.delete(f'/api/memory/{first}', headers=member_headers)).status_code == 404  # чужая запись


# ---- usage ----

async def test_usage_summary_is_scoped_to_the_owner_and_validates_days():
    async with client_for() as (client, app):
        await make_bot(client, 'alpha')
        member, member_headers = await add_member(client, app)
        await make_bot(client, 'beta', headers=member_headers, provider='claude', model='secret-model-B')
        async with app.state.pool.acquire() as con:
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id,title) values('beta',$1,'T') returning id", member)
            await con.execute("insert into bothub.usage(bot_id,thread_id,provider,model,tokens_in,tokens_out) values('beta',$1,'claude','secret-model-B',5,5)", thread)
        mine = await client.get('/api/usage/summary', headers=OWNER)
        assert 'secret-model-B' not in mine.text and 'beta' not in mine.text
        for days in ('0', '91', '-1', 'x', '07', '1.5', '9' * 30):
            assert (await client.get('/api/usage/summary', params={'days': days}, headers=OWNER)).status_code == 422, days
        for days in ('1', '90'):
            assert (await client.get('/api/usage/summary', params={'days': days}, headers=OWNER)).status_code == 200, days


# ---- план выборок ленты на чужих данных ----

PLAN_ROWS = re.compile(r'actual time=[\d.]+\.\.[\d.]+ rows=(\d+) loops=(\d+)')
REMOVED = re.compile(r'Rows Removed by (?:Join )?Filter: (\d+)')


async def test_feed_selects_do_not_read_other_owners_rows_by_the_thousand():
    """Два пользователя: у чужого 200 тредов по 1000 событий (200 тыс.) и 100 тыс. ходов, у владельца один тред и 20 событий.
    EXPLAIN ANALYZE каждой выборки: ни один узел скана не отдаёт тысячи строк, фильтр соединения не отбрасывает тысячи."""
    async with client_for() as (client, app):
        await make_bot(client, 'alpha')
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            other = await con.fetchval("insert into bothub.users(email,password_hash) values('other@example.com','x') returning id")
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('obot','O','fake','fake',$1)", other)
            await con.execute("insert into bothub.threads(bot_id,owner_id,title) select 'obot',$1,'t' from generate_series(1,200)", other)
            await con.execute("insert into bothub.events(thread_id,seq,kind,actor,payload,ts) "
                              "select th.id, g, case when g%2=0 then 'browser_step' else 'assistant_msg' end, 'system', '{}'::jsonb, now()-g*interval '1 second' "
                              "from bothub.threads th, generate_series(1,1000) g where th.owner_id=$1", other)
            await con.execute("insert into bothub.turns(thread_id,prompt,status,client,started_at,finished_at,created_at) "
                              "select th.id,'p','done','schedule',now()-g*interval '1 second',now()-g*interval '1 second',now()-g*interval '1 second' "
                              "from bothub.threads th, generate_series(1,500) g where th.owner_id=$1", other)
            await con.execute("insert into bothub.approvals(thread_id,bot_id,risk,title,tool,args,args_hash,status,expires_at,created_at,decided_at) "
                              "select th.id,'obot','other','t','x','{}'::jsonb,'h','approved',now()+interval '1 hour',now()-g*interval '1 second',now() "
                              "from bothub.threads th, generate_series(1,100) g where th.owner_id=$1", other)
            mine = await con.fetchval("insert into bothub.threads(bot_id,owner_id,title) values('alpha',$1,'m') returning id", owner)
            await con.execute("insert into bothub.events(thread_id,seq,kind,actor,payload,ts) "
                              "select $1,g,'browser_step','system','{}'::jsonb, now()-interval '30 days'-g*interval '1 second' from generate_series(1,20) g", mine)
            await con.execute('analyze')
            for source in ('browser_step', 'browser_control', 'turn', 'approval', 'schedule_run', 'procedure', 'memory'):
                plan = '\n'.join(row[0] for row in await con.fetch('explain (analyze, costs off) ' + activity.SQL[source], owner, None, None, None, 51))
                scanned = [int(rows) * int(loops) for rows, loops in PLAN_ROWS.findall(plan)]
                assert max(scanned or [0]) < 1000, (source, plan)
                assert all(int(n) < 1000 for n in REMOVED.findall(plan)), (source, plan)
            page = await client.get('/api/activity', headers=OWNER)
            assert page.status_code == 200 and len(page.json()['items']) == 20  # чужие данные в ленту не попали


async def test_migration_023_is_applied_with_its_indexes_and_the_check_failed_reason():
    async with client_for() as (client, app):
        async with app.state.pool.acquire() as con:
            names = {row['indexname'] for row in await con.fetch("select indexname from pg_indexes where schemaname='bothub'")}
            for name in ('turns_thread_started_idx', 'turns_thread_finished_idx', 'turns_thread_schedule_idx', 'approvals_thread_created_idx',
                         'approvals_thread_decided_idx', 'events_thread_activity_idx', 'procedure_runs_proc_created_idx',
                         'procedure_runs_proc_finished_idx', 'threads_owner_bot_idx', 'memory_proposed_idx', 'activity_log_owner_at_idx'):
                assert name in names, name
            for name in ('turns_started_idx', 'events_activity_idx', 'approvals_created_idx'):
                assert name not in names, name
            definition = await con.fetchval("select pg_get_constraintdef(oid) from pg_constraint where conname='schedules_last_skip_reason_check'")
            assert 'check_failed' in definition
