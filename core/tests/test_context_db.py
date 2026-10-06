"""Заполнение контекста и сжатие треда (раздел 15, миграция 022): измерение по usage, ручное и автоматическое
сжатие, преамбула следующего хода. Нужен Postgres из conftest.py. Чистая логика: test_context_pure.py."""
import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest

from bothub import auth
from bothub import context as ctx
from bothub.main import create_app
from bothub.runner.base import RunnerEvent

OWNER = {'Authorization': 'Bearer test-owner'}
SUMMARY = '## Goals\nship the release\n\n## Decisions\nuse the blue button'


class Brain:
    """Раннер с записью контекстов ходов. Обычный ход отвечает и сообщает размер контекста, ход сжатия
    (по тексту инструкции) отдаёт сводку. Сессия: продолжает переданную, иначе заводит sess-N."""
    provider = 'fake'

    def __init__(self, context_tokens=None, context_window=None, summary=SUMMARY, fail_compact=False):
        self.context_tokens, self.context_window = context_tokens, context_window
        self.summary, self.fail_compact = summary, fail_compact
        self.turns = []
        self.sessions = 0

    async def run(self, turn):
        self.turns.append(turn)
        if turn.prompt == ctx.COMPACT_INSTRUCTION:
            if self.fail_compact:
                raise RuntimeError('cli crashed')
            yield RunnerEvent('assistant_msg', {'text': self.summary, 'final': False}, turn.cli_session_id)
            yield RunnerEvent('assistant_msg', {'text': '', 'final': True}, turn.cli_session_id)
            yield RunnerEvent('usage', {'tokens_in': 50, 'tokens_out': 5, 'model': 'fake', 'seconds': 0.1}, turn.cli_session_id)
            return
        if turn.cli_session_id:
            sid = turn.cli_session_id
        else:
            self.sessions += 1
            sid = f'sess-{self.sessions}'
        yield RunnerEvent('assistant_msg', {'text': f'reply {len(self.turns)}', 'final': False}, sid)
        yield RunnerEvent('assistant_msg', {'text': '', 'final': True}, sid)
        usage = {'tokens_in': 10, 'tokens_out': 20, 'model': 'fake', 'seconds': 0.1}
        if self.context_tokens is not None:
            usage |= {'context_tokens': self.context_tokens, 'context_estimated': False}
        if self.context_window is not None:
            usage['context_window'] = self.context_window
        yield RunnerEvent('usage', usage, sid)

    async def stop(self, turn_id):
        pass

    def normal(self):
        return [turn for turn in self.turns if turn.prompt != ctx.COMPACT_INSTRUCTION]


@asynccontextmanager
async def api(brain=None, manual=False):
    brain = brain or Brain()
    app = create_app(lambda provider: brain)
    if manual:
        async def idle():
            await asyncio.Event().wait()

        app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app, brain


@pytest.fixture(autouse=True)
def quiet_background_loops(monkeypatch):
    monkeypatch.setenv('BOTHUB_OUTBOX_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_SCHEDULER_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')


