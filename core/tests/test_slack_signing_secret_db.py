"""Секрет подписи Slack расписания: хранится зашифрованным, наружу не отдаётся, Slack-адаптер проверяет только им (нужен conftest.py)."""
import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest

from bothub.secrets import decrypt_secret
from test_activity_db import OWNER, client_for, hook_for, make_bot

@pytest.fixture(autouse=True)
def secret_keys(monkeypatch):
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())


SECRET = 'slack-signing-secret-xyz'


def slack_headers(body: bytes, secret: str, ts: int | None = None):
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), b'v0:' + str(ts).encode() + b':' + body, hashlib.sha256).hexdigest()
    return {'x-slack-signature': f'v0={digest}', 'x-slack-request-timestamp': str(ts), 'content-type': 'application/json'}


def github_headers(body: bytes, secret: str):
    return {'x-hub-signature-256': 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), 'x-github-event': 'ping',
            'content-type': 'application/json'}


async def set_secret(client, schedule_id, value):
    return await client.patch(f'/api/schedules/{schedule_id}', json={'slack_signing_secret': value}, headers=OWNER)


async def test_secret_is_write_only_encrypted_and_clearable():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        assert hook['has_slack_signing_secret'] is False and 'slack_signing_secret' not in hook
        response = await set_secret(client, hook['id'], SECRET)
        assert response.status_code == 200 and response.json()['has_slack_signing_secret'] is True
        listed = await client.get('/api/schedules', headers=OWNER)
        assert SECRET not in response.text and SECRET not in listed.text
        assert 'slack_signing_secret' not in listed.json()[0] and listed.json()[0]['has_slack_signing_secret'] is True
        async with app.state.pool.acquire() as con:
            stored = bytes(await con.fetchval('select slack_signing_secret from bothub.schedules where id=$1', uuid.UUID(hook['id'])))
        assert SECRET.encode() not in stored
        assert decrypt_secret(stored, uuid.UUID(hook['id']).bytes).decode() == SECRET
        # другие поля не стирают секрет
        other = await client.patch(f"/api/schedules/{hook['id']}", json={'prompt': 'again'}, headers=OWNER)
        assert other.json()['has_slack_signing_secret'] is True
        cleared = await set_secret(client, hook['id'], None)
        assert cleared.status_code == 200 and cleared.json()['has_slack_signing_secret'] is False
        for bad in ('', '   ', 5, 'x' * 300):
            assert (await set_secret(client, hook['id'], bad)).status_code in (400, 422)


async def test_slack_accepts_only_its_signing_secret():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        url = f"/hooks/{hook['id']}/slack"
        body = json.dumps({'type': 'url_verification', 'challenge': 'abc'}).encode()
        # секрет не задан: 403, даже с верной подписью по hook_token
        assert (await client.post(url, content=body, headers=slack_headers(body, hook['hook_token']))).status_code == 403
        await set_secret(client, hook['id'], SECRET)
        ok = await client.post(url, content=body, headers=slack_headers(body, SECRET))
        assert ok.status_code == 200 and ok.json() == {'challenge': 'abc'}
        assert (await client.post(url, content=body, headers=slack_headers(body, hook['hook_token']))).status_code == 403
        assert (await client.post(url, content=body, headers=slack_headers(body, 'wrong'))).status_code == 403
        assert (await client.post(url, content=body, headers={'content-type': 'application/json'})).status_code == 403
        assert (await client.post(url, content=body, headers=slack_headers(body, SECRET, ts=int(time.time()) - 3600))).status_code == 403
        await set_secret(client, hook['id'], None)
        assert (await client.post(url, content=body, headers=slack_headers(body, SECRET))).status_code == 403


async def test_github_still_uses_hook_token():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await set_secret(client, hook['id'], SECRET)
        url = f"/hooks/{hook['id']}/github"
        body = b'{}'
        assert (await client.post(url, content=body, headers=github_headers(body, hook['hook_token']))).status_code == 202
        assert (await client.post(url, content=body, headers=github_headers(body, SECRET))).status_code == 403


async def test_missing_server_keys_is_503_not_500(monkeypatch):
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        monkeypatch.delenv('BOTHUB_SECRET_KEYS')
        response = await set_secret(client, hook['id'], SECRET)
        assert response.status_code == 503 and response.json()['error'] == 'secret_keys_missing'
