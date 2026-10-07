"""Регистрация Mac, владение и адресация вызовов. Требуется PostgreSQL."""

import asyncio
import contextvars
import hashlib
import hmac
import os
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import WebSocket, WebSocketDisconnect

from bothub import auth
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}


@pytest.fixture(autouse=True)
def local_base_path(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')


def bot_token(bot_id):
    digest = hmac.new(b'test-secret', bot_id.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'Bearer bot:{bot_id}:{digest}'}


@pytest.mark.asyncio
async def test_macs_are_private_and_deleting_one_clears_its_bots():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            created = await client.post('/api/macs', json={'name':'  Work Mac  '}, headers=OWNER)
            assert created.status_code == 201, created.text
            mac = created.json()
            assert mac['name'] == 'Work Mac' and mac['token']
            listed = (await client.get('/api/macs', headers=OWNER)).json()
            assert len(listed) == 1 and listed[0]['id'] == mac['id']
            assert 'token' not in listed[0] and 'token_hash' not in listed[0]
            async with app.state.pool.acquire() as con:
                stored = await con.fetchval('select token_hash from bothub.macs where id=$1', uuid.UUID(mac['id']))
            assert stored == auth.token_hash(mac['token']) and stored != mac['token']

            bot = await client.post('/api/bots', json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake','mac_id':mac['id']}, headers=OWNER)
            assert bot.status_code == 200 and bot.json()['mac_id'] == mac['id'], bot.text

            invite = (await client.post('/api/invites', json={}, headers=OWNER)).json()
            accepted = await client.post('/api/invites/accept', json={'token':invite['token'],'email':'other@example.com','password':'long-password'})
            assert accepted.status_code == 201, accepted.text
            login = await client.post('/api/auth/login', json={'email':'other@example.com','password':'long-password'})
            assert login.status_code == 200, login.text
            member = {'Cookie':f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF':login.json()['csrf_token'], 'Origin':'https://testserver'}
            assert (await client.get('/api/macs', headers=member)).json() == []
            assert (await client.delete('/api/macs/'+mac['id'], headers=member)).status_code == 404
            foreign = await client.post('/api/bots', json={'id':'beta','name':'Beta','provider':'fake','model':'fake','mac_id':mac['id']}, headers=member)
            assert foreign.status_code == 404, foreign.text
            own = await client.post('/api/bots', json={'id':'beta','name':'Beta','provider':'fake','model':'fake'}, headers=member)
            assert own.status_code == 200, own.text
            assert (await client.patch('/api/bots/beta', json={'mac_id':mac['id']}, headers=member)).status_code == 404
            assert (await client.delete('/api/macs/'+mac['id'], headers=OWNER)).status_code == 200
            bots = (await client.get('/api/bots', headers=OWNER)).json()
            assert bots[0]['mac_id'] is None
            assert (await client.get('/api/macs', headers=OWNER)).json() == []
            assert (await client.get('/api/mac/status', headers={'Authorization':'Bearer '+mac['token']})).status_code == 401


def test_mac_calls_go_to_the_bot_selected_mac():
    app = create_app()
    async def idle():
        await asyncio.Event().wait()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    with TestClient(app, base_url='https://testserver') as client:
        mac_a = client.post('/api/macs', json={'name':'A'}, headers=OWNER).json()
        mac_b = client.post('/api/macs', json={'name':'B'}, headers=OWNER).json()
        calls = []
        for bot_id, mac in (('alpha', mac_a), ('beta', mac_b)):
            bot = client.post('/api/bots', json={'id':bot_id,'name':bot_id,'provider':'fake','model':'fake',
                'executor':'container','mac_id':mac['id']}, headers=OWNER)
            assert bot.status_code == 200, bot.text
            thread = client.post('/api/threads', json={'bot_id':bot_id}, headers=OWNER).json()
            turn = client.post('/api/threads/'+thread['id']+'/turns', json={'prompt':'screen'}, headers=OWNER).json()
            calls.append((bot_id, thread['id'], turn['id']))

        async def activate_turns():
            con = await asyncpg.connect(os.environ['DATABASE_URL'])
            try:
                for _, _, turn_id in calls:
                    await con.execute("update bothub.turns set status='running',lease_until=now()+interval '1 minute' where id=$1", uuid.UUID(turn_id))
            finally:
                await con.close()
        asyncio.run(activate_turns())

        with client.websocket_connect('/agent/mac', headers={'Authorization':'Bearer '+mac_a['token']}) as ws_a, \
             client.websocket_connect('/agent/mac', headers={'Authorization':'Bearer '+mac_b['token']}) as ws_b:
            ws_a.send_json({'type':'hello','host':'A'})
            ws_b.send_json({'type':'hello','host':'B'})
            for _ in range(50):
                rows = client.get('/api/macs', headers=OWNER).json()
                if len(rows) == 2 and all(row['state'] == 'online' for row in rows):
                    break
                time.sleep(.02)
            else:
                pytest.fail('Mac agents did not become online')

            for (bot_id, thread_id, turn_id), target in zip(calls, (ws_a, ws_b)):
                args = {'engine':'gemini','prompt':f'Work for {bot_id}'}
                approval = client.post('/api/approvals', json={'thread_id':thread_id,'turn_id':turn_id,
                    'risk':'exec','title':'delegate','tool':'mcp__bothub__mac_delegate','args':args}, headers=bot_token(bot_id))
                assert approval.status_code in (200, 201), approval.text
                decided = client.post('/api/approvals/'+approval.json()['id']+'/decide',
                    json={'decision':'approve','remember':False,'client':'iphone'}, headers=OWNER)
                assert decided.status_code == 200, decided.text
                with ThreadPoolExecutor(max_workers=2) as pool:
                    incoming = pool.submit(target.receive_json)
                    response = pool.submit(client.post, '/api/mac/call', json={
                        'thread_id':thread_id,'turn_id':turn_id,'tool':'delegate','args':args,'timeout':3},
                        headers=bot_token(bot_id))
                    call = incoming.result(timeout=3)
                    assert call['type'] == 'call' and call['tool'] == 'delegate' and call['args'] == args
                    target.send_json({'type':'result','id':call['id'],'ok':True,'data':{'host':bot_id}})
                    result = response.result(timeout=3)
                    assert result.status_code == 200 and result.json()['data']['host'] == bot_id, result.text


def test_mac_websocket_rejects_query_token_even_when_it_is_valid():
    app = create_app()
    with TestClient(app, base_url='https://testserver') as client:
        mac = client.post('/api/macs', json={'name':'A'}, headers=OWNER).json()
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/agent/mac?token='+mac['token']) as ws:
                ws.receive_json()
        assert denied.value.code == 4401


def test_legacy_env_and_migrated_hash_both_authenticate_same_first_mac(monkeypatch):
    monkeypatch.setenv('MAC_AGENT_TOKEN', 'old-env-token')
    app = create_app()
    with TestClient(app, base_url='https://testserver') as client:
        mac = client.post('/api/macs', json={'name':'Migrated'}, headers=OWNER).json()
        second = client.post('/api/macs', json={'name':'Other'}, headers=OWNER).json()
        async def pin_migrated_mac():
            con = await asyncpg.connect(os.environ['DATABASE_URL'])
            try:
                await con.execute("insert into bothub.settings(key,value) values('legacy_mac_id',to_jsonb($1::text))",mac['id'])
            finally:
                await con.close()
        asyncio.run(pin_migrated_mac())
        for token in ('old-env-token', mac['token']):
            with client.websocket_connect('/agent/mac', headers={'Authorization':'Bearer '+token}) as ws:
                ws.send_json({'type':'hello','host':'legacy'})
                for _ in range(50):
                    if client.get('/api/macs', headers=OWNER).json()[0]['state'] == 'online':
                        break
                    time.sleep(.02)
                else:
                    pytest.fail('Mac did not become online after authentication')
        async def stored_hash():
            con = await asyncpg.connect(os.environ['DATABASE_URL'])
            try:
                return (await con.fetchval('select token_hash from bothub.macs where id=$1',uuid.UUID(mac['id'])),
                    await con.fetchval("select value #>> '{}' from bothub.settings where key='legacy_mac_id'"))
            finally:
                await con.close()
        stored, pin = asyncio.run(stored_hash())
        assert stored == auth.token_hash(mac['token']) and pin == mac['id']
        assert client.delete('/api/macs/'+mac['id'],headers=OWNER).status_code == 200
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer old-env-token'}) as ws:
                ws.receive_json()
        assert denied.value.code == 4401
        denied_api = client.post('/api/files',data={'thread_id':str(uuid.uuid4())},
            files={'file':('probe.txt',b'x')},headers={'Authorization':'Bearer old-env-token'})
        assert denied_api.status_code == 401, denied_api.text
        with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer '+second['token']}) as ws:
            ws.send_json({'type':'hello','host':'other'})


@pytest.mark.empty_users
def test_setup_pins_old_mac_status_and_deletion_does_not_redirect_env_token(monkeypatch):
    monkeypatch.setenv('MAC_AGENT_TOKEN','setup-env-token')
    app = create_app()
    with TestClient(app,base_url='https://testserver') as client:
        async def seed_old_status():
            async with app.state.pool.acquire() as con:
                await con.execute("insert into bothub.mac_status(state,info) values('offline','{\"host\":\"Old Mac\"}'::jsonb)")
        client.portal.call(seed_old_status)
        setup = client.post('/api/setup',json={'email':'setup@example.com','password':'long-password'},headers=OWNER)
        assert setup.status_code == 201, setup.text
        first = client.get('/api/macs',headers=OWNER).json()[0]
        assert first['name'] == 'Old Mac'
        second = client.post('/api/macs',json={'name':'Second'},headers=OWNER).json()

        async def pin_and_status():
            async with app.state.pool.acquire() as con:
                return (await con.fetchval("select value #>> '{}' from bothub.settings where key='legacy_mac_id'"),
                    await con.fetchval('select mac_id from bothub.mac_status order by id limit 1'))
        pin, linked = client.portal.call(pin_and_status)
        assert pin == first['id'] and linked == uuid.UUID(first['id'])
        with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer setup-env-token'}) as ws:
            ws.send_json({'type':'hello','host':'old'})
        assert client.delete('/api/macs/'+first['id'],headers=OWNER).status_code == 200
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer setup-env-token'}) as ws:
                ws.receive_json()
        assert denied.value.code == 4401
        with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer '+second['token']}) as ws:
            ws.send_json({'type':'hello','host':'second'})


@pytest.mark.empty_users
def test_setup_without_old_mac_writes_tombstone_but_matching_hash_still_authenticates(monkeypatch):
    app = create_app()
    with TestClient(app,base_url='https://testserver') as client:
        setup = client.post('/api/setup',json={'email':'setup@example.com','password':'long-password'},headers=OWNER)
        assert setup.status_code == 201, setup.text
        async def pin_value():
            async with app.state.pool.acquire() as con:
                return await con.fetchrow("select value from bothub.settings where key='legacy_mac_id'")
        pin = client.portal.call(pin_value)
        assert pin is not None and pin['value'] is None
        mac = client.post('/api/macs',json={'name':'Later'},headers=OWNER).json()
        monkeypatch.setenv('MAC_AGENT_TOKEN','env-only-token')
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer env-only-token'}) as ws:
                ws.receive_json()
        assert denied.value.code == 4401
        monkeypatch.setenv('MAC_AGENT_TOKEN',mac['token'])
        with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer '+mac['token']}) as ws:
            ws.send_json({'type':'hello','host':'later'})
        hashed_api = client.post('/api/files',data={'thread_id':str(uuid.uuid4())},
            files={'file':('probe.txt',b'x')},headers={'Authorization':'Bearer '+mac['token']})
        assert hashed_api.status_code == 404, hashed_api.text


@pytest.mark.asyncio
async def test_migration_pins_env_only_setup_admin_without_old_mac_rows():
    schema = 'multi_mac_migration_' + uuid.uuid4().hex[:8]
    owner_id = uuid.uuid4()
    con = await asyncpg.connect(os.environ['DATABASE_URL'])
    try:
        await con.execute(f'create schema {schema}')
        await con.execute(f'set search_path to {schema}')
        await con.execute('create table users(id uuid primary key, role text, status text)')
        await con.execute('create table settings(key text primary key, value jsonb not null)')
        await con.execute('create table mac_tokens(user_id uuid, token_hash text, revoked_at timestamptz, created_at timestamptz)')
        await con.execute('create table mac_status(id integer primary key, owner_id uuid, info jsonb, last_seen timestamptz)')
        await con.execute('create table bots(id text primary key, owner_id uuid)')
        await con.execute("insert into users values($1,'admin','active')",owner_id)
        await con.execute("insert into settings values('setup_user_id',to_jsonb($1::text))",str(owner_id))
        await con.execute("insert into bots values('old-bot',$1)",owner_id)
        migration = Path(__file__).resolve().parents[1].joinpath('bothub/migrations/036_multi_mac.sql').read_text()
        await con.execute(migration.replace('set search_path = bothub;',f'set search_path = {schema};',1))
        row = await con.fetchrow("select m.id,m.token_hash,b.mac_id from macs m join bots b on b.owner_id=m.owner_id where m.owner_id=$1",owner_id)
        pin = await con.fetchval("select value #>> '{}' from settings where key='legacy_mac_id'")
        assert row and row['token_hash'] is None and row['mac_id'] == row['id'] and pin == str(row['id'])
    finally:
        await con.execute('set search_path to public')
        await con.execute(f'drop schema if exists {schema} cascade')
        await con.close()


@pytest.mark.asyncio
async def test_patch_mac_id_rejects_active_turn_and_allows_idle_turn():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            a = (await client.post('/api/macs',json={'name':'A'},headers=OWNER)).json()
            b = (await client.post('/api/macs',json={'name':'B'},headers=OWNER)).json()
            bot = await client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake','mac_id':a['id']},headers=OWNER)
            assert bot.status_code == 200, bot.text
            thread = (await client.post('/api/threads',json={'bot_id':'alpha'},headers=OWNER)).json()
            turn = (await client.post('/api/threads/'+thread['id']+'/turns',json={'prompt':'work'},headers=OWNER)).json()
            async with app.state.pool.acquire() as con:
                await con.execute("update bothub.turns set status='waiting_approval' where id=$1",uuid.UUID(turn['id']))
            for mac_id in (b['id'], None):
                denied = await client.patch('/api/bots/alpha',json={'mac_id':mac_id},headers=OWNER)
                assert denied.status_code == 409, denied.text
            async with app.state.pool.acquire() as con:
                await con.execute("update bothub.turns set status='done' where id=$1",uuid.UUID(turn['id']))
            allowed = await client.patch('/api/bots/alpha',json={'mac_id':b['id']},headers=OWNER)
            assert allowed.status_code == 200 and allowed.json()['mac_id'] == b['id'], allowed.text


@pytest.mark.asyncio
async def test_parallel_patch_rechecks_mac_id_after_first_patch_and_active_turn(monkeypatch):
    app = create_app()
    async def idle():
        await asyncio.Event().wait()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    first_updated = asyncio.Event()
    second_reached_lock = asyncio.Event()
    release_first = asyncio.Event()
    patch_role = contextvars.ContextVar('patch_role', default=None)
    original_fetchrow = asyncpg.Connection.fetchrow
    original_execute = asyncpg.Connection.execute

    async def gated_fetchrow(self, query, *args, **kwargs):
        row = await original_fetchrow(self, query, *args, **kwargs)
        if patch_role.get() == 'first' and query.startswith('update bothub.bots set'):
            first_updated.set()
            await release_first.wait()
        elif patch_role.get() == 'second' and query.startswith('select * from bothub.bots') and 'for update' not in query:
            # The pre-fix handler read A outside its transaction; keep that stale read.
            assert str(row['mac_id']) == a['id']
            second_reached_lock.set()
        return row

    async def gated_execute(self, query, *args, **kwargs):
        if patch_role.get() == 'second' and "pg_advisory_xact_lock(hashtext('bothub-claim-turn'))" in query:
            # The fixed handler reaches this lock before reading the bot row.
            second_reached_lock.set()
        return await original_execute(self, query, *args, **kwargs)

    async def patch_as(role, client, mac_id):
        token = patch_role.set(role)
        try:
            return await client.patch('/api/bots/alpha',json={'mac_id':mac_id},headers=OWNER)
        finally:
            patch_role.reset(token)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            a = (await client.post('/api/macs',json={'name':'A'},headers=OWNER)).json()
            b = (await client.post('/api/macs',json={'name':'B'},headers=OWNER)).json()
            bot = await client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake','mac_id':a['id']},headers=OWNER)
            assert bot.status_code == 200, bot.text
            thread = (await client.post('/api/threads',json={'bot_id':'alpha'},headers=OWNER)).json()
            turn = (await client.post('/api/threads/'+thread['id']+'/turns',json={'prompt':'work'},headers=OWNER)).json()
            monkeypatch.setattr(asyncpg.Connection, 'fetchrow', gated_fetchrow)
            monkeypatch.setattr(asyncpg.Connection, 'execute', gated_execute)
            first = asyncio.create_task(patch_as('first', client, b['id']))
            second = None
            try:
                await asyncio.wait_for(first_updated.wait(), 10)
                second = asyncio.create_task(patch_as('second', client, a['id']))
                await asyncio.wait_for(second_reached_lock.wait(), 10)
                async with app.state.pool.acquire() as con:
                    await con.execute("update bothub.turns set status='waiting_approval' where id=$1",uuid.UUID(turn['id']))
            finally:
                release_first.set()
            first_response = await asyncio.wait_for(first, 10)
            second_response = await asyncio.wait_for(second, 10)
            assert first_response.status_code == 200, first_response.text
            assert second_response.status_code == 409, second_response.text
            bots = (await client.get('/api/bots',headers=OWNER)).json()
            assert bots[0]['mac_id'] == b['id']


def test_delete_waits_for_inflight_send_and_prevents_later_mac_calls(monkeypatch):
    entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
    original_send = WebSocket.send_json

    async def gated_send(self, data, *args, **kwargs):
        if data.get('type') == 'call':
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        return await original_send(self, data, *args, **kwargs)

    monkeypatch.setattr(WebSocket, 'send_json', gated_send)
    app = create_app()
    async def idle(): await asyncio.Event().wait()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    with TestClient(app, base_url='https://testserver') as client:
        mac = client.post('/api/macs',json={'name':'A'},headers=OWNER).json()
        bot = client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake','mac_id':mac['id']},headers=OWNER)
        assert bot.status_code == 200, bot.text
        thread = client.post('/api/threads',json={'bot_id':'alpha'},headers=OWNER).json()
        turn = client.post('/api/threads/'+thread['id']+'/turns',json={'prompt':'work'},headers=OWNER).json()

        async def activate():
            con = await asyncpg.connect(os.environ['DATABASE_URL'])
            try:
                await con.execute("update bothub.turns set status='running',lease_until=now()+interval '1 minute' where id=$1",uuid.UUID(turn['id']))
            finally:
                await con.close()
        asyncio.run(activate())
        args = {'engine':'gemini','prompt':'work'}
        approval = client.post('/api/approvals',json={'thread_id':thread['id'],'turn_id':turn['id'],
            'risk':'exec','title':'delegate','tool':'mcp__bothub__mac_delegate','args':args},headers=bot_token('alpha')).json()
        assert client.post('/api/approvals/'+approval['id']+'/decide',json={'decision':'approve','remember':False,'client':'iphone'},headers=OWNER).status_code == 200
        with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer '+mac['token']}) as ws:
            ws.send_json({'type':'hello','host':'A'})
            for _ in range(50):
                if client.get('/api/macs',headers=OWNER).json()[0]['state'] == 'online': break
                time.sleep(.02)
            else: pytest.fail('Mac did not become online')
            with ThreadPoolExecutor(max_workers=2) as pool:
                call = pool.submit(client.post,'/api/mac/call',json={'thread_id':thread['id'],'turn_id':turn['id'],
                    'tool':'delegate','args':args,'timeout':2},headers=bot_token('alpha'))
                assert entered.wait(3)
                def delete():
                    deleting.set()
                    return client.delete('/api/macs/'+mac['id'],headers=OWNER)
                deletion = pool.submit(delete)
                try:
                    assert deleting.wait(3)
                    time.sleep(.1)
                    assert not deletion.done()
                finally:
                    release.set()
                sent = ws.receive_json()
                assert sent['type'] == 'call'
                assert deletion.result(timeout=3).status_code == 200
                assert call.result(timeout=3).status_code == 409
        assert client.get('/api/macs',headers=OWNER).json() == []


def test_delete_hides_socket_before_stale_call_can_send(monkeypatch):
    closing, release = threading.Event(), threading.Event()
    sent = []
    original_close, original_send = WebSocket.close, WebSocket.send_json

    async def gated_close(self, *args, **kwargs):
        if kwargs.get('code') == 4401:
            closing.set()
            assert await asyncio.to_thread(release.wait, 5)
        return await original_close(self, *args, **kwargs)

    async def spy_send(self, data, *args, **kwargs):
        if data.get('type') == 'call': sent.append(data)
        return await original_send(self, data, *args, **kwargs)

    monkeypatch.setattr(WebSocket, 'close', gated_close)
    monkeypatch.setattr(WebSocket, 'send_json', spy_send)
    app = create_app()
    async def idle(): await asyncio.Event().wait()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    with TestClient(app, base_url='https://testserver') as client:
        mac = client.post('/api/macs',json={'name':'A'},headers=OWNER).json()
        bot = client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake','mac_id':mac['id']},headers=OWNER)
        assert bot.status_code == 200, bot.text
        thread = client.post('/api/threads',json={'bot_id':'alpha'},headers=OWNER).json()
        turn = client.post('/api/threads/'+thread['id']+'/turns',json={'prompt':'work'},headers=OWNER).json()

        async def activate():
            con = await asyncpg.connect(os.environ['DATABASE_URL'])
            try:
                await con.execute("update bothub.turns set status='running',lease_until=now()+interval '1 minute' where id=$1",uuid.UUID(turn['id']))
            finally:
                await con.close()
        asyncio.run(activate())
        args = {'engine':'gemini','prompt':'work'}
        approval = client.post('/api/approvals',json={'thread_id':thread['id'],'turn_id':turn['id'],
            'risk':'exec','title':'delegate','tool':'mcp__bothub__mac_delegate','args':args},headers=bot_token('alpha')).json()
        assert client.post('/api/approvals/'+approval['id']+'/decide',json={'decision':'approve','remember':False,'client':'iphone'},headers=OWNER).status_code == 200
        with client.websocket_connect('/agent/mac',headers={'Authorization':'Bearer '+mac['token']}) as ws:
            ws.send_json({'type':'hello','host':'A'})
            for _ in range(50):
                if client.get('/api/macs',headers=OWNER).json()[0]['state'] == 'online': break
                time.sleep(.02)
            else: pytest.fail('Mac did not become online')
            with ThreadPoolExecutor(max_workers=1) as pool:
                deletion = pool.submit(client.delete,'/api/macs/'+mac['id'],headers=OWNER)
                try:
                    assert closing.wait(3)
                    call = client.post('/api/mac/call',json={'thread_id':thread['id'],'turn_id':turn['id'],
                        'tool':'delegate','args':args,'timeout':2},headers=bot_token('alpha'))
                    assert call.status_code == 409 and call.json()['error'] == 'mac_unavailable', call.text
                    assert sent == []
                finally:
                    release.set()
                assert deletion.result(timeout=3).status_code == 200
