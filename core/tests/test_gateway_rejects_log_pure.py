"""Шлюз: отказы до маршрутизации считаются по паре (статус, detail), раз в 60 с уходят в лог; 401/403 апстрима логируются.

По одному коду 401 нельзя понять, что именно не так: токен, ход бота или число токенов в заголовках. Ответ апстрима
(«User not found» у OpenRouter) раньше был виден только CLI в контейнере бота."""
import json
import logging

import httpx
import pytest
from fastapi import FastAPI

import bothub.gateway as gateway_module
from bothub.gateway import GatewayProvider, create_gateway_router
from test_gateway import gateway, public_resolver

pytestmark = pytest.mark.pure

REJECTS = "gateway pre-route rejects"
UPSTREAM = "gateway upstream rejected"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(gateway_module, "pre_route_clock", fake)
    return fake


def reject_lines(caplog):
    return [record for record in caplog.records if record.getMessage().startswith(REJECTS)]


async def reject_five_ways(client, auth):
    await client.post("/gateway/p1/v1/responses", json={"model": "gpt-test"})                                    # нет токена
    await client.post("/gateway/p1/v1/responses", headers={"Authorization": "Bearer gw:x:p1:9:bad"}, json={})   # чужая подпись
    await client.post("/gateway/p1/v1/responses", headers={"Authorization": "Basic abc"}, json={})              # не Bearer
    await client.post("/gateway/p1/v1/files", headers=auth, json={"model": "gpt-test"})                          # нет маршрута
    await client.put("/gateway/p1/v1/responses", headers=auth)                                                   # метод


async def test_counts_are_split_by_status_and_detail():
    client, auth, _ = await gateway("openai_api", lambda request: httpx.Response(200))
    async with client:
        await reject_five_ways(client, auth)
        await client.post("/gateway/p1/v1/responses", json={"model": "gpt-test"})
    assert client.gateway_router.pre_route_counts == {
        (401, "one gateway token is required"): 2,
        (401, "invalid gateway token"): 1,
        (401, "invalid gateway authorization"): 1,
        (404, "unsupported gateway route"): 1,
        (405, "unsupported gateway method"): 1}


async def test_inactive_turn_is_a_separate_401_reason():
    async def lookup(bot, provider):
        return GatewayProvider("p1", "openai_api", "https://api.example", "real-key", True, ["gpt-test"])

    async def authorize(bot, provider, turn):
        return False

    async def record(*usage):
        pass
    router = create_gateway_router(lookup, record, "secret", resolver=public_resolver,
                                   transport=httpx.MockTransport(lambda request: httpx.Response(200)), turn_authorize=authorize)
    app = FastAPI()
    app.include_router(router)
    await router.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer " + router.issue_token("bot-1", "p1", turn_id="turn-1")}
            assert (await client.post("/gateway/p1/v1/responses", headers=headers, json={"model": "gpt-test"})).status_code == 401
            assert (await client.post("/gateway/p1/v1/responses", headers={"Authorization": "Bearer nope"}, json={})).status_code == 401
    finally:
        await router.shutdown()
    assert router.pre_route_counts == {(401, "turn is not active"): 1, (401, "invalid gateway token"): 1}


async def test_the_log_line_comes_once_a_minute_with_status_and_reason_keys(clock, caplog):
    caplog.set_level(logging.INFO, logger="bothub.gateway")
    client, auth, _ = await gateway("openai_api", lambda request: httpx.Response(200))
    async with client:
        await reject_five_ways(client, auth)
        assert reject_lines(caplog) == []  # минута ещё не прошла
        clock.now += 59
        await client.post("/gateway/p1/v1/responses", json={"model": "gpt-test"})
        assert reject_lines(caplog) == []
        clock.now += 1
        await client.post("/gateway/p1/v1/responses", json={"model": "gpt-test"})
        lines = reject_lines(caplog)
        assert len(lines) == 1
        assert lines[0].getMessage() == REJECTS + ": " + str({
            "401 one gateway token is required": 3, "401 invalid gateway token": 1, "401 invalid gateway authorization": 1,
            "404 unsupported gateway route": 1, "405 unsupported gateway method": 1})
        await client.post("/gateway/p1/v1/responses", json={"model": "gpt-test"})  # следующая строка не раньше чем через 60 с
        assert len(reject_lines(caplog)) == 1
        clock.now += 60
        await client.post("/gateway/p1/v1/files", headers=auth, json={})
        assert len(reject_lines(caplog)) == 2
        assert "404 unsupported gateway route': 2" in reject_lines(caplog)[1].getMessage()


# ---- ответ апстрима 401/403 ------------------------------------------------------------------------------

def upstream_records(caplog):
    return [record for record in caplog.records if record.getMessage() == UPSTREAM]


@pytest.mark.parametrize("status", [401, 403])
async def test_upstream_rejection_is_logged_without_key_or_headers(caplog, status):
    caplog.set_level(logging.DEBUG)

    def upstream(request):
        return httpx.Response(status, json={"error": {"message": "User not found. real-key", "code": status}},
                              headers={"set-cookie": "session=secret-cookie"})
    client, auth, observed = await gateway("openai_compatible", upstream)
    async with client:
        response = await client.post("/gateway/p1/v1/chat/completions", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == status and response.json()["error"]["code"] == str(status)
    found = upstream_records(caplog)
    assert len(found) == 1 and found[0].levelno == logging.WARNING
    record = found[0]
    assert record.provider_id == "p1" and record.status == status
    assert "User not found." in record.body and len(record.body) <= 200
    assert "real-key" not in caplog.text and "secret-cookie" not in caplog.text and "Bearer" not in caplog.text
    assert "real-key" not in json.dumps(record.__dict__, default=str)
    assert observed and observed[-1][-1] == status  # учёт расхода прежний


async def test_upstream_body_is_cut_to_200_characters(caplog):
    caplog.set_level(logging.DEBUG)
    client, auth, _ = await gateway("openai_compatible", lambda request: httpx.Response(
        401, json={"error": {"message": "x" * 5000}}))
    async with client:
        await client.post("/gateway/p1/v1/chat/completions", headers=auth, json={"model": "gpt-test"})
    assert len(upstream_records(caplog)[0].body) == 200


async def test_upstream_rejection_with_a_non_json_body_is_logged_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    client, auth, _ = await gateway("openai_compatible", lambda request: httpx.Response(
        403, text="<html>Forbidden for key real-key</html>", headers={"content-type": "text/html"}))
    async with client:
        response = await client.post("/gateway/p1/v1/chat/completions", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == 403
    body = upstream_records(caplog)[0].body
    assert "Forbidden" in body and "real-key" not in body and "***" in body


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_other_upstream_errors_are_not_logged_as_rejections(caplog, status):
    caplog.set_level(logging.DEBUG)
    client, auth, _ = await gateway("openai_compatible", lambda request: httpx.Response(status, json={"error": {"message": "no"}}))
    async with client:
        response = await client.post("/gateway/p1/v1/chat/completions", headers=auth, json={"model": "gpt-test"})
    assert response.status_code == status
    assert upstream_records(caplog) == []


async def test_successful_response_is_not_logged_as_a_rejection(caplog):
    caplog.set_level(logging.DEBUG)
    client, auth, _ = await gateway("openai_compatible", lambda request: httpx.Response(200, json={"usage": {"prompt_tokens": 1}}))
    async with client:
        await client.post("/gateway/p1/v1/chat/completions", headers=auth, json={"model": "gpt-test"})
    assert upstream_records(caplog) == []
