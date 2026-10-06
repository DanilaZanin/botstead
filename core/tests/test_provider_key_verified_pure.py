"""key_verified: второй пробный запрос с заведомо неверным ключом (docs/contracts.md, раздел 11).

OpenRouter отдаёт GET /v1/models без проверки ключа, поэтому «ok, 464 модели» ничего не говорит о ключе. После успешной
пробы openai_compatible и openai_api шлют тот же запрос с `Bearer invalid-` и 16 случайными символами:
401/403 -> true, 2xx -> false, сеть/5xx -> null. anthropic и google: true при 200, подписочные: null."""
import asyncio
import re
import time

import httpx
import pytest

from bothub.main import ProbeError, fetch_provider_models
from test_provider_verify_pure import ADMIN, GOOD_KEY, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.pure

MODELS = {'data': [{'id': 'm1'}, {'id': 'm2'}]}


async def public_resolver(host, port):
    return [(None, None, None, None, ('8.8.8.8', port))]


def is_wrong_key(request):
    return request.headers.get('authorization', '').startswith('Bearer invalid-')


def server(monkeypatch, on_wrong_key, kind_body=MODELS):
    """Транспорт: настоящий ключ получает список моделей, неверный (второй запрос) ответ on_wrong_key(request)."""
    seen = []

    def upstream(request):
        seen.append(request)
        if is_wrong_key(request):
            return on_wrong_key(request)
        return httpx.Response(200, json=kind_body)
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    return seen


# ---- fetch_provider_models ---------------------------------------------------------------------------

