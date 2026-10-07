"""Истёкший вход помечает только подписку владельца хода."""
import pytest

from bothub.runner.subprocess import CLI_AUTH_EXPIRED, CLI_AUTH_MESSAGE, CliAuthExpired
from test_wakeups_db import OWNER, add_member, client_for, make_bot


@pytest.fixture(autouse=True)
def api_root(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')


class AuthRunner:
    provider = 'claude'

    async def run(self, turn):
        raise CliAuthExpired()
        yield  # раннер остаётся асинхронным генератором

    async def stop(self, turn_id):
        pass


async def test_cli_auth_failure_marks_only_bound_owner_provider():
    async with client_for(runner_factory=lambda provider: AuthRunner()) as (client, app):
        await make_bot(client, 'auth-bot')
        foreign_owner, _ = await add_member(client, app)
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval("select owner_id from bothub.bots where id='auth-bot'")
            provider = await con.fetchval(
                "insert into bothub.providers(owner_id,kind,cli,name,status) "
                "values($1,'cli_subscription','claude','Claude','ok') returning id", owner)
            foreign = await con.fetchval(
                "insert into bothub.providers(owner_id,kind,cli,name,status) "
                "values($1,'cli_subscription','claude','Claude','ok') returning id", foreign_owner)
            model = await con.fetchval('insert into bothub.models(provider_id,name) values($1,$2) returning id', provider, 'test-model')
            await con.execute("update bothub.bots set provider='claude',model='test-model',provider_id=$1,model_id=$2 "
                              "where id='auth-bot'", provider, model)

        thread = (await client.post('/api/threads', json={'bot_id': 'auth-bot'}, headers=OWNER)).json()['id']
        response = await client.post(f'/api/threads/{thread}/turns', json={'prompt': 'hello'}, headers=OWNER)
        assert response.status_code == 200, response.text
        row = await app.state.claim_turn()
        assert row and str(row['id']) == response.json()['id']
        await app.state.run_turn(row)

        async with app.state.pool.acquire() as con:
            turn = await con.fetchrow('select status,error from bothub.turns where id=$1', row['id'])
            assert turn['status'] == 'error' and turn['error'] == CLI_AUTH_MESSAGE
            assert await con.fetchval('select status from bothub.providers where id=$1', provider) == 'needs_login'
            assert await con.fetchval('select status from bothub.providers where id=$1', foreign) == 'ok'
        events = (await client.get(f'/api/threads/{thread}/events', headers=OWNER)).json()
        assert not any(event['kind'] == 'assistant_msg' for event in events)
        assert any(event['kind'] == 'guard' and event['payload'].get('reason') == CLI_AUTH_EXPIRED for event in events)
        assert any(event['kind'] == 'status' and event['payload'].get('code') == CLI_AUTH_EXPIRED
                   and event['payload'].get('provider_id') == str(provider) for event in events)
