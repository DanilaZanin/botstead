import gzip
import asyncio
import json
import logging
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI

from bothub.gateway import GatewayProvider, create_gateway_router, validate_base_url
from bothub.secrets import issue_gateway_token

pytestmark = pytest.mark.pure


def test_provider_binding_requires_owner_status():
    with pytest.raises(TypeError):
        GatewayProvider("p1", "openai_api", "https://api.example", "key")


def test_gateway_openapi_operation_ids_are_unique():
    async def lookup(bot_id, provider_id):
        return None

    async def record(*usage):
        pass

    app = FastAPI()
    app.include_router(create_gateway_router(lookup, record, "secret"))
    operations = [operation for path in app.openapi()["paths"].values()
                  for operation in path.values() if operation.get("operationId", "").startswith("proxy_")]
    operation_ids = [operation["operationId"] for operation in operations]
    assert not operations
    assert len(operation_ids) == len(set(operation_ids))


async def public_resolver(host, port):
    return [(None, None, None, None, ("8.8.8.8", port))]


@pytest.mark.asyncio
async def test_validate_base_url_rejects_unsafe_addresses_and_syntax():
    for url in ("http://api.example", "https://user:pass@api.example", "https://api.example/#x",
                "https://api.example/?key=x", "https://api.example:99999", "https://127.0.0.1",
                "https://[::1]", "https://api.example\\@evil.example", "https://api.example."):
        with pytest.raises(ValueError):
            await validate_base_url(url, resolver=public_resolver)
    async def mixed(host, port):
        return [(None, None, None, None, ("8.8.8.8", port)),
                (None, None, None, None, ("169.254.169.254", port))]
    with pytest.raises(ValueError):
        await validate_base_url("https://api.example", resolver=mixed)


@pytest.mark.asyncio
async def test_http_allowed_only_for_admin_approved_private_host():
    async def private(host, port):
        return [(None, None, None, None, ("192.168.1.20", port))]
    with pytest.raises(ValueError):
        await validate_base_url("http://ollama.lan:11434", resolver=private)
    assert await validate_base_url("http://ollama.lan:11434", allowed_private_hosts=["ollama.lan"], resolver=private) == "http://ollama.lan:11434"
    with pytest.raises(ValueError):
        await validate_base_url("http://ollama.lan:11434", allowed_private_hosts=["ollama.lan"], resolver=public_resolver)

async def gateway(kind, handler, *, provider_id="p1", owner_active=True, allowed_models=None, betas=None, router_options=None):
    observed = []
    async def lookup(bot_id, wanted):
        models = ["gpt-test", "claude-test", "gemini-test", "gpt", "claude", "gemini"] if allowed_models is None else allowed_models
        return GatewayProvider(provider_id, kind, "https://api.example", "real-key", owner_active, models,
                               betas) if bot_id == "bot-1" else None
    async def record(*usage):
        observed.append(usage)
    app = FastAPI()
    options = {"resolver": public_resolver, **(router_options or {})}
    router = create_gateway_router(lookup, record, "hmac-secret", transport=httpx.MockTransport(handler), **options)
    app.include_router(router)
    class TestClient(httpx.AsyncClient):
        async def __aenter__(self):
            await router.startup()
            return await super().__aenter__()

        async def __aexit__(self, *args):
            try:
                return await super().__aexit__(*args)
            finally:
                await router.shutdown()
    client = TestClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    client.gateway_router = router
    token = issue_gateway_token("bot-1", provider_id, "hmac-secret", ttl=(router_options or {}).get("max_turn_seconds", 1800) + 120)
    return client, {"Authorization": f"Bearer {token}"}, observed


@pytest.mark.asyncio
async def test_openai_key_substitution_usage_and_spoofing():
    def upstream(request):
        assert request.headers["authorization"] == "Bearer real-key"
        assert "x-api-key" not in request.headers
        assert "x-goog-api-key" not in request.headers
        assert "cookie" not in request.headers
        assert "key=evil" not in str(request.url)
        return httpx.Response(200, json={"model": "gpt-test", "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
    client, auth, observed = await gateway("openai_api", upstream)
    async with client:
        response = await client.post("/gateway/p1/v1/chat/completions?key=evil", headers=auth | {
            "cookie": "secret"}, json={"model": "gpt-test"})
        assert response.status_code == 200
        assert observed == [("bot-1", "p1", "gpt-test", 2, 3, 0, 0, 200)]
        assert (await client.post("/gateway/p1/v1/chat/completions", headers=auth | {
            "x-api-key": "evil"})).status_code == 401
        assert (await client.post("/gateway/p2/v1/chat/completions", headers=auth)).status_code == 401
        assert (await client.post("/gateway/p1/v1/files", headers=auth)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,path,expected", [
    ("anthropic_api", "v1/messages", "x-api-key"),
    ("google_api", "v1beta/models/gemini-test:generateContent", "x-goog-api-key"),
])
async def test_provider_key_headers(kind, path, expected):
    def upstream(request):
        assert request.headers[expected] == "real-key"
        assert request.headers.get("authorization") is None
        assert request.headers.get("x-api-key") == ("real-key" if expected == "x-api-key" else None)
        return httpx.Response(200, json={"usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 5}})
    client, auth, observed = await gateway(kind, upstream)
    async with client:
        response = await client.post(f"/gateway/p1/{path}", headers=auth, json={"model": "gpt-test" if kind == "anthropic_api" else "gemini-test"})
        assert response.status_code == 200
        assert observed == [("bot-1", "p1", "gpt-test" if kind == "anthropic_api" else "gemini-test", 4, 5, 0, 0, 200)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,path,token_header", [
    ("anthropic_api", "v1/messages", "x-api-key"),
    ("google_api", "v1beta/models/gemini-test:generateContent", "x-goog-api-key"),
])
async def test_native_client_auth_headers_are_substituted(kind, path, token_header):
    def upstream(request):
        assert request.headers[token_header] == "real-key"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={})
    client, auth, _ = await gateway(kind, upstream)
    token = auth["Authorization"].removeprefix("Bearer ")
    async with client:
        response = await client.post(f"/gateway/p1/{path}", headers={token_header: token}, json={"model": "claude-test" if kind == "anthropic_api" else "gemini-test"})
        assert response.status_code == 200
        assert (await client.post(f"/gateway/p1/{path}", headers={
            "x-api-key": token, "x-goog-api-key": "different"}, json={"model": "claude-test"})).status_code == 401


@pytest.mark.asyncio
async def test_redirect_never_followed():
    calls = 0
    def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
    client, auth, observed = await gateway("openai_api", upstream)
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
        assert response.status_code == 502
        assert calls == 1
        assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 502)]


