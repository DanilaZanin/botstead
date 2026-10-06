"""Ожидание человека не длиннее BOTHUB_PROCEDURE_WAIT_HOURS на Postgres: миграция 020, SQL `PgStore.pause/recover/expire_wait`,
событие в треде, очередь turn'ов и расписания бота после остановки. Логика без базы: test_procedure_runner_wait_pure.py.

В этой среде базы нет: файл проверен только на сбор и импорт, прогон на живой схеме остаётся стенду."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from test_procedure_runner_db import CLICK, drive, insert_run, launcher_for, run_row, runner_env, setup  # noqa: F401  (runner_env: фикстура)
from test_procedures_db import client_for, owner_id


async def set_waiting_since(app, run_id, ago):
    async with app.state.pool.acquire() as con:
        await con.execute('update bothub.procedure_runs set waiting_since=now()-$2::interval where id=$1', run_id, ago)


async def events_of(app, thread_id):
    async with app.state.pool.acquire() as con:
        rows = await con.fetch("select payload from bothub.events where thread_id=$1 and kind='system' order by seq", thread_id)
    return [json.loads(row['payload']) if isinstance(row['payload'], str) else dict(row['payload']) for row in rows]


async def test_migration_020_adds_waiting_since_and_pause_and_recover_set_it():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app)
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='020_procedure_wait_since.sql'") == 1
            assert await con.fetchval("select is_nullable from information_schema.columns where table_schema='bothub' "
                                      "and table_name='procedure_runs' and column_name='waiting_since'") == 'YES'
        store = app.state.procedure_runner.store
        paused = await insert_run(app, proc['id'], 'running', steps=[CLICK])
        assert (await run_row(app, paused))['waiting_since'] is None
        assert await store.pause(paused, 'element_not_found')
        assert (await run_row(app, paused))['waiting_since'] is not None
        flying = await insert_run(app, proc['id'], 'running', steps=[CLICK], in_flight=True)
        await store.recover()
        assert (await run_row(app, flying))['status'] == 'waiting_human' and (await run_row(app, flying))['waiting_since'] is not None


async def test_a_wait_older_than_the_limit_stops_the_run_writes_the_note_and_frees_the_turn_queue():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app)
        async with app.state.pool.acquire() as con:
            thread = await con.fetchval("select id from bothub.threads where bot_id='alpha' limit 1")
            queued = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'q','api') returning id", thread)
        run = await insert_run(app, proc['id'], 'waiting_human', reason='element_not_found', in_flight=True)
        await set_waiting_since(app, run, timedelta(hours=23))
        await drive(app)
        assert (await run_row(app, run))['status'] == 'waiting_human'
        assert await app.state.claim_turn() is None  # пока запуск ждёт, turn стоит в очереди (не теряется)
        await set_waiting_since(app, run, timedelta(hours=25))
        await drive(app)
        row = await run_row(app, run)
        assert (row['status'], row['error'], row['reason'], row['in_flight']) == ('stopped', 'wait_expired', None, False)
        assert row['finished_at'] is not None
        notes = [e for e in await events_of(app, row['thread_id']) if e.get('run_id') == str(run)]
        assert len(notes) == 1 and 'wait_expired' in notes[0]['text'] and str(run) in notes[0]['text'] and chr(0x2014) not in notes[0]['text']
        claimed = await app.state.claim_turn()
        assert claimed is not None and claimed['id'] == queued  # очередь turn'ов пошла дальше


async def test_a_pending_approval_is_closed_when_the_wait_expires_and_a_late_owner_decision_loses():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app)
        run = await insert_run(app, proc['id'], 'waiting_approval')
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select thread_id from bothub.procedure_runs where id=$1', run)
            approval = await con.fetchval(
                "insert into bothub.approvals(thread_id,bot_id,risk,title,tool,args,args_hash,status,expires_at) "
                "values($1,'alpha','pay','x','procedure_step','{}'::jsonb,'h','pending',now()+interval '48 hours') returning id", row['thread_id'])
            await con.execute('update bothub.procedure_runs set approval_id=$2 where id=$1', run, approval)
        await set_waiting_since(app, run, timedelta(hours=25))
        await drive(app)
        assert (await run_row(app, run))['error'] == 'wait_expired'
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select status from bothub.approvals where id=$1', approval) == 'expired'
        store = app.state.procedure_runner.store
        assert await store.expire_wait(run, datetime.now(timezone.utc)) is None  # уже остановлен: повторный переход ничего не меняет


async def test_a_due_schedule_waits_for_the_bot_to_be_free_and_fires_once():
    async with client_for(launcher=launcher_for('alpha')) as (client, app):
        proc = await setup(client, app)
        owner = await owner_id(app)
        run = await insert_run(app, proc['id'], 'waiting_human', reason='element_not_found')
        await set_waiting_since(app, run, timedelta(hours=2))
        async with app.state.pool.acquire() as con:
            schedule = await con.fetchval(
                "insert into bothub.schedules(bot_id,name,kind,cron,prompt,enabled,next_run_at,owner_id) "
                "values('alpha','hourly','cron','0 * * * *','p',true,now()-interval '1 minute',$1) returning id", owner)
            due = await con.fetchval('select next_run_at from bothub.schedules where id=$1', schedule)
        for _ in range(3):  # несколько проходов планировщика за время ожидания: turn'ов нет, срок не сдвигается
            await app.state.run_due_schedules()
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='schedule'") == 0
            assert await con.fetchval('select next_run_at from bothub.schedules where id=$1', schedule) == due
        await set_waiting_since(app, run, timedelta(hours=25))
        await drive(app)  # остановка по сроку освобождает бота
        await app.state.run_due_schedules()
        await app.state.run_due_schedules()
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='schedule'") == 1  # ровно один, без дублей
            assert await con.fetchval('select next_run_at from bothub.schedules where id=$1', schedule) > datetime.now(timezone.utc)
