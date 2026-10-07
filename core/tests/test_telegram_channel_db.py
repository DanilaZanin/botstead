"""Канал Telegram: шифрование токена, webhook, проверка chat_id, отправка ответа при завершении хода."""
import asyncio
import base64
import json
import uuid

import pytest

from bothub.runner.base import RunnerEvent
from test_activity_db import OWNER, add_member, bot_headers, client_for, make_bot, make_thread

TELEGRAM_TOKEN = "123456:ABC-DEF1234ghijkl"


@pytest.fixture(autouse=True)
def secret_keys(monkeypatch):
    monkeypatch.setenv("BOTHUB_SECRET_KEYS", "1:" + base64.b64encode(b"k" * 32).decode())


async def test_put_get_delete_telegram_channel():
    async with client_for() as (client, app):
        await make_bot(client)
        bot_id = "alpha"

        get = await client.get(f"/api/bots/{bot_id}/channels/telegram", headers=OWNER)
        assert get.status_code == 200
        data = get.json()
        assert data["kind"] == "telegram"
        assert data["enabled"] is False
        assert data["token_last4"] is None

        put = await client.put(
            f"/api/bots/{bot_id}/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": [123, 456], "enabled": True},
            headers=OWNER,
        )
        assert put.status_code == 200, put.text
        ch = put.json()
        assert ch["kind"] == "telegram"
        assert ch["enabled"] is True
        assert ch["allowed_chat_ids"] == [123, 456]
        assert ch["token_last4"] == TELEGRAM_TOKEN[-4:]
        assert "token" not in ch
        assert "token_enc" not in ch
        assert "webhook_secret" not in ch

        get2 = await client.get(f"/api/bots/{bot_id}/channels/telegram", headers=OWNER)
        assert get2.status_code == 200
        assert get2.json()["token_last4"] == TELEGRAM_TOKEN[-4:]

        put2 = await client.put(
            f"/api/bots/{bot_id}/channels/telegram",
            json={"allowed_chat_ids": [789], "enabled": False},
            headers=OWNER,
        )
        assert put2.status_code == 200
        assert put2.json()["allowed_chat_ids"] == [789]
        assert put2.json()["enabled"] is False

        delete = await client.delete(f"/api/bots/{bot_id}/channels/telegram", headers=OWNER)
        assert delete.status_code == 200
        assert delete.json() == {"ok": True}

        get3 = await client.get(f"/api/bots/{bot_id}/channels/telegram", headers=OWNER)
        assert get3.status_code == 200
        assert get3.json()["token_last4"] is None


async def test_telegram_channel_requires_owner():
    async with client_for() as (client, app):
        await make_bot(client)
        assert (await client.get("/api/bots/alpha/channels/telegram")).status_code == 401
        assert (await client.put("/api/bots/alpha/channels/telegram", json={"token": "x"})).status_code == 401
        assert (await client.delete("/api/bots/alpha/channels/telegram")).status_code == 401
        assert (await client.get("/api/bots/alpha/channels/telegram", headers=bot_headers('alpha'))).status_code == 403
        assert (await client.put("/api/bots/alpha/channels/telegram", json={"token": "x"}, headers=bot_headers('alpha'))).status_code == 403
        assert (await client.delete("/api/bots/alpha/channels/telegram", headers=bot_headers('alpha'))).status_code == 403


async def test_webhook_secret_verification():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put(
            "/api/bots/alpha/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": [111], "enabled": True},
            headers=OWNER,
        )
        assert put.status_code == 200
        channel_id = put.json()["id"]

        async with app.state.pool.acquire() as con:
            secret = await con.fetchval("select webhook_secret from bothub.bot_channels where id=$1", uuid.UUID(channel_id))

        body = json.dumps({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111}, "text": "hello"}}).encode()

        resp = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        resp2 = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong", "Content-Type": "application/json"},
        )
        assert resp2.status_code == 403

        resp3 = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp3.status_code == 403


async def test_webhook_rejects_unknown_chat_id():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put(
            "/api/bots/alpha/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": [111], "enabled": True},
            headers=OWNER,
        )
        channel_id = put.json()["id"]

        async with app.state.pool.acquire() as con:
            secret = await con.fetchval("select webhook_secret from bothub.bot_channels where id=$1", uuid.UUID(channel_id))

        body = json.dumps({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 999}, "text": "hi"}}).encode()
        resp = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        async with app.state.pool.acquire() as con:
            count = await con.fetchval("select count(*) from bothub.turns where client='hook'")
        assert count == 0