@pytest.mark.asyncio
async def test_request_body_limit_applies_while_streaming():
    calls = 0
    def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)
    async def oversized():
        yield b"x" * (10 * 1024 * 1024)
        yield b"x"
    client, auth, _ = await gateway("openai_api", upstream)
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, content=oversized())
        assert response.status_code == 413
        assert calls == 0


@pytest.mark.asyncio
async def test_sse_bytes_and_usage_recorded():
    payload = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-test","usage":{"input_tokens":7}}}\n\n'
               b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":11}}\n\n')
    class EventStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
    def upstream(request):
        return httpx.Response(200, stream=EventStream(), headers={"content-type": "text/event-stream"})
    client, auth, observed = await gateway("anthropic_api", upstream)
    async with client:
        response = await client.post("/gateway/p1/v1/messages", headers=auth, json={"model": "claude-test", "stream": True})
        assert response.status_code == 200
        assert response.content == payload
        assert observed == [("bot-1", "p1", "claude-test", 7, 11, 0, 0, 200)]


@pytest.mark.asyncio
async def test_gzip_sse_is_decoded_and_usage_recorded():
    payload = b'data: {"model":"gpt-test","usage":{"prompt_tokens":13,"completion_tokens":17}}\n\n'
    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield gzip.compress(payload)
    def upstream(request):
        return httpx.Response(200, stream=CompressedStream(), headers={
            "content-type": "text/event-stream", "content-encoding": "gzip"})
    client, auth, observed = await gateway("openai_api", upstream)
    async with client:
        response = await client.post("/gateway/p1/v1/chat/completions", headers=auth, json={"model": "gpt-test", "stream": True})
        assert response.status_code == 200
        assert response.content == payload
        assert "content-encoding" not in response.headers
        assert observed == [("bot-1", "p1", "gpt-test", 13, 17, 0, 0, 200)]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_active", [False, None, "false", 1])
async def test_disabled_owner_is_denied_before_upstream(owner_active):
    calls = 0
    def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)
    client, auth, observed = await gateway("openai_api", upstream, owner_active=owner_active)
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
        assert response.status_code == 403
        assert calls == 0
        assert observed == []


@pytest.mark.asyncio
async def test_owner_revocation_blocks_next_request_with_same_token():
    active = True
    calls = 0

    async def lookup(bot_id, provider_id):
        return GatewayProvider(provider_id, "openai_api", "https://api.example", "real-key", active, ["gpt"])

    async def record(*usage):
        pass

    def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    app = FastAPI()
    router = create_gateway_router(lookup, record, "hmac-secret", transport=httpx.MockTransport(upstream), resolver=public_resolver)
    app.include_router(router)
    token = issue_gateway_token("bot-1", "p1", "hmac-secret")
    await router.startup()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.post("/gateway/p1/v1/responses", headers=headers, json={"model": "gpt"})).status_code == 200
        active = False
        assert (await client.post("/gateway/p1/v1/responses", headers=headers, json={"model": "gpt"})).status_code == 403
    assert calls == 1
    await router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,path,usage,expected", [
    ("anthropic_api", "v1/messages", {"model": "claude", "usage": {
        "input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 6,
        "cache_creation_input_tokens": 2}}, ("bot-1", "p1", "gpt-test", 10, 4, 6, 2, 200)),
    ("openai_api", "v1/responses", {"model": "gpt", "usage": {
        "input_tokens": 10, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 6}}},
        ("bot-1", "p1", "gpt-test", 10, 4, 6, 0, 200)),
    ("google_api", "v1beta/models/gemini-test:generateContent", {"modelVersion": "gemini", "usageMetadata": {
        "promptTokenCount": 10, "candidatesTokenCount": 4, "cachedContentTokenCount": 6}},
        ("bot-1", "p1", "gemini-test", 10, 4, 6, 0, 200)),
])
async def test_usage_cache_counts(kind, path, usage, expected):
    client, auth, observed = await gateway(kind, lambda request: httpx.Response(200, json=usage))
    async with client:
        response = await client.post(f"/gateway/p1/{path}", headers=auth, json={"model": "gemini-test" if kind == "google_api" else "gpt-test"})
        assert response.status_code == 200
        assert observed == [expected]


