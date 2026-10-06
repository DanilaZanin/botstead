"""Проверяющая модель действий на Postgres (раздел 20, миграция 029): настройка бота, POST /api/approvals, события, лента."""
import base64
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from bothub.main import create_app

OWNER = {'Authorization': 'Bearer test-owner'}
KEY = 'sk-checker-db-key-0123456789'
SECRET_PASSWORD = 'hunter2-very-secret'


def bot_token(bot_id):
    digest = hmac.new(b'test-secret', bot_id.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'Bearer bot:{bot_id}:{digest}'}


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setenv('BOTHUB_SECRET_KEYS', '1:' + base64.b64encode(b'k' * 32).decode())
    monkeypatch.setenv('BOTHUB_BASE_PATH', '/')
    monkeypatch.delenv('PROVIDER_PRIVATE_ALLOW', raising=False)
    state = {'answer': '{"verdict":"ask","reason":"не уверен"}', 'status': 200, 'calls': []}

    async def handler(request):
        if request.method == 'POST':
            state['calls'].append(request)
            if state.get('hook'):
                await state['hook']()
            if state['status'] != 200:
                return httpx.Response(state['status'], text='fail')
            return httpx.Response(200, json={'choices': [{'message': {'content': state['answer']}}]})
        if request.headers.get('authorization', '').startswith('Bearer invalid-'):
            return httpx.Response(401, json={})
        return httpx.Response(200, json={'data': [{'id': 'm1'}, {'id': 'judge'}]})

    async def resolver(host, port):
        return [(None, None, None, None, ('8.8.8.8', port))]

    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(handler))
    monkeypatch.setattr('bothub.main._resolve_host', resolver)
    monkeypatch.setattr('bothub.checker._resolve_host', resolver)
    monkeypatch.setattr('bothub.main.own_networks', lambda: ())
    return state


@asynccontextmanager
async def api():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
            yield client, app


async def setup(client, **bot_fields):
    """Провайдер с моделями m1 и judge, бот scout на m1, тред с запросом владельца; возвращает (бот, id модели judge, тред, ход)."""
    created = await client.post('/api/providers', headers=OWNER, json={
        'kind': 'openai_compatible', 'name': 'P', 'base_url': 'https://api.example', 'secret': KEY})
    assert created.status_code == 201, created.text
    models = {m['name']: m['id'] for m in (await client.get('/api/models', headers=OWNER)).json()}
    body = {'id': 'scout', 'name': 'Scout', 'provider': 'fake', 'model': 'fake', 'role': 'Ищет вакансии',
            'provider_id': created.json()['id'], 'model_id': models['m1']} | bot_fields
    response = await client.post('/api/bots', json=body, headers=OWNER)
    assert response.status_code in (200, 201), response.text
    thread = (await client.post('/api/threads', json={'bot_id': 'scout', 'title': 'T'}, headers=OWNER)).json()
    return response.json(), models['judge'], thread, models


async def add_turn(app, thread_id, prompt):
    async with app.state.pool.acquire() as con:
        turn = await con.fetchval("insert into bothub.turns(thread_id,prompt,client) values($1,$2,'api') returning id", uuid.UUID(thread_id), prompt)
        seq = await con.fetchval('update bothub.threads set last_seq=last_seq+1 where id=$1 returning last_seq', uuid.UUID(thread_id))
        await con.execute("insert into bothub.events(thread_id,seq,turn_id,kind,actor,client,payload) values($1,$2,$3,'user_msg','owner','api',$4::jsonb)",
                          uuid.UUID(thread_id), seq, turn, json.dumps({'text': prompt}))
    return str(turn)


async def ask(client, thread, tool='Bash', args=None, turn=None):
    body = {'thread_id': thread['id'], 'turn_id': turn, 'risk': 'other', 'title': 'Действие', 'tool': tool,
            'args': args or {'command': 'git push origin main'}}
    response = await client.post('/api/approvals', json=body, headers=bot_token('scout'))
    assert response.status_code in (200, 201), response.text
    return response.json()


async def events(client, thread, kind=None):
    rows = (await client.get(f"/api/threads/{thread['id']}/events", headers=OWNER)).json()
    return [e for e in rows if kind is None or e['kind'] == kind]