async def test_webhook_creates_turn_for_allowed_chat():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put(
            "/api/bots/alpha/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": [111], "enabled": True},
            headers=OWNER,
        )
        channel_id = put.json()["id"]

        async with app.state.pool.acquire() as con:
            secret = await con.fetchval("select webhook_secret from bothub.bot_channels where id=$1", uuid.UUID(channel_id))

        body = json.dumps({"update_id": 2, "message": {"message_id": 2, "chat": {"id": 111}, "text": "привет!"}}).encode()
        resp = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        async with app.state.pool.acquire() as con:
            thread = await con.fetchrow("select * from bothub.threads where bot_id='alpha' and title='Telegram 111'")
            assert thread is not None
            assert thread["kind"] == "direct"

            turn = await con.fetchrow("select * from bothub.turns where thread_id=$1 and client='hook'", thread["id"])
            assert turn is not None
            assert turn["prompt"] == "привет!"

        # Повторное сообщение от того же chat_id переиспользует тот же тред
        body2 = json.dumps({"update_id": 3, "message": {"message_id": 3, "chat": {"id": 111}, "text": "ещё"}}).encode()
        resp2 = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body2,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"},
        )
        assert resp2.status_code == 200

        async with app.state.pool.acquire() as con:
            threads = await con.fetch("select id from bothub.threads where bot_id='alpha' and title='Telegram 111' and status='active'")
            assert len(threads) == 1  # тред переиспользован


async def test_disabled_channel_webhook_404():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put(
            "/api/bots/alpha/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": [111], "enabled": False},
            headers=OWNER,
        )
        channel_id = put.json()["id"]

        async with app.state.pool.acquire() as con:
            secret = await con.fetchval("select webhook_secret from bothub.bot_channels where id=$1", uuid.UUID(channel_id))

        body = json.dumps({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111}, "text": "hi"}}).encode()
        resp = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"},
        )
        assert resp.status_code == 404


async def test_invalid_token_rejected():
    async with client_for() as (client, app):
        await make_bot(client)
        for bad_token in ["", "   ", None]:
            resp = await client.put(
                "/api/bots/alpha/channels/telegram",
                json={"token": bad_token, "allowed_chat_ids": [1], "enabled": True},
                headers=OWNER,
            )
            assert resp.status_code in (400, 422), f"{bad_token!r}: {resp.status_code}"

        resp = await client.put(
            "/api/bots/alpha/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": ["abc"], "enabled": True},
            headers=OWNER,
        )
        assert resp.status_code == 400


async def test_non_existent_bot_404():
    async with client_for() as (client, app):
        assert (await client.get("/api/bots/ghost/channels/telegram", headers=OWNER)).status_code == 404
        assert (await client.put("/api/bots/ghost/channels/telegram", json={"token": "x"}, headers=OWNER)).status_code == 404
        assert (await client.delete("/api/bots/ghost/channels/telegram", headers=OWNER)).status_code == 404