@pytest.mark.asyncio
async def test_stream_usage_cache_counts():
    payload = (b'data: {"type":"message_start","message":{"model":"claude","usage":{"input_tokens":10,"cache_read_input_tokens":6,"cache_creation_input_tokens":2}}}\n\n'
               b'data: {"type":"message_delta","usage":{"output_tokens":4}}\n\n')
    class EventStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
    client, auth, observed = await gateway("anthropic_api", lambda request: httpx.Response(
        200, stream=EventStream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/messages", headers=auth, json={"model": "claude", "stream": True})
        assert response.status_code == 200
        assert observed == [("bot-1", "p1", "claude", 10, 4, 6, 2, 200)]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"model":[]}',
    b'{"model":"foreign"}', b'{"model":"gpt-test","model":"foreign"}',
    b'{"model":"gpt-test","nested":{"a":1,"a":2}}',
    b'{"model":"gpt-test","temperature":NaN}',
    b'{"model":"gpt-test","temperature":Infinity}',
    b'{"model":"gpt-test","temperature":-Infinity}',
    pytest.param(b'{"model":"gpt-test","nested":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}', id="deeply_nested")])
async def test_model_allowlist_rejects_malformed_body(payload):
    calls = []
    client, auth, observed = await gateway("openai_api", lambda request: calls.append(request) or httpx.Response(200))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, content=payload)
    assert response.status_code == 403
    assert response.json()["detail"] == "model_disabled"
    assert not calls
    assert observed == []


@pytest.mark.asyncio
async def test_models_route_filtered_without_upstream():
    client, auth, observed = await gateway("openai_api", lambda request: pytest.fail("upstream called"), allowed_models=["gpt-test"])
    async with client:
        response = await client.get("/gateway/p1/v1/models", headers=auth)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["gpt-test"]


@pytest.mark.asyncio
async def test_empty_model_allowlist_denies_requests_and_lists_no_models():
    client, auth, observed = await gateway("openai_api", lambda request: pytest.fail("upstream called"), allowed_models=[])
    async with client:
        listing = await client.get("/gateway/p1/v1/models", headers=auth)
        denied = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert listing.status_code == 200
    assert listing.json()["data"] == []
    assert denied.status_code == 403
    assert observed == []


@pytest.mark.asyncio
async def test_string_model_allowlist_cannot_grant_substring():
    client, auth, observed = await gateway("openai_api", lambda request: pytest.fail("upstream called"),
                                           allowed_models="gpt-test")
    async with client:
        listing = await client.get("/gateway/p1/v1/models", headers=auth)
        denied = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt"})
    assert listing.status_code == 403
    assert denied.status_code == 403
    assert observed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["foreign", "gemini-test%2f..", "gemini-test%3astreamGenerateContent", "gemini-test%252f.."] )
async def test_google_path_model_tricks(name):
    calls = []
    client, auth, _ = await gateway("google_api", lambda request: calls.append(request) or httpx.Response(200), allowed_models=["gemini-test"])
    async with client:
        response = await client.post(f"/gateway/p1/v1beta/models/{name}:generateContent", headers=auth, json={})
    assert response.status_code == 403
    assert not calls


@pytest.mark.asyncio
async def test_chat_stream_requests_usage_and_beta_header_blocked():
    def upstream(request):
        assert json.loads(request.content)["stream_options"]["include_usage"] is True
        assert "anthropic-beta" not in request.headers
        return httpx.Response(200, json={})
    client, auth, _ = await gateway("openai_api", upstream)
    async with client:
        assert (await client.post("/gateway/p1/v1/chat/completions", headers=auth, json={"model": "gpt-test", "stream": True, "stream_options": {"include_usage": False}})).status_code == 200
    client, auth, _ = await gateway("anthropic_api", lambda request: httpx.Response(200) if "anthropic-beta" not in request.headers else pytest.fail("unapproved beta reached upstream"))
    async with client:
        response = await client.post("/gateway/p1/v1/messages", headers=auth | {"anthropic-beta": "secret-beta"}, json={"model": "claude-test"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_large_sse_event_usage_and_error_accounting(caplog):
    caplog.set_level(logging.DEBUG)
    payload = b'data: {"model":"gpt-test","padding":"' + b'x' * (1024 * 1024 + 1) + b'","usage":{"input_tokens":9}}\n\n'
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in (payload[:500000], payload[500000:]):
                yield part
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"}))
    async with client:
        assert (await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})).status_code == 200
    assert observed == [("bot-1", "p1", "gpt-test", 9, 0, 0, 0, 200)]
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(401, text="real-key rejected"))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 401
    assert b"real-key" not in response.content
    assert "real-key" not in caplog.text
    assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 401)]


