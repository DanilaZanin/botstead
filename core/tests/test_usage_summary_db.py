"""Тесты сводки расхода (/api/usage/summary) в БД Postgres: изоляция владельца, фильтр дней, группировка по моделям, посуточный график, ошибки валидации."""
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub import auth
from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}
NOW = timezone.utc


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
                                  email, auth.hash_password('long-password'))
    login = await client.post('/api/auth/login', json={'email': email, 'password': 'long-password'})
    assert login.status_code == 200, login.text
    headers = {'Cookie': f"bothub_session={login.cookies['bothub_session']}", 'X-CSRF': login.json()['csrf_token'],
               'Origin': 'https://testserver'}
    return user, headers


async def owner_id(app):
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select id from bothub.users where email='fixture@example.com'")


async def make_bot(client, bot_id='alpha', headers=OWNER, budget=200000, provider='fake', model='fake'):
    response = await client.post('/api/bots', json={
        'id': bot_id, 'name': bot_id.title(), 'provider': provider, 'model': model,
        'budget_daily_tokens': budget
    }, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def make_thread(client, bot_id='alpha', headers=OWNER):
    response = await client.post('/api/threads', json={'bot_id': bot_id}, headers=headers)
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()['id'])


async def make_turn(app, thread, status='done'):
    async with app.state.pool.acquire() as con:
        return await con.fetchval('insert into bothub.turns(thread_id,prompt,status) values($1,$2,$3) returning id',
                                  thread, 'task', status)


