"""Воспроизведение процедур на Postgres: миграция 019, SQL `PgStore`, маршруты run, stop и decide, подтверждения, секреты, восстановление
после рестарта. Нужен Postgres (`scripts/dev.sh`, `cd core && uv run pytest`). Логика шагов без базы: test_procedure_runner_pure.py.

В этой среде базы нет: файл проверен только на сбор и импорт, прогон на живой схеме остаётся стенду."""
import asyncio
import base64
import json
import uuid

import pytest

from bothub.launcher_client import FakeBot, FakeLauncherClient
from bothub.secrets import encrypt_secret
from procedure_fakes import World
from test_procedures_db import OWNER, add_member, client_for, create, make_bot, make_thread, owner_id

SECRET = 'hunter2-SECRET-VALUE'
CLICK = {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Продолжить'}, 'safe_to_retry': False}
PAY = {'id': 's2', 'action': 'click', 'target': {'role': 'button', 'name': 'Оплатить картой'}, 'safe_to_retry': False}


@pytest.fixture(autouse=True)
def runner_env(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_PROCEDURE_INTERVAL', '3600')  # фоновый цикл не вмешивается: шаги гоняет тест
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())


def launcher_for(*bot_ids):
    launcher = FakeLauncherClient()
    for bot_id in bot_ids:
        launcher.bots[bot_id] = FakeBot(bot_id, 'o1')
    return launcher


async def drive(app, rounds=30):
    runner = app.state.procedure_runner
    for _ in range(rounds):
        busy = await runner.tick()
        tasks = list(runner._tasks.values())
        if tasks:
            await asyncio.gather(*tasks)
        if not busy and not tasks:
            return
    raise AssertionError('runner did not settle')


async def run_row(app, run_id):
    async with app.state.pool.acquire() as con:
        return dict(await con.fetchrow('select * from bothub.procedure_runs where id=$1', uuid.UUID(str(run_id))))


async def insert_run(app, procedure, status, *, bot_id='alpha', steps=None, next_step=0, **fields):
    async with app.state.pool.acquire() as con:
        thread = await con.fetchval('select id from bothub.threads where bot_id=$1 order by created_at desc limit 1', bot_id)
        return await con.fetchval(
            'insert into bothub.procedure_runs(procedure_id,procedure_version,bot_id,thread_id,status,steps,next_step,in_flight,reason) '
            'values($1,1,$2,$3,$4,$5::jsonb,$6,$7,$8) returning id',
            uuid.UUID(str(procedure)), bot_id, thread, status, json.dumps(steps or [CLICK]), next_step,
            fields.get('in_flight', False), fields.get('reason'))


async def setup(client, app, *, steps=(CLICK,), params=(), **options):
    await make_bot(client)
    await make_thread(client)
    proc = await create(client, name='Flow', params=list(params), steps=list(steps), bot_id='alpha')
    return proc


# --- миграция и создание ----------------------------------------------------------------------------------------------------

async def test_migration_019_adds_the_runner_columns():
    async with client_for() as (_, app):
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='019_procedure_runner.sql'") == 1
            columns = {r['column_name']: r for r in await con.fetch(
                "select column_name, column_default, is_nullable from information_schema.columns "
                "where table_schema='bothub' and table_name='procedure_runs'")}
            for name in ('reason', 'approval_id', 'attempt', 'in_flight', 'secret_params'):
                assert name in columns, name
            assert columns['attempt']['is_nullable'] == 'NO' and columns['in_flight']['is_nullable'] == 'NO'
            assert columns['reason']['is_nullable'] == 'YES' and columns['approval_id']['is_nullable'] == 'YES'


async def test_run_creates_a_snapshot_a_thread_and_a_note_and_records_no_bot():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app, steps=[CLICK, PAY])
        started = await client.post(f"/api/procedures/{proc['id']}/run", json={}, headers=OWNER)
        assert started.status_code == 201, started.text
        run = started.json()
        row = await run_row(app, run['id'])
        assert (row['status'], row['bot_id'], row['procedure_version'], row['next_step'], row['attempt'], row['in_flight']) \
            == ('queued', 'alpha', 1, 0, 0, False)
        assert [s['id'] for s in row['steps']] == ['s1', 's2'] and row['secret_params'] == [] and row['thread_id'] is not None
        async with app.state.pool.acquire() as con:
            thread = await con.fetchrow('select kind, title, bot_id, owner_id from bothub.threads where id=$1', row['thread_id'])
            assert (thread['kind'], thread['title'], thread['bot_id'], thread['owner_id']) == ('routine', 'Flow', 'alpha', await owner_id(app))
            assert 'Запущена процедура' in await con.fetchval("select payload->>'text' from bothub.events where thread_id=$1 and kind='system'",
                                                                row['thread_id'])
        await client.patch(f"/api/procedures/{proc['id']}", json={'steps': [CLICK]}, headers=OWNER)
        assert len((await run_row(app, run['id']))['steps']) == 2  # снимок
        free = await create(client, name='Free', steps=[CLICK])
        # У процедуры из create() есть обязательный параметр email: без него ответ 400 раньше проверки бота.
        none = await client.post(f"/api/procedures/{free['id']}/run", json={'params': {'email': 'user@example.org'}}, headers=OWNER)
        assert none.status_code == 201, none.text
        assert none.status_code == 201 and none.json()['status'] == 'failed' and none.json()['error'] == 'no_bot'
        assert none.json()['thread_id'] is None and none.json()['finished_at'] is not None


