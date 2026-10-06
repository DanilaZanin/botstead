"""Postgres regressions for the third wire review."""
import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub.launcher_client import ExecExit, FakeLauncherClient, LauncherUnavailable, LauncherTimeout, LauncherServerError
from bothub.main import create_app
from bothub.runner.base import RunnerEvent

OWNER = {'Authorization': 'Bearer test-owner'}


@asynccontextmanager
async def client_for(*, launcher=None, runner_factory=None):
    app = create_app(runner_factory=runner_factory, launcher=launcher)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            yield client, app


async def owner_id(con):
    return await con.fetchval("select id from bothub.users where email='fixture@example.com'")


async def bound_bot(con, bot_id='bound'):
    owner = await owner_id(con)
    provider = await con.fetchval("insert into bothub.providers(owner_id,kind,name,status,secret_encrypted) "
        "values($1,'openai_api','API','ok',decode('00','hex')) returning id", owner)
    model = await con.fetchval("insert into bothub.models(provider_id,name) values($1,'gpt-test') returning id", provider)
    await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,provider_id,model_id) "
        "values($1,$1,'codex','gpt-test',$2,$3,$4)", bot_id, owner, provider, model)
    return owner, provider, model


class UnavailableRunner:
    def __init__(self):
        self.gate = asyncio.Event()

    async def run(self, turn):
        yield RunnerEvent('assistant_msg', {'text':'started','final':False})
        await self.gate.wait()

    async def stop(self, turn_id):
        raise LauncherUnavailable('offline')


async def wait_status(app, turn_id, status):
    async with asyncio.timeout(5):
        while True:
            async with app.state.pool.acquire() as con:
                current = await con.fetchval('select status from bothub.turns where id=$1',turn_id)
            if current == status:
                return
            await asyncio.sleep(.05)


async def test_stop_and_disable_fail_turn_when_launcher_unavailable():
    runner = UnavailableRunner()
    async with client_for(runner_factory=lambda _:runner) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            member = await con.fetchval("insert into bothub.users(email,password_hash,role) "
                "values('member@example.com','x','member') returning id")
            for user, bot_id in ((owner,'stop-bot'),(member,'disable-bot')):
                await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) "
                    "values($1,$1,'fake','fake',$2)",bot_id,user)
                thread = await con.fetchval('insert into bothub.threads(bot_id,owner_id) values($1,$2) returning id',bot_id,user)
                await con.execute("insert into bothub.turns(thread_id,prompt,client) values($1,'start','api')",thread)
            turns = await con.fetch("select t.id,th.bot_id from bothub.turns t join bothub.threads th on th.id=t.thread_id")
        for turn in turns:
            await wait_status(app,turn['id'],'running')
        stop_id = next(turn['id'] for turn in turns if turn['bot_id']=='stop-bot')
        disable_id = next(turn['id'] for turn in turns if turn['bot_id']=='disable-bot')
        response = await client.post(f'/api/turns/{stop_id}/stop',headers=OWNER)
        assert response.status_code==200 and response.json()['status']=='error'
        response = await client.patch(f'/api/users/{member}',json={'disabled':True},headers=OWNER)
        assert response.status_code==200
        await wait_status(app,disable_id,'error')
        async with app.state.pool.acquire() as con:
            rows = await con.fetch('select error from bothub.turns where id=any($1::uuid[])',[stop_id,disable_id])
            assert len(rows)==2 and all(row['error']=='launcher_unavailable' for row in rows)


async def test_disabling_user_stops_remaining_turns_after_one_stop_error():
    class PartlyBrokenRunner:
        def __init__(self):
            self.gate = asyncio.Event()
            self.stopped = []

        async def run(self, turn):
            yield RunnerEvent('assistant_msg', {'text':'started','final':False})
            await self.gate.wait()

        async def stop(self, turn_id):
            self.stopped.append(turn_id)
            if len(self.stopped) == 1:
                raise RuntimeError('unexpected stop failure')

    runner = PartlyBrokenRunner()
    async with client_for(runner_factory=lambda _: runner) as (client, app):
        async with app.state.pool.acquire() as con:
            member = await con.fetchval("insert into bothub.users(email,password_hash,role) values('two-turns@example.com','x','member') returning id")
            turns = []
            for bot_id in ('disable-one', 'disable-two'):
                await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values($1,$1,'fake','fake',$2)", bot_id, member)
                thread = await con.fetchval('insert into bothub.threads(bot_id,owner_id) values($1,$2) returning id', bot_id, member)
                turns.append(await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'go','api') returning id", thread))
        for turn_id in turns:
            await wait_status(app, turn_id, 'running')
        response = await client.patch(f'/api/users/{member}', json={'disabled':True}, headers=OWNER)
        assert response.status_code == 200
        assert len(runner.stopped) == 2
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select status from bothub.turns where id=$1', uuid.UUID(runner.stopped[1])) == 'stopped'