async def add_usage(app, bot_id, *, thread_id=None, turn_id=None, provider='claude', model='claude-sonnet-5',
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


async def day_start(app, offset=0):
    """Полночь суток offset дней назад и её дата, как их считает база (та же date_trunc, что в маршруте)."""
    async with app.state.pool.acquire() as con:
        row = await con.fetchrow(
            "select date_trunc('day',now())-$1::int*interval '1 day' ts, "
            "to_char(date_trunc('day',now())-$1::int*interval '1 day','YYYY-MM-DD') date", offset)
    return row['ts'], row['date']


# --- Тесты GET /api/usage/summary -------------------------------------------------------------

async def test_usage_summary_default_structure():
    async with client_for() as (client, app):
        await make_bot(client, 'scout', budget=300000)
        thread = await make_thread(client, 'scout')
        turn = await make_turn(app, thread)
        await add_usage(app, 'scout', thread_id=thread, turn_id=turn, provider='claude', model='claude-sonnet-5',
                        tokens_in=1000, tokens_out=500, tokens_cache_read=200, tokens_cache_write=100)

        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200, response.text
        data = response.json()

        assert data['days'] == 7
        assert data['total_tokens'] == 1800
        assert len(data['daily']) == 7
        assert isinstance(data['daily'], list)
        assert data['daily'][-1]['total_tokens'] == 1800

        assert len(data['bots']) == 1
        bot = data['bots'][0]
        assert bot['bot_id'] == 'scout'
        assert bot['budget'] == 300000
        assert bot['tokens_today'] == 1800
        assert bot['tokens_period'] == 1800
        assert bot['last_activity'] is not None

        assert len(data['models']) == 1
        model = data['models'][0]
        assert model['provider'] == 'claude'
        assert model['model'] == 'claude-sonnet-5'
        assert model['tokens_in'] == 1000
        assert model['tokens_out'] == 500
        assert model['tokens_cache_read'] == 200
        assert model['tokens_cache_write'] == 100
        assert model['total_tokens'] == 1800
        assert model['turns'] == 1

        assert isinstance(data['providers'], list)


async def test_usage_summary_own_data_only():
    async with client_for() as (client, app):
        await make_bot(client, 'mine')
        thread_mine = await make_thread(client, 'mine')
        turn_mine = await make_turn(app, thread_mine)
        await add_usage(app, 'mine', thread_id=thread_mine, turn_id=turn_mine, provider='claude', model='claude-sonnet-5',
                        tokens_in=500, tokens_out=200)

        _, member_headers = await add_member(client, app, 'other@example.com')
        await make_bot(client, 'foreign', headers=member_headers)
        thread_foreign = await make_thread(client, 'foreign', headers=member_headers)
        turn_foreign = await make_turn(app, thread_foreign)
        await add_usage(app, 'foreign', thread_id=thread_foreign, turn_id=turn_foreign, provider='codex', model='gpt-5.4',
                        tokens_in=3000, tokens_out=1000)

        res_mine = await client.get('/api/usage/summary', headers=OWNER)
        assert res_mine.status_code == 200
        mine_data = res_mine.json()
        assert [b['bot_id'] for b in mine_data['bots']] == ['mine']
        assert mine_data['total_tokens'] == 700
        assert [m['model'] for m in mine_data['models']] == ['claude-sonnet-5']

        res_other = await client.get('/api/usage/summary', headers=member_headers)
        assert res_other.status_code == 200
        other_data = res_other.json()
        assert [b['bot_id'] for b in other_data['bots']] == ['foreign']
        assert other_data['total_tokens'] == 4000
        assert [m['model'] for m in other_data['models']] == ['gpt-5.4']


async def test_usage_summary_period_filter():
    async with client_for() as (client, app):
        await make_bot(client, 'time-bot')
        thread = await make_thread(client, 'time-bot')
        turn1 = await make_turn(app, thread)
        turn2 = await make_turn(app, thread)
        turn3 = await make_turn(app, thread)
        turn4 = await make_turn(app, thread)

        now = datetime.now(NOW)
        # 1) Сегодня: 100 токенов
        await add_usage(app, 'time-bot', thread_id=thread, turn_id=turn1, tokens_in=60, tokens_out=40, ts=now)
        # 2) 3 дня назад: 200 токенов
        await add_usage(app, 'time-bot', thread_id=thread, turn_id=turn2, tokens_in=120, tokens_out=80, ts=now - timedelta(days=3))
        # 3) 15 дней назад: 400 токенов
        await add_usage(app, 'time-bot', thread_id=thread, turn_id=turn3, tokens_in=240, tokens_out=160, ts=now - timedelta(days=15))
        # 4) 45 дней назад: 800 токенов
        await add_usage(app, 'time-bot', thread_id=thread, turn_id=turn4, tokens_in=480, tokens_out=320, ts=now - timedelta(days=45))

        # days=1: только сегодня
        r1 = await client.get('/api/usage/summary', params={'days': 1}, headers=OWNER)
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1['days'] == 1
        assert len(d1['daily']) == 1
        assert d1['total_tokens'] == 100
        assert d1['bots'][0]['tokens_today'] == 100
        assert d1['bots'][0]['tokens_period'] == 100

        # days=7: сегодня + 3 дня назад = 300
        r7 = await client.get('/api/usage/summary', params={'days': 7}, headers=OWNER)
        assert r7.status_code == 200
        d7 = r7.json()
        assert d7['days'] == 7
        assert len(d7['daily']) == 7
        assert d7['total_tokens'] == 300
        assert d7['bots'][0]['tokens_today'] == 100
        assert d7['bots'][0]['tokens_period'] == 300

        # days=30: сегодня + 3 дня + 15 дней = 700
        r30 = await client.get('/api/usage/summary', params={'days': 30}, headers=OWNER)
        assert r30.status_code == 200
        d30 = r30.json()
        assert d30['days'] == 30
        assert len(d30['daily']) == 30
        assert d30['total_tokens'] == 700
        assert d30['bots'][0]['tokens_today'] == 100
        assert d30['bots'][0]['tokens_period'] == 700

        # days=90: все 4 записи = 1500
        r90 = await client.get('/api/usage/summary', params={'days': 90}, headers=OWNER)
        assert r90.status_code == 200
        d90 = r90.json()
        assert d90['days'] == 90
        assert len(d90['daily']) == 90
        assert d90['total_tokens'] == 1500
        assert d90['bots'][0]['tokens_period'] == 1500


async def test_usage_summary_per_model_grouping():
    async with client_for() as (client, app):
        await make_bot(client, 'multi-model')
        thread = await make_thread(client, 'multi-model')
        t1 = await make_turn(app, thread)
        t2 = await make_turn(app, thread)
        t3 = await make_turn(app, thread)

        # 2 turn'а на Sonnet
        await add_usage(app, 'multi-model', thread_id=thread, turn_id=t1, provider='claude', model='claude-sonnet-5',
                        tokens_in=100, tokens_out=50, tokens_cache_read=20, tokens_cache_write=10)
        await add_usage(app, 'multi-model', thread_id=thread, turn_id=t1, provider='claude', model='claude-sonnet-5',
                        tokens_in=200, tokens_out=100, tokens_cache_read=40, tokens_cache_write=20)
        await add_usage(app, 'multi-model', thread_id=thread, turn_id=t2, provider='claude', model='claude-sonnet-5',
                        tokens_in=300, tokens_out=150, tokens_cache_read=60, tokens_cache_write=30)

        # 1 turn на Opus
        await add_usage(app, 'multi-model', thread_id=thread, turn_id=t3, provider='claude', model='claude-opus-5-5',
                        tokens_in=500, tokens_out=250, tokens_cache_read=0, tokens_cache_write=0)

        response = await client.get('/api/usage/summary', params={'days': 7}, headers=OWNER)
        assert response.status_code == 200
        models = {m['model']: m for m in response.json()['models']}

        sonnet = models['claude-sonnet-5']
        assert sonnet['provider'] == 'claude'
        assert sonnet['tokens_in'] == 600
        assert sonnet['tokens_out'] == 300
        assert sonnet['tokens_cache_read'] == 120
        assert sonnet['tokens_cache_write'] == 60
        assert sonnet['total_tokens'] == 1080
        assert sonnet['turns'] == 2

        opus = models['claude-opus-5-5']
        assert opus['provider'] == 'claude'
        assert opus['tokens_in'] == 500
        assert opus['tokens_out'] == 250
        assert opus['total_tokens'] == 750
        assert opus['turns'] == 1


async def test_usage_summary_daily_series_sequential_dates():
    async with client_for() as (client, app):
        await make_bot(client, 'series-bot')
        response = await client.get('/api/usage/summary', params={'days': 5}, headers=OWNER)
        assert response.status_code == 200
        daily = response.json()['daily']
        assert len(daily) == 5
        dates = [item['date'] for item in daily]
        for i in range(len(dates) - 1):
            d1 = datetime.strptime(dates[i], '%Y-%m-%d').date()
            d2 = datetime.strptime(dates[i + 1], '%Y-%m-%d').date()
            assert (d2 - d1).days == 1
        assert all(item['total_tokens'] == 0 for item in daily)


async def test_usage_summary_bot_without_usage():
    async with client_for() as (client, app):
        await make_bot(client, 'idle-bot', budget=150000)
        response = await client.get('/api/usage/summary', headers=OWNER)
        assert response.status_code == 200
        bots = response.json()['bots']
        assert len(bots) == 1
        b = bots[0]
        assert b['bot_id'] == 'idle-bot'
        assert b['budget'] == 150000
        assert b['tokens_today'] == 0
        assert b['tokens_period'] == 0
        assert b['last_activity'] is None


@pytest.mark.parametrize('bad_days', ['0', '-1', '91', '100', 'abc', '7.5', '', '07', '+7', '1e1', '0x7'])
async def test_usage_summary_rejects_invalid_days_with_422(bad_days):
    async with client_for() as (client, _):
        response = await client.get('/api/usage/summary', params={'days': bad_days}, headers=OWNER)
        assert response.status_code == 422, response.text
        assert response.json() == {'error': 'invalid', 'detail': 'days'}


async def test_usage_summary_days_edges_are_accepted():
    async with client_for() as (client, _):
        for days in (1, 90):
            response = await client.get('/api/usage/summary', params={'days': days}, headers=OWNER)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body['days'] == days
            assert len(body['daily']) == days


async def month_start(app):
    """Полночь первого числа текущего месяца, как её считает база (та же date_trunc, что в маршруте)."""
    async with app.state.pool.acquire() as con:
        return await con.fetchval("select date_trunc('month',now())")


async def test_usage_summary_bot_tokens_month():
    """tokens_month накапливает расход с начала календарного месяца независимо от days (панель «Квоты»)."""
    async with client_for() as (client, app):
        await make_bot(client, 'month-bot')
        thread = await make_thread(client, 'month-bot')
        now = datetime.now(NOW)
        this_month = await month_start(app)
        await add_usage(app, 'month-bot', thread_id=thread, tokens_in=60, tokens_out=40, ts=now)  # сегодня
        await add_usage(app, 'month-bot', thread_id=thread, tokens_in=120, tokens_out=80, ts=this_month)  # ровно с начала месяца
        await add_usage(app, 'month-bot', thread_id=thread, tokens_in=480, tokens_out=320, ts=now - timedelta(days=45))  # до месяца

        async with app.state.pool.acquire() as con: first_of_month = await con.fetchval("select date_trunc('day',now())=date_trunc('month',now())")
        r1 = await client.get('/api/usage/summary', params={'days': 1}, headers=OWNER)
        assert r1.status_code == 200, r1.text
        bot = r1.json()['bots'][0]
        assert bot['tokens_today'] == (300 if first_of_month else 100)
        assert bot['tokens_month'] == 300
        assert bot['tokens_period'] == (300 if first_of_month else 100)


async def test_usage_summary_bot_tokens_month_own_data_only():
    """Чужой расход не попадает в tokens_month (та же изоляция owner_id, что у остальных полей)."""
    async with client_for() as (client, app):
        await make_bot(client, 'mine')
        thread_mine = await make_thread(client, 'mine')
        await add_usage(app, 'mine', thread_id=thread_mine, tokens_in=100, tokens_out=50)

        _, member_headers = await add_member(client, app, 'monthly@example.com')
        await make_bot(client, 'foreign', headers=member_headers)
        thread_foreign = await make_thread(client, 'foreign', headers=member_headers)
        await add_usage(app, 'foreign', thread_id=thread_foreign, tokens_in=3000, tokens_out=1000)

        mine = (await client.get('/api/usage/summary', headers=OWNER)).json()
        assert [b['bot_id'] for b in mine['bots']] == ['mine']
        assert mine['bots'][0]['tokens_month'] == 150

        other = (await client.get('/api/usage/summary', headers=member_headers)).json()
        assert [b['bot_id'] for b in other['bots']] == ['foreign']
        assert other['bots'][0]['tokens_month'] == 4000


async def test_usage_summary_period_boundary_is_calendar_day_start():
    """Период начинается с полуночи суток (days-1) дней назад: запись ровно days суток назад в него не входит."""
    async with client_for() as (client, app):
        await make_bot(client, 'edge-bot')
        thread = await make_thread(client, 'edge-bot')
        first_midnight, _ = await day_start(app, 2)  # начало периода days=3
        now = datetime.now(NOW)
        await add_usage(app, 'edge-bot', thread_id=thread, tokens_in=10, tokens_out=0, ts=first_midnight)  # ровно на начале периода
        await add_usage(app, 'edge-bot', thread_id=thread, tokens_in=100, tokens_out=0,
                        ts=first_midnight - timedelta(microseconds=1))  # на микросекунду раньше
        await add_usage(app, 'edge-bot', thread_id=thread, tokens_in=1000, tokens_out=0,
                        ts=now - timedelta(days=3))  # ровно days суток назад: это сутки до начала периода

        r3 = await client.get('/api/usage/summary', params={'days': 3}, headers=OWNER)
        assert r3.status_code == 200, r3.text
        d3 = r3.json()
        assert d3['total_tokens'] == 10
        assert d3['bots'][0]['tokens_period'] == 10
        assert d3['models'][0]['total_tokens'] == 10
        assert d3['daily'][0]['total_tokens'] == 10

        # days=4 сдвигает начало на сутки назад: обе ранние записи теперь внутри
        r4 = await client.get('/api/usage/summary', params={'days': 4}, headers=OWNER)
        assert r4.status_code == 200, r4.text
        d4 = r4.json()
        assert d4['total_tokens'] == 1110
        assert d4['bots'][0]['tokens_period'] == 1110
        assert d4['models'][0]['total_tokens'] == 1110
        assert d4['daily'][0]['total_tokens'] == 1100
        assert d4['daily'][1]['total_tokens'] == 10


async def test_usage_summary_daily_zero_filled_gap_days():
    async with client_for() as (client, app):
        await make_bot(client, 'gap-bot')
        thread = await make_thread(client, 'gap-bot')
        dates = []
        for offset, tokens in ((6, 150), (2, 300), (0, 30)):
            midnight, _ = await day_start(app, offset)
            await add_usage(app, 'gap-bot', thread_id=thread, tokens_in=tokens, tokens_out=0, ts=midnight + timedelta(hours=12))
        for offset in range(6, -1, -1):
            dates.append((await day_start(app, offset))[1])

        response = await client.get('/api/usage/summary', params={'days': 7}, headers=OWNER)
        assert response.status_code == 200, response.text
        daily = response.json()['daily']
        assert [d['date'] for d in daily] == dates
        assert [d['total_tokens'] for d in daily] == [150, 0, 0, 0, 300, 0, 30]
        assert response.json()['total_tokens'] == 480


async def test_usage_summary_other_user_usage_invisible_in_every_section():
    async with client_for() as (client, app):
        await make_bot(client, 'mine')
        thread_mine = await make_thread(client, 'mine')
        turn_mine = await make_turn(app, thread_mine)
        today, today_date = await day_start(app, 0)
        three_ago, three_date = await day_start(app, 3)
        await add_usage(app, 'mine', thread_id=thread_mine, turn_id=turn_mine, provider='claude', model='claude-sonnet-5',
                        tokens_in=100, tokens_out=50, ts=today + timedelta(hours=12))

        _, member_headers = await add_member(client, app, 'stranger@example.com')
        await make_bot(client, 'foreign', headers=member_headers)
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set provider='codex' where id='foreign'")
        thread_foreign = await make_thread(client, 'foreign', headers=member_headers)
        turn_foreign = await make_turn(app, thread_foreign)
        # та же модель и тот же день, что у владельца, плюс своя модель и другой день
        await add_usage(app, 'foreign', thread_id=thread_foreign, turn_id=turn_foreign, provider='claude', model='claude-sonnet-5',
                        tokens_in=9000, tokens_out=900, ts=today + timedelta(hours=12))
        await add_usage(app, 'foreign', thread_id=thread_foreign, turn_id=turn_foreign, provider='codex', model='gpt-5.4',
                        tokens_in=70000, tokens_out=7000, ts=three_ago + timedelta(hours=12))

        mine = (await client.get('/api/usage/summary', params={'days': 7}, headers=OWNER)).json()
        assert mine['total_tokens'] == 150
        assert [b['bot_id'] for b in mine['bots']] == ['mine']
        assert mine['bots'][0]['tokens_today'] == 150
        assert mine['bots'][0]['tokens_period'] == 150
        assert [(m['provider'], m['model'], m['total_tokens'], m['turns']) for m in mine['models']] == [('claude', 'claude-sonnet-5', 150, 1)]
        by_date = {d['date']: d['total_tokens'] for d in mine['daily']}
        assert by_date[today_date] == 150
        assert by_date[three_date] == 0
        assert sum(by_date.values()) == 150
        assert [p['provider'] for p in mine['providers']] == ['fake']

        theirs = (await client.get('/api/usage/summary', params={'days': 7}, headers=member_headers)).json()
        assert theirs['total_tokens'] == 9900 + 77000
        assert [b['bot_id'] for b in theirs['bots']] == ['foreign']
        assert sorted(m['model'] for m in theirs['models']) == ['claude-sonnet-5', 'gpt-5.4']
        assert [p['provider'] for p in theirs['providers']] == ['codex']
