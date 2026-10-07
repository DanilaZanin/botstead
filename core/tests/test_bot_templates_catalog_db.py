"""GET /api/templates/catalog и GET /api/templates/catalog/{id}: список карточек готовых шаблонов из templates/
и полный документ по id. 401 без владельца, 404 на несуществующий/битый id. Чистая логика разбора:
test_bot_templates_catalog_pure.py."""
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub import auth
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}


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
    return {'Cookie': f"bothub_session={login.cookies['bothub_session']}",
            'X-CSRF': login.json()['csrf_token'],
            'Origin': 'https://testserver'}


async def test_catalog_lists_all_six_templates():
    # Маршрут доступен только владельцу; ответ — карточки {id, name, role, description, avatar}.
    async with client_for() as (client, app):
        response = await client.get('/api/templates/catalog', headers=OWNER)
        assert response.status_code == 200, response.text
        cards = response.json()
        assert {c['id'] for c in cards} >= {'researcher', 'mail_triage', 'pr_reviewer', 'daily_digest', 'house_helper', 'translator'}
        for card in cards:
            assert set(card) == {'id', 'name', 'role', 'description', 'avatar'}
            assert card['name'].strip()
            assert card['avatar'].strip()


async def test_catalog_full_doc_passes_validator():
    # Полный документ по id должен приниматься POST /api/bots/import (тот же parse_bot_template).
    async with client_for() as (client, app):
        response = await client.get('/api/templates/catalog/researcher', headers=OWNER)
        assert response.status_code == 200, response.text
        doc = response.json()
        assert doc['format'] == 'botstead-bot' and doc['version'] == 1
        assert doc['name'] == 'Scout' and doc['executor'] == 'container'
        # Тот же валидатор, что у импорта: POST примет документ.
        bot_response = await client.post('/api/bots/import', json={**doc, 'provider_id': None, 'model_id': None},
                                         headers=OWNER)
        assert bot_response.status_code == 201, bot_response.text
        assert bot_response.json()['name'] == 'Scout'


async def test_catalog_full_doc_404_for_unknown_id():
    async with client_for() as (client, app):
        # id не проходит по BOT_TEMPLATE_CATALOG_ID — 404 (а не 400), чужой id не отдаёт содержимого файла.
        response = await client.get('/api/templates/catalog/../etc/passwd', headers=OWNER)
        assert response.status_code == 404
        # Несуществующий, но валидный по формату id — 404 (файла нет).
        response = await client.get('/api/templates/catalog/does_not_exist', headers=OWNER)
        assert response.status_code == 404


async def test_catalog_accessible_to_logged_in_users():
    # Каталог отдаётся любому залогиненному пользователю (owner=True на маршруте == «любой user»);
    # каталог не зависит от owner_id: шаблоны одни и те же для всех. Без сессии/токена — 401.
    async with client_for() as (client, app):
        member = await add_member(client, app)
        response = await client.get('/api/templates/catalog', headers=member)
        assert response.status_code == 200, response.text
        response = await client.get('/api/templates/catalog/researcher', headers=member)
        assert response.status_code == 200, response.text


async def test_catalog_401_without_auth():
    async with client_for() as (client, app):
        response = await client.get('/api/templates/catalog')
        assert response.status_code == 401
        response = await client.get('/api/templates/catalog/researcher')
        assert response.status_code == 401