@pytest.mark.parametrize('kind', ['openai_compatible', 'openai_api'])
@pytest.mark.parametrize('status', [401, 403])
async def test_server_that_checks_keys_gives_true(monkeypatch, kind, status):
    seen = server(monkeypatch, lambda request: httpx.Response(status, json={'error': 'bad key'}))
    names = await fetch_provider_models(kind, 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert names == ['m1', 'm2'] and names.key_verified is True
    assert len(seen) == 2


@pytest.mark.parametrize('kind', ['openai_compatible', 'openai_api'])
async def test_server_that_ignores_keys_gives_false_and_the_status_stays_ok(monkeypatch, kind):
    seen = server(monkeypatch, lambda request: httpx.Response(200, json=MODELS))  # как OpenRouter
    names = await fetch_provider_models(kind, 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert names == ['m1', 'm2'] and names.key_verified is False  # модели получены, ProbeError нет
    assert len(seen) == 2


@pytest.mark.parametrize('status', [500, 502, 503, 429, 404, 400])
async def test_other_answers_to_the_wrong_key_give_null(monkeypatch, status):
    server(monkeypatch, lambda request: httpx.Response(status))
    names = await fetch_provider_models('openai_compatible', 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert names == ['m1', 'm2'] and names.key_verified is None


@pytest.mark.parametrize('error', [httpx.ConnectError('down'), httpx.ReadTimeout('slow'), OSError('reset')])
async def test_network_failure_of_the_second_request_gives_null_not_an_error(monkeypatch, error):
    def fail(request):
        raise error
    server(monkeypatch, fail)
    names = await fetch_provider_models('openai_compatible', 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert names == ['m1', 'm2'] and names.key_verified is None


async def test_second_request_uses_the_same_path_host_and_a_random_wrong_key(monkeypatch):
    seen = server(monkeypatch, lambda request: httpx.Response(401))
    await fetch_provider_models('openai_compatible', 'https://api.example/api/v1', GOOD_KEY, resolver=public_resolver)
    await fetch_provider_models('openai_compatible', 'https://api.example/api/v1', GOOD_KEY, resolver=public_resolver)
    real_1, wrong_1, real_2, wrong_2 = seen
    assert real_1.headers['authorization'] == f'Bearer {GOOD_KEY}'
    for wrong in (wrong_1, wrong_2):
        assert re.fullmatch(r'Bearer invalid-[0-9a-f]{16}', wrong.headers['authorization'])
        assert GOOD_KEY not in str(wrong.headers) and GOOD_KEY not in str(wrong.url)
        assert (wrong.method, wrong.url, wrong.headers['host']) == (real_1.method, real_1.url, real_1.headers['host'])
        assert wrong.extensions['sni_hostname'] == 'api.example'
    assert wrong_1.headers['authorization'] != wrong_2.headers['authorization']  # каждый раз новый ключ


@pytest.mark.parametrize('kind,body,header', [
    ('anthropic_api', MODELS, 'x-api-key'),
    ('google_api', {'models': [{'name': 'models/g1'}]}, 'x-goog-api-key'),
])
async def test_anthropic_and_google_give_true_on_200_with_one_request(monkeypatch, kind, body, header):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json=body)
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    names = await fetch_provider_models(kind, 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert names and names.key_verified is True
    assert len(seen) == 1 and seen[0].headers[header] == GOOD_KEY


@pytest.mark.parametrize('first_status,code', [(401, 'key_rejected'), (403, 'key_rejected'), (503, 'unreachable'), (404, 'incompatible')])
async def test_failed_first_probe_sends_no_second_request(monkeypatch, first_status, code):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(first_status)
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    with pytest.raises(ProbeError) as raised:
        await fetch_provider_models('openai_compatible', 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert raised.value.code == code and len(seen) == 1


async def test_second_request_shares_the_single_check_timeout(monkeypatch):
    monkeypatch.setattr('bothub.main.PROVIDER_CHECK_TIMEOUT', 1.5)
    seen = []

    async def upstream(request):
        seen.append(request)
        if is_wrong_key(request):
            await asyncio.sleep(3600)  # второй запрос завис
        return httpx.Response(200, json=MODELS)
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    started = time.monotonic()
    names = await fetch_provider_models('openai_compatible', 'https://api.example', GOOD_KEY, resolver=public_resolver)
    elapsed = time.monotonic() - started
    assert names == ['m1', 'm2'] and names.key_verified is None  # проверка не провалена: ключ просто не определён
    assert len(seen) == 2 and elapsed < 1.5, 'вместе с первым запросом укладывается в PROVIDER_CHECK_TIMEOUT'


async def test_no_budget_left_skips_the_second_request(monkeypatch):
    monkeypatch.setattr('bothub.main.PROVIDER_CHECK_TIMEOUT', 0.5)  # после первого запроса остаётся меньше запаса в секунду
    seen = server(monkeypatch, lambda request: httpx.Response(401))
    names = await fetch_provider_models('openai_compatible', 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert names.key_verified is None and len(seen) == 1


@pytest.mark.parametrize('body', [{'data': []}, {'data': 'x'}, {'items': []}])
async def test_unusable_model_list_fails_as_before_without_a_second_request(monkeypatch, body):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json=body)
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    with pytest.raises(ProbeError) as raised:
        await fetch_provider_models('openai_compatible', 'https://api.example', GOOD_KEY, resolver=public_resolver)
    assert raised.value.code == 'incompatible' and len(seen) == 1


# ---- маршруты: ответ проверки и колонка ----------------------------------------------------------------

def loose_server(env):
    env.upstream.behaviour = lambda request: httpx.Response(200, json=MODELS)  # любой ключ принят, как у OpenRouter


async def test_create_returns_key_verified_true_when_the_server_rejects_the_wrong_key(env):
    created = await env.create()  # Upstream по умолчанию: 401 на ключ без 'good'
    assert created.status_code == 201 and created.json()['status'] == 'ok' and created.json()['key_verified'] is True
    assert env.db.only()['key_verified'] is True
    assert len(env.upstream.requests) == 1 and len(env.upstream.unkeyed) == 1


async def test_create_returns_key_verified_false_but_ok_for_a_server_that_takes_any_key(env):
    loose_server(env)
    created = await env.create(name='OpenRouter', base_url='https://openrouter.ai/api/v1', secret='sk-or-wrong-key-0000')
    assert created.status_code == 201, created.text
    body = created.json()
    assert body['status'] == 'ok' and body['key_verified'] is False and body['last_error'] is None
    assert env.db.only()['key_verified'] is False
    listed = await env.client.get('/api/providers', headers=ADMIN)
    assert 'sk-or-wrong-key-0000' not in listed.text and listed.json()[0]['key_verified'] is False


async def test_create_with_a_failing_second_request_stores_null(env):
    def behaviour(request):
        if is_wrong_key(request):
            raise httpx.ConnectError('down')
        return httpx.Response(200, json=MODELS)
    env.upstream.behaviour = behaviour
    created = await env.create()
    assert created.status_code == 201 and created.json()['status'] == 'ok' and created.json()['key_verified'] is None


async def test_force_create_does_not_probe_and_keeps_null(env):
    created = await env.create(force=True)
    assert created.status_code == 201 and created.json()['status'] == 'unchecked' and created.json()['key_verified'] is None
    assert not env.upstream.requests and not env.upstream.unkeyed


async def test_patch_with_a_new_key_updates_the_value(env):
    created = await env.create()
    assert created.json()['key_verified'] is True
    loose_server(env)
    replaced = await env.patch(created.json(), secret='sk-good-replacement-ABCD')
    assert replaced.status_code == 200 and replaced.json()['key_verified'] is False
    assert env.db.only()['key_verified'] is False
    forced = await env.patch(created.json(), secret=GOOD_KEY, force=True)
    assert forced.status_code == 200 and forced.json()['key_verified'] is None


async def test_check_stores_the_value_and_a_failed_check_clears_it(env):
    created = await env.create(force=True)
    provider_id = created.json()['id']
    loose_server(env)
    checked = await env.client.post(f'/api/providers/{provider_id}/check', headers=ADMIN)
    assert checked.status_code == 200 and checked.json()['status'] == 'ok' and checked.json()['key_verified'] is False
    assert env.db.only()['key_verified'] is False
    env.db.only()['last_check_at'] = None  # кэш проверки
    env.upstream.behaviour = lambda request: httpx.Response(401)
    failed = await env.client.post(f'/api/providers/{provider_id}/check', headers=ADMIN)
    assert failed.json()['status'] == 'error' and failed.json()['key_verified'] is None
    assert env.db.only()['key_verified'] is None


async def test_anthropic_provider_gets_true(env):
    env.upstream.behaviour = lambda request: httpx.Response(200, json=MODELS)
    created = await env.create(kind='anthropic_api', base_url=None, name='Claude')
    assert created.status_code == 201 and created.json()['key_verified'] is True
    assert not env.upstream.unkeyed
