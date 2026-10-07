"""Тесты цен модели: проба провайдера сохраняет цены, сводка расхода отдаёт cost_usd."""
import asyncio
import json
import base64
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}
NOW = timezone.utc

MODELS_WITH_PRICE = {
    "data": [
        {"id": "openai/gpt-4o", "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
        {"id": "free-model", "pricing": {"prompt": "0", "completion": "0"}},
    ]
}


def pricing_upstream(request):
    return httpx.Response(200, json=MODELS_WITH_PRICE)


@pytest.fixture(autouse=True)
def local_path(monkeypatch):
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())


@asynccontextmanager
async def client_for(**options):
    app = create_app(**options)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app


async def owner_id(app):
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select id from bothub.users where email='fixture@example.com'")


async def make_provider(client, app, monkeypatch):
    """Создаёт openai_compatible провайдера, подменяет PROBE_TRANSPORT на наш fake."""
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(pricing_upstream))
    response = await client.post('/api/providers', json={
        'kind': 'openai_compatible', 'name': 'OpenRouter', 'base_url': 'https://openrouter.ai/api/v1',
        'secret': 'sk-or-v1-good-key-000000000000000'
    }, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


async def make_bot_with_provider(client, provider_id, model='openai/gpt-4o', bot_id='scout'):
    models = await client.get('/api/models', headers=OWNER)
    assert models.status_code == 200, models.text
    model_id = next(item['id'] for item in models.json()
                    if item['provider_id'] == provider_id and item['name'] == model)
    response = await client.post('/api/bots', json={
        'id': bot_id, 'name': bot_id.title(), 'provider': 'fake',
        'provider_id': provider_id, 'model': model, 'model_id': model_id,
        'budget_daily_tokens': 200000
    }, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


# --- Цены сохраняются при проверке провайдера ----------------------------------------------------

async def test_provider_check_stores_pricing(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        async with app.state.pool.acquire() as con:
            rows = await con.fetch('select name,price_in_per_mtok,price_out_per_mtok from bothub.models '
                                    'where provider_id=$1 order by name',
                                    uuid.UUID(provider['id']))
            assert len(rows) == 2
            assert rows[0]['name'] == 'free-model'
            assert float(rows[0]['price_in_per_mtok']) == 0.0
            assert float(rows[0]['price_out_per_mtok']) == 0.0
            assert rows[1]['name'] == 'openai/gpt-4o'
            assert float(rows[1]['price_in_per_mtok']) == 2.5
            assert float(rows[1]['price_out_per_mtok']) == 10.0


async def test_provider_sync_models_updates_pricing(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        # Change the upstream to have different pricing, then force check
        new_body = {"data": [{"id": "openai/gpt-4o", "pricing": {"prompt": "0.000005", "completion": "0.00002"}}]}
        monkeypatch.setattr('bothub.main.PROBE_TRANSPORT',
                            httpx.MockTransport(lambda r: httpx.Response(200, json=new_body)))
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.providers set last_check_at=now()-interval '6 seconds' where id=$1",
                              uuid.UUID(provider['id']))
        checked = await client.post(f'/api/providers/{provider["id"]}/check?force=1', headers=OWNER)
        assert checked.status_code == 200, checked.text
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select price_in_per_mtok,price_out_per_mtok from bothub.models '
                                      'where provider_id=$1 and name=$2',
                                      uuid.UUID(provider['id']), 'openai/gpt-4o')
            assert float(row['price_in_per_mtok']) == 5.0
            assert float(row['price_out_per_mtok']) == 20.0


# --- usage summary отдаёт cost_usd ----------------------------------------------------------------

async def add_usage(app, bot_id, *, thread_id=None, turn_id=None, provider='openai_compatible', model='openai/gpt-4o',
                    tokens_in=100, tokens_out=50, tokens_cache_read=0, tokens_cache_write=0, ts=None):
    async with app.state.pool.acquire() as con:
        if ts is None:
            return await con.fetchrow(
                'insert into bothub.usage(bot_id,thread_id,turn_id,provider,model,tokens_in,tokens_out,tokens_cache_read,tokens_cache_write) '
                'values($1,$2,$3,$4,$5,$6,$7,$8,$9) returning *',
                bot_id, thread_id, turn_id, provider, model, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write)
        return await con.fetchrow(
            'insert into bothub.usage(bot_id,thread_id,turn_id,provider,model,tokens_in,tokens_out,tokens_cache_read,tokens_cache_write,ts) '
            'values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) returning *',
            bot_id, thread_id, turn_id, provider, model, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write, ts)


async def make_turn(app, thread, status='done'):
    async with app.state.pool.acquire() as con:
        return await con.fetchval('insert into bothub.turns(thread_id,prompt,status) values($1,$2,$3) returning id',
                                  thread, 'task', status)


async def make_thread(client, bot_id='scout', headers=OWNER):
    response = await client.post('/api/threads', json={'bot_id': bot_id}, headers=headers)
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()['id'])


async def test_usage_summary_returns_cost_usd_per_model(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        # 1M input tokens at $2.50/MTok = $2.50, 100K output at $10/MTok = $1.00, total = $3.50
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        tokens_in=1000000, tokens_out=100000)

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['cost_usd_total'] == 3.5
        assert data['cost_partial'] is False
        assert len(data['models']) == 1
        assert data['models'][0]['cost_usd'] == 3.5
        assert data['bots'][0]['cost_usd'] == 3.5


async def test_usage_summary_cache_tokens_counted_as_input(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        # 1000 input + 200 cache_read + 100 cache_write = 1300 input tokens
        # 1300 * 2.5/1e6 = 0.00325, 500 * 10/1e6 = 0.005, total = 0.00825
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        tokens_in=1000, tokens_out=500, tokens_cache_read=200, tokens_cache_write=100)

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['cost_usd_total'] == 0.0083  # round(0.00825, 4) = 0.0083
        assert data['cost_partial'] is False
        assert data['models'][0]['cost_usd'] == 0.0083


async def test_usage_summary_cost_null_for_model_without_price(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        # Модель без цены: считаем что model not in models table
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        provider='openai_compatible', model='unknown-model',
                        tokens_in=1000, tokens_out=500)

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['models'][0]['cost_usd'] is None
        assert data['cost_partial'] is True


async def test_usage_summary_cost_partial_true_when_some_models_have_no_price(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        tokens_in=1000000, tokens_out=100000)
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        provider='openai_compatible', model='unknown-model',
                        tokens_in=1000, tokens_out=500)

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['cost_partial'] is True
        assert len(data['models']) == 2
        costs = {m['model']: m['cost_usd'] for m in data['models']}
        assert costs['openai/gpt-4o'] == 3.5
        assert costs['unknown-model'] is None
        assert data['bots'][0]['cost_usd'] is None