@pytest.mark.asyncio
async def test_rebinding_pins_validated_ip():
    calls = 0
    async def resolver(host, port):
        nonlocal calls
        calls += 1
        return [(None, None, None, None, ("8.8.8.8" if calls == 1 else "127.0.0.1", port))]
    observed_requests = []
    client, auth, _ = await gateway("openai_api", lambda request: observed_requests.append(request) or httpx.Response(200),
                                    router_options={"resolver": resolver})
    async with client:
        assert (await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})).status_code == 200
    assert calls == 1
    assert observed_requests and urlsplit(str(observed_requests[0].url)).hostname == "8.8.8.8"
    assert observed_requests[0].headers["host"] == "api.example"
    assert observed_requests[0].extensions["sni_hostname"] == "api.example"


@pytest.mark.asyncio
async def test_disallowed_special_addresses():
    for address in ("127.0.0.1", "169.254.169.254", "224.0.0.1", "64:ff9b::a9fe:a9fe", "fd00:ec2::254"):
        async def resolver(host, port):
            return [(None, None, None, None, (address, port))]
        with pytest.raises(ValueError):
            await validate_base_url("https://ollama.lan", allowed_private_hosts=["ollama.lan"], resolver=resolver)


@pytest.mark.asyncio
async def test_system_dns_rejects_localhost_without_resolver_mock():
    with pytest.raises(ValueError, match="non-public address"):
        await validate_base_url("https://localhost")

@pytest.mark.asyncio
async def test_stream_usage_with_crlf_split_between_chunks():
    class SplitStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"usage":{"input_tokens":7}}\r'
            yield b'\n\r'
            yield b'\n'
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=SplitStream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert observed == [("bot-1", "p1", "gpt-test", 7, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_parallel_limit_records_rejection():
    entered = asyncio.Event()
    release = asyncio.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await release.wait()
            yield b'data: {"usage":{"input_tokens":1}}\n\n'
    client, auth, observed = await gateway(
        "openai_api", lambda request: httpx.Response(200, stream=SlowStream(),
        headers={"content-type": "text/event-stream"}),
        router_options={"max_parallel_per_bot": 1})
    async with client:
        first = asyncio.create_task(client.post("/gateway/p1/v1/responses", headers=auth,
                                                 json={"model": "gpt-test"}))
        await entered.wait()
        second = await client.post("/gateway/p1/v1/responses", headers=auth,
                                   json={"model": "gpt-test"})
        assert second.status_code == 429
        release.set()
        assert (await first).status_code == 200
    assert sorted(row[-1] for row in observed) == [200]
    assert client.gateway_router.pre_route_counts == {(429, "gateway concurrency limit"): 1}


@pytest.mark.asyncio
async def test_stream_deadline_records_zero_usage():
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1.1)
            yield b'data: {"usage":{"input_tokens":99}}\n\n'
    client, auth, observed = await gateway(
        "openai_api", lambda request: httpx.Response(200, stream=SlowStream(),
        headers={"content-type": "text/event-stream"}), router_options={"max_turn_seconds": 1})
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200  # Headers were committed before the stream expired.
    assert response.content == b""
    assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 504)]


@pytest.mark.asyncio
async def test_stream_disconnect_records_partial_usage():
    class DisconnectStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"model":"gpt-test","usage":{"input_tokens":3}}\n\n'
            raise asyncio.CancelledError
    client, auth, observed = await gateway(
        "openai_api", lambda request: httpx.Response(200, stream=DisconnectStream(),
        headers={"content-type": "text/event-stream"}))
    async with client:
        # ASGITransport asserts when the response task is cancelled before completion.
        with pytest.raises(AssertionError):
            await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert observed == [("bot-1", "p1", "gpt-test", 3, 0, 0, 0, 499)]


@pytest.mark.asyncio
async def test_pinned_request_preserves_host_and_sni():
    def upstream(request):
        assert urlsplit(str(request.url)).hostname == "8.8.8.8"
        assert request.headers["host"] == "api.example"
        assert request.extensions["sni_hostname"] == "api.example"
        return httpx.Response(200, json={})
    client, auth, _ = await gateway("openai_api", upstream)
    async with client:
        assert (await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})).status_code == 200