async def test_run_validates_bot_thread_params_and_secret_references():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        _, member = await add_member(client, app)
        proc = await setup(client, app, params=[{'name': 'pw', 'secret': True, 'required': False}])
        url = f"/api/procedures/{proc['id']}/run"
        assert (await client.post(url, json={'bot_id': 'nobody'}, headers=OWNER)).status_code == 400
        assert (await client.post(url, json={'thread_id': str(uuid.uuid4())}, headers=OWNER)).status_code == 400
        assert (await client.post(url, json={'params': {'zzz': 1}}, headers=OWNER)).status_code == 400
        assert (await client.post(url, json={'params': {'pw': SECRET}}, headers=OWNER)).status_code == 400
        missing = await client.post(url, json={'params': {'pw': 'vault:nope'}}, headers=OWNER)
        assert missing.status_code == 400 and SECRET not in missing.text
        assert (await client.post(url, json={}, headers=member)).status_code == 404
        async with app.state.pool.acquire() as con:
            owner = await owner_id(app)
            secret_id = uuid.uuid4()
            await con.execute('insert into bothub.secrets(id,owner_id,bot_id,name,value_encrypted) values($1,$2,null,$3,$4)',
                              secret_id, owner, 'shared', encrypt_secret(SECRET.encode(), secret_id.bytes))
        ok = await client.post(url, json={'params': {'pw': 'vault:shared'}}, headers=OWNER)
        assert ok.status_code == 201 and ok.json()['params'] == {'pw': 'vault:shared'} and ok.json()['secret_params'] == ['pw']
        assert SECRET not in ok.text
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set executor='mac' where id='alpha'")
        assert (await client.post(url, json={}, headers=OWNER)).status_code == 400


async def test_run_is_refused_without_a_ready_launcher_and_for_non_active_procedures():
    async with client_for() as (client, app):  # без лаунчера
        proc = await setup(client, app)
        assert (await client.post(f"/api/procedures/{proc['id']}/run", json={}, headers=OWNER)).status_code == 503
        await client.patch(f"/api/procedures/{proc['id']}", json={'status': 'archived'}, headers=OWNER)
        refused = await client.post(f"/api/procedures/{proc['id']}/run", json={}, headers=OWNER)
        assert refused.status_code == 409 and refused.json()['error'] == 'not_runnable'


# --- ход запуска на настоящей схеме ----------------------------------------------------------------------------------------------

