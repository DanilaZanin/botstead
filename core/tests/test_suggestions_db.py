"""Подсказки: изоляция владельца, принятие и планировщик. Требуется Postgres."""
import pytest

from test_wakeups_db import OWNER, add_member, client_for, make_bot


@pytest.mark.asyncio
async def test_suggestions_owner_scope_and_accept():
    async with client_for() as (client, app):
        bot = await make_bot(client, proactive_interval_hours=1)
        other_id, other = await add_member(client, app)
        async with app.state.pool.acquire() as con:
            suggestion_id = await con.fetchval(
                "insert into bothub.suggestions(owner_id,bot_id,text) values($1,$2,$3) returning id",
                bot['owner_id'], bot['id'], 'Проверить журнал')
        assert (await client.get('/api/suggestions', headers=other)).json() == []
        assert (await client.post(f'/api/suggestions/{suggestion_id}/accept', headers=other)).status_code == 404
        assert (await client.post(f'/api/suggestions/{suggestion_id}/dismiss', headers=other)).status_code == 404
        listed = await client.get('/api/suggestions', headers=OWNER)
        assert [s['text'] for s in listed.json()] == ['Проверить журнал']
        accepted = await client.post(f'/api/suggestions/{suggestion_id}/accept', headers=OWNER)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['prompt'] == 'Проверить журнал'
        assert (await client.post(f'/api/suggestions/{suggestion_id}/accept', headers=OWNER)).status_code == 409
        assert (await client.get('/api/suggestions', headers=OWNER)).json() == []


@pytest.mark.asyncio
async def test_scheduler_skips_paused_and_enqueues_once():
    async with client_for() as (client, app):
        bot = await make_bot(client, proactive_interval_hours=1)
        async with app.state.pool.acquire() as con:
            await con.execute('update bothub.bots set paused=true where id=$1', bot['id'])
        await app.state.run_due_suggestions()
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where turn_type='proactive'") == 0
            await con.execute('update bothub.bots set paused=false where id=$1', bot['id'])
        await app.state.run_due_suggestions()
        await app.state.run_due_suggestions()
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where turn_type='proactive'") == 1