async def test_usage_summary_no_usage_returns_zero_cost(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['cost_usd_total'] == 0.0
        assert data['cost_partial'] is False
        assert data['bots'][0]['cost_usd'] == 0.0
        assert data['bots'][0]['cost_usd_today'] == 0.0
        assert data['bots'][0]['cost_usd_month'] == 0.0


async def test_usage_summary_bot_cost_is_sum_of_model_costs(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        # Две модели с ценами для одного бота
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        tokens_in=1000000, tokens_out=100000)  # $3.50
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        model='free-model', tokens_in=1000000, tokens_out=100000)  # $0.00

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['cost_usd_total'] == 3.5
        assert data['cost_partial'] is False
        assert data['bots'][0]['cost_usd'] == 3.5


async def test_usage_summary_cost_rounded_to_4_decimals(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        # 3 tokens input * 2.5/1e6 = 0.0000075 -> round(0.0000075, 4) = 0.0
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        tokens_in=3, tokens_out=0)

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['models'][0]['cost_usd'] == 0.0


async def test_usage_summary_two_bots_cost(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'], bot_id='alpha')
        await make_bot_with_provider(client, provider['id'], bot_id='beta')
        thread_a = await make_thread(client, 'alpha')
        thread_b = await make_thread(client, 'beta')
        turn_a = await make_turn(app, thread_a)
        turn_b = await make_turn(app, thread_b)
        await add_usage(app, 'alpha', thread_id=thread_a, turn_id=turn_a,
                        tokens_in=1000000, tokens_out=100000)  # $3.50
        await add_usage(app, 'beta', thread_id=thread_b, turn_id=turn_b,
                        tokens_in=2000000, tokens_out=200000)  # $7.00

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['cost_usd_total'] == 10.5
        assert data['cost_partial'] is False
        bot_costs = {b['bot_id']: b['cost_usd'] for b in data['bots']}
        assert bot_costs == {'alpha': 3.5, 'beta': 7.0}


async def test_usage_summary_keeps_historical_model_price_after_provider_change(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        models = (await client.get('/api/models', headers=OWNER)).json()
        old_model_id = uuid.UUID(next(m['id'] for m in models if m['provider_id'] == provider['id']
                                      and m['name'] == 'openai/gpt-4o'))
        old_usage = await add_usage(app, 'scout', tokens_in=1000000, tokens_out=100000)
        owner = await owner_id(app)
        async with app.state.pool.acquire() as con:
            await con.execute('update bothub.usage set provider_id=$1,model_id=$2 where id=$3',
                              uuid.UUID(provider['id']), old_model_id, old_usage['id'])
            new_provider_id = await con.fetchval(
                "insert into bothub.providers(owner_id,kind,name,base_url,status) "
                "values($1,'openai_compatible','New prices','https://example.com/v1','ok') returning id",
                owner)
            new_model_id = await con.fetchval(
                "insert into bothub.models(provider_id,name,price_in_per_mtok,price_out_per_mtok) "
                "values($1,'openai/gpt-4o',5,20) returning id", new_provider_id)
        changed = await client.patch('/api/bots/scout', json={
            'provider_id': str(new_provider_id), 'model_id': str(new_model_id)
        }, headers=OWNER)
        assert changed.status_code == 200, changed.text
        new_usage = await add_usage(app, 'scout', tokens_in=1000000, tokens_out=100000)
        async with app.state.pool.acquire() as con:
            await con.execute('update bothub.usage set provider_id=$1,model_id=$2 where id=$3',
                              new_provider_id, new_model_id, new_usage['id'])

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data['models']) == 1
        assert data['models'][0]['cost_usd'] == 10.5
        assert data['bots'][0]['cost_usd'] == 10.5
        assert data['cost_usd_total'] == 10.5
        assert data['cost_partial'] is False


async def test_usage_summary_bot_cost_today_and_month_windows(monkeypatch):
    """Панель «Квоты» у бота на API-провайдере: cost_usd_today и cost_usd_month не зависят от days."""
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            now, this_month, period_start, first_of_month = await con.fetchrow(
                "select now(),date_trunc('month',now()),date_trunc('day',now()-interval '89 days'),"
                "date_trunc('day',now())=date_trunc('month',now())")
        # Проверяем начало месяца, начало 90-дневного периода и записи за их пределами.
        await add_usage(app, 'scout', thread_id=thread, tokens_in=1000000, tokens_out=100000, ts=now)
        await add_usage(app, 'scout', thread_id=thread, tokens_in=200000, tokens_out=20000, ts=this_month)
        await add_usage(app, 'scout', thread_id=thread, tokens_in=2000000, tokens_out=200000, ts=now - timedelta(days=45))
        await add_usage(app, 'scout', thread_id=thread, tokens_in=400000, tokens_out=40000,
                        ts=this_month - timedelta(microseconds=1))
        await add_usage(app, 'scout', thread_id=thread, tokens_in=600000, tokens_out=60000, ts=period_start)
        await add_usage(app, 'scout', thread_id=thread, tokens_in=800000, tokens_out=80000,
                        ts=period_start - timedelta(microseconds=1))

        response = await client.get('/api/usage/summary', params={'days': 1}, headers=OWNER)
        assert response.status_code == 200, response.text
        bot = response.json()['bots'][0]
        assert bot['cost_usd_today'] == (4.2 if first_of_month else 3.5)
        assert bot['cost_usd_month'] == 4.2
        # cost_usd за период days=1: только сегодняшний расход
        assert bot['cost_usd'] == (4.2 if first_of_month else 3.5)
        assert bot['tokens_month'] == 1320000

        response = await client.get('/api/usage/summary', params={'days': 90}, headers=OWNER)
        assert response.status_code == 200, response.text
        bot = response.json()['bots'][0]
        assert bot['cost_usd'] == 14.7
        assert bot['cost_usd_today'] == (4.2 if first_of_month else 3.5)
        assert bot['cost_usd_month'] == 4.2


async def test_usage_summary_bot_cost_missing_price_outside_window(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            previous_month_end = await con.fetchval("select date_trunc('month',now())-interval '1 microsecond'")
        await add_usage(app, 'scout', thread_id=thread, tokens_in=1000000, tokens_out=100000)
        await add_usage(app, 'scout', thread_id=thread, model='unknown-model', tokens_in=1000,
                        ts=previous_month_end)

        response = await client.get('/api/usage/summary', params={'days': 1}, headers=OWNER)
        assert response.status_code == 200, response.text
        bot = response.json()['bots'][0]
        assert bot['cost_usd'] == 3.5
        assert bot['cost_usd_today'] == 3.5
        assert bot['cost_usd_month'] == 3.5

        response = await client.get('/api/usage/summary', params={'days': 90}, headers=OWNER)
        assert response.status_code == 200, response.text
        bot = response.json()['bots'][0]
        assert bot['cost_usd'] is None
        assert bot['cost_usd_today'] == 3.5
        assert bot['cost_usd_month'] == 3.5


async def test_usage_summary_bot_cost_with_usage_only_outside_short_window(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            previous_month_end = await con.fetchval("select date_trunc('month',now())-interval '1 microsecond'")
        await add_usage(app, 'scout', thread_id=thread, tokens_in=1000000, tokens_out=100000,
                        ts=previous_month_end)

        response = await client.get('/api/usage/summary', params={'days': 1}, headers=OWNER)
        assert response.status_code == 200, response.text
        bot = response.json()['bots'][0]
        assert bot['cost_usd'] == 0.0
        assert bot['cost_usd_today'] == 0.0
        assert bot['cost_usd_month'] == 0.0

        response = await client.get('/api/usage/summary', params={'days': 90}, headers=OWNER)
        assert response.status_code == 200, response.text
        assert response.json()['bots'][0]['cost_usd'] == 3.5


async def test_usage_summary_bot_cost_missing_price_this_month_before_today(monkeypatch):
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        async with app.state.pool.acquire() as con:
            today_start, first_of_month = await con.fetchrow(
                "select date_trunc('day',now()),date_trunc('day',now())=date_trunc('month',now())")
        if first_of_month:
            pytest.skip('В первый день месяца нет более раннего расхода в этом месяце')
        await add_usage(app, 'scout', thread_id=thread, tokens_in=1000000, tokens_out=100000)
        await add_usage(app, 'scout', thread_id=thread, model='unknown-model', tokens_in=1000,
                        ts=today_start - timedelta(microseconds=1))

        response = await client.get('/api/usage/summary', params={'days': 1}, headers=OWNER)
        assert response.status_code == 200, response.text
        bot = response.json()['bots'][0]
        assert bot['cost_usd'] == 3.5
        assert bot['cost_usd_today'] == 3.5
        assert bot['cost_usd_month'] is None


async def test_usage_summary_bot_cost_windows_null_without_price(monkeypatch):
    """Расход без цены в окне обнуляет оценку окна (null), а не занижает её."""
    async with client_for() as (client, app):
        provider = await make_provider(client, app, monkeypatch)
        await make_bot_with_provider(client, provider['id'])
        thread = await make_thread(client)
        turn = await make_turn(app, thread)
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn,
                        model='unknown-model', tokens_in=1000, tokens_out=500)

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        bot = response.json()['bots'][0]
        assert bot['cost_usd'] is None
        assert bot['cost_usd_today'] is None
        assert bot['cost_usd_month'] is None
