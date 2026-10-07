"""Slack Bot Token, дедупликация и отправка ответа. Нужен Postgres из conftest.py."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import asyncpg

from bothub.secrets import decrypt_secret
from bothub import channels_slack
from bothub.main import create_app
from bothub.runner.base import RunnerEvent
from test_activity_db import OWNER, client_for, hook_for, make_bot

# Фиктивный токен собран из частей, чтобы сканер секретов публикации не принимал его за настоящий.
SLACK_TEST_TOKEN = 'xox' + 'b-test-token'


@pytest.fixture(autouse=True)
def secret_keys(monkeypatch):
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())
    monkeypatch.setenv('BOTHUB_WORKER_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_SCHEDULER_INTERVAL', '3600')
    monkeypatch.setenv('BOTHUB_OUTBOX_INTERVAL', '3600')
    async def fake_auth(token):
        return 'T1'
    monkeypatch.setattr('bothub.channels_slack.slack_team_id', fake_auth)


class SlackRunner:
    async def run(self, turn):
        yield RunnerEvent('assistant_msg', {'text': 'ответ', 'final': True})
        yield RunnerEvent('usage', {'tokens_in': 1, 'tokens_out': 1, 'model': 'fake', 'seconds': 0})

    async def stop(self, turn_id):
        pass


@asynccontextmanager
async def worker_client_for():
    app = create_app(runner_factory=lambda provider: SlackRunner())
    async def idle():
        await asyncio.Event().wait()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app


async def set_token(client, schedule_id, value):
    return await client.patch(f'/api/schedules/{schedule_id}', json={'slack_bot_token': value}, headers=OWNER)


def slack_headers(body, secret, retry=None, ts=None):
    headers = {'content-type': 'application/json'}
    headers.update(_slack_headers(body, secret, retry, ts))
    return headers


def _slack_headers(body, secret, retry=None, ts=None):
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), b'v0:' + str(ts).encode() + b':' + body, hashlib.sha256).hexdigest()
    headers = {'x-slack-signature': f'v0={digest}', 'x-slack-request-timestamp': str(ts)}
    if retry is not None:
        headers['x-slack-retry-num'] = retry
    return headers


async def test_bot_token_is_write_only_encrypted_and_clearable():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        token = SLACK_TEST_TOKEN
        response = await set_token(client, hook['id'], token)
        assert response.status_code == 200 and response.json()['has_slack_bot_token'] is True
        assert 'slack_bot_token' not in response.json() and token not in response.text
        async with app.state.pool.acquire() as con:
            stored = bytes(await con.fetchval('select slack_bot_token from bothub.schedules where id=$1', uuid.UUID(hook['id'])))
            assert await con.fetchval('select slack_team_id from bothub.schedules where id=$1', uuid.UUID(hook['id'])) == 'T1'
        assert token.encode() not in stored
        assert decrypt_secret(stored, uuid.UUID(hook['id']).bytes).decode() == token
        cleared = await set_token(client, hook['id'], None)
        assert cleared.status_code == 200 and cleared.json()['has_slack_bot_token'] is False
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select slack_team_id from bothub.schedules where id=$1', uuid.UUID(hook['id'])) is None
        for bad in ('', '   ', 5, 'x' * 300, 'bad\x00token'):
            assert (await set_token(client, hook['id'], bad)).status_code in (400, 422)


async def test_retry_header_and_same_event_id_do_not_queue_second_turn():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await client.patch(f"/api/schedules/{hook['id']}", json={'slack_signing_secret': 'signing'}, headers=OWNER)
        await set_token(client, hook['id'], SLACK_TEST_TOKEN)
        payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'Ev1', 'event': {'type': 'app_mention', 'channel': 'C1', 'text': 'hi'}}
        body = json.dumps(payload).encode()
        url = f"/hooks/{hook['id']}/slack"
        assert (await client.post(url, content=body, headers=slack_headers(body, 'signing'))).status_code == 202
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.turns') == 1
        # тот же event_id ещё раз: дедупликация должна сработать и без заголовка retry
        assert (await client.post(url, content=body, headers=slack_headers(body, 'signing'))).status_code == 202
        assert (await client.post(url, content=body, headers=slack_headers(body, 'signing', retry='1'))).status_code == 202
        retry_body = json.dumps({**payload, 'event_id': 'Ev3'}).encode()
        assert (await client.post(url, content=retry_body, headers=slack_headers(retry_body, 'signing', retry='1'))).status_code == 202
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.turns') == 2
            assert await con.fetchval('select count(*) from bothub.slack_handled_events where event_id=$1', 'Ev1') == 1
        concurrent_body = json.dumps({**payload, 'event_id': 'Ev-concurrent'}).encode()
        responses = await asyncio.gather(*(
            client.post(url, content=concurrent_body, headers=slack_headers(concurrent_body, 'signing'))
            for _ in range(2)))
        assert all(response.status_code == 202 for response in responses)
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.turns') == 3


async def test_slack_team_binding_rejects_foreign_and_unbound_events():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await client.patch(f"/api/schedules/{hook['id']}", json={'slack_signing_secret': 'signing'}, headers=OWNER)
        url = f"/hooks/{hook['id']}/slack"
        payload = {'type': 'event_callback', 'team_id': 'T2', 'event_id': 'Ev-other',
                   'event': {'type': 'app_mention', 'channel': 'C1', 'text': 'hi'}}
        body = json.dumps(payload).encode()
        assert (await client.post(url, content=body, headers=slack_headers(body, 'signing'))).status_code == 202
        await set_token(client, hook['id'], SLACK_TEST_TOKEN)
        assert (await client.post(url, content=body, headers=slack_headers(body, 'signing'))).status_code == 403
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.turns') == 0
            assert await con.fetchval('select count(*) from bothub.slack_handled_events') == 0


async def test_slack_auth_failure_preserves_previous_token(monkeypatch):
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        assert (await set_token(client, hook['id'], 'xoxb-good')).status_code == 200
        async def reject(token):
            raise channels_slack.SlackError('slack_auth_failed')
        monkeypatch.setattr(channels_slack, 'slack_team_id', reject)
        assert (await set_token(client, hook['id'], 'xoxb-bad')).status_code == 400
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select slack_bot_token,slack_team_id from bothub.schedules where id=$1', uuid.UUID(hook['id']))
        assert decrypt_secret(bytes(row['slack_bot_token']), uuid.UUID(hook['id']).bytes).decode() == 'xoxb-good'
        assert row['slack_team_id'] == 'T1'


async def test_slack_dedup_transaction_starts_after_launcher_check(monkeypatch):
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await client.patch(f"/api/schedules/{hook['id']}", json={'slack_signing_secret': 'signing'}, headers=OWNER)
        await set_token(client, hook['id'], SLACK_TEST_TOKEN)
        assert isinstance(app.state.pool, asyncpg.Pool)
        assert app.state.pool.get_max_size() >= 2
        monkeypatch.setenv('BOTHUB_RUNNER_EXEC', 'docker')
        entered, release = asyncio.Event(), asyncio.Event()
        class Launcher:
            calls = 0
            async def status(self, bot_id):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                return SimpleNamespace(running=True)
            async def aclose(self):
                pass
        app.state.launcher = Launcher()
        app.state.launcher_ready = True
        payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'Ev-wait',
                   'event': {'type': 'app_mention', 'channel': 'C1', 'text': 'hi'}}
        body = json.dumps(payload).encode()
        url = f"/hooks/{hook['id']}/slack"
        headers = slack_headers(body, 'signing')
        pending = asyncio.create_task(client.post(url, content=body, headers=headers))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = await asyncio.wait_for(client.post(url, content=body, headers=headers), 2)
            assert second.status_code == 202
            assert not pending.done()
        finally:
            release.set()
            first = await asyncio.wait_for(pending, 2)
        assert first.status_code == 202
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.slack_handled_events') == 1
            assert await con.fetchval('select count(*) from bothub.turns') == 1


async def test_slack_reply_goes_to_channel_and_thread(monkeypatch):
    async with worker_client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await client.patch(f"/api/schedules/{hook['id']}", json={'slack_signing_secret': 'signing'}, headers=OWNER)
        await set_token(client, hook['id'], SLACK_TEST_TOKEN)
        payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'Ev2', 'event': {'type': 'app_mention', 'channel': 'C1', 'ts': '111.1', 'text': 'hi'}}
        body = json.dumps(payload).encode()
        assert (await client.post(f"/hooks/{hook['id']}/slack", content=body, headers=slack_headers(body, 'signing'))).status_code == 202
        sent = []
        delivered = asyncio.Event()
        async def fake_send(token, channel, thread_ts, text):
            sent.append((token, channel, thread_ts, text))
            delivered.set()
        monkeypatch.setattr('bothub.channels_slack.send_slack_message', fake_send)
        row = await app.state.claim_turn()
        assert row is not None
        await app.state.run_turn(row)
        await asyncio.wait_for(delivered.wait(), 2)
        assert sent == [(SLACK_TEST_TOKEN, 'C1', '111.1', 'ответ')]


async def test_slack_network_runs_after_turn_cleanup_without_pool_connection(monkeypatch):
    async with worker_client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await client.patch(f"/api/schedules/{hook['id']}", json={'slack_signing_secret': 'signing'}, headers=OWNER)
        await set_token(client, hook['id'], SLACK_TEST_TOKEN)
        payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'Ev-bg',
                   'event': {'type': 'app_mention', 'channel': 'C1', 'text': 'hi'}}
        body = json.dumps(payload).encode()
        assert (await client.post(f"/hooks/{hook['id']}/slack", content=body,
                                  headers=slack_headers(body, 'signing'))).status_code == 202
        entered, release = asyncio.Event(), asyncio.Event()
        pool_sizes = []
        async def blocked_send(token, channel, thread_ts, text):
            pool_sizes.append((app.state.pool.get_idle_size(), app.state.pool.get_size()))
            entered.set()
            await release.wait()
        monkeypatch.setattr('bothub.channels_slack.send_slack_message', blocked_send)
        row = await app.state.claim_turn()
        assert row is not None
        try:
            await asyncio.wait_for(app.state.run_turn(row), 2)
            await asyncio.wait_for(entered.wait(), 2)
            assert pool_sizes[0][0] == pool_sizes[0][1]
            async with asyncio.timeout(2):
                async with app.state.pool.acquire() as con:
                    assert await con.fetchval('select status from bothub.turns where id=$1', row['id']) == 'done'
                    assert await con.fetchval("select count(*) from bothub.outbox where dedup_key=$1", f"turn:{row['id']}") == 1
        finally:
            release.set()


async def test_slack_reply_network_failure_is_logged_without_failing_turn(monkeypatch, caplog):
    async with worker_client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await client.patch(f"/api/schedules/{hook['id']}", json={'slack_signing_secret': 'signing'}, headers=OWNER)
        await set_token(client, hook['id'], SLACK_TEST_TOKEN)
        payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'Ev4', 'event': {'type': 'app_mention', 'channel': 'C1', 'thread_ts': '111.0', 'ts': '111.1', 'text': 'hi'}}
        body = json.dumps(payload).encode()
        assert (await client.post(f"/hooks/{hook['id']}/slack", content=body, headers=slack_headers(body, 'signing'))).status_code == 202
        sent = []
        attempted = asyncio.Event()
        async def failed_send(token, channel, thread_ts, text):
            sent.append((channel, thread_ts))
            attempted.set()
            raise httpx.ConnectError('network down')
        monkeypatch.setattr('bothub.channels_slack.send_slack_message', failed_send)
        row = await app.state.claim_turn()
        assert row is not None
        await app.state.run_turn(row)
        await asyncio.wait_for(attempted.wait(), 2)
        await asyncio.sleep(0)
        assert sent == [('C1', '111.0')]
        assert 'slack_reply_failed' in caplog.text
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select status from bothub.turns where id=$1', row['id']) == 'done'