@pytest.mark.parametrize('failure,reason', [(LauncherTimeout, 'launcher_timeout'), (LauncherServerError, 'launcher_server_error')])
async def test_stop_handles_all_launcher_errors(failure, reason):
    class BrokenRunner(UnavailableRunner):
        async def stop(self, turn_id):
            raise failure('stop failed')

    async with client_for(runner_factory=lambda _: BrokenRunner()) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('broken-stop','B','fake','fake',$1)", owner)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('broken-stop',$1) returning id", owner)
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'go','api') returning id", thread)
        await wait_status(app, turn, 'running')
        response = await client.post(f'/api/turns/{turn}/stop', headers=OWNER)
        assert response.status_code == 200
        assert response.json()['status'] == 'error'
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select error from bothub.turns where id=$1', turn) == reason
            assert await con.fetchval("select count(*) from bothub.events where turn_id=$1 and kind='guard'", turn) == 1


async def test_stop_retry_exhaustion_blocks_next_turn():
    launcher = FakeLauncherClient()
    runner = UnavailableRunner()
    async with client_for(launcher=launcher, runner_factory=lambda _: runner) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('retry-stop','B','fake','fake',$1)", owner)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('retry-stop',$1) returning id", owner)
            first = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'one','api') returning id", thread)
            second = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'two','api') returning id", thread)
        await wait_status(app, first, 'running')
        for _ in range(3):
            launcher.fail_next(LauncherServerError('still running'))
        response = await client.post(f'/api/turns/{first}/stop', headers=OWNER)
        assert response.status_code == 200 and response.json()['status'] == 'error'
        async with asyncio.timeout(5):
            while True:
                async with app.state.pool.acquire() as con:
                    finished = await con.fetchval("select count(*) from bothub.events where turn_id=$1 and kind='guard' and payload->>'reason'='launcher_stop_failed'", first)
                if finished:
                    break
                await asyncio.sleep(.05)
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select status from bothub.bots where id='retry-stop'") == 'error_starting'
            assert await con.fetchval("select status from bothub.turns where id=$1", second) == 'queued'
            assert await con.fetchval("select count(*) from bothub.events where turn_id=$1 and kind='guard' and payload->>'reason'='launcher_stop_failed'", first) == 1


async def test_three_concurrent_stops_share_one_retry_chain():
    class CountingLauncher(FakeLauncherClient):
        def __init__(self):
            super().__init__()
            self.stop_count = 0
        async def stop_exec(self, exec_id, *, bot_id=None):
            self.stop_count += 1
            raise LauncherServerError('unavailable')
    launcher = CountingLauncher()
    runner = UnavailableRunner()
    async with client_for(launcher=launcher,runner_factory=lambda _:runner) as (client,app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('triple','Triple','fake','fake',$1)",owner)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('triple',$1) returning id",owner)
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'go','api') returning id",thread)
        await wait_status(app,turn,'running')
        results = await asyncio.gather(*(client.post(f'/api/turns/{turn}/stop',headers=OWNER) for _ in range(3)))
        assert all(response.status_code==200 and response.json()['status']=='error' for response in results)
        async with asyncio.timeout(5):
            while launcher.stop_count < 3: await asyncio.sleep(.01)
        assert launcher.stop_count == 3


async def test_recreate_rejects_active_turn():
    launcher = FakeLauncherClient()
    runner = UnavailableRunner()
    async with client_for(launcher=launcher,runner_factory=lambda _:runner) as (client,app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('active-recreate','B','fake','fake',$1)",owner)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('active-recreate',$1) returning id",owner)
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'go','api') returning id",thread)
        await wait_status(app,turn,'running')
        response = await client.post('/api/bots/active-recreate/recreate',headers=OWNER)
        assert response.status_code == 409 and response.json()['error']=='conflict'
        assert not any(call[0]=='recreate_bot' for call in launcher.calls)


