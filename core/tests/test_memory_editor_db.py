"""DB tests for the memory editor (GET with q and sorting, PATCH with bot_id/expires_at, DELETE).
Postgres is required."""
import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub import auth
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}
PASSWORD = 'long-password'


@pytest.fixture(autouse=True)
def local_path(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')


@asynccontextmanager
async def client_for(**options):
    app = create_app(**options)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app


async def add_member(client, app, email='member@example.com'):
    async with app.state.pool.acquire() as con:
        user = await con.fetchval('insert into bothub.users(email,password_hash) values($1,$2) returning id',
                                  email, auth.hash_password(PASSWORD))
    login = await client.post('/api/auth/login', json={'email': email, 'password': PASSWORD})
    assert login.status_code == 200, login.text
    headers = {'Cookie': f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF': login.json()['csrf_token'],
               'Origin': 'https://testserver'}
    return user, headers


def bot_headers(bot_id):
    digest = hmac.new(b'test-secret', bot_id.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'Bearer bot:{bot_id}:{digest}'}


async def make_bot(client, bot_id='alpha', headers=OWNER):
    response = await client.post('/api/bots', json={'id': bot_id, 'name': bot_id.title(), 'provider': 'fake', 'model': 'fake'},
                                 headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_delete_own_memory():
    async with client_for() as (client, app):
        created = (await client.post('/api/memory', json={'text': 'To be deleted'}, headers=OWNER)).json()
        memory_id = created['id']

        # Delete own entry
        delete_res = await client.delete(f"/api/memory/{memory_id}", headers=OWNER)
        assert delete_res.status_code == 200, delete_res.text
        assert delete_res.json() == {'ok': True}

        # Check it is no longer returned in GET
        memories = (await client.get('/api/memory', headers=OWNER)).json()
        assert not any(m['id'] == memory_id for m in memories)

        # Deleting again returns 404
        second_delete = await client.delete(f"/api/memory/{memory_id}", headers=OWNER)
        assert second_delete.status_code == 404, second_delete.text
        assert second_delete.json()['error'] == 'not_found'


async def test_delete_foreign_memory_returns_404():
    async with client_for() as (client, app):
        owner_entry = (await client.post('/api/memory', json={'text': 'Owner private memory'}, headers=OWNER)).json()
        _, member_headers = await add_member(client, app)

        # Foreign user tries to delete owner's memory
        delete_res = await client.delete(f"/api/memory/{owner_entry['id']}", headers=member_headers)
        assert delete_res.status_code == 404, delete_res.text
        assert delete_res.json()['error'] == 'not_found'

        # Memory is still intact for owner
        memories = (await client.get('/api/memory', headers=OWNER)).json()
        assert any(m['id'] == owner_entry['id'] for m in memories)


async def test_bot_token_cannot_delete_memory():
    async with client_for() as (client, app):
        await make_bot(client, 'alpha')
        entry = (await client.post('/api/memory', json={'text': 'Bot cannot delete'}, headers=OWNER)).json()

        # Bot tries to delete
        delete_res = await client.delete(f"/api/memory/{entry['id']}", headers=bot_headers('alpha'))
        assert delete_res.status_code == 403, delete_res.text
        assert delete_res.json()['error'] == 'forbidden'

        # Entry remains
        memories = (await client.get('/api/memory', headers=OWNER)).json()
        assert any(m['id'] == entry['id'] for m in memories)


async def test_move_entry_between_shared_and_bot():
    async with client_for() as (client, app):
        await make_bot(client, 'alpha')
        await make_bot(client, 'beta')
        _, member_headers = await add_member(client, app)
        await make_bot(client, 'member-bot', headers=member_headers)

        # Start as shared entry (bot_id is None)
        entry = (await client.post('/api/memory', json={'text': 'Shared knowledge'}, headers=OWNER)).json()
        assert entry['bot_id'] is None

        # Move to alpha
        patched = (await client.patch(f"/api/memory/{entry['id']}", json={'bot_id': 'alpha'}, headers=OWNER)).json()
        assert patched['bot_id'] == 'alpha'

        # Move back to shared (bot_id: null)
        patched_shared = (await client.patch(f"/api/memory/{entry['id']}", json={'bot_id': None}, headers=OWNER)).json()
        assert patched_shared['bot_id'] is None

        # Move to beta
        patched_beta = (await client.patch(f"/api/memory/{entry['id']}", json={'bot_id': 'beta'}, headers=OWNER)).json()
        assert patched_beta['bot_id'] == 'beta'

        # Move to non-existent bot returns 404
        bad_bot = await client.patch(f"/api/memory/{entry['id']}", json={'bot_id': 'nonexistent'}, headers=OWNER)
        assert bad_bot.status_code == 404, bad_bot.text
        assert bad_bot.json()['error'] == 'not_found'

        # Move to foreign bot returns 404
        foreign_bot = await client.patch(f"/api/memory/{entry['id']}", json={'bot_id': 'member-bot'}, headers=OWNER)
        assert foreign_bot.status_code == 404, foreign_bot.text
        assert foreign_bot.json()['error'] == 'not_found'


async def test_search_with_percent_and_underscore_and_case_insensitive():
    async with client_for() as (client, app):
        e1 = (await client.post('/api/memory', json={'text': 'Discount 100% on checkout'}, headers=OWNER)).json()
        e2 = (await client.post('/api/memory', json={'text': 'Discount 1000 on checkout'}, headers=OWNER)).json()
        e3 = (await client.post('/api/memory', json={'text': 'file_name_test.py'}, headers=OWNER)).json()
        e4 = (await client.post('/api/memory', json={'text': 'file-name-test.py'}, headers=OWNER)).json()
        e5 = (await client.post('/api/memory', json={'text': 'Server CONFIG: PROD_SERVER'}, headers=OWNER)).json()

        # Search with % wildcard escaped
        res_percent = (await client.get('/api/memory', params={'q': '100%'}, headers=OWNER)).json()
        assert [m['id'] for m in res_percent] == [e1['id']]

        # Search with _ wildcard escaped
        res_underscore = (await client.get('/api/memory', params={'q': '_name_'}, headers=OWNER)).json()
        assert [m['id'] for m in res_underscore] == [e3['id']]

        # Case-insensitive substring search with underscore
        res_case = (await client.get('/api/memory', params={'q': 'config: prod_server'}, headers=OWNER)).json()
        assert [m['id'] for m in res_case] == [e5['id']]

        # General search
        res_general = (await client.get('/api/memory', params={'q': 'discount'}, headers=OWNER)).json()
        ids = {m['id'] for m in res_general}
        assert e1['id'] in ids and e2['id'] in ids and e3['id'] not in ids

        # No match search
        res_empty = (await client.get('/api/memory', params={'q': 'absent_token_xyz'}, headers=OWNER)).json()
        assert res_empty == []


async def test_memory_expiry_and_patch_expiry():
    async with client_for() as (client, app):
        await make_bot(client, 'alpha')
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        m_future = (await client.post('/api/memory', json={'text': 'Future fact', 'bot_id': 'alpha', 'expires_at': future}, headers=OWNER)).json()
        m_past = (await client.post('/api/memory', json={'text': 'Past fact', 'bot_id': 'alpha', 'expires_at': past}, headers=OWNER)).json()

        # Owner GET sees both
        owner_seen = (await client.get('/api/memory', headers=OWNER)).json()
        owner_ids = {m['id'] for m in owner_seen}
        assert m_future['id'] in owner_ids and m_past['id'] in owner_ids

        # Bot GET sees only unexpired
        bot_seen = (await client.get('/api/memory', headers=bot_headers('alpha'))).json()
        bot_ids = {m['id'] for m in bot_seen}
        assert m_future['id'] in bot_ids
        assert m_past['id'] not in bot_ids

        # Patch expires_at to new date
        new_future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        patched = (await client.patch(f"/api/memory/{m_future['id']}", json={'expires_at': new_future}, headers=OWNER)).json()
        assert patched['expires_at'] is not None

        # Patch expires_at to None (clear expiry)
        cleared = (await client.patch(f"/api/memory/{m_future['id']}", json={'expires_at': None}, headers=OWNER)).json()
        assert cleared['expires_at'] is None

        # Patch with invalid date returns 400
        invalid = await client.patch(f"/api/memory/{m_future['id']}", json={'expires_at': 'not-a-date'}, headers=OWNER)
        assert invalid.status_code == 400


async def test_patch_text_and_version_bump():
    async with client_for() as (client, app):
        entry = (await client.post('/api/memory', json={'text': 'Original'}, headers=OWNER)).json()
        assert entry['version'] == 1

        # Changing text increments version
        p1 = (await client.patch(f"/api/memory/{entry['id']}", json={'text': 'Modified text'}, headers=OWNER)).json()
        assert p1['version'] == 2 and p1['text'] == 'Modified text'

        # Updating status without changing text keeps version unchanged
        p2 = (await client.patch(f"/api/memory/{entry['id']}", json={'status': 'archived'}, headers=OWNER)).json()
        assert p2['version'] == 2 and p2['status'] == 'archived'

        # Updating with the same text keeps version unchanged
        p3 = (await client.patch(f"/api/memory/{entry['id']}", json={'text': 'Modified text'}, headers=OWNER)).json()
        assert p3['version'] == 2

        # Text over 16 KiB rejected
        too_long = await client.patch(f"/api/memory/{entry['id']}", json={'text': 'x' * (16 * 1024 + 1)}, headers=OWNER)
        assert too_long.status_code == 400


async def test_patch_rejects_empty_and_nul_text_and_keeps_entry():
    async with client_for() as (client, app):
        entry = (await client.post('/api/memory', json={'text': 'Keep me'}, headers=OWNER)).json()
        for bad in ('', '   \n', 'a\x00b', 5):
            response = await client.patch(f"/api/memory/{entry['id']}", json={'text': bad}, headers=OWNER)
            # NUL отсекает общая проверка текстовых полей тела (422), остальное сам маршрут (400).
            assert response.status_code in (400, 422), (bad, response.text)
        current = [m for m in (await client.get('/api/memory', headers=OWNER)).json() if m['id'] == entry['id']][0]
        assert current['text'] == 'Keep me' and current['version'] == 1


async def test_patch_naive_expires_at_is_utc():
    async with client_for() as (client, app):
        entry = (await client.post('/api/memory', json={'text': 'Dated'}, headers=OWNER)).json()
        patched = await client.patch(f"/api/memory/{entry['id']}", json={'expires_at': '2030-01-01T12:00:00'}, headers=OWNER)
        assert patched.status_code == 200, patched.text
        assert datetime.fromisoformat(patched.json()['expires_at'].replace('Z', '+00:00')) == datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


async def test_newest_first_sorting():
    async with client_for() as (client, app):
        e1 = (await client.post('/api/memory', json={'text': 'First fact'}, headers=OWNER)).json()
        e2 = (await client.post('/api/memory', json={'text': 'Second fact'}, headers=OWNER)).json()
        e3 = (await client.post('/api/memory', json={'text': 'Third fact'}, headers=OWNER)).json()

        memories = (await client.get('/api/memory', headers=OWNER)).json()
        ids = [m['id'] for m in memories]
        # Newest first: e3, e2, e1
        assert ids.index(e3['id']) < ids.index(e2['id']) < ids.index(e1['id'])
