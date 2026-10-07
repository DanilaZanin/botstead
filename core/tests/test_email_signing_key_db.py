"""Ключ подписи Mailgun хранится зашифрованным; почтовый маршрут использует отдельный токен (нужен conftest.py)."""
import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest

from bothub.secrets import decrypt_secret
from test_activity_db import OWNER, add_member, client_for, cron_schedule, hook_for, make_bot

STALE_TS = 1700000000  # время 2023: при реальном часе ядра гарантированно старше 15 минут


@pytest.fixture(autouse=True)
def secret_keys(monkeypatch):
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())


KEY = 'mailgun-signing-key-xyz'


def signed_body():
    return json.dumps({'sender': 'a@example.com', 'recipient': 'b@example.com', 'subject': 'Hi',
                       'body-plain': 'Привет'},
                      sort_keys=True).encode()


def json_headers(body: bytes, secret: str = KEY, ts: int = STALE_TS):
    sig = hmac.new(secret.encode(), str(ts).encode() + body, hashlib.sha256).hexdigest()
    return {'content-type': 'application/json', 'x-mailgun-timestamp': str(ts), 'x-mailgun-signature': sig}


async def set_key(client, schedule_id, value):
    return await client.patch(f'/api/schedules/{schedule_id}', json={'email_signing_key': value}, headers=OWNER)


async def test_key_is_write_only_encrypted_and_clearable():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        assert hook['email_route_token'] and hook['email_route_token'] != hook['hook_token']
        assert hook['has_email_signing_key'] is False and 'email_signing_key' not in hook
        response = await set_key(client, hook['id'], KEY)
        assert response.status_code == 200 and response.json()['has_email_signing_key'] is True
        listed = await client.get('/api/schedules', headers=OWNER)
        assert KEY not in response.text and KEY not in listed.text
        assert 'email_signing_key' not in listed.json()[0] and listed.json()[0]['has_email_signing_key'] is True
        async with app.state.pool.acquire() as con:
            stored = bytes(await con.fetchval('select email_signing_key from bothub.schedules where id=$1', uuid.UUID(hook['id'])))
        assert KEY.encode() not in stored
        assert decrypt_secret(stored, uuid.UUID(hook['id']).bytes).decode() == KEY
        # другие поля не стирают ключ
        other = await client.patch(f"/api/schedules/{hook['id']}", json={'prompt': 'again'}, headers=OWNER)
        assert other.json()['has_email_signing_key'] is True
        cleared = await set_key(client, hook['id'], None)
        assert cleared.status_code == 200 and cleared.json()['has_email_signing_key'] is False
        for bad in ('', '   ', 5, 'x' * 300):
            assert (await set_key(client, hook['id'], bad)).status_code in (400, 422)