@pytest.mark.asyncio
async def test_allowed_anthropic_beta_forwarded():
    def upstream(request):
        assert request.headers["anthropic-beta"] == "approved-beta"
        return httpx.Response(200, json={})
    client, auth, _ = await gateway("anthropic_api", upstream, betas=["approved-beta"])
    async with client:
        response = await client.post("/gateway/p1/v1/messages", headers=auth | {"anthropic-beta": "approved-beta"}, json={"model": "claude-test"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_upstream_network_error_is_generic_and_not_recorded(caplog):
    caplog.set_level(logging.DEBUG)
    def upstream(request):
        raise httpx.ConnectError("real-key connection detail")
    client, auth, observed = await gateway("openai_api", upstream)
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 502
    assert b"real-key" not in response.content
    assert "real-key" not in caplog.text
    assert observed == []


@pytest.mark.asyncio
async def test_success_body_and_headers_never_echo_provider_key():
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, content=b'{"echo":"real-key","model":"real-key","usage":{"input_tokens":2}}',
        headers={"content-type": "application/json; note=real-key", "retry-after": "real-key"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert b"real-key" not in response.content
    assert "real-key" not in str(response.headers)
    assert response.json()["echo"] == "***"
    assert observed == [("bot-1", "p1", "gpt-test", 2, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_sse_key_redacted_across_chunk_boundaries():
    class SecretStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"echo":"real-'
            yield b'key","usage":{"input_tokens":3}}\n\n'

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=SecretStream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert b"real-key" not in response.content
    assert b'"echo":"***"' in response.content
    assert observed == [("bot-1", "p1", "gpt-test", 3, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_slow_upload_deadline_records_failure():
    async def slow_upload():
        await asyncio.sleep(1.1)
        yield b'{"model":"gpt-test"}'
    client, auth, observed = await gateway("openai_api", lambda request: pytest.fail("upstream called"),
                                           router_options={"max_turn_seconds": 1})
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, content=slow_upload())
    assert response.status_code == 504
    assert observed == []


@pytest.mark.asyncio
async def test_mixed_anthropic_beta_is_filtered():
    client, auth, _ = await gateway("anthropic_api", lambda request: httpx.Response(200) if request.headers.get("anthropic-beta") == "approved-beta" else pytest.fail("wrong beta header"), betas=["approved-beta"])
    async with client:
        response = await client.post("/gateway/p1/v1/messages", headers=auth | {
            "anthropic-beta": "forbidden-beta, approved-beta"}, json={"model": "claude-test"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_string_beta_allowlist_cannot_grant_substring():
    client, auth, _ = await gateway("anthropic_api", lambda request: httpx.Response(200) if "anthropic-beta" not in request.headers else pytest.fail("unapproved beta reached upstream"),
                                    betas="approved-beta")
    async with client:
        response = await client.post("/gateway/p1/v1/messages", headers=auth | {
            "anthropic-beta": "beta"}, json={"model": "claude-test"})
    assert response.status_code == 200


def test_provider_binding_repr_hides_api_key():
    binding = GatewayProvider("p", "openai_api", "https://example.org", "secret-key", True, ["gpt"])
    assert "secret-key" not in repr(binding)



@pytest.mark.asyncio
async def test_stream_read_error_records_failure_status():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("real-key upstream detail")
            yield b""

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=BrokenStream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200  # Headers were committed before the read failed.
    assert b"real-key" not in response.content
    assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 502)]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"type": "message_start", "message": "bad"},
    {"type": "response.completed", "response": ["bad"]},
])
async def test_malformed_usage_envelope_does_not_break_buffered_response(payload):
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(200, json=payload))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert response.json() == payload
    assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_malformed_usage_envelope_does_not_break_sse():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"message_start","message":"bad"}\n\n'
            yield b'data: {"usage":{"input_tokens":3}}\n\n'

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=Stream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert b'"input_tokens":3' in response.content
    assert observed == [("bot-1", "p1", "gpt-test", 3, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_unexpected_stream_error_records_failure_status():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"usage":{"input_tokens":3}}\n\n'
            raise ValueError("real-key upstream parser detail")

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=BrokenStream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert b"real-key" not in response.content
    assert observed == [("bot-1", "p1", "gpt-test", 3, 0, 0, 0, 502)]


@pytest.mark.asyncio
async def test_stream_close_error_still_records_status():
    class BrokenClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"usage":{"input_tokens":3}}\n\n'

        async def aclose(self):
            raise ValueError("real-key close detail")

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=BrokenClose(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert b"real-key" not in response.content
    assert observed == [("bot-1", "p1", "gpt-test", 3, 0, 0, 0, 502)]


@pytest.mark.asyncio
async def test_error_response_close_failure_releases_slot_and_records_usage():
    class BrokenClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"real-key"

        async def aclose(self):
            raise ValueError("real-key close detail")

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        401, stream=BrokenClose(), headers={"content-type": "text/plain"}),
        router_options={"max_parallel_per_bot": 1})
    async with client:
        first = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
        second = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert first.status_code == second.status_code == 401
    assert b"real-key" not in first.content + second.content
    assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 401)] * 2


