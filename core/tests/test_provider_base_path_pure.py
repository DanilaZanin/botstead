"""Provider base_url with a path (https://host/api/v1): split_base, the probe, the gateway and the routes.

The gateway and the probe append v1/... themselves, so the trailing version of the user's address is dropped and the
rest of the path (base_path) is kept in front of it."""
import httpx
import pytest
from fastapi import FastAPI

from bothub.gateway import GatewayProvider, create_gateway_router, inspect_target, normalized_base, split_base
from bothub.main import fetch_provider_models
from bothub.secrets import issue_gateway_token
from test_provider_verify_pure import GOOD_KEY, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.pure


async def public_resolver(host, port):
    return [(None, None, None, None, ('8.8.8.8', port))]


@pytest.mark.parametrize('url,expected', [
    ('https://api.example', ('https://api.example', '')),
    ('https://api.example/', ('https://api.example', '')),
    ('https://api.example/v1', ('https://api.example', '')),
    ('https://api.example/v1/', ('https://api.example', '')),
    ('https://api.example/V1', ('https://api.example', '')),
    ('https://api.example/v1beta', ('https://api.example', '')),
    ('https://api.example/api', ('https://api.example', '/api')),
    ('https://api.example/api/', ('https://api.example', '/api')),
    ('https://api.example/api/v1', ('https://api.example', '/api')),
    ('https://api.example/openai/deployments/x', ('https://api.example', '/openai/deployments/x')),
    ('https://api.example/v1/proxy', ('https://api.example', '/v1/proxy')),   # версия не в конце: остаётся
    ('https://api.example/v1/v1', ('https://api.example', '')),               # хвостовые версии срезаются все
    ('http://ollama.lan:11434/v1', ('http://ollama.lan:11434', '')),
    ('https://[2001:db8::1]:8443/gw/v1', ('https://[2001:db8::1]:8443', '/gw')),
])
def test_split_base_normalizes_the_path(url, expected):
    assert split_base(url) == expected
    assert normalized_base(url) == expected[0] + expected[1]
    assert split_base(normalized_base(url)) == expected  # идемпотентность


@pytest.mark.parametrize('url', [
    'https://api.example//v1', 'https://api.example/a//b', 'https://api.example/a/../b', 'https://api.example/./a',
    'https://api.example/..', 'https://api.example/a/%2e%2e/b', 'https://api.example/a%2Fb', 'https://api.example/a;b',
    'https://api.example/a b', 'https://api.example/a\\b', 'https://api.example/ü', 'https://api.example:99999/api'])
def test_split_base_rejects_prefixes_that_can_move_the_request(url):
    with pytest.raises(ValueError):
        split_base(url)


async def test_inspect_target_keeps_base_path_in_origin_and_pinned():
    target = await inspect_target('https://api.example:8443/api/v1/', (), public_resolver)
    assert target.base_path == '/api'
    assert target.origin == 'https://api.example:8443/api'
    assert target.pinned == 'https://8.8.8.8:8443/api'
    root = await inspect_target('https://api.example/v1', (), public_resolver)
    assert (root.base_path, root.origin, root.pinned) == ('', 'https://api.example', 'https://8.8.8.8')


@pytest.mark.parametrize('url', ['https://api.example/a/../b', 'https://api.example//x', 'https://api.example/a%2fb'])
async def test_inspect_target_refuses_a_tricky_path(url):
    with pytest.raises(ValueError):
        await inspect_target(url, (), public_resolver)


# ---- проба -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('kind,base,expected', [
    ('openai_compatible', 'https://api.example/api/v1', '/api/v1/models'),
    ('openai_compatible', 'https://api.example/api', '/api/v1/models'),
    ('openai_compatible', 'https://api.example/v1', '/v1/models'),
    ('openai_compatible', 'https://api.example', '/v1/models'),
    ('anthropic_api', 'https://api.example/claude/', '/claude/v1/models'),
    ('google_api', 'https://api.example/g/v1beta', '/g/v1beta/models'),
])
async def test_probe_uses_the_base_path(monkeypatch, kind, base, expected):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={'models': [{'name': 'models/g1'}]} if kind == 'google_api' else {'data': [{'id': 'm1'}]})
    monkeypatch.setattr('bothub.main.PROBE_TRANSPORT', httpx.MockTransport(upstream))
    names = await fetch_provider_models(kind, base, GOOD_KEY, resolver=public_resolver)
    assert names and len(seen) == (2 if kind.startswith('openai') else 1)  # у openai-совместимых второй запрос с неверным ключом
    assert all(request.url.path == expected for request in seen)
    assert seen[0].url.path == expected and seen[0].url.host == '8.8.8.8' and seen[0].headers['host'] == 'api.example'