async def test_a_run_goes_through_a_real_approval_to_done_and_leaves_no_secret_behind():
    launcher = launcher_for('alpha')
    world = World()
    world.add('button', 'Продолжить').add('button', 'Оплатить картой').add('textbox', 'Пароль')
    launcher.procedure_handler = world.handler
    async with client_for(launcher=launcher) as (client, app):
        owner = None
        proc = await setup(client, app, steps=[
            CLICK, PAY, {'id': 's3', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'secret_ref': 'vault:bank',
                         'precondition': {'url_matches': r'^https://example\.com/'}}])
        async with app.state.pool.acquire() as con:
            owner = await owner_id(app)
            secret_id = uuid.uuid4()
            await con.execute('insert into bothub.secrets(id,owner_id,bot_id,name,value_encrypted) values($1,$2,$3,$4,$5)',
                              secret_id, owner, 'alpha', 'bank', encrypt_secret(SECRET.encode(), secret_id.bytes))
        run = (await client.post(f"/api/procedures/{proc['id']}/run", json={}, headers=OWNER)).json()
        await drive(app)
        row = await run_row(app, run['id'])
        assert (row['status'], row['next_step'], row['reason']) == ('waiting_approval', 1, None) and row['approval_id']
        async with app.state.pool.acquire() as con:
            approval = dict(await con.fetchrow('select * from bothub.approvals where id=$1', row['approval_id']))
            assert (approval['tool'], approval['risk'], approval['status'], approval['thread_id'], approval['bot_id'], approval['turn_id']) \
                == ('procedure_step', 'pay', 'pending', row['thread_id'], 'alpha', None)
            assert approval['args']['step_id'] == 's2' and approval['args_hash'] and approval['op_hash']
            assert await con.fetchval("select count(*) from bothub.events where thread_id=$1 and kind='approval_req'", row['thread_id']) == 1
            assert await con.fetchval("select count(*) from bothub.outbox where dedup_key=$1", f"approval:{approval['id']}") == 1
        listed = (await client.get('/api/approvals?status=pending', headers=OWNER)).json()
        assert [a['id'] for a in listed] == [str(approval['id'])]
        decided = await client.post(f"/api/approvals/{approval['id']}/decide", json={'decision': 'approve', 'remember': True}, headers=OWNER)
        assert decided.status_code == 200 and decided.json()['status'] == 'approved' and decided.json()['remember'] is False
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select auto_allow from bothub.bots where id='alpha'") == []  # правило не создано
        await drive(app)
        row = await run_row(app, run['id'])
        assert (row['status'], row['next_step']) == ('waiting_approval', 2)  # шаг с секретом: login, снова подтверждение
        pending = (await client.get('/api/approvals?status=pending', headers=OWNER)).json()
        assert len(pending) == 1 and pending[0]['risk'] == 'login'
        await client.post(f"/api/approvals/{pending[0]['id']}/decide", json={'decision': 'approve'}, headers=OWNER)
        await drive(app)
        row = await run_row(app, run['id'])
        assert (row['status'], row['next_step'], row['approval_id'], row['in_flight'], row['finished_at'] is not None) \
            == ('done', 3, None, False, True)
        assert [e['status'] for e in row['step_log']] == ['ok', 'ok', 'ok']
        assert [a[2] for a in world.acted if a[0] == 'fill'] == [SECRET]
        async with app.state.pool.acquire() as con:
            used = await con.fetchval("select count(*) from bothub.approvals where used_at is not null")
            assert used == 2
            dump = json.dumps([[dict(r) for r in await con.fetch(f'select * from bothub.{table}')]
                               for table in ('procedure_runs', 'approvals', 'events', 'outbox')], default=str, ensure_ascii=False)
        assert SECRET not in dump
        assert SECRET not in json.dumps((await client.get(f"/api/procedure-runs/{run['id']}", headers=OWNER)).json())