@pytest.mark.asyncio
async def test_usage_callback_never_receives_provider_key_as_model():
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, json={"model": "provider-alias", "usage": {"input_tokens": 1}}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert b"provider-alias" in response.content
    assert observed == [("bot-1", "p1", "gpt-test", 1, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_sse_usage_callback_uses_requested_model():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"model":"provider-alias","usage":{"input_tokens":2}}\n\n'

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=Stream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert observed == [("bot-1", "p1", "gpt-test", 2, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_buffered_upstream_response_limit_is_configurable_and_incremental():
    class ChunkedStream(httpx.AsyncByteStream):
        def __init__(self):
            self.read_count = 0

        async def __aiter__(self):
            for chunk in (b"12345", b"6789", b"ignored"):
                self.read_count += 1
                yield chunk

    stream = ChunkedStream()
    client, auth, observed = await gateway(
        "openai_api",
        lambda request: httpx.Response(200, stream=stream),
        router_options={"max_upstream_response_bytes": 8},
    )
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 502
    assert stream.read_count == 2
    assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 502)]


def test_buffered_upstream_response_limit_defaults_to_32_mib():
    import inspect

    limit = inspect.signature(create_gateway_router).parameters["max_upstream_response_bytes"]
    assert limit.default == 32 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [
    {"input_tokens": 1_000_000_001},
    {"prompt_tokens": 1_000_000_001},
    {"promptTokenCount": 1_000_000_001},
    {"output_tokens": 1_000_000_001},
    {"completion_tokens": 1_000_000_001},
    {"candidatesTokenCount": 1_000_000_001},
    {"cache_read_input_tokens": 1_000_000_001},
    {"cachedContentTokenCount": 1_000_000_001},
    {"input_tokens_details": {"cached_tokens": 1_000_000_001}},
    {"prompt_tokens_details": {"cached_tokens": 1_000_000_001}},
    {"cache_creation_input_tokens": 1_000_000_001},
    {"total_tokens": 1_000_000_001},
    {"input_tokens": 2, "prompt_tokens": 1_000_000_001},
    {"input_tokens": 2, "input_tokens_details": {"cached_tokens": 1_000_000_001}},
])
async def test_buffered_upstream_usage_over_limit_is_502(usage):
    client, auth, observed = await gateway(
        "openai_api", lambda request: httpx.Response(200, json={"usage": usage}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 502
    assert observed == [("bot-1", "p1", "gpt-test", 0, 0, 0, 0, 502)]


@pytest.mark.asyncio
async def test_upstream_usage_limit_accepts_one_billion():
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, json={"usage": {"input_tokens": 1_000_000_000}}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert observed == [("bot-1", "p1", "gpt-test", 1_000_000_000, 0, 0, 0, 200)]


@pytest.mark.asyncio
async def test_first_sse_usage_over_limit_closes_empty_stream():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"usage":{"input_tokens":1000000001}}\n\n'
            yield b'data: {"usage":{"input_tokens":2}}\n\n'
    client, auth, observed = await gateway('openai_api', lambda request: httpx.Response(
        200, stream=Stream(), headers={'content-type':'text/event-stream'}))
    async with client:
        response = await client.post('/gateway/p1/v1/responses', headers=auth, json={'model':'gpt-test'})
    assert response.status_code == 200 and response.content == b''
    assert observed == [('bot-1','p1','gpt-test',0,0,0,0,502)]


@pytest.mark.asyncio
async def test_provider_key_cannot_be_recorded_as_usage_model():
    client, auth, observed = await gateway('openai_api', lambda request: httpx.Response(
        200, json={'type':'response.completed','response':{'model':'real-key','usage':{'input_tokens':2}}}))
    async with client:
        response = await client.post('/gateway/p1/v1/responses', headers=auth, json={'model':'gpt-test'})
    assert response.status_code == 200
    assert all('real-key' not in str(row) for row in observed)


@pytest.mark.asyncio
async def test_sse_usage_over_limit_accounts_502_and_terminates_after_headers():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"usage":{"input_tokens":3}}\n\n'
            yield b'data: {"usage":{"prompt_tokens":1000000001}}\n\n'
            yield b'data: {"usage":{"input_tokens":9}}\n\n'

    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(
        200, stream=Stream(), headers={"content-type": "text/event-stream"}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 200
    assert response.content.startswith(b'data: {"usage":{')
    assert b'1000000001' not in response.content
    assert b'"input_tokens":9' not in response.content
    assert observed == [("bot-1", "p1", "gpt-test", 3, 0, 0, 0, 502)]


@pytest.mark.asyncio
async def test_dns_deadline_records_failure():
    async def slow_resolver(host, port):
        await asyncio.sleep(1.1)
        return [(None, None, None, None, ("8.8.8.8", port))]
    client, auth, observed = await gateway("openai_api", lambda request: pytest.fail("upstream called"),
        router_options={"max_turn_seconds": 1, "resolver": slow_resolver})
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 504
    assert observed == []


@pytest.mark.asyncio
async def test_upstream_connection_deadline_does_not_record_unconfirmed_delivery():
    async def slow_upstream(request):
        await asyncio.sleep(1.1)
        return httpx.Response(200, json={})

    client, auth, observed = await gateway("openai_api", slow_upstream,
        router_options={"max_turn_seconds": 1})
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 504
    assert observed == []


@pytest.mark.asyncio
async def test_downstream_disconnect_records_usage_and_releases_slot():
    seen = []
    calls = 0
    async def lookup(bot_id, provider_id):
        return GatewayProvider(provider_id, "openai_api", "https://api.example", "real-key", True, ["gpt-test"])
    async def record(*usage):
        seen.append(usage)
    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"model":"gpt-test","usage":{"input_tokens":3}}\n\n'
            await asyncio.sleep(10)
    def upstream(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, stream=InterruptedStream(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={})
    app = FastAPI()
    router = create_gateway_router(lookup, record, "hmac-secret",
        resolver=public_resolver, transport=httpx.MockTransport(upstream), max_parallel_per_bot=1)
    app.include_router(router)
    await router.startup()
    token = issue_gateway_token("bot-1", "p1", "hmac-secret")
    body = b'{"model":"gpt-test"}'
    first_body_sent = False
    first_chunk_sent = asyncio.Event()
    async def receive():
        nonlocal first_body_sent
        if not first_body_sent:
            first_body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}
    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk_sent.set()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/gateway/p1/v1/responses", "raw_path": b"/gateway/p1/v1/responses",
        "query_string": b"", "root_path": "", "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"test"), (b"authorization", f"Bearer {token}".encode()),
                    (b"content-type", b"application/json")],
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    assert seen == [("bot-1", "p1", "gpt-test", 3, 0, 0, 0, 499)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        second = await client.post("/gateway/p1/v1/responses", headers={"authorization": f"Bearer {token}"},
                                   json={"model": "gpt-test"})
    assert second.status_code == 200
    assert calls == 2
    assert len(seen) == 2
    await router.shutdown()

@pytest.mark.asyncio
async def test_model_case_insensitive():
    calls = []
    client, auth, _ = await gateway("openai_api", lambda request: calls.append(request.content) or httpx.Response(200))
    async with client:
        for payload in (b'{"model":"gpt-test","Model":"foreign"}',
                        b'{"MODEL":"gpt-test"}', b'\xef\xbb\xbf{"model":"gpt-test"}',
                        '{"model":"gpt-test"}'.encode("utf-16"),
                        gzip.compress(b'{"model":"gpt-test"}')):
            response = await client.post("/gateway/p1/v1/responses", headers=auth, content=payload)
            assert response.status_code == 403
        encoded = await client.post("/gateway/p1/v1/responses", headers=auth | {"content-encoding": "gzip"},
                                    content=b'{"model":"gpt-test"}')
        assert encoded.status_code == 403
        response = await client.post("/gateway/p1/v1/responses", headers=auth,
                                     content=b'{ "model" : "gpt-test", "x": 1 }')
    assert response.status_code == 200
    assert calls == [b'{"model":"gpt-test","x":1}']


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,path,model,extra", [
    ("openai_api", "v1/responses", "gpt-test", ""),
    ("openai_api", "v1/chat/completions", "gpt-test", ',"stream":true'),
    ("google_api", "v1beta/models/gemini-test:generateContent", "gemini-test", ""),
])
async def test_json_surrogate_does_not_crash_gateway(kind, path, model, extra):
    forwarded = []
    client, auth, _ = await gateway(kind, lambda request: forwarded.append(request.content) or httpx.Response(200))
    payload = f'{{"model":"{model}","text":"\\ud800"{extra}}}'.encode()
    async with client:
        response = await client.post(f"/gateway/p1/{path}", headers=auth, content=payload)
    assert response.status_code == 200
    assert len(forwarded) == 1
    assert b'"text":"\\ud800"' in forwarded[0]