async def test_checker_model_is_validated_and_stored(upstream):
    async with api() as (client, app):
        bot, judge, thread, _ = await setup(client)
        assert bot['checker_model_id'] is None
        saved = await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)
        assert saved.status_code == 200 and saved.json()['checker_model_id'] == judge
        assert (await client.patch('/api/bots/scout', json={'checker_model_id': str(uuid.uuid4())}, headers=OWNER)).status_code == 400
        async with app.state.pool.acquire() as con:
            owner = await con.fetchval("select id from bothub.users where email='fixture@example.com'")
            other = await con.fetchval("insert into bothub.users(email,password_hash) values('other@example.com','x') returning id")
            sub = await con.fetchval("insert into bothub.providers(owner_id,kind,cli,name) values($1,'cli_subscription','claude','S') returning id", owner)
            sub_model = await con.fetchval("insert into bothub.models(provider_id,name) values($1,'sonnet') returning id", sub)
            foreign = await con.fetchval("insert into bothub.providers(owner_id,kind,name,base_url,secret_encrypted) values($1,'openai_compatible','F','https://f.example',decode('00','hex')) returning id", other)
            foreign_model = await con.fetchval("insert into bothub.models(provider_id,name) values($1,'x') returning id", foreign)
        for bad in (sub_model, foreign_model):
            response = await client.patch('/api/bots/scout', json={'checker_model_id': str(bad)}, headers=OWNER)
            assert response.status_code == 400, response.text
        assert (await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)).json()['checker_model_id'] == judge
        cleared = await client.patch('/api/bots/scout', json={'checker_model_id': None}, headers=OWNER)
        assert cleared.status_code == 200 and cleared.json()['checker_model_id'] is None
        # при создании бота та же проверка
        created = await client.post('/api/bots', headers=OWNER, json={'id': 'second', 'name': 'Second', 'provider': 'fake', 'model': 'fake',
            'provider_id': bot['provider_id'], 'model_id': bot['model_id'], 'checker_model_id': str(foreign_model)})
        assert created.status_code == 400
        ok = await client.post('/api/bots', headers=OWNER, json={'id': 'second', 'name': 'Second', 'provider': 'fake', 'model': 'fake',
            'provider_id': bot['provider_id'], 'model_id': bot['model_id'], 'checker_model_id': judge})
        assert ok.status_code in (200, 201) and ok.json()['checker_model_id'] == judge


async def test_deny_closes_the_approval_and_writes_events_and_activity(upstream):
    upstream['answer'] = '{"verdict":"deny","reason":"Отправка кода не входила в запрос"}'
    async with api() as (client, app):
        bot, judge, thread, _ = await setup(client)
        await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)
        turn = await add_turn(app, thread['id'], 'Найди вакансии SRE')
        result = await ask(client, thread, args={'command': 'git push origin main', 'password': SECRET_PASSWORD}, turn=turn)
        assert result['status'] == 'rejected' and result['reason'] == 'checker_denied'
        assert result['checker_verdict'] == 'deny' and result['checker_reason'] == 'Отправка кода не входила в запрос'
        assert len(upstream['calls']) == 1
        sent = upstream['calls'][0].content.decode()
        assert 'Найди вакансии SRE' in sent and 'Ищет вакансии' in sent and 'git push origin main' in sent
        assert KEY not in sent and SECRET_PASSWORD not in sent  # ключ провайдера и секретное поле не уходят
        assert upstream['calls'][0].headers['authorization'] == f'Bearer {KEY}' and upstream['calls'][0].url.host == '8.8.8.8'
        req = (await events(client, thread, 'approval_req'))[-1]['payload']
        dec = (await events(client, thread, 'approval_dec'))[-1]['payload']
        assert req['checker'] == {'verdict': 'deny', 'reason': 'Отправка кода не входила в запрос'}
        assert dec['decision'] == 'rejected' and dec['reason'] == 'checker_denied' and dec['approval_id'] == result['id']
        denied = (await events(client, thread, 'checker_denied'))[-1]['payload']
        assert denied == {'approval_id': result['id'], 'tool': 'Bash', 'reason': 'Отправка кода не входила в запрос'}
        assert (await client.get('/api/approvals', headers=OWNER)).json() == []  # владельцу вопрос не пришёл
        feed = (await client.get('/api/activity?kind=approval', headers=OWNER)).json()
        codes = [item['title']['code'] for item in feed['items']]
        assert 'checker_denied' in codes and 'approval_rejected' not in codes
        assert all(SECRET_PASSWORD not in json.dumps(item) for item in feed['items'])