# ---- шлюз ------------------------------------------------------------------------------------------------

async def gateway_client(kind, base_url, handler):
    async def lookup(bot_id, wanted):
        return GatewayProvider('p1', kind, base_url, 'real-key', True, ['gpt-test', 'claude-test', 'gemini-test'], None)

    async def record(*usage):
        pass
    app = FastAPI()
    router = create_gateway_router(lookup, record, 'hmac-secret', transport=httpx.MockTransport(handler), resolver=public_resolver)
    app.include_router(router)
    await router.startup()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    token = issue_gateway_token('bot-1', 'p1', 'hmac-secret', ttl=1920)
    return router, client, {'Authorization': f'Bearer {token}'}


@pytest.mark.parametrize('kind,base,path,model,expected', [
    ('openai_compatible', 'https://api.example/api/v1', 'v1/chat/completions', 'gpt-test', '/api/v1/chat/completions'),
    ('openai_api', 'https://api.example/api', 'v1/responses', 'gpt-test', '/api/v1/responses'),
    ('openai_compatible', 'https://api.example/v1', 'v1/chat/completions', 'gpt-test', '/v1/chat/completions'),
    ('openai_compatible', 'https://api.example', 'v1/chat/completions', 'gpt-test', '/v1/chat/completions'),
    ('anthropic_api', 'https://api.example/claude/', 'v1/messages', 'claude-test', '/claude/v1/messages'),
    ('google_api', 'https://api.example/g/v1beta', 'v1beta/models/gemini-test:generateContent', 'gemini-test',
     '/g/v1beta/models/gemini-test:generateContent'),
])
async def test_gateway_forwards_under_the_base_path(kind, base, path, model, expected):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={'model': model, 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    router, client, auth = await gateway_client(kind, base, upstream)
    try:
        response = await client.post(f'/gateway/p1/{path}', headers=auth, json={'model': model})
        assert response.status_code == 200, response.text
        assert len(seen) == 1 and seen[0].url.path == expected
        assert seen[0].url.host == '8.8.8.8' and seen[0].headers['host'] == 'api.example'
    finally:
        await client.aclose()
        await router.shutdown()


async def test_gateway_models_route_is_local_with_a_base_path():
    router, client, auth = await gateway_client('openai_compatible', 'https://api.example/api/v1', lambda request: pytest.fail('upstream'))
    try:
        response = await client.get('/gateway/p1/v1/models', headers=auth)
        assert response.status_code == 200 and [m['id'] for m in response.json()['data']] == ['gpt-test', 'claude-test', 'gemini-test']
    finally:
        await client.aclose()
        await router.shutdown()


async def test_gateway_refuses_a_stored_tricky_path():
    router, client, auth = await gateway_client('openai_compatible', 'https://api.example/a/../b', lambda request: pytest.fail('upstream'))
    try:
        response = await client.post('/gateway/p1/v1/chat/completions', headers=auth, json={'model': 'gpt-test'})
        assert response.status_code == 502
    finally:
        await client.aclose()
        await router.shutdown()


# ---- маршруты --------------------------------------------------------------------------------------------

async def test_create_stores_the_normalized_address_and_probes_under_the_path(env):  # noqa: F811
    response = await env.create(base_url='https://api.example/api/v1/')
    assert response.status_code == 201, response.text
    assert response.json()['base_url'] == 'https://api.example/api'
    assert env.db.only()['base_url'] == 'https://api.example/api'
    assert env.upstream.requests[0].url.path == '/api/v1/models'


async def test_create_refuses_a_path_with_dot_segments_without_a_probe(env):  # noqa: F811
    response = await env.create(base_url='https://api.example/a/../b')
    assert response.status_code == 422 and response.json()['error'] == 'invalid_base_url'
    assert not env.upstream.requests and not env.db.providers


async def test_retyping_the_same_address_with_v1_is_not_a_change(env):  # noqa: F811
    await env.create(headers=env.member, base_url='https://llm.lan/api')
    provider = dict(env.db.only())
    assert (await env.allow(provider, True)).json()['status'] == 'ok'
    same = await env.patch(provider, headers=env.member, secret=GOOD_KEY, base_url='https://llm.lan/api/v1/')
    assert same.status_code == 200 and same.json()['allow_private'] is True and same.json()['status'] == 'ok'
    moved = await env.patch(provider, headers=env.member, secret=GOOD_KEY, base_url='https://llm.lan/other')
    assert moved.json()['allow_private'] is False and moved.json()['status'] == 'pending_admin'