async def test_rejected_approval_fails_the_run_in_the_database():
    launcher = launcher_for('alpha')
    world = World().add('button', 'Оплатить картой')
    launcher.procedure_handler = world.handler
    async with client_for(launcher=launcher) as (client, app):
        proc = await setup(client, app, steps=[PAY])
        run = (await client.post(f"/api/procedures/{proc['id']}/run", json={}, headers=OWNER)).json()
        await drive(app)
        approval = (await client.get('/api/approvals?status=pending', headers=OWNER)).json()[0]
        await client.post(f"/api/approvals/{approval['id']}/decide", json={'decision': 'reject'}, headers=OWNER)
        await drive(app)
        row = await run_row(app, run['id'])
        assert (row['status'], row['error'], row['finished_at'] is not None) == ('failed', 'approval_rejected', True)
        assert row['step_log'][0]['status'] == 'failed' and not world.acted


# --- stop, decide, переходы и восстановление ----------------------------------------------------------------------------------

async def test_stop_and_decide_routes_use_the_real_rows():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        _, member = await add_member(client, app)
        proc = await setup(client, app, steps=[CLICK, {**CLICK, 'id': 's2'}])
        waiting = await insert_run(app, proc['id'], 'waiting_human', steps=[CLICK, {**CLICK, 'id': 's2'}], reason='element_not_found')
        url = f'/api/procedure-runs/{waiting}/decide'
        assert (await client.post(url, json={'action': 'skip'}, headers=member)).status_code == 404
        skipped = await client.post(url, json={'action': 'skip'}, headers=OWNER)
        assert skipped.status_code == 200
        body = skipped.json()
        assert (body['status'], body['next_step'], body['reason']) == ('running', 1, None) and body['step_log'][0]['status'] == 'skipped'
        assert (await client.post(url, json={'action': 'retry'}, headers=OWNER)).status_code == 409
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.procedure_runs set status='waiting_human', reason='unknown_outcome', attempt=2, in_flight=true where id=$1", waiting)
        retried = (await client.post(url, json={'action': 'retry'}, headers=OWNER)).json()
        assert (retried['status'], retried['reason'], retried['attempt'], retried['in_flight']) == ('running', None, 0, False)
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.procedure_runs set status='waiting_human' where id=$1", waiting)
        assert (await client.post(url, json={'action': 'skip'}, headers=OWNER)).json()['status'] == 'done'
        other = await insert_run(app, proc['id'], 'waiting_human')
        assert (await client.post(f'/api/procedure-runs/{other}/decide', json={'action': 'stop'}, headers=OWNER)).json()['status'] == 'stopped'
        live = await insert_run(app, proc['id'], 'waiting_approval')
        stopped = await client.post(f'/api/procedure-runs/{live}/stop', headers=OWNER)
        assert stopped.status_code == 200 and stopped.json()['status'] == 'stopped' and stopped.json()['finished_at']
        assert (await client.post(f'/api/procedure-runs/{live}/stop', headers=OWNER)).status_code == 409
        assert (await client.post(f'/api/procedure-runs/{live}/stop', headers=member)).status_code == 404