async def test_empty_text_message_ignored():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put(
            "/api/bots/alpha/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": [111], "enabled": True},
            headers=OWNER,
        )
        channel_id = put.json()["id"]

        async with app.state.pool.acquire() as con:
            secret = await con.fetchval("select webhook_secret from bothub.bot_channels where id=$1", uuid.UUID(channel_id))

        body = json.dumps({"update_id": 3, "message": {"message_id": 3, "chat": {"id": 111}, "text": "   "}}).encode()
        resp = await client.post(
            f"/api/channels/telegram/{channel_id}/webhook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        async with app.state.pool.acquire() as con:
            count = await con.fetchval("select count(*) from bothub.turns where client='hook'")
        assert count == 0


@pytest.mark.parametrize("send_fails", [False, True])
async def test_send_reply_on_turn_done(monkeypatch, caplog, send_fails):
    """Отправка в Telegram не влияет на завершение хода, даже при сетевой ошибке."""
    import bothub.channels_telegram as ct

    send_calls = []
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def mock_send_message(token, chat_id, text):
        send_calls.append((token, chat_id, text))
        send_started.set()
        await release_send.wait()
        if send_fails:
            raise OSError("telegram unavailable")
        return {"ok": True}

    monkeypatch.setattr(ct, "send_message", mock_send_message)

    class ReplyRunner:
        async def run(self, turn):
            yield RunnerEvent("assistant_msg", {"text": "Здравствуйте!", "final": True})

    async with client_for(runner_factory=lambda provider: ReplyRunner()) as (client, app):
        await make_bot(client)
        put = await client.put(
            "/api/bots/alpha/channels/telegram",
            json={"token": TELEGRAM_TOKEN, "allowed_chat_ids": [111], "enabled": True},
            headers=OWNER,
        )
        assert put.status_code == 200

        async with app.state.pool.acquire() as con:
            secret = await con.fetchval("select webhook_secret from bothub.bot_channels where bot_id='alpha' and kind='telegram'")

        body = json.dumps({"update_id": 4, "message": {"message_id": 4, "chat": {"id": 111}, "text": "привет"}}).encode()
        resp = await client.post(
            f"/api/channels/telegram/{put.json()['id']}/webhook",
            content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        turn = await app.state.claim_turn()
        assert turn is not None
        await asyncio.wait_for(app.state.run_turn(turn), timeout=2)
        await asyncio.wait_for(send_started.wait(), timeout=2)
        assert not release_send.is_set()
        assert app.state.pool.get_idle_size() == app.state.pool.get_size()
        async with app.state.pool.acquire() as con:
            status = await con.fetchval('select status from bothub.turns where id=$1', turn['id'])
            events = await con.fetch('select kind from bothub.events where turn_id=$1 order by seq', turn['id'])
        assert status == 'done'
        assert [event['kind'] for event in events if event['kind'] in ('user_msg', 'assistant_msg', 'status')] == [
            'user_msg', 'status', 'assistant_msg', 'status']
        assert send_calls == [(TELEGRAM_TOKEN, 111, 'Здравствуйте!')]
        release_send.set()
        if send_fails:
            async with asyncio.timeout(2):
                while 'telegram_send_reply_failed' not in caplog.text:
                    await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(0)
        assert ('telegram_send_reply_failed' in caplog.text) is send_fails


async def test_concurrent_put_encrypts_with_stored_channel_id():
    from bothub.secrets import decrypt_secret

    async with client_for() as (client, app):
        await make_bot(client)
        responses = await asyncio.gather(*(
            client.put('/api/bots/alpha/channels/telegram',
                       json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
            for _ in range(2)
        ))
        assert all(response.status_code == 200 for response in responses), responses
        assert len({response.json()['id'] for response in responses}) == 1
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow("select id,token_enc from bothub.bot_channels where bot_id='alpha'")
        assert decrypt_secret(bytes(row['token_enc']), row['id'].bytes).decode() == TELEGRAM_TOKEN


async def test_webhook_duplicate_update_creates_one_turn():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put('/api/bots/alpha/channels/telegram',
                               json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        channel_id = put.json()['id']
        async with app.state.pool.acquire() as con:
            secret = await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', uuid.UUID(channel_id))
        payload = {'update_id': 45, 'message': {'chat': {'id': 111}, 'text': 'once'}}
        url = f'/api/channels/telegram/{channel_id}/webhook'
        headers = {'X-Telegram-Bot-Api-Secret-Token': secret}
        responses = await asyncio.gather(*(client.post(url, json=payload, headers=headers) for _ in range(2)))
        assert all(response.status_code == 200 for response in responses)
        assert (await client.post(url, json=payload, headers=headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 1
            assert await con.fetchval('select count(*) from bothub.telegram_updates where channel_id=$1', uuid.UUID(channel_id)) == 1


async def test_webhook_rejects_invalid_update_id_and_boolean_chat_id():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put('/api/bots/alpha/channels/telegram',
                               json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [1]}, headers=OWNER)
        channel_id = put.json()['id']
        async with app.state.pool.acquire() as con:
            secret = await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', uuid.UUID(channel_id))
        url = f'/api/channels/telegram/{channel_id}/webhook'
        headers = {'X-Telegram-Bot-Api-Secret-Token': secret}
        for invalid in (None, True, '1', 2**64):
            response = await client.post(url, json={'update_id': invalid, 'message': {'chat': {'id': 1}, 'text': 'bad'}}, headers=headers)
            assert response.status_code == 400
        response = await client.post(url, json={'update_id': 12, 'message': {'chat': {'id': True}, 'text': 'bad'}}, headers=headers)
        assert response.status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 0


async def test_user_named_telegram_thread_never_receives_reply(monkeypatch):
    import bothub.channels_telegram as ct

    sent = []

    async def mock_send(*args):
        sent.append(args)

    class ReplyRunner:
        async def run(self, turn):
            yield RunnerEvent('assistant_msg', {'text': 'private answer', 'final': True})

    monkeypatch.setattr(ct, 'send_message', mock_send)
    async with client_for(runner_factory=lambda provider: ReplyRunner()) as (client, app):
        await make_bot(client)
        await client.put('/api/bots/alpha/channels/telegram',
                         json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        thread_id = await make_thread(client, title='Telegram 111')
        turn = await client.post(f'/api/threads/{thread_id}/turns', json={'prompt': 'hi'}, headers=OWNER)
        assert turn.status_code == 200
        claimed = await app.state.claim_turn()
        assert claimed is not None
        await asyncio.wait_for(app.state.run_turn(claimed), timeout=2)
        replies = [task for task in asyncio.all_tasks() if task.get_coro().__name__ == '_send_telegram_reply']
        if replies:
            await asyncio.wait_for(asyncio.gather(*replies), timeout=2)
        assert sent == []


async def test_channel_is_not_visible_to_another_user():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put('/api/bots/alpha/channels/telegram',
                               json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        assert put.status_code == 200
        _, member_headers = await add_member(client, app)
        url = '/api/bots/alpha/channels/telegram'
        assert (await client.get(url, headers=member_headers)).status_code == 404
        assert (await client.put(url, json={'token': '123456:other'}, headers=member_headers)).status_code == 404
        assert (await client.delete(url, headers=member_headers)).status_code == 404
        assert (await client.get(url, headers=OWNER)).json()['token_last4'] == TELEGRAM_TOKEN[-4:]


async def test_put_rejects_path_injection_and_boolean_chat_id():
    async with client_for() as (client, app):
        await make_bot(client)
        for bad_token in ('123456:a/../b', '123456:a?url=x', '123456:a\nX:1'):
            response = await client.put('/api/bots/alpha/channels/telegram',
                                        json={'token': bad_token}, headers=OWNER)
            assert response.status_code == 400
        response = await client.put('/api/bots/alpha/channels/telegram',
                                    json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [True]}, headers=OWNER)
        assert response.status_code == 400


async def test_token_change_rotates_webhook_secret():
    async with client_for() as (client, app):
        await make_bot(client)
        url = '/api/bots/alpha/channels/telegram'
        put = await client.put(url, json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        assert put.status_code == 200
        channel_id = uuid.UUID(put.json()['id'])
        async with app.state.pool.acquire() as con:
            old_secret = await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', channel_id)
        assert (await client.put(url, json={'allowed_chat_ids': [111]}, headers=OWNER)).status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', channel_id) == old_secret
        assert (await client.put(url, json={'token': '123456:replacement', 'allowed_chat_ids': [111]}, headers=OWNER)).status_code == 200
        async with app.state.pool.acquire() as con:
            new_secret = await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', channel_id)
        assert new_secret != old_secret
        payload = {'update_id': 80, 'message': {'chat': {'id': 111}, 'text': 'new bot'}}
        hook_url = f'/api/channels/telegram/{channel_id}/webhook'
        assert (await client.post(hook_url, json=payload, headers={'X-Telegram-Bot-Api-Secret-Token': old_secret})).status_code == 403
        assert (await client.post(hook_url, json=payload, headers={'X-Telegram-Bot-Api-Secret-Token': new_secret})).status_code == 200


async def test_partial_put_preserves_whitelist_and_disabled_state():
    async with client_for() as (client, app):
        await make_bot(client)
        url = '/api/bots/alpha/channels/telegram'
        put = await client.put(url, json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111], 'enabled': False}, headers=OWNER)
        assert put.status_code == 200
        changed = await client.put(url, json={'token': '123456:replacement'}, headers=OWNER)
        assert changed.status_code == 200
        assert changed.json()['allowed_chat_ids'] == [111]
        assert changed.json()['enabled'] is False
        assert changed.json()['token_last4'] == 'ment'


async def test_archived_telegram_thread_is_replaced():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put('/api/bots/alpha/channels/telegram',
                               json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        channel_id = uuid.UUID(put.json()['id'])
        async with app.state.pool.acquire() as con:
            secret = await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', channel_id)
        url = f'/api/channels/telegram/{channel_id}/webhook'
        headers = {'X-Telegram-Bot-Api-Secret-Token': secret}
        for update_id in (1, 2):
            response = await client.post(url, json={'update_id': update_id,
                                                     'message': {'chat': {'id': 111}, 'text': 'hi'}}, headers=headers)
            assert response.status_code == 200
            if update_id == 1:
                async with app.state.pool.acquire() as con:
                    old_thread = await con.fetchval('select thread_id from bothub.telegram_threads where channel_id=$1', channel_id)
                archived = await client.patch(f'/api/threads/{old_thread}', json={'status': 'archived'}, headers=OWNER)
                assert archived.status_code == 200
        async with app.state.pool.acquire() as con:
            new_thread = await con.fetchval('select thread_id from bothub.telegram_threads where channel_id=$1', channel_id)
            assert new_thread != old_thread
            assert await con.fetchval('select status from bothub.threads where id=$1', new_thread) == 'active'
            assert await con.fetchval('select count(*) from bothub.turns where thread_id=$1', new_thread) == 1


async def test_pending_turn_keeps_reply_address_after_thread_replacement(monkeypatch):
    import bothub.channels_telegram as ct

    sent = []
    sent_event = asyncio.Event()

    async def mock_send(token, chat_id, text):
        sent.append((token, chat_id, text))
        sent_event.set()

    class ReplyRunner:
        async def run(self, turn):
            yield RunnerEvent('assistant_msg', {'text': 'old reply', 'final': True})

    monkeypatch.setattr(ct, 'send_message', mock_send)
    async with client_for(runner_factory=lambda provider: ReplyRunner()) as (client, app):
        await make_bot(client)
        put = await client.put('/api/bots/alpha/channels/telegram',
                               json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        channel_id = uuid.UUID(put.json()['id'])
        async with app.state.pool.acquire() as con:
            secret = await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', channel_id)
        url = f'/api/channels/telegram/{channel_id}/webhook'
        headers = {'X-Telegram-Bot-Api-Secret-Token': secret}
        first = {'update_id': 1, 'message': {'chat': {'id': 111}, 'text': 'first'}}
        assert (await client.post(url, json=first, headers=headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            old_thread = await con.fetchval('select thread_id from bothub.telegram_threads where channel_id=$1', channel_id)
            old_turn = await con.fetchval('select turn_id from bothub.telegram_updates where channel_id=$1 and update_id=1', channel_id)
        assert (await client.patch(f'/api/threads/{old_thread}', json={'status': 'archived'}, headers=OWNER)).status_code == 200
        second = {'update_id': 2, 'message': {'chat': {'id': 111}, 'text': 'second'}}
        assert (await client.post(url, json=second, headers=headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select thread_id from bothub.telegram_threads where channel_id=$1', channel_id) != old_thread
        claimed = await app.state.claim_turn()
        assert claimed['id'] == old_turn
        await asyncio.wait_for(app.state.run_turn(claimed), timeout=2)
        await asyncio.wait_for(sent_event.wait(), timeout=2)
        assert sent == [(TELEGRAM_TOKEN, 111, 'old reply')]


async def test_deleted_turn_keeps_update_dedup_record():
    async with client_for() as (client, app):
        await make_bot(client)
        put = await client.put('/api/bots/alpha/channels/telegram',
                               json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        channel_id = uuid.UUID(put.json()['id'])
        async with app.state.pool.acquire() as con:
            secret = await con.fetchval('select webhook_secret from bothub.bot_channels where id=$1', channel_id)
        url = f'/api/channels/telegram/{channel_id}/webhook'
        payload = {'update_id': 77, 'message': {'chat': {'id': 111}, 'text': 'once'}}
        headers = {'X-Telegram-Bot-Api-Secret-Token': secret}
        assert (await client.post(url, json=payload, headers=headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            turn_id = await con.fetchval('select turn_id from bothub.telegram_updates where channel_id=$1 and update_id=77', channel_id)
            await con.execute('delete from bothub.events where turn_id=$1', turn_id)
            await con.execute('delete from bothub.turns where id=$1', turn_id)
            assert await con.fetchval('select turn_id from bothub.telegram_updates where channel_id=$1 and update_id=77', channel_id) is None
        assert (await client.post(url, json=payload, headers=headers)).status_code == 200
        async with app.state.pool.acquire() as con:
            assert await con.fetchval('select count(*) from bothub.telegram_updates where channel_id=$1 and update_id=77', channel_id) == 1
            assert await con.fetchval("select count(*) from bothub.turns where client='hook'") == 0


async def test_telegram_network_calls_release_pool_and_hide_token(monkeypatch, caplog):
    import bothub.channels_telegram as ct

    monkeypatch.setenv('BOTHUB_PUBLIC_ORIGIN', 'https://example.com')
    pool = None
    seen = []

    async def mock_set_webhook(token, url, secret):
        seen.append(('set', pool.get_idle_size() == pool.get_size()))
        raise RuntimeError(f'network failed: {token}')

    async def mock_delete_webhook(token):
        seen.append(('delete', pool.get_idle_size() == pool.get_size()))
        raise RuntimeError(f'network failed: {token}')

    monkeypatch.setattr(ct, 'set_webhook', mock_set_webhook)
    monkeypatch.setattr(ct, 'delete_webhook', mock_delete_webhook)
    async with client_for() as (client, app):
        pool = app.state.pool
        await make_bot(client)
        put = await client.put('/api/bots/alpha/channels/telegram',
                               json={'token': TELEGRAM_TOKEN, 'allowed_chat_ids': [111]}, headers=OWNER)
        assert put.status_code == 200
        assert TELEGRAM_TOKEN not in put.text
        assert (await client.delete('/api/bots/alpha/channels/telegram', headers=OWNER)).status_code == 200
    assert seen == [('set', True), ('delete', True)]
    assert TELEGRAM_TOKEN not in caplog.text


async def test_delayed_put_reconciles_latest_webhook(monkeypatch):
    import bothub.channels_telegram as ct
    from bothub.secrets import decrypt_secret

    monkeypatch.setenv('BOTHUB_PUBLIC_ORIGIN', 'https://example.com')
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    remote = {}
    replacement = '123456:replacement'

    async def mock_set(token, url, secret):
        if token == TELEGRAM_TOKEN:
            first_started.set()
            await release_first.wait()
        remote[token] = secret

    async def mock_delete(token):
        remote.pop(token, None)

    monkeypatch.setattr(ct, 'set_webhook', mock_set)
    monkeypatch.setattr(ct, 'delete_webhook', mock_delete)
    async with client_for() as (client, app):
        await make_bot(client)
        url = '/api/bots/alpha/channels/telegram'
        first = asyncio.create_task(client.put(url, json={'token': TELEGRAM_TOKEN}, headers=OWNER))
        await asyncio.wait_for(first_started.wait(), timeout=2)
        second = await client.put(url, json={'token': replacement}, headers=OWNER)
        assert second.status_code == 200
        release_first.set()
        assert (await asyncio.wait_for(first, timeout=2)).status_code == 200
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow("select * from bothub.bot_channels where bot_id='alpha'")
        stored_token = decrypt_secret(bytes(row['token_enc']), row['id'].bytes).decode()
        assert stored_token == replacement
        assert remote == {stored_token: row['webhook_secret']}


async def test_delayed_delete_reconciles_recreated_webhook(monkeypatch):
    import bothub.channels_telegram as ct

    monkeypatch.setenv('BOTHUB_PUBLIC_ORIGIN', 'https://example.com')
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()
    remote = {}
    block_delete = False

    async def mock_set(token, url, secret):
        remote[token] = secret

    async def mock_delete(token):
        nonlocal block_delete
        if block_delete:
            block_delete = False
            delete_started.set()
            await release_delete.wait()
        remote.pop(token, None)

    monkeypatch.setattr(ct, 'set_webhook', mock_set)
    monkeypatch.setattr(ct, 'delete_webhook', mock_delete)
    async with client_for() as (client, app):
        await make_bot(client)
        url = '/api/bots/alpha/channels/telegram'
        original = await client.put(url, json={'token': TELEGRAM_TOKEN}, headers=OWNER)
        assert original.status_code == 200
        block_delete = True
        deleting = asyncio.create_task(client.delete(url, headers=OWNER))
        await asyncio.wait_for(delete_started.wait(), timeout=2)
        recreated = await client.put(url, json={'token': TELEGRAM_TOKEN}, headers=OWNER)
        assert recreated.status_code == 200
        assert recreated.json()['id'] != original.json()['id']
        release_delete.set()
        assert (await asyncio.wait_for(deleting, timeout=2)).status_code == 200
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow("select * from bothub.bot_channels where bot_id='alpha'")
        assert remote == {TELEGRAM_TOKEN: row['webhook_secret']}