@pytest.mark.asyncio
async def test_google_body_model_must_match_path():
    calls = []
    client, auth, _ = await gateway("google_api", lambda request: calls.append(request.content) or httpx.Response(200))
    async with client:
        bad = await client.post("/gateway/p1/v1beta/models/gemini-test:generateContent", headers=auth,
                                json={"model": "foreign"})
        good = await client.post("/gateway/p1/v1beta/models/gemini-test:generateContent", headers=auth,
                                 content=b'{ "model" : "gemini-test" }')
    assert bad.status_code == 403
    assert good.status_code == 200
    assert calls == [b'{"model":"gemini-test"}']


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 0, "true", None, [], {}])
async def test_stream_type(value):
    calls = []
    client, auth, _ = await gateway("openai_api", lambda request: calls.append(request) or httpx.Response(200))
    async with client:
        response = await client.post("/gateway/p1/v1/chat/completions", headers=auth,
                                     json={"model": "gpt-test", "stream": value})
    assert response.status_code == 400
    assert not calls


@pytest.mark.asyncio
async def test_anthropic_beta_default():
    forwarded = []
    client, auth, _ = await gateway("anthropic_api", lambda request: forwarded.append(request.headers.get("anthropic-beta")) or httpx.Response(200))
    async with client:
        response = await client.post("/gateway/p1/v1/messages", headers=auth | {
            "anthropic-beta": "claude-code-20250219,unapproved-beta"}, json={"model": "claude-test"})
    assert response.status_code == 200
    assert forwarded == ["claude-code-20250219"]


@pytest.mark.asyncio
async def test_error_upstream():
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(429, json={
        "error": {"type": "quota", "code": "rate_limit", "message": "real-key " + "x" * 3000,
                  "request_id": "private"}}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "quota"
    assert response.json()["error"]["code"] == "rate_limit"
    assert "real-key" not in response.text
    assert "***" in response.text
    assert len(response.json()["error"]["message"]) <= 2048
    assert "request_id" not in response.text
    assert observed[-1][-1] == 429


@pytest.mark.asyncio
async def test_client_lifecycle():
    async def lookup(bot_id, provider_id):
        return GatewayProvider(provider_id, "openai_api", "https://api.example", "real-key", True, ["gpt-test"])
    async def record(*usage):
        pass
    router = create_gateway_router(lookup, record, "secret", transport=httpx.MockTransport(lambda request: httpx.Response(200)),
                                   resolver=public_resolver)
    assert router.client is None
    await router.startup()
    assert router.client is not None and not router.client.is_closed
    await router.shutdown()
    assert router.client is None
    app = FastAPI()
    app.include_router(router)
    async with app.router.lifespan_context(app):
        assert router.client is not None and not router.client.is_closed
    assert router.client is None


@pytest.mark.asyncio
async def test_ttl_unified():
    calls = []
    client, auth, _ = await gateway("openai_api", lambda request: calls.append(request) or httpx.Response(200),
                                    router_options={"token_ttl": 900})
    token = client.gateway_router.issue_token("bot-1", "p1")
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers={"Authorization": f"Bearer {token}"},
                                     json={"model": "gpt-test"})
    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_per_bot_turn_ttl_signed_expiry():
    client, _, _ = await gateway("openai_api", lambda request: httpx.Response(200),
                                 router_options={"max_turn_seconds": 1, "token_ttl": 2})
    token = client.gateway_router.issue_token("bot-1", "p1", max_turn_seconds=3600)
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers={"Authorization": f"Bearer {token}"},
                                     json={"model": "gpt-test"})
    assert response.status_code == 200


