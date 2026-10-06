"""Private provider addresses: the per-provider allow_private flag and the always-forbidden ranges."""
import httpx
import pytest
from fastapi import FastAPI

from bothub.gateway import (GatewayProvider, PrivateAddressError, UnresolvedHostError, create_gateway_router,
                            validate_base_url)
from bothub.secrets import issue_gateway_token

pytestmark = pytest.mark.pure

PRIVATE = ("10.1.2.3", "172.16.5.5", "172.31.255.254", "192.168.1.20", "fd12:3456::1", "fc00::7", "100.64.0.1",
           "100.127.255.254")
FORBIDDEN = ("127.0.0.1", "::1", "169.254.169.254", "169.254.1.1", "fe80::1", "224.0.0.1", "ff02::1", "0.0.0.0", "::",
             "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "fd00:ec2::254", "100.100.100.200",
             "::10.0.0.5", "::192.168.1.20", "::1.2.3.4")


def resolving(*addresses):
    async def resolver(host, port):
        return [(None, None, None, None, (address, port)) for address in addresses]
    return resolver


@pytest.mark.parametrize("address", PRIVATE)
async def test_private_address_needs_the_flag(address):
    resolver = resolving(address)
    with pytest.raises(PrivateAddressError):
        await validate_base_url("https://llm.lan", resolver=resolver)
    assert await validate_base_url("https://llm.lan", resolver=resolver, allow_private=True) == "https://llm.lan"
    # http остаётся допустим только для одобренного приватного адреса
    with pytest.raises(PrivateAddressError):
        await validate_base_url("http://llm.lan:11434", resolver=resolver)
    assert await validate_base_url("http://llm.lan:11434", resolver=resolver, allow_private=True) == "http://llm.lan:11434"


@pytest.mark.parametrize("address", FORBIDDEN)
async def test_forbidden_addresses_are_refused_even_with_the_flag(address):
    for flag in (False, True):
        with pytest.raises(ValueError) as caught:
            await validate_base_url("https://llm.lan", resolver=resolving(address), allow_private=flag)
        assert not isinstance(caught.value, PrivateAddressError)
    with pytest.raises(ValueError):  # глобальное разрешение хоста тоже не снимает запрет
        await validate_base_url("https://llm.lan", allowed_private_hosts=["llm.lan"], resolver=resolving(address))
    with pytest.raises(ValueError):  # и литерал IP в адресе
        await validate_base_url(f"https://[{address}]" if ":" in address else f"https://{address}", allow_private=True)


async def test_one_forbidden_answer_outweighs_private_ones():
    resolver = resolving("192.168.1.20", "127.0.0.1")
    for flag in (False, True):
        with pytest.raises(ValueError) as caught:
            await validate_base_url("https://llm.lan", resolver=resolver, allow_private=flag)
        assert not isinstance(caught.value, PrivateAddressError)


async def test_http_never_reaches_public_addresses_and_flag_does_not_change_that():
    for flag in (False, True):
        with pytest.raises(ValueError) as caught:
            await validate_base_url("http://api.example", resolver=resolving("8.8.8.8"), allow_private=flag)
        assert not isinstance(caught.value, PrivateAddressError)


async def test_public_address_does_not_need_the_flag_and_mixed_answers_do():
    assert await validate_base_url("https://api.example", resolver=resolving("8.8.8.8")) == "https://api.example"
    with pytest.raises(PrivateAddressError):
        await validate_base_url("https://api.example", resolver=resolving("8.8.8.8", "10.0.0.5"))
    assert await validate_base_url("https://api.example", resolver=resolving("8.8.8.8", "10.0.0.5"),
                                   allow_private=True) == "https://api.example"


async def test_unresolved_hostname_is_its_own_error():
    async def failing(host, port):
        return []
    with pytest.raises(UnresolvedHostError):
        await validate_base_url("https://nowhere.example", resolver=failing)


async def test_global_host_allowance_still_works_and_is_per_host():
    resolver = resolving("192.168.1.20")
    assert await validate_base_url("http://llm.lan", allowed_private_hosts=["llm.lan"], resolver=resolver) == "http://llm.lan"
    with pytest.raises(PrivateAddressError):
        await validate_base_url("http://other.lan", allowed_private_hosts=["llm.lan"], resolver=resolver)


def gateway_client(provider, resolver, seen, **options):
    async def lookup(bot_id, provider_id):
        return provider

    async def record(*usage):
        pass

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"model": "gpt-test", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    app = FastAPI()
    router = create_gateway_router(lookup, record, "hmac-secret", transport=httpx.MockTransport(upstream),
                                   resolver=resolver, **options)
    app.include_router(router)
    token = issue_gateway_token("bot-1", "p1", "hmac-secret", ttl=1920)
    return router, app, {"Authorization": f"Bearer {token}"}


async def call_gateway(provider, resolver, **options):
    seen = []
    router, app, headers = gateway_client(provider, resolver, seen, **options)
    await router.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/gateway/p1/v1/chat/completions", headers=headers, json={"model": "gpt-test"})
    finally:
        await router.shutdown()
    return response, seen


def provider_at(base_url, *, allow_private, ips=("192.168.1.20",)):
    return GatewayProvider("p1", "openai_api", base_url, "real-key", True, ["gpt-test"], allow_private=allow_private,
                           allow_private_ips=tuple(ips))


async def test_gateway_honours_the_flag_of_the_provider():
    private = resolving("192.168.1.20")
    refused, seen = await call_gateway(provider_at("https://llm.lan", allow_private=False), private)
    assert refused.status_code == 502 and not seen
    allowed, seen = await call_gateway(provider_at("https://llm.lan", allow_private=True), private)
    assert allowed.status_code == 200 and len(seen) == 1
    assert seen[0].headers["host"] == "llm.lan" and seen[0].url.host == "192.168.1.20"
    http_allowed, seen = await call_gateway(provider_at("http://llm.lan:11434", allow_private=True), private)
    assert http_allowed.status_code == 200 and len(seen) == 1


@pytest.mark.parametrize("address", FORBIDDEN)
async def test_gateway_refuses_forbidden_addresses_with_the_flag(address):
    response, seen = await call_gateway(provider_at("https://llm.lan", allow_private=True), resolving(address))
    assert response.status_code == 502 and not seen


async def test_gateway_flag_follows_a_dns_change_to_a_forbidden_address():
    answers = iter(["192.168.1.20", "169.254.169.254"])

    async def rebinding(host, port):
        return [(None, None, None, None, (next(answers), port))]

    provider = provider_at("https://llm.lan", allow_private=True)
    first, seen = await call_gateway(provider, rebinding)
    second, seen_after = await call_gateway(provider, rebinding)
    assert first.status_code == 200 and second.status_code == 502 and not seen_after
