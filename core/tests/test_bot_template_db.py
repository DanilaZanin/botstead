"""Шаблон бота (раздел 9, экспорт/импорт файлом): маршруты GET /api/bots/{id}/export и POST /api/bots/import, круговой
обход с расписанием и процедурой. Нужен Postgres. Чистая логика разбора документа: test_bot_template_pure.py."""
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub import auth
from bothub.launcher_client import FakeLauncherClient
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}
SENTINEL = 'client-value-4f9a1c'
CLICK = {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}, 'risk': 'none'}


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
        await con.fetchval('insert into bothub.users(email,password_hash) values($1,$2) returning id',
                           email, auth.hash_password('long-password'))
    login = await client.post('/api/auth/login', json={'email': email, 'password': 'long-password'})
    assert login.status_code == 200, login.text
    headers = {'Cookie': f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF': login.json()['csrf_token'],
               'Origin': 'https://testserver'}
    return headers


async def owner_id(app):
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select id from bothub.users where email='fixture@example.com'")


async def make_bot(client, **fields):
    base = {'name': 'Скаут', 'provider': 'fake', 'model': 'fake', 'executor': 'container'}
    base.update(fields)
    response = await client.post('/api/bots', json=base, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


def full_doc(**fields):
    base = {
        'format': 'botstead-bot', 'version': 1,
        'name': 'Скаут', 'role': 'Ищет вакансии', 'instructions': 'Смотри hh ежедневно',
        'avatar': 'scout', 'executor': 'container',
        'auto_allow': [{'tool': 'mac_find_files'}],
        'mcp_allow': ['mcp__github__mac_find_files'],
        'budget_daily_tokens': 200000, 'auto_compact_percent': 80,
        'schedules': [{'cron': '0 9 * * 1-5', 'timezone': 'Europe/Moscow', 'prompt': 'проверь вакансии', 'enabled': True, 'name': 'утро'}],
        'procedures': [{'format': 'bothub-procedure/1', 'name': 'Вход', 'description': 'вход в hh',
                        'params': [], 'steps': [CLICK]}],
    }
    base.update(fields)
    return base


async def test_export_omits_ids_owner_provider_and_procedures_use_export_document():
    async with client_for() as (client, app):
        bot = await make_bot(client, name='Скаут')
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            await con.execute("insert into bothub.procedures(owner_id,bot_id,name,description,params,steps,source,status) "
                              "values($1,$2,'Вход','вход в hh','[]'::jsonb,$3::jsonb,'import','active')",
                              owner, bot['id'], json.dumps([CLICK]))
            await con.execute("insert into bothub.schedules(bot_id,name,kind,cron,timezone,prompt,enabled,owner_id) "
                              "values($1,'утро','cron','0 9 * * 1-5','Europe/Moscow','проверь вакансии',true,$2)",
                              bot['id'], owner)
        response = await client.get(f"/api/bots/{bot['id']}/export", headers=OWNER)
        assert response.status_code == 200, response.text
        doc = response.json()
        assert doc['format'] == 'botstead-bot' and doc['version'] == 1
        assert set(doc) == {'format', 'version', 'name', 'role', 'instructions', 'avatar', 'executor',
                            'auto_allow', 'mcp_allow', 'budget_daily_tokens', 'auto_compact_percent',
                            'schedules', 'procedures'}
        for leaked in (bot['id'], 'fixture@example.com', 'provider_id', 'model_id', 'fake', SENTINEL):
            assert leaked not in response.text
        assert doc['name'] == 'Скаут' and doc['procedures'][0]['name'] == 'Вход'
        assert doc['procedures'][0].get('computed_risk') is None
        assert doc['schedules'][0]['cron'] == '0 9 * * 1-5'


async def test_export_404_for_another_owners_bot():
    async with client_for() as (client, app):
        bot = await make_bot(client, name='A')
        member = await add_member(client, app)
        response = await client.get(f"/api/bots/{bot['id']}/export", headers=member)
        assert response.status_code == 404, response.text


async def test_import_creates_bot_with_procedures_and_schedules_and_avoids_leaks():
    async with client_for() as (client, app):
        doc = full_doc()
        response = await client.post('/api/bots/import', json=doc, headers=OWNER)
        assert response.status_code == 201, response.text
        bot = response.json()
        assert bot['name'] == 'Скаут' and bot['container'] == 'skipped' and bot['recreate_url'] is None
        async with app.state.pool.acquire() as con:
            schedules = await con.fetch('select cron, timezone, prompt, enabled, name from bothub.schedules where bot_id=$1', bot['id'])
            procedures = await con.fetch('select name, description, source, status from bothub.procedures where bot_id=$1', bot['id'])
        assert len(schedules) == 1 and schedules[0]['cron'] == '0 9 * * 1-5' and schedules[0]['name'] == 'утро'
        assert len(procedures) == 1 and procedures[0]['name'] == 'Вход' and procedures[0]['source'] == 'import'
        assert SENTINEL not in response.text
        export = await client.get(f"/api/bots/{bot['id']}/export", headers=OWNER)
        assert export.status_code == 200
        edoc = export.json()
        for key in ('name', 'role', 'instructions', 'avatar', 'executor', 'auto_allow', 'mcp_allow',
                    'budget_daily_tokens', 'auto_compact_percent'):
            assert edoc[key] == doc[key], key
        assert edoc['schedules'][0]['cron'] == doc['schedules'][0]['cron']
        assert edoc['procedures'][0]['name'] == doc['procedures'][0]['name']


async def test_import_appends_suffix_on_name_collision():
    async with client_for() as (client, _):
        await make_bot(client, name='Скаут')
        response = await client.post('/api/bots/import', json=full_doc(), headers=OWNER)
        assert response.status_code == 201, response.text
        assert response.json()['name'] == 'Скаут (2)'


async def test_import_unknown_field_returns_422_without_value():
    async with client_for() as (client, _):
        doc = full_doc()
        doc['secret'] = SENTINEL
        response = await client.post('/api/bots/import', json=doc, headers=OWNER)
        assert response.status_code == 422
        assert response.json()['error'] == 'invalid' and SENTINEL not in response.text


async def test_import_invalid_body_size_returns_413():
    async with client_for() as (client, _):
        big = full_doc()
        big['instructions'] = 'a' * (300 * 1024)
        response = await client.post('/api/bots/import', json=big, headers=OWNER)
        assert response.status_code == 413, response.text
        assert '256 KiB' in response.json()['detail']


async def test_import_provider_model_requires_both_or_neither():
    async with client_for() as (client, _):
        import uuid as _u
        response = await client.post('/api/bots/import', json=full_doc(provider_id=str(_u.uuid4())), headers=OWNER)
        assert response.status_code == 400 and 'provider_id' in response.json()['detail']


async def test_import_same_owner_dedupes_procedure_names():
    # unique (owner_id, name) у процедур: файл, выгруженный у этого же владельца, не должен падать на своих названиях
    async with client_for() as (client, _):
        first = await client.post('/api/bots/import', json=full_doc(), headers=OWNER)
        assert first.status_code == 201, first.text
        export = (await client.get(f"/api/bots/{first.json()['id']}/export", headers=OWNER)).json()
        second = await client.post('/api/bots/import', json=export, headers=OWNER)
        assert second.status_code == 201, second.text
        assert second.json()['name'] == 'Скаут (2)'
        again = (await client.get(f"/api/bots/{second.json()['id']}/export", headers=OWNER)).json()
        assert again['procedures'][0]['name'] == 'Вход (2)'


async def test_import_bad_provider_id_is_400_not_500():
    async with client_for() as (client, _):
        response = await client.post('/api/bots/import', json=full_doc(provider_id='not-a-uuid', model_id='not-a-uuid'), headers=OWNER)
        assert response.status_code == 400 and 'provider_id' in response.json()['detail']


async def test_import_nul_character_is_422_not_500():
    async with client_for() as (client, _):
        response = await client.post('/api/bots/import', json=full_doc(instructions='a\u0000b'), headers=OWNER)
        assert response.status_code == 422, response.text


async def test_import_failed_procedure_leaves_no_bot():
    async with client_for() as (client, _):
        doc = full_doc()
        doc['procedures'] = [{'name': 'P', 'steps': []}]
        response = await client.post('/api/bots/import', json=doc, headers=OWNER)
        assert response.status_code == 422
        listed = (await client.get('/api/bots', headers=OWNER)).json()
        assert not any(item['name'] == 'Скаут' for item in listed)


async def test_import_invalid_procedure_in_array_returns_422():
    async with client_for() as (client, _):
        doc = full_doc()
        doc['procedures'] = [{'name': 'P'}]
        response = await client.post('/api/bots/import', json=doc, headers=OWNER)
        assert response.status_code == 422 and 'procedures[0]' in response.json()['detail']


async def test_import_without_model_creates_a_bot_without_model():
    # без provider_id/model_id бот не должен получить тестовый провайдер fake: только статус «нет модели»
    async with client_for() as (client, _):
        response = await client.post('/api/bots/import', json=full_doc(), headers=OWNER)
        assert response.status_code == 201, response.text
        bot = response.json()
        assert bot['status'] == 'no_model' and bot['provider'] != 'fake' and not bot.get('provider_id')