async def test_pending_recreate_waits_for_stop_retry():
    class HeldStopLauncher(FakeLauncherClient):
        def __init__(self):
            super().__init__()
            self.stop_started = asyncio.Event()
            self.release_stop = asyncio.Event()

        async def stop_exec(self, exec_id, *, bot_id=None):
            self.stop_started.set()
            await self.release_stop.wait()
            await super().stop_exec(exec_id, bot_id=bot_id)

    launcher = HeldStopLauncher()
    runner = UnavailableRunner()
    async with client_for(launcher=launcher, runner_factory=lambda _: runner) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,need_restart) values('held-stop','B','fake','fake',$1,false)", owner)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('held-stop',$1) returning id", owner)
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'go','api') returning id", thread)
        await launcher.create_bot('held-stop', str(owner))
        await wait_status(app, turn, 'running')
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set need_restart=true where id='held-stop'")
        response = await client.post(f'/api/turns/{turn}/stop', headers=OWNER)
        assert response.status_code == 200
        await asyncio.wait_for(launcher.stop_started.wait(), 2)
        await asyncio.sleep(.05)
        assert not any(call[0] == 'recreate_bot' for call in launcher.calls)
        launcher.release_stop.set()
        async with asyncio.timeout(2):
            while not any(call[0] == 'recreate_bot' for call in launcher.calls):
                await asyncio.sleep(.01)


async def test_delete_bot_missing_container_succeeds_and_recreate_uses_owner():
    launcher = FakeLauncherClient()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) "
                "values('empty','Empty','fake','fake',$1)",owner)
        failed = await client.delete('/api/bots/empty',headers=OWNER)
        assert failed.status_code==200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select 1 from bothub.bots where id='empty'") is None
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('empty','Empty','fake','fake',$1)",owner)
        recreated = await client.post('/api/bots/empty/recreate',headers=OWNER)
        assert recreated.status_code==200
        assert ('create_bot','empty',str(owner)) in launcher.calls


async def test_delete_bot_launcher_exception_keeps_row():
    launcher = FakeLauncherClient()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('delete-error','B','fake','fake',$1)", owner)
        launcher.fail_next(LauncherServerError('failed'))
        response = await client.delete('/api/bots/delete-error', headers=OWNER)
        assert response.status_code == 502
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select 1 from bothub.bots where id='delete-error'") == 1


async def test_recreate_rejects_foreign_container_without_recreating():
    launcher = FakeLauncherClient()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('foreign','B','fake','fake',$1)", owner)
        await launcher.create_bot('foreign', str(uuid.uuid4()))
        response = await client.post('/api/bots/foreign/recreate', headers=OWNER)
        assert response.status_code == 409
        assert not any(call[0] == 'recreate_bot' for call in launcher.calls)


async def test_recreate_preserves_no_model_without_binding():
    launcher = FakeLauncherClient()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,status) values('unbound-recreate','B','claude','sonnet',$1,'no_model')", owner)
        response = await client.post('/api/bots/unbound-recreate/recreate', headers=OWNER)
        assert response.status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select status from bothub.bots where id='unbound-recreate'") == 'no_model'


async def test_failed_pending_recreate_clears_flag_and_blocks_claim():
    class FailingRecreateLauncher(FakeLauncherClient):
        def __init__(self):
            super().__init__()
            self.fail_recreate = True

        async def recreate_bot(self, bot_id):
            if self.fail_recreate:
                self.fail_recreate = False
                self.calls.append(('recreate_bot', bot_id))
                raise LauncherServerError('recreate failed')
            return await super().recreate_bot(bot_id)

    launcher = FailingRecreateLauncher()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,need_restart) values('restart','B','fake','fake',$1,true)", owner)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('restart',$1) returning id", owner)
            turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,'go','api') returning id", thread)
        await launcher.create_bot('restart', str(owner))
        await app.state.recreate_pending_bot('restart')
        assert ('recreate_bot', 'restart') in launcher.calls
        async with app.state.pool.acquire() as con:
            assert tuple(await con.fetchrow("select need_restart,status from bothub.bots where id='restart'")) == (False, 'error_starting')
        assert await app.state.claim_turn() is None
        listed = await client.get('/api/bots', headers=OWNER)
        assert listed.json()[0]['recreate_url'] == '/api/bots/restart/recreate'
        recreated = await client.post('/api/bots/restart/recreate', headers=OWNER)
        assert recreated.status_code == 200
        claimed = await app.state.claim_turn()
        if claimed:
            assert claimed['id'] == turn
        else:
            await wait_status(app, turn, 'running')