async def test_transitions_are_conditional_on_the_status():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app, steps=[CLICK, {**CLICK, 'id': 's2'}])
        store = app.state.procedure_runner.store
        run = await insert_run(app, proc['id'], 'running', steps=[CLICK, {**CLICK, 'id': 's2'}])
        entry = {'step_id': 's1', 'status': 'ok', 'at': 'x', 'duration_ms': 1}
        assert await store.set_in_flight(run, True) and (await run_row(app, run))['in_flight'] is True
        assert await store.bump_attempt(run) == 1 and (await run_row(app, run))['in_flight'] is False
        assert not await store.advance(run, 1, entry, last=False)  # next_step не тот
        assert await store.advance(run, 0, entry, last=False)
        row = await run_row(app, run)
        assert (row['next_step'], row['attempt'], row['step_log'], row['status']) == (1, 0, [entry], 'running')
        assert await store.advance(run, 1, {**entry, 'step_id': 's2'}, last=True)
        row = await run_row(app, run)
        assert (row['status'], row['next_step'], row['finished_at'] is not None) == ('done', 2, True)
        for call in (store.set_in_flight(run, True), store.bump_attempt(run), store.pause(run, 'x'), store.finish(run),
                     store.advance(run, 2, entry, last=False), store.fail(run, 'boom')):
            assert not await call  # завершённый запуск не двигается
        stopped = await insert_run(app, proc['id'], 'stopped')
        assert not await store.begin(stopped) and not await store.resume(stopped, 'waiting_human')
        queued = await insert_run(app, proc['id'], 'queued')
        assert await store.begin(queued) and not await store.begin(queued)
        assert (await run_row(app, queued))['started_at'] is not None
        assert await store.pause(queued, 'precondition_failed')
        assert (await run_row(app, queued))['reason'] == 'precondition_failed'
        assert await store.resume(queued, 'waiting_human') and (await run_row(app, queued))['reason'] is None
        assert await store.fail(queued, 'expect_failed', entry)
        row = await run_row(app, queued)
        assert (row['status'], row['error'], row['step_log']) == ('failed', 'expect_failed', [entry])
        ids = [r['id'] for r in await store.active_runs()]
        assert queued not in ids and run not in ids


async def test_recover_follows_the_contract_in_sql():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app)
        safe = await insert_run(app, proc['id'], 'running', steps=[{**CLICK, 'safe_to_retry': True}], in_flight=True)
        unsafe = await insert_run(app, proc['id'], 'running', steps=[CLICK], in_flight=True)
        idle = await insert_run(app, proc['id'], 'running', steps=[CLICK], in_flight=False)
        beyond = await insert_run(app, proc['id'], 'running', steps=[CLICK], next_step=1, in_flight=True)
        waiting = await insert_run(app, proc['id'], 'waiting_human', in_flight=True, reason='precondition_failed')
        await app.state.procedure_runner.recover()
        assert (await run_row(app, safe))['status'] == 'running' and (await run_row(app, safe))['in_flight'] is False
        row = await run_row(app, unsafe)
        assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'unknown_outcome', False)
        assert (await run_row(app, idle))['status'] == 'running'
        assert (await run_row(app, beyond))['status'] == 'waiting_human'
        assert (await run_row(app, waiting))['reason'] == 'precondition_failed'


async def test_secrets_are_read_bot_first_and_unreadable_ones_are_missing():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        await setup(client, app)
        store = app.state.procedure_runner.store
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            for bot_id, name, value in ((None, 'pw', 'shared-value'), ('alpha', 'pw', 'bot-value'), (None, 'only-shared', 'x-value')):
                secret_id = uuid.uuid4()
                await con.execute('insert into bothub.secrets(id,owner_id,bot_id,name,value_encrypted) values($1,$2,$3,$4,$5)',
                                  secret_id, owner, bot_id, name, encrypt_secret(value.encode(), secret_id.bytes))
            await con.execute('insert into bothub.secrets(id,owner_id,bot_id,name,value_encrypted) values($1,$2,null,$3,$4)',
                              uuid.uuid4(), owner, 'broken', b'not a ciphertext at all, but long enough to try decrypting it')
        found = await store.load_secrets(owner, 'alpha', ['pw', 'only-shared', 'broken', 'absent'])
        assert found == {'pw': 'bot-value', 'only-shared': 'x-value', 'broken': None}
        assert await store.load_secrets(owner, 'alpha', []) == {}
        assert (await store.load_secrets(owner, 'beta', ['pw'])) == {'pw': 'shared-value'}


# --- правки по ревью Opus (пункты 1, 3, 4, 11): SQL тех же правил, что в test_procedure_runner_review2_pure.py ---------------------

