"""Поручения бота боту (docs/contracts.md, раздел 19). Нужен Postgres из conftest.py.
Чистая логика и MCP-инструменты: test_delegation_pure.py."""
import asyncio
import uuid

import pytest

from bothub import delegation
from test_wakeups_db import OWNER, add_member, bot_headers, client_for, feed, make_bot, make_thread, thread_events


async def source_turn(client, bot_id='alpha', thread=None):
    """Ход бота-отправителя: от него бот вызывает инструмент (BOTHUB_TURN_ID)."""
    thread = thread or await make_thread(client, bot_id)
    response = await client.post(f'/api/threads/{thread}/turns', json={'prompt': 'начни', 'client': 'api'}, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()['id']


async def delegate(client, turn, bot='beta', task='Собери отчёт', caller='alpha'):
    return await client.post('/api/bots/delegations', json={'bot': bot, 'task': task, 'turn_id': turn}, headers=bot_headers(caller))


async def sent(client, turn, **kwargs):
    response = await delegate(client, turn, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def result(client, turn_id, caller='alpha'):
    return await client.get(f'/api/bots/delegations/{turn_id}', headers=bot_headers(caller))


async def two_bots(client):
    await make_bot(client, 'alpha')
    await make_bot(client, 'beta')
    return await source_turn(client)


async def set_turn(app, turn_id, status, error=None):
    async with app.state.pool.acquire() as con:
        await con.execute('update bothub.turns set status=$2,error=$3,finished_at=now() where id=$1', uuid.UUID(turn_id), status, error)


async def add_events(app, turn_id, events):
    async with app.state.pool.acquire() as con:
        thread_id = await con.fetchval('select thread_id from bothub.turns where id=$1', uuid.UUID(turn_id))
        for kind, text in events:
            seq = await con.fetchval('update bothub.threads set last_seq=last_seq+1 where id=$1 returning last_seq', thread_id)
            await con.execute("insert into bothub.events(thread_id,seq,turn_id,kind,actor,payload) values($1,$2,$3,$4,'bot:beta',jsonb_build_object('text',$5::text))",
                              thread_id, seq, uuid.UUID(turn_id), kind, text)


# ---- постановка ----

async def test_delegation_creates_a_turn_in_a_new_thread_of_the_target():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        created = await sent(client, turn, bot='beta', task='Собери отчёт')
        assert set(created) == {'turn_id', 'thread_id'}
        async with app.state.pool.acquire() as con:
            thread = await con.fetchrow('select * from bothub.threads where id=$1', uuid.UUID(created['thread_id']))
            row = await con.fetchrow('select * from bothub.turns where id=$1', uuid.UUID(created['turn_id']))
        assert thread['bot_id'] == 'beta' and thread['title'] == 'Поручения от Alpha' and thread['kind'] == 'direct'
        assert row['status'] == 'queued' and row['client'] == 'delegate' and row['delegated_by_bot'] == 'alpha' and str(row['delegated_from_turn']) == turn
        assert row['prompt'] == delegation.message_text('Alpha', 'Собери отчёт') and row['prompt'].startswith('Поручение от бота Alpha:')
        message = [e for e in await thread_events(client, created['thread_id']) if e['kind'] == 'user_msg'][0]
        assert message['actor'] == 'bot:alpha' and message['client'] == 'delegate' and message['turn_id'] == created['turn_id']
        assert message['payload'] == {'text': row['prompt'], 'delegated_from': {'bot_id': 'alpha', 'name': 'Alpha'}}


async def test_the_target_is_found_by_name_or_id_and_the_latest_thread_is_reused():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        old = await make_thread(client, 'beta', title='старый')
        recent = await make_thread(client, 'beta', title='свежий')
        await client.post(f'/api/threads/{old}/turns', json={'prompt': 'ещё позже', 'client': 'api'}, headers=OWNER)  # последняя активность в «старом»
        first = await sent(client, turn, bot='BETA')
        assert first['thread_id'] == old and recent != old
        second = await sent(client, turn, bot='beta')
        assert second['thread_id'] == old
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.threads where bot_id='beta'") == 2
        await make_bot(client, 'gamma', name='Мой Помощник')
        third = await sent(client, turn, bot='мой помощник')
        assert third['thread_id'] != old


async def test_an_archived_thread_is_not_reused():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        archived = await make_thread(client, 'beta')
        await client.patch(f'/api/threads/{archived}', json={'status': 'archived'}, headers=OWNER)
        created = await sent(client, turn)
        assert created['thread_id'] != archived


async def test_the_feed_gets_delegation_sent():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        created = await sent(client, turn, task='Собери отчёт\nпо серверам')
        items = [i for i in await feed(client, kind='schedule') if i['title']['code'] == 'delegation_sent']
        assert len(items) == 1 and items[0]['bot_id'] == 'alpha' and items[0]['thread_id'] == created['thread_id']
        assert items[0]['title']['params'] == {'from_bot': 'Alpha', 'to_bot': 'Beta', 'to_bot_id': 'beta', 'turn_id': created['turn_id']}
        assert items[0]['detail'] == 'Собери отчёт по серверам'


# ---- отказы ----

@pytest.mark.parametrize('kwargs,status,detail', [
    ({'bot': 'alpha'}, 409, 'self_delegation'), ({'bot': 'ALPHA'}, 409, 'self_delegation'), ({'bot': 'nobody'}, 404, 'bot_not_found'),
    ({'task': '  '}, 422, 'task_empty'), ({'task': 'x' * 4001}, 422, 'task_too_long'), ({'bot': ' '}, 422, 'bot_invalid'),
])
async def test_refusals_create_nothing(kwargs, status, detail):
    async with client_for() as (client, app):
        turn = await two_bots(client)
        response = await delegate(client, turn, **kwargs)
        assert response.status_code == status and response.json()['detail'] == detail, response.text
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='delegate'") == 0
            assert await con.fetchval("select count(*) from bothub.threads where bot_id='beta'") == 0


async def test_task_limits_are_inclusive():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        assert (await delegate(client, turn, task='x')).status_code == 201
        assert (await delegate(client, turn, task='x' * 4000)).status_code == 201


@pytest.mark.parametrize('sql,code', [
    ('update bothub.bots set paused=true where id=$1', 'target_paused'),
    ("update bothub.bots set status='no_model' where id=$1", 'target_no_model'),
    ("update bothub.bots set status='error_starting' where id=$1", 'target_error_starting'),
])
async def test_an_unavailable_target_is_refused(sql, code):
    async with client_for() as (client, app):
        turn = await two_bots(client)
        async with app.state.pool.acquire() as con:
            await con.execute(sql, 'beta')
        response = await delegate(client, turn)
        assert response.status_code == 409 and response.json()['detail'] == code, response.text
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='delegate'") == 0


async def test_a_delegated_turn_cannot_delegate_further():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        await make_bot(client, 'gamma')
        created = await sent(client, turn)
        response = await delegate(client, created['turn_id'], bot='gamma', caller='beta')
        assert response.status_code == 409 and response.json()['detail'] == 'delegation_depth', response.text
        # обычный ход того же бота beta поручать может
        assert (await delegate(client, await source_turn(client, 'beta'), bot='gamma', caller='beta')).status_code == 201


async def test_at_most_five_active_delegations_per_calling_bot():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        created = [await sent(client, turn) for _ in range(delegation.MAX_ACTIVE)]
        response = await delegate(client, turn)
        assert response.status_code == 409 and response.json()['detail'] == 'delegation_limit'
        await set_turn(app, created[0]['turn_id'], 'done')  # завершённое место освобождает
        assert (await delegate(client, turn)).status_code == 201
        # предел у отправителя свой: у beta счётчик чистый
        assert (await delegate(client, await source_turn(client, 'beta'), bot='alpha', caller='beta')).status_code == 201


async def test_the_source_turn_must_belong_to_the_calling_bot_and_owner():
    async with client_for() as (client, app):
        member, member_headers = await add_member(client, app)
        turn = await two_bots(client)
        await make_bot(client, 'gamma', headers=member_headers)
        gamma_turn = (await client.post(f"/api/threads/{await make_thread(client, 'gamma', headers=member_headers)}/turns",
                                        json={'prompt': 'x', 'client': 'api'}, headers=member_headers)).json()['id']
        assert (await delegate(client, str(uuid.uuid4()))).status_code == 404
        assert (await delegate(client, gamma_turn)).status_code == 404  # ход чужого владельца
        beta_turn = await source_turn(client, 'beta')
        assert (await delegate(client, beta_turn)).status_code == 404  # ход другого бота
        assert (await delegate(client, turn, bot='gamma')).status_code == 404  # бот чужого владельца невидим
        assert (await delegate(client, turn, bot=str(uuid.uuid4()))).status_code == 404


async def test_only_a_bot_token_may_delegate():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        body = {'bot': 'beta', 'task': 'x', 'turn_id': turn}
        assert (await client.post('/api/bots/delegations', json=body, headers=OWNER)).status_code == 403
        assert (await client.post('/api/bots/delegations', json=body)).status_code == 401
        assert (await client.post('/api/bots/delegations', json=body, headers={'Authorization': 'Bearer bot:alpha:bad'})).status_code == 401
        assert (await client.get(f'/api/bots/delegations/{turn}', headers=OWNER)).status_code == 403
        for bad in ({'bot': 'beta', 'task': 'x'}, {**body, 'extra': 1}, {**body, 'turn_id': 'x'}, {**body, 'task': 'a\x00b'}):
            assert (await client.post('/api/bots/delegations', json=bad, headers=bot_headers('alpha'))).status_code in (400, 422)


# ---- результат ----

async def test_the_result_follows_the_target_turn():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        created = await sent(client, turn)
        body = (await result(client, created['turn_id'])).json()
        assert body == {'turn_id': created['turn_id'], 'thread_id': created['thread_id'], 'to_bot': 'beta', 'status': 'queued', 'result': None, 'error': None}
        await add_events(app, created['turn_id'], [('assistant_msg', 'Сейчас посмотрю'), ('tool_call', 'ls'), ('tool_result', 'ok'),
                                                  ('assistant_msg', 'Готово.'), ('assistant_msg', 'Отчёт: всё в порядке')])
        await set_turn(app, created['turn_id'], 'running')
        assert (await result(client, created['turn_id'])).json()['result'] is None  # пока не закончил, текста нет
        await set_turn(app, created['turn_id'], 'done')
        body = (await result(client, created['turn_id'])).json()
        assert body['status'] == 'done' and body['result'] == 'Готово.\n\nОтчёт: всё в порядке' and body['error'] is None


async def test_the_result_is_cut_at_8000_characters_and_errors_are_reported():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        first, second = await sent(client, turn), await sent(client, turn)
        await add_events(app, first['turn_id'], [('assistant_msg', 'я' * 9000)])
        await set_turn(app, first['turn_id'], 'done')
        assert (await result(client, first['turn_id'])).json()['result'] == 'я' * 8000
        await set_turn(app, second['turn_id'], 'error', 'budget_exceeded')
        body = (await result(client, second['turn_id'])).json()
        assert body['status'] == 'error' and body['error'] == 'budget_exceeded' and body['result'] is None


async def test_only_the_delegating_bot_reads_the_result():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        await make_bot(client, 'gamma')
        created = await sent(client, turn)
        for caller in ('beta', 'gamma'):  # получатель тоже не читает: результат только у того, кто поручил
            assert (await result(client, created['turn_id'], caller)).status_code == 404
        assert (await result(client, str(uuid.uuid4()))).status_code == 404
        assert (await result(client, turn)).status_code == 404  # обычный ход не поручение
        assert (await result(client, created['turn_id'])).status_code == 200


async def test_the_feed_gets_delegation_done_once_when_the_target_turn_ends():
    async with client_for() as (client, app):
        turn = await two_bots(client)
        created = await sent(client, turn)
        assert (await client.post(f"/api/turns/{created['turn_id']}/stop", headers=OWNER)).status_code == 200
        await asyncio.sleep(0)
        items = [i for i in await feed(client, kind='schedule') if i['title']['code'] == 'delegation_done']
        assert len(items) == 1 and items[0]['bot_id'] == 'alpha' and items[0]['thread_id'] == created['thread_id']
        assert items[0]['title']['params'] == {'from_bot': 'Alpha', 'to_bot': 'Beta', 'to_bot_id': 'beta', 'turn_id': created['turn_id'], 'outcome': 'stopped'}
        # обычный ход события не даёт
        plain = await source_turn(client, 'beta')
        await client.post(f'/api/turns/{plain}/stop', headers=OWNER)
        assert len([i for i in await feed(client, kind='schedule') if i['title']['code'] == 'delegation_done']) == 1