async def test_usage_sum_above_int32_survives_summary():
    async with client_for() as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,budget_daily_tokens) values('large-usage','B','fake','fake',$1,$2)", owner, 10**12)
            await con.execute("insert into bothub.usage(bot_id,provider,model,tokens_in,tokens_out,tokens_cache_read,tokens_cache_write) values('large-usage','fake','fake',$1,$1,$1,$1)", 10**9)
        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200
        assert response.json()['bots'][0]['tokens_today'] == 4 * 10**9


async def test_provider_unbind_validation_and_delete_marks_no_model():
    async with client_for() as (client, app):
        async with app.state.pool.acquire() as con:
            _, provider, _ = await bound_bot(con)
        invalid = await client.patch('/api/bots/bound',json={'provider_id':None,'model_id':None},headers=OWNER)
        assert invalid.status_code==400
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select provider_id from bothub.bots where id='bound'")==provider
        deleted = await client.delete(f'/api/providers/{provider}',headers=OWNER)
        assert deleted.status_code==200
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow("select status,provider_id,model_id from bothub.bots where id='bound'")
            assert row['status']=='no_model' and row['provider_id'] is None and row['model_id'] is None


async def test_provider_delete_clears_pending_recreate_without_losing_no_model():
    launcher = FakeLauncherClient()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            _, provider, _ = await bound_bot(con)
            await con.execute("update bothub.bots set need_restart=true where id='bound'")
        deleted = await client.delete(f'/api/providers/{provider}', headers=OWNER)
        assert deleted.status_code == 200
        await app.state.recreate_pending_bot('bound')
        async with app.state.pool.acquire() as con:
            assert tuple(await con.fetchrow("select status,need_restart from bothub.bots where id='bound'")) == ('no_model', False)
        assert not any(call[0] in ('create_bot', 'recreate_bot') for call in launcher.calls)


async def test_deleted_provider_blocks_new_turn_and_pauses_schedules():
    async with client_for() as (client, app):
        async with app.state.pool.acquire() as con:
            owner, provider, _ = await bound_bot(con)
            thread = await con.fetchval("insert into bothub.threads(bot_id,owner_id) values('bound',$1) returning id", owner)
            schedule = await con.fetchval("insert into bothub.schedules(bot_id,name,kind,cron,prompt,owner_id,enabled,next_run_at) values('bound','Daily','cron','* * * * *','go',$1,true,now()) returning id", owner)
        deleted = await client.delete(f'/api/providers/{provider}', headers=OWNER)
        assert deleted.status_code == 200
        response = await client.post(f'/api/threads/{thread}/turns', json={'prompt':'go'}, headers=OWNER)
        assert response.status_code == 409
        await app.state.run_due_schedules()
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select enabled from bothub.schedules where id=$1', schedule) is True
            assert await con.fetchval('select count(*) from bothub.turns where thread_id=$1', thread) == 0
            assert await con.fetchval("select status from bothub.bots where id='bound'") == 'no_model'


async def test_unbound_bot_runs_in_legacy_mode_until_owner_has_a_registry():
    """Действующая установка (вход по OWNER_TOKEN, реестр пуст): бот без привязки работает как раньше.
    Как только у владельца появился провайдер, новый бот без модели не создаётся."""
    async with client_for() as (client, app):
        created = await client.post('/api/bots', json={'id':'unbound','name':'Unbound','provider':'claude',
            'model':'sonnet','schedule':{'cron':'* * * * *','prompt':'go'}}, headers=OWNER)
        assert created.status_code == 200, created.text
        assert created.json()['status'] == 'idle'
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select registry_bound from bothub.bots where id='unbound'") is False
        thread = await client.post('/api/threads', json={'bot_id':'unbound'}, headers=OWNER)
        assert thread.status_code == 200
        turn = await client.post(f"/api/threads/{thread.json()['id']}/turns", json={'prompt':'go'}, headers=OWNER)
        assert turn.status_code in (200, 201, 202), turn.text
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            provider = await con.fetchval("insert into bothub.providers(owner_id,kind,name,status,secret_encrypted) values($1,'openai_api','API','ok',decode('00','hex')) returning id", owner)
            await con.fetchval("insert into bothub.models(provider_id,name) values($1,'gpt-test') returning id", provider)
        second = await client.post('/api/bots', json={'id':'unbound2','name':'Unbound 2','provider':'claude','model':'sonnet'}, headers=OWNER)
        assert second.status_code == 400, second.text