async def test_a_second_run_for_a_bot_is_409_in_sql_and_a_finished_one_frees_it():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app, params=[])
        url = f"/api/procedures/{proc['id']}/run"
        first = await client.post(url, json={}, headers=OWNER)
        assert first.status_code == 201
        second = await client.post(url, json={}, headers=OWNER)
        assert second.status_code == 409 and second.json()['error'] == 'conflict'
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.procedure_runs') == 1
            await con.execute("update bothub.procedure_runs set status='stopped', finished_at=now() where id=$1", uuid.UUID(first.json()['id']))
        assert (await client.post(url, json={}, headers=OWNER)).status_code == 201


async def test_store_sees_a_turn_of_the_bot_and_the_worker_skips_a_bot_with_an_unfinished_run():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app)
        store = app.state.procedure_runner.store
        assert await store.bot_has_turn('alpha') is False
        async with app.state.pool.acquire() as con:
            thread = await con.fetchval("select id from bothub.threads where bot_id='alpha' limit 1")
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,client,status) values($1,'q','api','running') returning id", thread)
        assert await store.bot_has_turn('alpha') is True and await store.bot_has_turn('beta') is False
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='done' where id=$1", turn)
            queued = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'q','api') returning id", thread)
        run = await insert_run(app, proc['id'], 'waiting_human', reason='element_not_found')
        assert await store.bot_has_turn('alpha') is False
        assert await app.state.claim_turn() is None  # turn ждёт: у бота неконечный запуск процедуры
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select status from bothub.turns where id=$1', queued) == 'queued'
            await con.execute("update bothub.procedure_runs set status='stopped', finished_at=now() where id=$1", run)
        claimed = await app.state.claim_turn()
        assert claimed is not None and claimed['id'] == queued


async def test_stop_of_a_step_in_flight_writes_unknown_outcome_in_one_transaction():
    launcher = launcher_for('alpha')
    async with client_for(launcher=launcher) as (client, app):
        proc = await setup(client, app, steps=[CLICK, {**CLICK, 'id': 's2'}])
        flying = await insert_run(app, proc['id'], 'running', steps=[CLICK, {**CLICK, 'id': 's2'}], in_flight=True)
        stopped = await client.post(f'/api/procedure-runs/{flying}/stop', headers=OWNER)
        body = stopped.json()
        assert stopped.status_code == 200 and (body['status'], body['in_flight']) == ('stopped', False)
        assert [(e['step_id'], e['status'], e['error']) for e in body['step_log']] == [('s1', 'failed', 'unknown_outcome')]
        assert launcher.cancelled == [('alpha', f'proc-{uuid.UUID(str(flying)).hex}')]
        idle = await insert_run(app, proc['id'], 'waiting_human', reason='element_not_found')
        assert (await client.post(f'/api/procedure-runs/{idle}/stop', headers=OWNER)).json()['step_log'] == []


async def test_recover_does_not_repeat_a_secret_fill_even_when_it_was_saved_as_safe_to_retry():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app)
        secret = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'secret_ref': 'vault:bank',
                  'safe_to_retry': True}
        run = await insert_run(app, proc['id'], 'running', steps=[secret], in_flight=True)
        await app.state.procedure_runner.recover()
        row = await run_row(app, run)
        assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'unknown_outcome', False)


async def test_an_approval_stores_the_page_origin_and_its_hash_covers_it():
    launcher = launcher_for('alpha')
    world = World('https://shop.example/pay')
    world.add('button', 'Оплатить картой')
    launcher.procedure_handler = world.handler
    async with client_for(launcher=launcher) as (client, app):
        proc = await setup(client, app, steps=[PAY], params=[])
        run = (await client.post(f"/api/procedures/{proc['id']}/run", json={}, headers=OWNER)).json()
        await drive(app)
        async with app.state.pool.acquire() as con:
            approval = dict(await con.fetchrow('select * from bothub.approvals where id=(select approval_id from bothub.procedure_runs where id=$1)',
                                               uuid.UUID(run['id'])))
        assert approval['args']['origin'] == 'https://shop.example' and approval['title'].endswith(' на https://shop.example')
        from bothub.procedure_runner import args_hash
        assert approval['args_hash'] == args_hash(approval['args'])
