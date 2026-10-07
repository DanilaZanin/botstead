"""Экспорт ленты активности в CSV: доступ и изоляция данных. Нужен Postgres."""
from tests.test_activity_db import OWNER, add_member, bot_headers, client_for, make_bot, owner_id, seed


async def test_export_csv_shows_only_own_bots():
    async with client_for() as (client, app):
        owner = await owner_id(app)
        member, member_headers = await add_member(client, app)
        await make_bot(client)
        await make_bot(client, 'beta', member_headers)
        await seed(app, owner, 'alpha')
        await seed(app, member, 'beta')

        response = await client.get('/api/activity/export.csv?days=7', headers=OWNER)
        assert response.status_code == 200
        assert response.headers['content-type'] == 'text/csv; charset=utf-8'
        assert 'attachment; filename="activity.csv"' in response.headers['content-disposition']
        assert response.text.startswith('﻿')
        assert 'time,bot,kind,code,title,detail' in response.text
        assert 'Alpha' in response.text and 'turn_started' in response.text
        assert 'Beta' not in response.text and 'beta' not in response.text


async def test_export_csv_requires_owner():
    async with client_for() as (client, app):
        assert (await client.get('/api/activity/export.csv?days=7')).status_code == 401
        await make_bot(client)
        assert (await client.get('/api/activity/export.csv?days=7', headers=bot_headers('alpha'))).status_code == 403


async def test_export_csv_rejects_invalid_days():
    async with client_for() as (client, app):
        response = await client.get('/api/activity/export.csv?days=8', headers=OWNER)
        assert response.status_code == 422
        assert response.json() == {'error': 'invalid', 'detail': 'days'}