async def test_provider_base_url_change_requires_secret():
    async with client_for() as (client, app):
        async with app.state.pool.acquire() as con:
            _, provider, _ = await bound_bot(con)
        response = await client.patch(f'/api/providers/{provider}',json={'base_url':'https://new.example'},headers=OWNER)
        assert response.status_code==400
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select base_url from bothub.providers where id=$1',provider) is None


@pytest.mark.parametrize('body', [
    {'kind':'cli_subscription','cli':'claude','name':'x'*81},
    {'kind':'openai_api','name':'API','secret':'x'*8193},
    {'kind':'openai_compatible','name':'API','secret':'token','base_url':'https://'+'x'*2048+'.example'},
])
async def test_provider_input_bounds(body):
    async with client_for() as (client, _):
        response = await client.post('/api/providers', json=body, headers=OWNER)
        assert response.status_code == 400


async def test_provider_patch_input_bounds():
    async with client_for() as (client, app):
        async with app.state.pool.acquire() as con:
            _, provider, _ = await bound_bot(con)
        for body in ({'name':'x'*81}, {'secret':'x'*8193},
                     {'secret':'token','base_url':'https://'+'x'*2048+'.example'}):
            response = await client.patch(f'/api/providers/{provider}', json=body, headers=OWNER)
            assert response.status_code == 400


class StatusLauncher(FakeLauncherClient):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def login_output(self, session_id):
        self.active += 1
        if self.active == 2:
            self.both_started.set()
        await self.release.wait()
        self.active -= 1
        yield ExecExit(0,'exit')


async def test_subscription_checks_run_in_parallel_and_time_out(monkeypatch):
    launcher = StatusLauncher()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            for cli in ('claude','codex'):
                await con.execute("insert into bothub.providers(owner_id,kind,cli,name) "
                    "values($1,'cli_subscription',$2,$2)",owner,cli)
        check = asyncio.create_task(client.post('/api/models/refresh',headers=OWNER))
        await asyncio.wait_for(launcher.both_started.wait(),3)
        launcher.release.set()
        response = await check
        assert response.status_code==200
        assert [item['status'] for item in response.json()]==['ok','ok']
        launcher.release.clear()
        monkeypatch.setattr('bothub.main.PROVIDER_CHECK_TIMEOUT',.05)
        provider = response.json()[0]['id']
        async with app.state.pool.acquire() as con:
            await con.execute('update bothub.providers set last_check_at=null where id=$1', uuid.UUID(provider))
        timed_out = await client.post(f'/api/providers/{provider}/check',headers=OWNER)
        assert timed_out.status_code==200 and timed_out.json()['status']=='error'
        assert 'timeout' in timed_out.json()['last_error'].lower()


async def test_provider_checks_coalesce_and_cool_down():
    launcher = StatusLauncher()
    async with client_for(launcher=launcher) as (client, app):
        async with app.state.pool.acquire() as con:
            owner = await owner_id(con)
            provider = await con.fetchval("insert into bothub.providers(owner_id,kind,cli,name) values($1,'cli_subscription','claude','CLI') returning id", owner)
        first = asyncio.create_task(client.post(f'/api/providers/{provider}/check', headers=OWNER))
        await asyncio.sleep(.05)
        second = asyncio.create_task(client.post(f'/api/providers/{provider}/check', headers=OWNER))
        launcher.release.set()
        responses = await asyncio.gather(first, second)
        assert all(response.status_code == 200 for response in responses)
        assert sum(call[0] == 'open_login_session' for call in launcher.calls) == 1
        again = await client.post(f'/api/providers/{provider}/check', headers=OWNER)
        assert again.status_code == 200
        assert sum(call[0] == 'open_login_session' for call in launcher.calls) == 1
