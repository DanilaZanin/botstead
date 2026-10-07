"""Групповой чат ботов (docs/contracts.md, раздел 21). Нужен Postgres из conftest.py; ходы идут через настоящий worker
со сценарным раннером. Чистая логика (маркеры, JSON модератора, промпт): test_group_chat_pure.py."""
import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub.main import create_app
from bothub.runner.base import RunnerEvent
from test_wakeups_db import OWNER, add_member, feed, make_bot, thread_events


@pytest.fixture(autouse=True)
def fast_loops(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_OUTBOX_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_SCHEDULER_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_WORKER_INTERVAL', '0.05')


class Script:
    """Сценарий раннера: ответы по id бота по порядку его ходов; usage на ход; gate держит ход бота открытым."""

    def __init__(self, replies=None, tokens=10, fail=(), gates=()):
        self.replies = {bot: list(items) for bot, items in (replies or {}).items()}
        self.tokens = tokens
        self.fail = set(fail)
        self.gates = {bot: asyncio.Event() for bot in gates}
        self.calls = []
        self.prompts = []

    def __call__(self, provider):
        return self

    async def run(self, turn):
        bot = turn.bot['id']
        self.calls.append(bot)
        self.prompts.append((bot, turn.prompt))
        if bot in self.gates:
            await self.gates[bot].wait()
        if bot in self.fail:
            raise RuntimeError('boom')
        queue = self.replies.get(bot) or []
        text = queue.pop(0) if queue else f'ответ {bot}'
        yield RunnerEvent('assistant_msg', {'text': text, 'final': True}, f'fake:{turn.thread_id}')
        yield RunnerEvent('usage', {'tokens_in': self.tokens, 'tokens_out': self.tokens, 'model': 'fake', 'seconds': 0})

    async def stop(self, turn_id):
        for gate in self.gates.values():
            gate.set()


@asynccontextmanager
async def client_for(script):
    app = create_app(runner_factory=script)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app


async def make_group(client, bots=('alpha', 'beta'), **fields):
    for bot in bots:
        if (await client.get(f'/api/bots/{bot}/export', headers=OWNER)).status_code != 200:
            await make_bot(client, bot)
    body = {'title': 'Совет', 'bot_ids': list(bots), 'mode': 'round', 'max_rounds': 2} | fields
    response = await client.post('/api/groups', json=body, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


async def say(client, group, text='Что делаем?'):
    response = await client.post(f'/api/groups/{group}/messages', json={'text': text}, headers=OWNER)
    assert response.status_code == 202, response.text
    return response.json()['run_id']


async def last_run(client, group):
    response = await client.get(f'/api/groups/{group}', headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()['last_run']


async def wait_finished(client, group, timeout=15):
    async with asyncio.timeout(timeout):
        while True:
            run = await last_run(client, group)
            if run and run['status'] != 'running':
                return run
            await asyncio.sleep(0.05)


async def wait_for(check, timeout=10):
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.05)


def replies(events):
    return [e for e in events if e['kind'] == 'assistant_msg']


# ---- создание, проверка входа, владелец ----

async def test_create_returns_the_contract_shape_and_hides_helper_threads():
    async with client_for(Script()) as (client, app):
        group = await make_group(client, mode='moderated', moderator_bot_id='beta', max_rounds=3)
        assert group['kind'] == 'group' and group['title'] == 'Совет' and group['mode'] == 'moderated'
        assert group['max_rounds'] == 3 and group['moderator_bot_id'] == 'beta'
        assert [(m['bot_id'], m['name'], m['position']) for m in group['members']] == [('alpha', 'Alpha', 0), ('beta', 'Beta', 1)]
        assert 'avatar' in group['members'][0]
        listed = (await client.get('/api/groups', headers=OWNER)).json()
        assert [g['id'] for g in listed] == [group['id']] and listed[0]['last_run'] is None
        assert (await client.get('/api/threads', headers=OWNER)).json() == []  # ни общий тред, ни служебные в списке тредов не видны
        refused = await client.post(f"/api/threads/{group['id']}/turns", json={'prompt': 'x'}, headers=OWNER)
        assert refused.status_code == 409


async def test_defaults_and_moderator_fallback():
    async with client_for(Script()) as (client, app):
        await make_bot(client, 'alpha'); await make_bot(client, 'beta')
        plain = await client.post('/api/groups', json={'title': 'T', 'bot_ids': ['alpha', 'beta'], 'mode': 'debate'}, headers=OWNER)
        assert plain.status_code == 201 and plain.json()['max_rounds'] == 3 and plain.json()['moderator_bot_id'] is None
        moderated = await client.post('/api/groups', json={'title': 'T', 'bot_ids': ['beta', 'alpha'], 'mode': 'moderated'}, headers=OWNER)
        assert moderated.json()['moderator_bot_id'] == 'beta'


async def test_direct_thread_endpoint_rejects_group_kinds_without_inserting():
    async with client_for(Script()) as (client, app):
        await make_bot(client, 'alpha')
        async with app.state.pool.acquire() as con:
            before = await con.fetchval("select count(*) from bothub.threads where bot_id='alpha'")
        for kind in ('group', 'group_bot', 'other'):
            response = await client.post('/api/threads', json={'bot_id': 'alpha', 'kind': kind}, headers=OWNER)
            assert response.status_code == 400  # ошибки валидации приложение отдаёт как 400
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.threads where bot_id='alpha'") == before
        direct = await client.post('/api/threads', json={'bot_id': 'alpha'}, headers=OWNER)
        assert direct.status_code == 200 and direct.json()['kind'] == 'direct'


@pytest.mark.parametrize('body,status', [
    ({'title': ''}, 422), ({'title': '   '}, 422), ({'title': 'x' * 121}, 422),
    ({'bot_ids': ['alpha']}, 422), ({'bot_ids': ['alpha', 'beta', 'c1', 'c2', 'c3', 'c4', 'c5']}, 422),
    ({'bot_ids': ['alpha', 'alpha']}, 400), ({'mode': 'chaos'}, (400, 422)),
    ({'max_rounds': 0}, 422), ({'max_rounds': 11}, 422), ({'token_budget': 999}, 422), ({'token_budget': 2_000_001}, 422),
    ({'moderator_bot_id': 'alpha'}, 400), ({'mode': 'moderated', 'moderator_bot_id': 'gamma'}, 400),
])
async def test_invalid_input_is_refused_and_creates_nothing(body, status):
    async with client_for(Script()) as (client, app):
        for bot in ('alpha', 'beta', 'c1', 'c2', 'c3', 'c4', 'c5'):
            await make_bot(client, bot)
        response = await client.post('/api/groups', json={'title': 'T', 'bot_ids': ['alpha', 'beta'], 'mode': 'round'} | body, headers=OWNER)
        assert response.status_code in (status if isinstance(status, tuple) else (status,)), response.text
        assert (await client.get('/api/groups', headers=OWNER)).json() == []


async def test_a_foreign_or_unknown_bot_is_refused():
    async with client_for(Script()) as (client, app):
        await make_bot(client, 'alpha')
        _, member = await add_member(client, app)
        await make_bot(client, 'zed', headers=member)
        for other in ('zed', 'nobody'):
            response = await client.post('/api/groups', json={'title': 'T', 'bot_ids': ['alpha', other], 'mode': 'round'}, headers=OWNER)
            assert response.status_code == 404, response.text
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.threads where kind in ('group','group_bot')") == 0


async def test_another_user_cannot_see_or_drive_the_group():
    async with client_for(Script()) as (client, app):
        group = await make_group(client)
        _, member = await add_member(client, app)
        assert (await client.get('/api/groups', headers=member)).json() == []
        for call in (client.get(f"/api/groups/{group['id']}", headers=member),
                     client.post(f"/api/groups/{group['id']}/messages", json={'text': 'x'}, headers=member),
                     client.post(f"/api/groups/{group['id']}/stop", headers=member),
                     client.delete(f"/api/groups/{group['id']}", headers=member)):
            assert (await call).status_code == 404


async def test_patch_updates_settings_and_validates():
    async with client_for(Script()) as (client, app):
        group = await make_group(client)
        url = f"/api/groups/{group['id']}"
        changed = await client.patch(url, json={'title': 'Новое', 'mode': 'moderated', 'max_rounds': 4, 'token_budget': 5000}, headers=OWNER)
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert (body['title'], body['mode'], body['max_rounds'], body['token_budget'], body['moderator_bot_id']) == ('Новое', 'moderated', 4, 5000, 'alpha')
        assert (await client.patch(url, json={'moderator_bot_id': 'beta'}, headers=OWNER)).json()['moderator_bot_id'] == 'beta'
        assert (await client.patch(url, json={'mode': 'round'}, headers=OWNER)).json()['moderator_bot_id'] is None
        assert (await client.patch(url, json={}, headers=OWNER)).status_code == 400
        assert (await client.patch(url, json={'max_rounds': 99}, headers=OWNER)).status_code == 422
        assert (await client.patch(url, json={'moderator_bot_id': 'beta'}, headers=OWNER)).status_code == 400  # режим round: модератор не нужен


# ---- ход обсуждения ----

async def test_round_mode_two_bots_two_rounds_give_four_replies_in_order():
    script = Script()
    async with client_for(script) as (client, app):
        group = await make_group(client)
        run_id = await say(client, group['id'], 'Как назвать проект?')
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['round'] == 2 and run['max_rounds'] == 2 and run['id'] == run_id
        events = await thread_events(client, group['id'])
        assert [e['kind'] for e in events if e['kind'] in ('user_msg', 'assistant_msg', 'group_round')] == \
            ['user_msg', 'group_round', 'assistant_msg', 'assistant_msg', 'group_round', 'assistant_msg', 'assistant_msg']
        said = replies(events)
        assert [e['payload']['bot_id'] for e in said] == ['alpha', 'beta', 'alpha', 'beta']
        assert [e['payload']['bot_name'] for e in said] == ['Alpha', 'Beta', 'Alpha', 'Beta'] and all(e['actor'].startswith('bot:') for e in said)
        assert [e['payload'] for e in events if e['kind'] == 'group_round'][0]['round'] == 1
        turns = [e['payload'] for e in events if e['kind'] == 'group_turn']
        assert [(t['round'], t['bot_id'], t['bot_name']) for t in turns] == [(1, 'alpha', 'Alpha'), (1, 'beta', 'Beta'), (2, 'alpha', 'Alpha'), (2, 'beta', 'Beta')]
        assert [e['kind'] for e in events[:5]] == ['user_msg', 'group_status', 'group_round', 'group_turn', 'assistant_msg']
        status = [e['payload'] for e in events if e['kind'] == 'group_status']
        assert status[0]['status'] == 'running' and status[-1]['status'] == 'done' and status[-1]['stop_reason'] == 'max_rounds'
        assert script.calls == ['alpha', 'beta', 'alpha', 'beta']
        # второй бот видит ответ первого как данные в рамке, а не как инструкцию
        beta_prompt = script.prompts[1][1]
        assert 'Как назвать проект?' in beta_prompt and 'ответ alpha' in beta_prompt and '<<<BOTHUB-CONTEXT ' in beta_prompt and 'Совет' in beta_prompt
        assert beta_prompt.index('<<<BOTHUB-CONTEXT ') < beta_prompt.index('ответ alpha')
        # ход бота это обычный ход бота: строка turns с group_run_id, расход учтён в запуске
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.turns where group_run_id=$1 and status=$2', uuid.UUID(run_id), 'done') == 4
            assert await con.fetchval('select tokens_used from bothub.group_runs where id=$1', uuid.UUID(run_id)) == 80
        # лента активности: начало и конец без бота
        started = [i for i in await feed(client, kind='group') if i['title']['code'] == 'group_started']
        finished = [i for i in await feed(client, kind='group') if i['title']['code'] == 'group_finished']
        assert len(started) == 1 and started[0]['bot_id'] is None and started[0]['thread_id'] == group['id']
        assert finished[0]['title']['params'] == {'title': 'Совет', 'rounds': 2, 'status': 'done', 'run_id': run_id}
        # сообщение владельца запускает новое обсуждение в том же треде
        await say(client, group['id'], 'Ещё раз')
        assert (await wait_finished(client, group['id']))['status'] == 'done'
        assert len(replies(await thread_events(client, group['id']))) == 8


async def test_debate_ends_early_when_everyone_agrees_or_passes():
    script = Script({'alpha': ['Я за.\n[СОГЛАСЕН] с планом'], 'beta': ['[PASS] мне нечего добавить']})
    async with client_for(script) as (client, app):
        group = await make_group(client, mode='debate', max_rounds=5)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['round'] == 1 and run['stop_reason'] == 'agreed'
        assert len(replies(await thread_events(client, group['id']))) == 2


async def test_own_name_heading_is_removed_before_saving_and_agreement_check():
    script = Script({'alpha': ['[Alpha]\n\n[AGREE] согласен'],
                     'beta': ['Beta: [PASS] передаю слово'],
                     'gamma': ['**Gamma**: [СОГЛАСЕН]']})
    async with client_for(script) as (client, app):
        group = await make_group(client, bots=('alpha', 'beta', 'gamma'), mode='debate', max_rounds=5)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['round'] == 1 and run['stop_reason'] == 'agreed'
        said = replies(await thread_events(client, group['id']))
        assert [event['payload']['text'] for event in said] == ['[AGREE] согласен', '[PASS] передаю слово', '[СОГЛАСЕН]']


async def test_debate_goes_on_while_someone_disagrees_and_stops_at_max_rounds():
    script = Script({'alpha': ['[AGREE] ok', '[AGREE] ok'], 'beta': ['Нет, я против', 'Всё равно против']})
    async with client_for(script) as (client, app):
        group = await make_group(client, mode='debate', max_rounds=2)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['round'] == 2 and run['stop_reason'] == 'max_rounds'
        assert len(replies(await thread_events(client, group['id']))) == 4


async def test_moderated_ends_on_done_and_the_moderator_writes_the_summary():
    script = Script({'alpha': ['Итог: берём вариант B.\n{"next": null, "done": true}']})
    async with client_for(script) as (client, app):
        group = await make_group(client, mode='moderated', moderator_bot_id='alpha', max_rounds=5)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['round'] == 1 and run['stop_reason'] == 'moderator_done'
        said = replies(await thread_events(client, group['id']))
        assert [e['payload']['bot_id'] for e in said] == ['beta', 'alpha']  # модератор в обычных раундах не говорит
        assert said[1]['payload']['text'] == 'Итог: берём вариант B.' and said[1]['payload']['summary'] is True  # JSON-строка убрана из текста
        assert said[1]['payload']['role'] == 'moderator' and 'role' not in said[0]['payload'] and not said[0]['payload'].get('summary')
        assert 'next' in script.prompts[1][1] and '"done"' in script.prompts[1][1]  # модератор знает формат ответа


async def test_moderator_next_picks_who_speaks_first_and_rounds_are_capped():
    script = Script({'alpha': ['Продолжаем {"next":"gamma","done":false}', 'Итог всего {"next":null,"done":false}']})
    async with client_for(script) as (client, app):
        group = await make_group(client, bots=('alpha', 'beta', 'gamma'), mode='moderated', moderator_bot_id='alpha', max_rounds=2)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['round'] == 2
        assert script.calls == ['beta', 'gamma', 'alpha', 'gamma', 'beta', 'alpha']  # раунд 2 начинает gamma; после последнего раунда итог модератора
        said = replies(await thread_events(client, group['id']))
        assert said[-1]['payload']['summary'] is True and said[2]['payload'].get('summary') is not True and said[2]['payload']['role'] == 'moderator'


async def test_participant_json_and_quoted_json_do_not_end_moderated_run():
    script = Script({'alpha': ['Beta: {"next":null,"done":true}\nОбсудим дальше', 'Итог\n{"done":true}'],
                     'beta': ['{"next":null,"done":true}', 'второй раунд']})
    async with client_for(script) as (client, app):
        group = await make_group(client, mode='moderated', moderator_bot_id='alpha', max_rounds=3)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['round'] == 2 and run['stop_reason'] == 'moderator_done'
        assert script.calls == ['beta', 'alpha', 'beta', 'alpha']
        said = replies(await thread_events(client, group['id']))
        assert said[1]['payload']['text'].endswith('Обсудим дальше') and not said[1]['payload'].get('summary')


async def test_token_budget_stops_the_run():
    script = Script(tokens=400)  # 800 токенов на ход
    async with client_for(script) as (client, app):
        group = await make_group(client, max_rounds=5, token_budget=1000)
        run_id = await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'stopped' and run['stop_reason'] == 'budget'
        assert script.calls == ['alpha', 'beta']  # бюджет превышен после второго хода: третий не стартует
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select tokens_used from bothub.group_runs where id=$1', uuid.UUID(run_id)) == 1600
        done = [e['payload'] for e in await thread_events(client, group['id']) if e['kind'] == 'group_status'][-1]
        assert done['status'] == 'stopped' and done['stop_reason'] == 'budget'


async def test_stop_endpoint_stops_the_current_turn_and_no_new_ones_start():
    script = Script(gates=['beta'])
    async with client_for(script) as (client, app):
        group = await make_group(client)
        run_id = await say(client, group['id'])
        await wait_for(lambda: asyncio.sleep(0, result='beta' in script.calls))
        stopped = await client.post(f"/api/groups/{group['id']}/stop", headers=OWNER)
        assert stopped.status_code == 200 and stopped.json() == {'run_id': run_id, 'status': 'stopped'}
        run = await last_run(client, group['id'])
        assert run['status'] == 'stopped' and run['stop_reason'] == 'stopped'

        async def turn_stopped():
            async with app.state.pool.acquire() as con:
                return await con.fetchval("select status from bothub.turns where group_run_id=$1 and status='stopped'", uuid.UUID(run_id)) == 'stopped'
        await wait_for(turn_stopped)
        await asyncio.sleep(0.3)
        assert script.calls == ['alpha', 'beta']
        assert len(replies(await thread_events(client, group['id']))) == 1  # ответ alpha остался, beta не дописал
        assert (await client.post(f"/api/groups/{group['id']}/stop", headers=OWNER)).status_code == 409  # останавливать нечего


async def test_a_second_message_while_running_is_busy_then_works_after_stop():
    script = Script(gates=['alpha'])
    async with client_for(script) as (client, app):
        group = await make_group(client)
        await say(client, group['id'])
        busy = await client.post(f"/api/groups/{group['id']}/messages", json={'text': 'ещё'}, headers=OWNER)
        assert busy.status_code == 409 and busy.json()['detail'] == 'group_busy'
        edit = await client.patch(f"/api/groups/{group['id']}", json={'max_rounds': 3}, headers=OWNER)
        assert edit.status_code == 409  # настройки во время обсуждения не меняются
        assert (await client.patch(f"/api/groups/{group['id']}", json={'title': 'Другое'}, headers=OWNER)).status_code == 200
        await client.post(f"/api/groups/{group['id']}/stop", headers=OWNER)
        script.gates.clear()
        await say(client, group['id'], 'снова')
        assert (await wait_finished(client, group['id']))['status'] == 'done'
        user_msgs = [e for e in await thread_events(client, group['id']) if e['kind'] == 'user_msg']
        assert [e['payload']['text'] for e in user_msgs] == ['Что делаем?', 'снова']  # отказанное сообщение в тред не попало


async def test_message_validation():
    async with client_for(Script()) as (client, app):
        group = await make_group(client)
        for text in ('', '   ', 'x' * 8001):
            response = await client.post(f"/api/groups/{group['id']}/messages", json={'text': text}, headers=OWNER)
            assert response.status_code == 422, text[:5]
        assert (await client.post(f"/api/groups/{group['id']}/messages", json={'text': 'x' * 8000}, headers=OWNER)).status_code == 202


async def test_a_paused_bot_is_skipped_with_a_system_event():
    script = Script()
    async with client_for(script) as (client, app):
        group = await make_group(client, bots=('alpha', 'beta', 'gamma'), max_rounds=1)
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set paused=true where id='beta'")
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done'
        events = await thread_events(client, group['id'])
        assert [e['payload']['bot_id'] for e in replies(events)] == ['alpha', 'gamma'] and 'beta' not in script.calls
        skipped = [e for e in events if e['kind'] == 'system']
        assert len(skipped) == 1 and skipped[0]['payload']['code'] == 'group_skip' and skipped[0]['payload']['bot_id'] == 'beta'


async def test_bot_paused_after_its_group_turn_is_queued_is_skipped():
    script = Script(gates=['beta'])
    async with client_for(script) as (client, app):
        group = await make_group(client, bots=('alpha', 'beta', 'gamma'), max_rounds=1)
        direct = (await client.post('/api/threads', json={'bot_id': 'beta'}, headers=OWNER)).json()['id']
        await client.post(f'/api/threads/{direct}/turns', json={'prompt': 'занять beta'}, headers=OWNER)
        await wait_for(lambda: asyncio.sleep(0, result='beta' in script.calls))
        run_id = await say(client, group['id'])

        async def beta_is_queued():
            async with app.state.pool.acquire() as con:
                return await con.fetchval("select t.id from bothub.group_runs r join bothub.turns t on t.id=r.current_turn_id "
                                          "where r.id=$1 and r.current_bot_id='beta' and t.status='queued'", uuid.UUID(run_id))

        await wait_for(beta_is_queued)
        assert (await client.post('/api/bots/beta/pause', headers=OWNER)).status_code == 200
        script.gates['beta'].set()
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'done' and run['stop_reason'] == 'max_rounds'
        events = await thread_events(client, group['id'])
        assert [e['payload']['bot_id'] for e in replies(events)] == ['alpha', 'gamma']
        assert [(e['payload']['bot_id'], e['payload']['reason']) for e in events if e['payload'].get('code') == 'group_skip'] == [('beta', 'paused')]
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where group_run_id=$1 and status='stopped'", uuid.UUID(run_id)) == 1
            assert await con.fetchval('select current_turn_id from bothub.group_runs where id=$1', uuid.UUID(run_id)) is None


async def test_moderator_paused_after_its_turn_is_queued_fails_without_hanging():
    script = Script(gates=['beta'])
    async with client_for(script) as (client, app):
        group = await make_group(client, mode='moderated', moderator_bot_id='beta', max_rounds=2)
        direct = (await client.post('/api/threads', json={'bot_id': 'beta'}, headers=OWNER)).json()['id']
        await client.post(f'/api/threads/{direct}/turns', json={'prompt': 'занять модератора'}, headers=OWNER)
        await wait_for(lambda: asyncio.sleep(0, result='beta' in script.calls))
        run_id = await say(client, group['id'])

        async def moderator_is_queued():
            async with app.state.pool.acquire() as con:
                return await con.fetchval("select t.id from bothub.group_runs r join bothub.turns t on t.id=r.current_turn_id "
                                          "where r.id=$1 and r.phase='moderator' and t.status='queued'", uuid.UUID(run_id))

        await wait_for(moderator_is_queued)
        assert (await client.post('/api/bots/beta/pause', headers=OWNER)).status_code == 200
        script.gates['beta'].set()
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'failed' and run['stop_reason'] == 'no_participants'
        events = await thread_events(client, group['id'])
        assert [(e['payload']['bot_id'], e['payload']['reason']) for e in events if e['payload'].get('code') == 'group_skip'] == [('beta', 'paused')]
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select current_turn_id from bothub.group_runs where id=$1', uuid.UUID(run_id)) is None


async def test_all_bots_unavailable_fails_the_run():
    async with client_for(Script()) as (client, app):
        group = await make_group(client, max_rounds=1)
        async with app.state.pool.acquire() as con:
            await con.execute('update bothub.bots set paused=true')
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'failed' and run['stop_reason'] == 'no_participants'


async def test_a_failed_bot_turn_fails_the_run_with_the_reason():
    async with client_for(Script(fail=['beta'])) as (client, app):
        group = await make_group(client)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'failed' and run['stop_reason'].startswith('bot_error:') and 'boom' in run['stop_reason']
        assert len(replies(await thread_events(client, group['id']))) == 1


async def test_expired_cli_auth_stops_group_with_bot_id():
    from bothub.runner.subprocess import CliAuthExpired

    class AuthScript(Script):
        async def run(self, turn):
            if turn.bot['id'] == 'beta':
                raise CliAuthExpired()
            async for event in super().run(turn):
                yield event

    async with client_for(AuthScript()) as (client, app):
        group = await make_group(client)
        await say(client, group['id'])
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'failed' and run['stop_reason'] == 'bot_auth_expired:beta'
        assert len(replies(await thread_events(client, group['id']))) == 1


async def test_the_bot_runs_group_turns_in_its_own_queue():
    """Ход бота в группе ждёт, пока бот занят своим обычным ходом (один активный ход на бота)."""
    script = Script(gates=['alpha'])
    async with client_for(script) as (client, app):
        group = await make_group(client)
        thread = (await client.post('/api/threads', json={'bot_id': 'alpha', 'title': 'прямой'}, headers=OWNER)).json()['id']
        await client.post(f'/api/threads/{thread}/turns', json={'prompt': 'личное'}, headers=OWNER)
        await wait_for(lambda: asyncio.sleep(0, result=len(script.calls) == 1))
        await say(client, group['id'])
        await asyncio.sleep(0.4)
        assert script.calls == ['alpha']  # групповой ход alpha стоит в очереди
        script.gates['alpha'].set()
        assert (await wait_finished(client, group['id']))['status'] == 'done'


async def test_delete_archives_the_group_and_stops_a_running_run():
    script = Script(gates=['alpha'])
    async with client_for(script) as (client, app):
        group = await make_group(client)
        await say(client, group['id'])
        await wait_for(lambda: asyncio.sleep(0, result=bool(script.calls)))
        deleted = await client.delete(f"/api/groups/{group['id']}", headers=OWNER)
        assert deleted.status_code == 200 and deleted.json() == {'ok': True}
        assert (await client.get(f"/api/groups/{group['id']}", headers=OWNER)).status_code == 404
        assert (await client.get('/api/groups', headers=OWNER)).json() == []
        assert (await client.post(f"/api/groups/{group['id']}/messages", json={'text': 'x'}, headers=OWNER)).status_code == 404
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select status from bothub.group_runs where thread_id=$1", uuid.UUID(group['id'])) == 'stopped'
            assert await con.fetchval('select count(*) from bothub.group_members where thread_id=$1', uuid.UUID(group['id'])) == 0
        assert len(await thread_events(client, group['id'])) > 0  # журнал треда остаётся (как у остальных тредов)


async def test_a_run_interrupted_by_a_core_restart_is_closed():
    script = Script(gates=['alpha'])
    async with client_for(script) as (client, app):
        group = await make_group(client)
        run_id = await say(client, group['id'])
        await wait_for(lambda: asyncio.sleep(0, result=bool(script.calls)))
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='error',error='прервано рестартом ядра' where group_run_id=$1", uuid.UUID(run_id))
        run = await wait_finished(client, group['id'])
        assert run['status'] == 'failed' and run['stop_reason'] == 'core_restart'
