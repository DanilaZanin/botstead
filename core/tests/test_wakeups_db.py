"""Самопробуждение бота (docs/contracts.md, раздел 17). Нужен Postgres из conftest.py.
Чистая логика (срок, пределы, окно повтора, решение планировщика, MCP-инструмент): test_wakeups_pure.py."""
import asyncio
import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from bothub import auth, wakeups
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}


@pytest.fixture(autouse=True)
def quiet_loops(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_OUTBOX_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_SCHEDULER_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_WORKER_INTERVAL', '3600')


@asynccontextmanager
async def client_for(**options):
    """Worker, scheduler и outbox заменены пустыми циклами: проходы планировщика гоняются руками через app.state."""
    app = create_app(**options)

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
    client.cookies.clear()  # вход участника оставил cookie сессии в клиенте: без очистки запросы владельца шли бы от участника
    return user, headers


def bot_headers(bot_id):
    digest = hmac.new(b'test-secret', bot_id.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'Bearer bot:{bot_id}:{digest}'}


async def make_bot(client, bot_id='alpha', headers=OWNER, **fields):
    response = await client.post('/api/bots', json={'id': bot_id, 'name': bot_id.title(), 'provider': 'fake', 'model': 'fake'} | fields,
                                 headers=headers)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def make_thread(client, bot_id='alpha', headers=OWNER, title='T'):
    response = await client.post('/api/threads', json={'bot_id': bot_id, 'title': title}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['id']


async def ask(client, bot_id='alpha', **body):
    """Вызов бота: POST /api/bots/wakeups с токеном бота."""
    return await client.post('/api/bots/wakeups', json=body, headers=bot_headers(bot_id))


async def create(client, bot_id='alpha', **body):
    response = await ask(client, bot_id, **({'prompt': 'Проверь сборку', 'in_minutes': 30} | body))
    assert response.status_code == 201, response.text
    return response.json()


async def feed(client, **params):
    response = await client.get('/api/activity', params=params, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()['items']


async def feed_items(client, code):
    return [item for item in await feed(client, kind='schedule') if item['title']['code'] == code]


async def thread_events(client, thread_id):
    response = await client.get(f'/api/threads/{thread_id}/events', headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


async def row_of(app, wakeup_id):
    async with app.state.pool.acquire() as con:
        return await con.fetchrow('select *, now() as db_now from bothub.wakeups where id=$1', uuid.UUID(wakeup_id))


async def make_due(app, wakeup_id, overdue='1 minute'):
    async with app.state.pool.acquire() as con:
        await con.execute(f"update bothub.wakeups set scheduled_at=now()-interval '{overdue}' where id=$1", uuid.UUID(wakeup_id))


async def wakeup_turns(app, bot_id='alpha'):
    async with app.state.pool.acquire() as con:
        return await con.fetch("select t.*, th.id as th_id from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                               "where th.bot_id=$1 and t.client='wakeup' order by t.created_at", bot_id)


async def count_rows(app, where='true', *args):
    async with app.state.pool.acquire() as con:
        return await con.fetchval(f'select count(*) from bothub.wakeups where {where}', *args)


# ---- создание ----

async def test_bot_creates_a_wakeup_in_its_thread_and_the_feed_shows_it():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        before = datetime.now(timezone.utc)
        created = await create(client, in_minutes=30, reason='жду CI', thread_id=thread)
        assert created['deduplicated'] is False and created['status'] == 'active' and created['bot_id'] == 'alpha'
        assert created['prompt'] == 'Проверь сборку' and created['reason'] == 'жду CI' and created['thread_id'] == thread
        when = datetime.fromisoformat(created['scheduled_at'].replace('Z', '+00:00'))
        assert before + timedelta(minutes=29, seconds=50) <= when <= datetime.now(timezone.utc) + timedelta(minutes=30, seconds=10)
        items = await feed_items(client, 'wakeup_scheduled')
        assert len(items) == 1 and items[0]['bot_id'] == 'alpha' and items[0]['thread_id'] == thread and items[0]['detail'] == 'жду CI'
        assert items[0]['title']['params']['wakeup_id'] == created['id']
        notes = [event for event in await thread_events(client, thread) if event['payload'].get('code') == 'wakeup_scheduled']
        assert len(notes) == 1 and notes[0]['kind'] == 'system' and notes[0]['payload']['wakeup_id'] == created['id']


async def test_wakeup_by_absolute_time_and_without_a_thread():
    async with client_for() as (client, app):
        await make_bot(client)
        at = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
        created = await create(client, at=at.isoformat(), in_minutes=None)
        assert created['thread_id'] is None and created['reason'] == ''
        assert datetime.fromisoformat(created['scheduled_at'].replace('Z', '+00:00')) == at
        local = (at + timedelta(hours=1)).astimezone(timezone(timedelta(hours=3))).isoformat()
        assert (await ask(client, prompt='Другое', at=local)).status_code == 201


async def test_the_same_prompt_and_time_within_a_minute_is_idempotent():
    async with client_for() as (client, app):
        await make_bot(client)
        first = await create(client, in_minutes=60)
        again = await ask(client, prompt='Проверь сборку', in_minutes=60)
        assert again.status_code == 200 and again.json()['deduplicated'] is True and again.json()['id'] == first['id']
        later = datetime.fromisoformat(first['scheduled_at'].replace('Z', '+00:00')) + timedelta(seconds=45)
        near = await ask(client, prompt='Проверь сборку', at=later.isoformat())
        assert near.status_code == 200 and near.json()['id'] == first['id']
        assert await count_rows(app) == 1 and len(await feed_items(client, 'wakeup_scheduled')) == 1
        assert (await ask(client, prompt='Другой текст', in_minutes=60)).status_code == 201  # другой prompt
        assert (await ask(client, prompt='Проверь сборку', in_minutes=65)).status_code == 201  # срок дальше минуты
        assert await count_rows(app) == 3


async def test_parallel_identical_calls_make_one_wakeup():
    async with client_for() as (client, app):
        await make_bot(client)
        answers = await asyncio.gather(*[ask(client, prompt='Один раз', in_minutes=45) for _ in range(6)])
        assert sorted(answer.status_code for answer in answers) == [200] * 5 + [201]
        assert len({answer.json()['id'] for answer in answers}) == 1 and await count_rows(app) == 1


async def test_at_most_twenty_active_wakeups_per_bot():
    async with client_for() as (client, app):
        await make_bot(client)
        await make_bot(client, 'beta')
        ids = [(await create(client, prompt=f'п{number}', in_minutes=10 + number))['id'] for number in range(20)]
        refused = await ask(client, prompt='лишнее', in_minutes=100)
        assert refused.status_code == 409 and refused.json() == {'error': 'conflict', 'detail': 'wakeup_limit'}
        assert (await ask(client, prompt='п0', in_minutes=10)).status_code == 200  # повтор существующего не упирается в предел
        assert (await ask(client, 'beta', prompt='лишнее', in_minutes=100)).status_code == 201  # у другого бота свой счёт
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.wakeups set status='fired',fired_at=now() where id=$1", uuid.UUID(ids[0]))
        assert (await ask(client, prompt='теперь можно', in_minutes=100)).status_code == 201  # сработавшие не считаются
        assert (await ask(client, prompt='и снова лишнее', in_minutes=101)).status_code == 409
        assert (await client.delete(f'/api/wakeups/{ids[1]}', headers=OWNER)).status_code == 200  # отмена освобождает место
        assert (await ask(client, prompt='после отмены', in_minutes=102)).status_code == 201


@pytest.mark.parametrize('body,status,detail', [
    ({'prompt': 'p'}, 400, 'time_required'),
    ({'prompt': 'p', 'in_minutes': 5, 'at': '2099-01-01T00:00:00Z'}, 400, 'time_conflict'),
    ({'prompt': 'p', 'in_minutes': 0}, 400, 'too_soon'),
    ({'prompt': 'p', 'in_minutes': -3}, 400, 'too_soon'),
    ({'prompt': 'p', 'in_minutes': 30 * 24 * 60 + 1}, 400, 'too_far'),
    ({'prompt': 'p', 'at': '2020-01-01T00:00:00Z'}, 400, 'too_soon'),
    ({'prompt': 'p', 'at': '2099-01-01T00:00:00Z'}, 400, 'too_far'),
    ({'prompt': 'p', 'at': 'завтра в девять'}, 400, 'at_invalid'),
    ({'prompt': '   ', 'in_minutes': 5}, 400, 'prompt_empty'),
    ({'prompt': 'x' * 2001, 'in_minutes': 5}, 422, 'prompt_too_long'),
    ({'prompt': 'p', 'in_minutes': 5, 'reason': 'r' * 201}, 422, 'reason_too_long'),
])
async def test_refusals_carry_a_status_and_a_code_and_create_nothing(body, status, detail):
    async with client_for() as (client, app):
        await make_bot(client)
        response = await ask(client, **body)
        assert response.status_code == status and response.json() == {'error': 'invalid', 'detail': detail}, response.text
        assert await count_rows(app) == 0 and await feed_items(client, 'wakeup_scheduled') == []


async def test_malformed_bodies_are_rejected_by_the_model():
    async with client_for() as (client, app):
        await make_bot(client)
        for body in ({'prompt': 'p', 'in_minutes': '5'}, {'prompt': 'p', 'in_minutes': 2.5}, {'prompt': 'p', 'in_minutes': True},
                     {'prompt': 'p', 'in_minutes': 5, 'bot_id': 'beta'}, {'in_minutes': 5}, {'prompt': 'p', 'in_minutes': 5, 'thread_id': 'x'},
                     {'prompt': 'p\x00q', 'in_minutes': 5}):
            # тело целиком в json: ключ bot_id не должен уйти в параметр помощника ask
            response = await client.post('/api/bots/wakeups', json=body, headers=bot_headers('alpha'))
            assert response.status_code in (400, 422), (body, response.text)
        assert await count_rows(app) == 0


async def test_only_a_bot_token_may_create_and_only_in_its_own_threads():
    async with client_for() as (client, app):
        member, member_headers = await add_member(client, app)
        await make_bot(client)
        await make_bot(client, 'beta')
        beta_thread = await make_thread(client, 'beta')
        body = {'prompt': 'p', 'in_minutes': 5}
        assert (await client.post('/api/bots/wakeups', json=body, headers=OWNER)).status_code == 403  # владелец: не его маршрут
        assert (await client.post('/api/bots/wakeups', json=body)).status_code == 401
        assert (await client.post('/api/bots/wakeups', json=body, headers={'Authorization': 'Bearer bot:alpha:bad'})).status_code == 401
        assert (await ask(client, 'alpha', **body, thread_id=beta_thread)).status_code == 403  # тред другого бота того же владельца
        assert (await ask(client, 'alpha', **body, thread_id=str(uuid.uuid4()))).status_code == 404
        member_bot = await make_bot(client, 'gamma', headers=member_headers)
        member_thread = await make_thread(client, 'gamma', headers=member_headers)
        assert (await ask(client, 'alpha', **body, thread_id=member_thread)).status_code == 404  # тред чужого владельца
        assert member_bot['id'] == 'gamma' and await count_rows(app) == 0


# ---- срабатывание ----

async def test_a_due_wakeup_starts_a_turn_in_its_thread_once():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        created = await create(client, prompt='Проверь сборку', reason='жду CI', thread_id=thread)
        await app.state.run_due_wakeups()
        assert (await row_of(app, created['id']))['status'] == 'active' and await wakeup_turns(app) == []  # срок ещё не настал
        await make_due(app, created['id'])
        await app.state.run_due_wakeups()
        row = await row_of(app, created['id'])
        assert row['status'] == 'fired' and row['fired_at'] is not None and row['skip_reason'] is None
        turns = await wakeup_turns(app)
        assert len(turns) == 1 and str(turns[0]['th_id']) == thread and turns[0]['status'] == 'queued'
        assert turns[0]['prompt'] == wakeups.fire_text('Проверь сборку', 'жду CI')
        message = [event for event in await thread_events(client, thread) if event['kind'] == 'user_msg'][-1]
        assert message['actor'] == 'bot:alpha' and message['client'] == 'wakeup' and message['payload']['text'] == turns[0]['prompt']
        fired = await feed_items(client, 'wakeup_fired')
        assert len(fired) == 1 and fired[0]['thread_id'] == thread and fired[0]['detail'] == 'жду CI'
        await app.state.run_due_wakeups()  # одноразовость: второй проход ничего не добавляет
        await app.state.run_due_wakeups()
        assert len(await wakeup_turns(app)) == 1 and len(await feed_items(client, 'wakeup_fired')) == 1


async def test_a_wakeup_whose_thread_is_gone_goes_to_a_new_routine_thread():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        created = await create(client, thread_id=thread)
        archived = await client.patch(f'/api/threads/{thread}', json={'status': 'archived'}, headers=OWNER)
        assert archived.status_code == 200, archived.text
        loose = await create(client, prompt='Без треда')
        await make_due(app, created['id'])
        await make_due(app, loose['id'])
        await app.state.run_due_wakeups()
        turns = await wakeup_turns(app)
        assert len(turns) == 2 and all(str(turn['th_id']) != thread for turn in turns)
        async with app.state.pool.acquire() as con:
            kinds = {str(turn['th_id']): await con.fetchval('select kind from bothub.threads where id=$1', turn['th_id']) for turn in turns}
        assert set(kinds.values()) == {'routine'}
        moved = str((await row_of(app, created['id']))['thread_id'])
        assert moved != thread and moved in {str(turn['th_id']) for turn in turns}  # строка помнит тред, куда ушёл ход


async def test_two_due_wakeups_of_one_bot_both_fire_in_due_order():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        first = await create(client, prompt='первое', thread_id=thread)
        second = await create(client, prompt='второе', thread_id=thread)
        await make_due(app, first['id'], '10 minutes')
        await make_due(app, second['id'], '5 minutes')
        await app.state.run_due_wakeups()
        # оба хода созданы в одной транзакции (одинаковый created_at): порядок смотрим по номерам событий треда
        sent = [event['payload']['text'].rsplit('\n\n', 1)[-1] for event in await thread_events(client, thread)
                if event['kind'] == 'user_msg' and event['client'] == 'wakeup']
        assert sent == ['первое', 'второе']


async def test_a_paused_bot_skips_its_wakeup_and_the_feed_says_so():
    async with client_for() as (client, app):
        await make_bot(client)
        thread = await make_thread(client)
        created = await create(client, reason='напомнить', thread_id=thread)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        await make_due(app, created['id'])
        await app.state.run_due_wakeups()
        row = await row_of(app, created['id'])
        assert row['status'] == 'skipped' and row['skip_reason'] == 'bot_paused' and row['fired_at'] is None
        assert await wakeup_turns(app) == []
        skipped = await feed_items(client, 'wakeup_skipped')
        assert len(skipped) == 1 and skipped[0]['title']['params']['reason'] == 'bot_paused' and skipped[0]['detail'] == 'напомнить'
        assert skipped[0]['thread_id'] == thread
        notes = [event for event in await thread_events(client, thread) if event['payload'].get('code') == 'wakeup_skipped']
        assert len(notes) == 1 and notes[0]['kind'] == 'system' and notes[0]['payload']['reason'] == 'bot_paused'
        await client.post('/api/bots/alpha/resume', headers=OWNER)
        await app.state.run_due_wakeups()  # пропущенное не догоняется после возобновления
        assert await wakeup_turns(app) == [] and (await row_of(app, created['id']))['status'] == 'skipped'
        assert await feed_items(client, 'wakeup_fired') == []


async def test_a_pause_that_ended_before_the_time_does_not_skip_the_wakeup():
    async with client_for() as (client, app):
        await make_bot(client)
        created = await create(client, in_minutes=60)
        await client.post('/api/bots/alpha/pause', headers=OWNER)
        await client.post('/api/bots/alpha/resume', headers=OWNER)  # пауза закончилась до срока: сработает
        await make_due(app, created['id'])
        await app.state.run_due_wakeups()
        assert (await row_of(app, created['id']))['status'] == 'fired' and len(await wakeup_turns(app)) == 1


async def create_mac_bot(app, bot_id='mac'):
    async with app.state.pool.acquire() as con:
        owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
        await con.execute("insert into bothub.bots(id,name,provider,model,owner_id,executor) values($1,'Mac','fake','fake',$2,'mac')", bot_id, owner)


async def test_an_unavailable_executor_holds_the_wakeup_for_the_grace_window_then_skips_it():
    async with client_for() as (client, app):
        await create_mac_bot(app)
        waiting = await create(client, 'mac', prompt='ждёт', in_minutes=5)
        expired = await create(client, 'mac', prompt='просрочено', in_minutes=5)
        await make_due(app, waiting['id'], '10 minutes')
        await make_due(app, expired['id'], '16 minutes')
        await app.state.run_due_wakeups()
        held = await row_of(app, waiting['id'])
        assert held['status'] == 'active' and held['skip_reason'] is None  # Mac может вернуться
        gone = await row_of(app, expired['id'])
        assert gone['status'] == 'skipped' and gone['skip_reason'] == 'executor_unavailable'
        assert await wakeup_turns(app, 'mac') == []
        assert len(await feed_items(client, 'wakeup_skipped')) == 1
        assert await feed_items(client, 'wakeup_fired') == []


async def test_a_wakeup_overdue_without_a_block_still_fires_late():
    async with client_for() as (client, app):
        await make_bot(client)
        created = await create(client)
        await make_due(app, created['id'], '3 hours')  # ядро было выключено: сработает при первом проходе
        await app.state.run_due_wakeups()
        assert (await row_of(app, created['id']))['status'] == 'fired' and len(await wakeup_turns(app)) == 1


# ---- владелец: список и отмена ----

async def test_owner_lists_wakeups_with_a_status_filter():
    async with client_for() as (client, app):
        await make_bot(client)
        later = await create(client, prompt='позже', in_minutes=120)
        sooner = await create(client, prompt='раньше', in_minutes=10)
        done = await create(client, prompt='сработало', in_minutes=20)
        await make_due(app, done['id'])
        await app.state.run_due_wakeups()
        listed = await client.get('/api/bots/alpha/wakeups', headers=OWNER)
        assert listed.status_code == 200
        assert [item['id'] for item in listed.json()] == [sooner['id'], later['id'], done['id']]  # активные по сроку, затем история
        only = await client.get('/api/bots/alpha/wakeups', params={'status': 'active'}, headers=OWNER)
        assert [item['id'] for item in only.json()] == [sooner['id'], later['id']]
        history = await client.get('/api/bots/alpha/wakeups', params={'status': 'fired'}, headers=OWNER)
        assert [item['id'] for item in history.json()] == [done['id']] and history.json()[0]['fired_at']
        assert (await client.get('/api/bots/alpha/wakeups', params={'status': 'bogus'}, headers=OWNER)).status_code in (400, 422)
        assert (await client.get('/api/bots/nothing/wakeups', headers=OWNER)).status_code == 404


async def test_owner_cancels_an_active_wakeup_and_it_never_fires():
    async with client_for() as (client, app):
        await make_bot(client)
        created = await create(client)
        cancelled = await client.delete(f"/api/wakeups/{created['id']}", headers=OWNER)
        assert cancelled.status_code == 200 and cancelled.json()['ok'] is True
        assert await count_rows(app) == 0
        assert (await client.get('/api/bots/alpha/wakeups', headers=OWNER)).json() == []
        assert (await client.delete(f"/api/wakeups/{created['id']}", headers=OWNER)).status_code == 404  # уже нет
        await app.state.run_due_wakeups()
        assert await wakeup_turns(app) == [] and await feed_items(client, 'wakeup_fired') == []
        assert (await client.delete(f'/api/wakeups/{uuid.uuid4()}', headers=OWNER)).status_code == 404
        assert (await client.delete('/api/wakeups/not-a-uuid', headers=OWNER)).status_code in (400, 404, 422)


async def test_a_fired_wakeup_cannot_be_cancelled_and_stays_in_history():
    async with client_for() as (client, app):
        await make_bot(client)
        created = await create(client)
        await make_due(app, created['id'])
        await app.state.run_due_wakeups()
        response = await client.delete(f"/api/wakeups/{created['id']}", headers=OWNER)
        assert response.status_code == 409 and response.json() == {'error': 'conflict', 'detail': 'wakeup_not_active'}
        assert (await row_of(app, created['id']))['status'] == 'fired'


async def test_another_owner_gets_404_and_changes_nothing():
    async with client_for() as (client, app):
        member, member_headers = await add_member(client, app)
        await make_bot(client)
        created = await create(client)
        assert (await client.get('/api/bots/alpha/wakeups', headers=member_headers)).status_code == 404
        assert (await client.delete(f"/api/wakeups/{created['id']}", headers=member_headers)).status_code == 404
        assert (await row_of(app, created['id']))['status'] == 'active'
        await make_bot(client, 'gamma', headers=member_headers)
        assert (await client.get('/api/bots/gamma/wakeups', headers=member_headers)).json() == []  # у участника своих нет
        assert (await client.get('/api/bots/alpha/wakeups', headers=OWNER)).json()[0]['id'] == created['id']


async def test_a_bot_token_cannot_list_or_cancel():
    async with client_for() as (client, app):
        await make_bot(client)
        created = await create(client)
        assert (await client.get('/api/bots/alpha/wakeups', headers=bot_headers('alpha'))).status_code == 403
        assert (await client.delete(f"/api/wakeups/{created['id']}", headers=bot_headers('alpha'))).status_code == 403
        client.cookies.clear()
        assert (await client.get('/api/bots/alpha/wakeups')).status_code == 401
        assert (await count_rows(app)) == 1


async def test_wakeups_of_other_bots_stay_separate():
    async with client_for() as (client, app):
        await make_bot(client)
        await make_bot(client, 'beta')
        mine = await create(client, 'alpha', prompt='общий текст')
        theirs = await create(client, 'beta', prompt='общий текст')  # тот же prompt и срок у другого бота: не повтор
        assert mine['id'] != theirs['id']
        assert [item['id'] for item in (await client.get('/api/bots/beta/wakeups', headers=OWNER)).json()] == [theirs['id']]
        assert await count_rows(app, 'bot_id=$1', 'alpha') == 1


# ---- ограничения таблицы ----

async def test_table_constraints_back_up_the_api_limits():
    async with client_for() as (client, app):
        await make_bot(client)
        async with app.state.pool.acquire() as con:
            insert = "insert into bothub.wakeups(bot_id,scheduled_at,prompt,reason,status,skip_reason) values('alpha',now(),$1,$2,$3,$4)"
            await con.execute(insert, 'ok', '', 'active', None)
            for args in (('', '', 'active', None), ('x' * 2001, '', 'active', None), ('ok', 'r' * 201, 'active', None),
                         ('ok', '', 'waiting', None), ('ok', '', 'skipped', 'weather')):
                with pytest.raises(asyncpg.CheckViolationError):
                    await con.execute(insert, *args)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await con.execute("insert into bothub.wakeups(bot_id,scheduled_at,prompt) values('nobody',now(),'p')")


async def test_deleting_the_bot_removes_its_wakeups():
    async with client_for() as (client, app):
        await make_bot(client)
        await create(client)
        async with app.state.pool.acquire() as con:
            await con.execute("delete from bothub.bots where id='alpha'")
        assert await count_rows(app) == 0