async def make_bot(client, bot_id='scout', **changes):
    body = {'id': bot_id, 'name': bot_id, 'provider': 'fake', 'model': 'fake'} | changes
    response = await client.post('/api/bots', json=body, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


async def make_thread(client, bot_id='scout'):
    response = await client.post('/api/threads', json={'bot_id': bot_id, 'title': 'T'}, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()['id']


async def post_turn(client, thread_id, prompt='hi'):
    response = await client.post(f'/api/threads/{thread_id}/turns', json={'prompt': prompt, 'client': 'api'}, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


async def events(client, thread_id):
    return (await client.get(f'/api/threads/{thread_id}/events', headers=OWNER)).json()


async def get_thread(client, thread_id):
    response = await client.get(f'/api/threads/{thread_id}', headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


async def until(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(0.05)
    pytest.fail('условие не выполнилось за отведённое время')


async def idle(app):
    async with app.state.pool.acquire() as con:
        return not await con.fetchval("select count(*) from bothub.turns where status in ('queued','running','waiting_approval','waiting_mac')")


async def settle(app):
    """Очередь пуста и осталась пустой: автосжатие встаёт в очередь сразу после завершения хода."""
    while True:
        await until(lambda: idle(app))
        await asyncio.sleep(0.3)
        if await idle(app):
            return


async def run_turn(client, app, thread_id, prompt='hi'):
    turn = await post_turn(client, thread_id, prompt)
    await settle(app)
    return turn


async def thread_row(app, thread_id):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow('select * from bothub.threads where id=$1', uuid.UUID(thread_id))


async def turn_types(app, thread_id):
    async with app.state.pool.acquire() as con:
        return [row['turn_type'] for row in await con.fetch('select turn_type from bothub.turns where thread_id=$1 order by created_at,id', uuid.UUID(thread_id))]


async def kinds(client, thread_id, kind):
    return [event for event in await events(client, thread_id) if event['kind'] == kind]


async def add_member(client, app, email='member@example.com'):
    async with app.state.pool.acquire() as con:
        user = await con.fetchval('insert into bothub.users(email,password_hash) values($1,$2) returning id', email, auth.hash_password('long-password'))
    login = await client.post('/api/auth/login', json={'email': email, 'password': 'long-password'})
    assert login.status_code == 200, login.text
    return user, {'Cookie': f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF': login.json()['csrf_token'], 'Origin': 'https://testserver'}


# ---- измерение -----------------------------------------------------------------------------------

async def test_migration_022_adds_columns_defaults_and_bounds():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select 1 from bothub.schema_migrations where name='022_context_compaction.sql'") == 1
            row = await con.fetchrow('select * from bothub.threads where id=$1', uuid.UUID(thread))
            assert row['context_tokens'] is None and row['context_window'] is None and row['compactions'] == 0
            assert row['turns_since_compact'] == 3 and row['auto_compact_disabled'] is False and row['context_estimated'] is False
            with pytest.raises(asyncpg.CheckViolationError):
                await con.execute("update bothub.bots set auto_compact_percent=49")
            with pytest.raises(asyncpg.CheckViolationError):
                await con.execute("insert into bothub.turns(thread_id,prompt,turn_type) values($1,'p','x')", uuid.UUID(thread))
            await con.execute("update bothub.bots set auto_compact_percent=null")  # null допустим: автосжатие выключено


async def test_new_thread_has_empty_context_and_bot_defaults_to_80():
    async with api() as (client, app, _):
        bot = await make_bot(client)
        assert bot['auto_compact_percent'] == 80
        thread = await make_thread(client, bot['id'])
        view = await get_thread(client, thread)
        assert view['context'] == {'tokens': None, 'window': None, 'percent': None, 'estimated': False, 'compacted_at': None, 'compactions': 0}
        listed = (await client.get('/api/threads', headers=OWNER)).json()
        assert listed[0]['context'] == view['context']


async def test_context_comes_from_runner_usage_and_stays_out_of_the_usage_event():
    async with api(Brain(context_tokens=50_000, context_window=200_000)) as (client, app, _):
        await make_bot(client, auto_compact_percent=None)
        thread = await make_thread(client)
        await run_turn(client, app, thread)
        context = (await get_thread(client, thread))['context']
        assert context['tokens'] == 50_000 and context['window'] == 200_000 and context['percent'] == 25
        assert context['estimated'] is False and context['compactions'] == 0
        usage = (await kinds(client, thread, 'usage'))[0]['payload']
        assert usage['tokens_in'] == 10 and not [key for key in usage if key.startswith('context_')]


async def test_context_is_estimated_from_event_bytes_when_runner_gives_none():
    async with api(Brain()) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        await run_turn(client, app, thread, 'q' * 3500)
        context = (await get_thread(client, thread))['context']
        assert context['estimated'] is True and context['tokens'] >= 1000
        assert context['window'] is None and context['percent'] is None  # у fake окна по умолчанию нет


async def test_zero_context_from_runner_is_not_trusted():
    async with api(Brain(context_tokens=0, context_window=200_000)) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        await run_turn(client, app, thread, 'w' * 700)
        context = (await get_thread(client, thread))['context']
        assert context['estimated'] is True and context['tokens'] > 0 and context['window'] == 200_000


async def test_window_comes_from_models_registry_when_runner_gives_none():
    async with api(Brain(context_tokens=61_728)) as (client, app, _):
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            provider = await con.fetchval("insert into bothub.providers(owner_id,kind,cli,name,status) values($1,'cli_subscription','claude','CLI','ok') returning id", owner)
            model = await con.fetchval("insert into bothub.models(provider_id,name,context_window) values($1,'claude-test',123456) returning id", provider)
        await make_bot(client, provider='claude', model='claude-test', provider_id=str(provider), model_id=str(model), auto_compact_percent=None)
        thread = await make_thread(client)
        await run_turn(client, app, thread)
        context = (await get_thread(client, thread))['context']
        assert context['window'] == 123456 and context['percent'] == 50


async def test_foreign_thread_is_404_for_read_and_compact():
    async with api() as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        _, member = await add_member(client, app)
        assert (await client.get(f'/api/threads/{thread}', headers=member)).status_code == 404
        response = await client.post(f'/api/threads/{thread}/compact', headers=member)
        assert response.status_code == 404 and response.json()['error'] == 'not_found'
        assert await turn_types(app, thread) == []


# ---- ручное сжатие --------------------------------------------------------------------------------

async def test_compact_refuses_while_a_turn_is_active_or_there_is_nothing_to_compact():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        nothing = await client.post(f'/api/threads/{thread}/compact', headers=OWNER)
        assert nothing.status_code == 409 and nothing.json()['error'] == 'conflict'
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.threads set cli_session_id='sess-1' where id=$1", uuid.UUID(thread))
        queued = await post_turn(client, thread)
        for status in ('queued', 'running', 'waiting_approval'):
            async with app.state.pool.acquire() as con:
                await con.execute('update bothub.turns set status=$2 where id=$1', uuid.UUID(queued['id']), status)
            response = await client.post(f'/api/threads/{thread}/compact', headers=OWNER)
            assert response.status_code == 409 and response.json()['error'] == 'conflict', status
        assert await turn_types(app, thread) == ['normal']
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='done' where id=$1", uuid.UUID(queued['id']))
        ok = await client.post(f'/api/threads/{thread}/compact', headers=OWNER)
        assert ok.status_code == 200, ok.text
        assert ok.json()['turn_type'] == 'compact' and ok.json()['status'] == 'queued'
        again = await client.post(f'/api/threads/{thread}/compact', headers=OWNER)
        assert again.status_code == 409  # сжатие уже в очереди


async def test_compact_turn_is_invisible_in_the_feed():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.threads set cli_session_id='sess-1' where id=$1", uuid.UUID(thread))
        turn = (await client.post(f'/api/threads/{thread}/compact', headers=OWNER)).json()
        assert turn['prompt'] == ctx.COMPACT_INSTRUCTION
        assert [e for e in await events(client, thread) if e['kind'] == 'user_msg'] == []


async def test_archived_thread_cannot_be_compacted():
    async with api(manual=True) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.threads set cli_session_id='s',status='archived' where id=$1", uuid.UUID(thread))
        assert (await client.post(f'/api/threads/{thread}/compact', headers=OWNER)).status_code == 409


async def test_successful_compact_resets_session_and_next_turn_gets_the_preamble():
    brain = Brain(context_tokens=120_000, context_window=200_000)
    async with api(brain) as (client, app, _):
        await make_bot(client, auto_compact_percent=None)
        thread = await make_thread(client)
        await run_turn(client, app, thread, 'first question')
        await run_turn(client, app, thread, 'second question')
        before = await thread_row(app, thread)
        assert before['cli_session_id'] == 'sess-1' and before['summary'] == ''
        feed_before = [e['seq'] for e in await events(client, thread)]

        response = await client.post(f'/api/threads/{thread}/compact', headers=OWNER)
        assert response.status_code == 200, response.text
        await settle(app)

        row = await thread_row(app, thread)
        assert row['summary'] == SUMMARY and row['cli_session_id'] is None
        assert row['compactions'] == 1 and row['compacted_at'] is not None
        assert row['turns_since_compact'] == 0 and row['context_estimated'] is True
        compacted = (await kinds(client, thread, 'compacted'))[0]
        payload = compacted['payload']
        assert payload['tokens_before'] == 120_000 and payload['auto'] is False
        assert payload['tokens_after'] == row['context_tokens'] < 120_000
        assert payload['summary_chars'] == len(SUMMARY) and payload['reduction_percent'] > 90
        # история не удалена, ход сжатия не оставил user_msg и ответов бота
        feed = await events(client, thread)
        assert set(feed_before) <= {e['seq'] for e in feed}
        assert [e['payload']['text'] for e in feed if e['kind'] == 'user_msg'] == ['first question', 'second question']
        compact_turn = (await get_thread_turns(app, thread, 'compact'))[0]
        assert not [e for e in feed if e['turn_id'] == str(compact_turn) and e['kind'] not in ('status', 'compacted')]
        context = (await get_thread(client, thread))['context']
        assert context['compactions'] == 1 and context['compacted_at'] and context['tokens'] == row['context_tokens']
        # расход хода сжатия записан с типом compact
        async with app.state.pool.acquire() as con:
            usage = {r['turn_type']: r['n'] for r in await con.fetch('select turn_type,count(*) n from bothub.usage group by 1')}
        assert usage == {'normal': 2, 'compact': 1}

        await run_turn(client, app, thread, 'third question')
        third = brain.normal()[-1]
        assert third.cli_session_id is None  # новая сессия CLI
        assert third.prompt.startswith('Summary of previous conversation:')
        assert SUMMARY in third.prompt
        assert '[user]\nfirst question' in third.prompt and '[bot]\nreply 2' in third.prompt  # последние сообщения целиком
        assert third.prompt.endswith('New message from the user:\nthird question')
        shown = [e['payload']['text'] for e in await kinds(client, thread, 'user_msg')]
        assert shown[-1] == 'third question'  # в ленте сообщение владельца без преамбулы
        assert (await thread_row(app, thread))['cli_session_id'] == 'sess-2'

        await run_turn(client, app, thread, 'fourth question')
        fourth = brain.normal()[-1]
        assert fourth.cli_session_id == 'sess-2' and fourth.prompt == 'fourth question'  # дальше обычное продолжение сессии


async def get_thread_turns(app, thread_id, turn_type):
    async with app.state.pool.acquire() as con:
        return [row['id'] for row in await con.fetch('select id from bothub.turns where thread_id=$1 and turn_type=$2', uuid.UUID(thread_id), turn_type)]


async def test_compact_failure_changes_nothing_and_writes_an_error_event():
    brain = Brain(context_tokens=120_000, context_window=200_000, fail_compact=True)
    async with api(brain) as (client, app, _):
        await make_bot(client, auto_compact_percent=None)
        thread = await make_thread(client)
        await run_turn(client, app, thread, 'first')
        before = await thread_row(app, thread)
        assert (await client.post(f'/api/threads/{thread}/compact', headers=OWNER)).status_code == 200
        await settle(app)
        after = await thread_row(app, thread)
        for column in ('summary', 'cli_session_id', 'compactions', 'compacted_at', 'context_tokens', 'context_estimated'):
            assert after[column] == before[column], column
        assert after['turns_since_compact'] == 0
        assert not await kinds(client, thread, 'compacted')
        failed = await kinds(client, thread, 'compact_failed')
        assert len(failed) == 1 and 'cli crashed' in failed[0]['payload']['detail']
        assert (await kinds(client, thread, 'status'))[-1]['payload']['status'] == 'error'
        # тред работоспособен: обычный ход продолжает прежнюю сессию без преамбулы
        brain.fail_compact = False
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set status='idle'")
        await run_turn(client, app, thread, 'second')
        assert brain.normal()[-1].cli_session_id == 'sess-1' and brain.normal()[-1].prompt == 'second'


async def test_empty_summary_counts_as_a_failure():
    async with api(Brain(summary='  \n ')) as (client, app, _):
        await make_bot(client, auto_compact_percent=None)
        thread = await make_thread(client)
        await run_turn(client, app, thread)
        await client.post(f'/api/threads/{thread}/compact', headers=OWNER)
        await settle(app)
        row = await thread_row(app, thread)
        assert row['summary'] == '' and row['cli_session_id'] == 'sess-1' and row['compactions'] == 0
        assert len(await kinds(client, thread, 'compact_failed')) == 1


async def test_summary_is_masked_truncated_and_not_memorized():
    long_part = '\n\n'.join(f'## Part {n}\n' + 'z' * 480 for n in range(60))  # около 30 КиБ
    summary = 'Checked https://shop.example/cart?token=SECRETQUERY&x=1 and remember this fact\n\n' + long_part
    async with api(Brain(summary=summary)) as (client, app, _):
        await make_bot(client, auto_compact_percent=None)
        thread = await make_thread(client)
        await run_turn(client, app, thread)
        await client.post(f'/api/threads/{thread}/compact', headers=OWNER)
        await settle(app)
        stored = (await thread_row(app, thread))['summary']
        assert 'SECRETQUERY' not in stored and 'https://shop.example/cart' in stored  # та же маска, что у вывода браузера
        assert len(stored.encode()) <= ctx.SUMMARY_MAX_BYTES and stored.endswith('z' * 480)  # обрезано по абзацу
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.memory') == 0


async def test_model_switch_resets_context_together_with_the_session():
    async with api(Brain(context_tokens=1000, context_window=2000)) as (client, app, _):
        bot = await make_bot(client, auto_compact_percent=None)
        thread = await make_thread(client, bot['id'])
        await run_turn(client, app, thread)
        assert (await get_thread(client, thread))['context']['tokens'] == 1000
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            provider = await con.fetchval("insert into bothub.providers(owner_id,kind,cli,name,status) values($1,'cli_subscription','claude','CLI','ok') returning id", owner)
            model = await con.fetchval("insert into bothub.models(provider_id,name) values($1,'claude-test') returning id", provider)
        patched = await client.patch(f"/api/bots/{bot['id']}", json={'provider_id': str(provider), 'model_id': str(model)}, headers=OWNER)
        assert patched.status_code == 200, patched.text
        row = await thread_row(app, thread)
        assert row['cli_session_id'] is None and row['context_tokens'] is None and row['context_window'] is None


# ---- автосжатие -----------------------------------------------------------------------------------

async def compact_count(app, thread_id):
    return len(await get_thread_turns(app, thread_id, 'compact'))


async def test_auto_compact_fires_at_threshold_and_is_rate_limited():
    brain = Brain(context_tokens=170_000, context_window=200_000)  # 85 процентов при пороге 80
    async with api(brain) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        await run_turn(client, app, thread, 'one')
        assert await compact_count(app, thread) == 1
        assert (await get_thread(client, thread))['context']['compactions'] == 1
        compacted = (await kinds(client, thread, 'compacted'))[0]['payload']
        assert compacted['auto'] is True and compacted['tokens_before'] == 170_000
        assert not await kinds(client, thread, 'auto_compact_disabled')
        # защита от цикла: после сжатия два хода подряд без нового сжатия, третий запускает
        for prompt in ('two', 'three'):
            await run_turn(client, app, thread, prompt)
            assert await compact_count(app, thread) == 1, prompt
        await run_turn(client, app, thread, 'four')
        assert await compact_count(app, thread) == 2
        # сжатие шло между ходами: обычные ходы не пропущены и идут по порядку
        types = await turn_types(app, thread)
        assert types == ['normal', 'compact', 'normal', 'normal', 'normal', 'compact']


@pytest.mark.parametrize('brain_args,changes', [
    ({'context_tokens': 159_000, 'context_window': 200_000}, {}),  # 79,5 процента при пороге 80
    ({'context_tokens': 190_000}, {}),  # окна нет ни у раннера, ни у fake по умолчанию
    ({'context_tokens': 190_000, 'context_window': 200_000}, {'auto_compact_percent': None}),
], ids=['below-threshold', 'no-window', 'disabled'])
async def test_auto_compact_below_threshold_or_without_window_or_disabled_does_nothing(brain_args, changes):
    async with api(Brain(**brain_args)) as (client, app, _):
        await make_bot(client, **changes)
        thread = await make_thread(client)
        for prompt in ('a', 'b', 'c', 'd'):
            await run_turn(client, app, thread, prompt)
        assert await compact_count(app, thread) == 0


async def test_bot_setting_can_be_changed_disabled_and_is_validated():
    async with api(Brain(context_tokens=150_000, context_window=200_000)) as (client, app, _):  # 75 процентов
        bot = await make_bot(client)
        thread = await make_thread(client, bot['id'])
        await run_turn(client, app, thread, 'a')
        assert await compact_count(app, thread) == 0  # порог 80, заполнено 75
        patched = await client.patch(f"/api/bots/{bot['id']}", json={'auto_compact_percent': 70}, headers=OWNER)
        assert patched.status_code == 200 and patched.json()['auto_compact_percent'] == 70
        await run_turn(client, app, thread, 'b')
        assert await compact_count(app, thread) == 1
        off = await client.patch(f"/api/bots/{bot['id']}", json={'auto_compact_percent': None}, headers=OWNER)
        assert off.status_code == 200 and off.json()['auto_compact_percent'] is None
        for bad in (49, 96, 'x', 80.5):
            response = await client.patch(f"/api/bots/{bot['id']}", json={'auto_compact_percent': bad}, headers=OWNER)
            assert response.status_code in (400, 422) and response.json()['error'] == 'invalid', bad
        created = await client.post('/api/bots', json={'id': 'second', 'name': 's', 'provider': 'fake', 'model': 'fake', 'auto_compact_percent': 96}, headers=OWNER)
        assert created.status_code in (400, 422)


async def test_auto_compact_that_barely_helps_disables_itself_with_an_event():
    summary = '\n\n'.join(f'## Part {n}\n' + 'z' * 190 for n in range(70))  # около 14 КБ: после сжатия контекст почти тот же
    brain = Brain(context_tokens=5000, context_window=6000, summary=summary)  # 83 процента
    async with api(brain) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        await run_turn(client, app, thread, 'one')
        assert await compact_count(app, thread) == 1
        disabled = await kinds(client, thread, 'auto_compact_disabled')
        assert len(disabled) == 1 and disabled[0]['payload']['reduction_percent'] < ctx.MIN_REDUCTION_PERCENT
        assert (await thread_row(app, thread))['auto_compact_disabled'] is True
        for prompt in ('a', 'b', 'c', 'd', 'e'):
            await run_turn(client, app, thread, prompt)
        assert await compact_count(app, thread) == 1  # больше не пытается
        # ручное сжатие по-прежнему работает
        assert (await client.post(f'/api/threads/{thread}/compact', headers=OWNER)).status_code == 200


async def test_failed_auto_compact_is_retried_only_after_three_turns():
    brain = Brain(context_tokens=170_000, context_window=200_000, fail_compact=True)
    async with api(brain) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        await run_turn(client, app, thread, 'one')
        assert await compact_count(app, thread) == 1 and len(await kinds(client, thread, 'compact_failed')) == 1
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set status='idle'")
        for prompt in ('two', 'three'):
            await run_turn(client, app, thread, prompt)
            assert await compact_count(app, thread) == 1, prompt
        await run_turn(client, app, thread, 'four')
        assert await compact_count(app, thread) == 2


async def test_auto_compact_is_not_scheduled_mid_turn():
    gate = asyncio.Event()

    class Slow(Brain):
        async def run(self, turn):
            async for event in super().run(turn):
                if event.kind == 'usage':
                    await gate.wait()
                yield event

    brain = Slow(context_tokens=190_000, context_window=200_000)
    async with api(brain) as (client, app, _):
        await make_bot(client)
        thread = await make_thread(client)
        await post_turn(client, thread, 'busy')
        await asyncio.sleep(0.5)
        assert await turn_types(app, thread) == ['normal']  # ход ещё идёт, сжатия в очереди нет
        gate.set()
        await settle(app)
        assert await turn_types(app, thread) == ['normal', 'compact']