@pytest.mark.parametrize('answer', ['{"verdict":"allow","reason":"Это просил владелец"}', '{"verdict":"ask","reason":"Неясно"}'])
async def test_allow_and_ask_stay_pending_with_a_hint(upstream, answer):
    upstream['answer'] = answer
    verdict = json.loads(answer)
    async with api() as (client, app):
        _, judge, thread, _ = await setup(client)
        await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)
        result = await ask(client, thread)
        assert result['status'] == 'pending' and result.get('reason') is None  # allow сам ничего не разрешает
        assert result['checker_verdict'] == verdict['verdict'] and result['checker_reason'] == verdict['reason']
        req = (await events(client, thread, 'approval_req'))[-1]['payload']
        assert req['checker'] == verdict
        assert await events(client, thread, 'checker_denied') == []
        pending = (await client.get('/api/approvals', headers=OWNER)).json()
        assert [p['id'] for p in pending] == [result['id']] and pending[0]['checker_verdict'] == verdict['verdict']
        assert len(upstream['calls']) == 1


@pytest.mark.parametrize('mode', ['malformed', 'http500'])
async def test_checker_failure_means_ask(upstream, mode):
    if mode == 'malformed':
        upstream['answer'] = 'sorry, no json'
    else:
        upstream['status'] = 500
    async with api() as (client, app):
        _, judge, thread, _ = await setup(client)
        await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)
        result = await ask(client, thread)
        assert result['status'] == 'pending' and result['checker_verdict'] == 'ask'
        assert len(upstream['calls']) == 1  # не больше одного запроса на approval


async def test_no_checker_for_low_risk_or_bot_without_checker(upstream):
    async with api() as (client, app):
        _, judge, thread, _ = await setup(client)
        plain = await ask(client, thread)  # у бота нет проверяющей модели
        assert plain['status'] == 'pending' and plain['checker_verdict'] is None and upstream['calls'] == []
        await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)
        low = await ask(client, thread, tool='SomethingUnknown', args={'a': 1})  # риск other: не проверяется
        assert low['risk'] == 'other' and low['status'] == 'pending' and low['checker_verdict'] is None
        assert upstream['calls'] == []
        req = (await events(client, thread, 'approval_req'))[-1]['payload']
        assert 'checker' not in req


async def test_deleted_checker_model_disables_the_check(upstream):
    async with api() as (client, app):
        _, judge, thread, _ = await setup(client)
        await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)
        async with app.state.pool.acquire() as con:
            await con.execute('delete from bothub.models where id=$1', uuid.UUID(judge))
        bot = [b for b in (await client.get('/api/bots', headers=OWNER)).json() if b['id'] == 'scout'][0]
        assert bot['checker_model_id'] is None
        result = await ask(client, thread)
        assert result['status'] == 'pending' and upstream['calls'] == []


async def test_pool_connection_is_not_held_during_the_checker_call(upstream):
    upstream['answer'] = '{"verdict":"ask","reason":"r"}'
    async with api() as (client, app):
        _, judge, thread, _ = await setup(client)
        await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)
        busy = []

        async def hook():
            pool = app.state.pool
            busy.append(pool.get_size() - pool.get_idle_size())
        upstream['hook'] = hook
        result = await ask(client, thread)
        assert result['status'] == 'pending' and busy == [0]  # во время запроса ни одного занятого соединения


async def test_verdict_is_dropped_when_the_bot_changed_during_the_call(upstream):
    upstream['answer'] = '{"verdict":"deny","reason":"нет"}'
    async with api() as (client, app):
        _, judge, thread, _ = await setup(client)
        await client.patch('/api/bots/scout', json={'checker_model_id': judge}, headers=OWNER)

        async def hook():  # владелец выключил проверку, пока модель думала
            async with app.state.pool.acquire() as con:
                await con.execute("update bothub.bots set checker_model_id=null where id='scout'")
        upstream['hook'] = hook
        result = await ask(client, thread)
        assert result['status'] == 'pending' and result['checker_verdict'] is None  # устаревший deny не применяется
        assert await events(client, thread, 'checker_denied') == []