@pytest.mark.parametrize("deadline", [0, -1, True, 1.5])
def test_per_bot_token_deadline_rejects_invalid_values(deadline):
    async def lookup(bot_id, provider_id):
        return None

    async def record(*usage):
        pass

    router = create_gateway_router(lookup, record, "secret")
    with pytest.raises(ValueError):
        router.issue_token("bot-1", "p1", max_turn_seconds=deadline)


@pytest.mark.asyncio
async def test_accounting_pre_route():
    client, auth, observed = await gateway("openai_api", lambda request: httpx.Response(200),
                                           router_options={"max_parallel_per_bot": 1})
    async with client:
        missing = await client.post("/gateway/p1/v1/responses", json={"model": "gpt-test"})
        unsupported = await client.post("/gateway/p1/v1/files", headers=auth, json={"model": "gpt-test"})
        wrong_method = await client.get("/gateway/p1/v1/responses", headers=auth)
        put = await client.put("/gateway/p1/v1/responses", headers=auth)
    assert [missing.status_code, unsupported.status_code, wrong_method.status_code, put.status_code] == [401, 404, 404, 405]
    assert observed == []
    assert client.gateway_router.pre_route_counts == {(401, "one gateway token is required"): 1, (404, "unsupported gateway route"): 2,
                                                      (405, "unsupported gateway method"): 1}

@pytest.mark.asyncio
async def test_owner_connection_limit_across_bots():
    entered = asyncio.Event()
    release = asyncio.Event()
    usage = []

    async def lookup(bot_id, provider_id):
        return GatewayProvider(provider_id, "openai_api", "https://api.example", "real-key", True,
                               ["gpt-test"], owner_id="same-owner")

    async def record(*row):
        usage.append(row)

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await release.wait()
            yield b'data: {"usage":{"input_tokens":1}}\n\n'

    router = create_gateway_router(lookup, record, "secret", resolver=public_resolver,
                                   max_connections_per_user=1, max_parallel_per_bot=2,
                                   transport=httpx.MockTransport(lambda request: httpx.Response(
                                       200, stream=SlowStream(), headers={"content-type": "text/event-stream"})))
    app = FastAPI()
    app.include_router(router)
    await router.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/gateway/p1/v1/responses", headers={
                "Authorization": f"Bearer {router.issue_token('bot-1', 'p1')}"}, json={"model": "gpt-test"}))
            await entered.wait()
            second = await client.post("/gateway/p1/v1/responses", headers={
                "Authorization": f"Bearer {router.issue_token('bot-2', 'p1')}"}, json={"model": "gpt-test"})
            assert second.status_code == 429
            release.set()
            assert (await first).status_code == 200
    finally:
        await router.shutdown()
    assert router.pre_route_counts == {(429, "gateway connection limit"): 1}
    assert len(usage) == 1

@pytest.mark.asyncio
async def test_error_upstream_uses_scalar_fields_and_utf8_byte_cap():
    client, auth, _ = await gateway("openai_api", lambda request: httpx.Response(422, json={
        "error": {"type": {"nested": "private"}, "code": ["private"], "message": "界" * 2000}}))
    async with client:
        response = await client.post("/gateway/p1/v1/responses", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 422
    assert set(response.json()["error"]) == {"message"}
    assert len(response.json()["error"]["message"].encode("utf-8")) <= 2048

@pytest.mark.asyncio
async def test_turn_scoped_gateway_token_rejects_old_turn_and_records_exact_turn():
    observed=[]
    active={'turn-1'}
    async def lookup(bot, provider):
        return GatewayProvider('p1','openai_api','https://api.example','real-key',True,['gpt-test'])
    async def authorize(bot, provider, turn):
        return bot=='bot-1' and provider=='p1' and turn in active
    async def record(*args): observed.append(args)
    router=create_gateway_router(lookup,record,'secret',resolver=public_resolver,
        transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'model':'gpt-test','usage':{'prompt_tokens':2}})),
        turn_authorize=authorize)
    app=FastAPI(); app.include_router(router)
    await router.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            token=router.issue_token('bot-1','p1',turn_id='turn-1')
            headers={'Authorization':'Bearer '+token}
            response=await client.post('/gateway/p1/v1/chat/completions',headers=headers,json={'model':'gpt-test'})
            assert response.status_code==200
            assert observed[0][-1]=='turn-1'
            active.clear()
            response=await client.post('/gateway/p1/v1/chat/completions',headers=headers,json={'model':'gpt-test'})
            assert response.status_code==401
            assert len(observed)==1
    finally:
        await router.shutdown()