async def test_email_route_checks():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        assert hook['email_route_token'] != hook['hook_token']
        url = f"/hooks/{hook['id']}/email/{hook['email_route_token']}/json"
        body = signed_body()
        # ключ не задан: 403, подпись с hook_token не заменяет ключ Mailgun
        assert (await client.post(url, content=body, headers=json_headers(body, hook['hook_token']))).status_code == 403
        await set_key(client, hook['id'], KEY)
        assert (await client.post(f"/hooks/{hook['id']}", params={'token': hook['hook_token']}, json={'run': True})).status_code == 403
        github_body = b'{"action":"opened","repository":{"full_name":"x/y"},"issue":{"title":"x","number":1}}'
        github_sig = hmac.new(hook['hook_token'].encode(), github_body, hashlib.sha256).hexdigest()
        assert (await client.post(f"/hooks/{hook['id']}/github", content=github_body,
                                  headers={'x-hub-signature-256': 'sha256=' + github_sig, 'x-github-event': 'issues'})).status_code == 403
        # подпись по hook_token не проходит
        assert (await client.post(url, content=body, headers=json_headers(body, hook['hook_token']))).status_code == 403
        # подпись по чужому ключу не проходит
        assert (await client.post(url, content=body, headers=json_headers(body, 'wrong'))).status_code == 403
        assert (await client.post(url, content=body, headers={'content-type': 'application/json'})).status_code == 403
        assert (await client.post(f"/hooks/{hook['id']}/email?token={hook['hook_token']}", content=body,
                                  headers=json_headers(body))).status_code == 403
        # подпись верная, но timestamp старше 15 минут: 403 и turn не создан
        assert (await client.post(url, content=body, headers=json_headers(body))).status_code == 403
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 0
        # свежий timestamp: 200, turn создан с промптом почты
        fresh = int(time.time())
        response = await client.post(url, content=body, headers=json_headers(body, ts=fresh))
        assert response.status_code == 200 and response.json() == {'status': 'accepted'}, response.text
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow("select prompt from bothub.turns where client='hook'")
        assert row is not None
        assert 'Входящая почта (Mailgun)' in row['prompt']
        assert '"sender": "a@example.com"' in row['prompt']
        assert '"subject": "Hi"' in row['prompt']
        assert 'Привет' in row['prompt']
        assert 'Вложения не поддерживаются' in row['prompt']
        # повтор с тем же подписанным token не создаёт второй ход
        repeated = await client.post(url, content=body, headers=json_headers(body, ts=fresh))
        assert repeated.status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 1
        await set_key(client, hook['id'], None)
        assert (await client.post(url, content=body, headers=json_headers(body, ts=int(time.time())))).status_code == 403
        assert (await client.post(f"/hooks/{hook['id']}", params={'token': hook['email_route_token']}, json={})).status_code == 403
        email_sig = hmac.new(hook['email_route_token'].encode(), github_body, hashlib.sha256).hexdigest()
        assert (await client.post(f"/hooks/{hook['id']}/github", content=github_body,
                                  headers={'x-hub-signature-256': 'sha256=' + email_sig, 'x-github-event': 'issues'})).status_code == 403


async def test_future_timestamp_receipt_survives_fifteen_minutes():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await set_key(client, hook['id'], KEY)
        body = signed_body()
        ts = int(time.time()) + 899
        url = f"/hooks/{hook['id']}/email/{hook['email_route_token']}/json"
        headers = json_headers(body, ts=ts)
        assert (await client.post(url, content=body, headers=headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.email_webhook_receipts set received_at=now() - interval '15 minutes 1 second' where schedule_id=$1", uuid.UUID(hook['id']))
        assert (await client.post(url, content=body, headers=headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 1


async def test_slack_not_opened_by_email_key():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        # ключ Mailgun не открывается Slack-адаптеру: у каждого своя колонка
        await set_key(client, hook['id'], KEY)
        url = f"/hooks/{hook['id']}/slack"
        body = json.dumps({'type': 'url_verification', 'challenge': 'abc'}).encode()
        ts = int(time.time())
        digest = hmac.new(KEY.encode(), b'v0:' + str(ts).encode() + b':' + body, hashlib.sha256).hexdigest()
        assert (await client.post(url, content=body, headers={'x-slack-signature': f'v0={digest}', 'x-slack-request-timestamp': str(ts), 'content-type': 'application/json'})).status_code == 403
        listed = await client.get('/api/schedules', headers=OWNER)
        assert listed.json()[0]['has_email_signing_key'] is True


async def test_foreign_owner_cannot_read_or_change_key():
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        await set_key(client, hook['id'], KEY)
        _, member_headers = await add_member(client, app)
        assert (await client.get('/api/schedules', headers=member_headers)).json() == []
        response = await client.patch(f"/api/schedules/{hook['id']}", json={'email_signing_key': 'other'}, headers=member_headers)
        assert response.status_code == 404
        own = await client.get('/api/schedules', headers=OWNER)
        assert own.json()[0]['has_email_signing_key'] is True


async def test_email_key_rejected_for_cron_schedule():
    async with client_for() as (client, app):
        await make_bot(client)
        cron = await cron_schedule(client)
        response = await set_key(client, cron['id'], KEY)
        assert response.status_code == 400


async def test_missing_server_keys_is_503_not_500(monkeypatch):
    async with client_for() as (client, app):
        await make_bot(client)
        hook = await hook_for(client)
        monkeypatch.delenv('BOTHUB_SECRET_KEYS')
        response = await set_key(client, hook['id'], KEY)
        assert response.status_code == 503 and response.json()['error'] == 'secret_keys_missing'
