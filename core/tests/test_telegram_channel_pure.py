"""Чистые тесты сетевого адаптера Telegram без БД."""
from __future__ import annotations

import pytest
import logging


@pytest.mark.pure
async def test_send_message_long_text_fake(monkeypatch):
    """send_message вызывает Telegram API; в pure-тесте проверяем мок."""
    import bothub.channels_telegram as ct

    calls = []

    async def mock_api_call(method, token, body, timeout=15.0):
        calls.append((method, token, body, timeout))
        return {"ok": True}

    monkeypatch.setattr(ct, "_api_call", mock_api_call)

    await ct.send_message("fake-token", 123, "short")
    assert calls[-1][0] == "sendMessage"
    assert calls[-1][2]["text"] == "short"
    assert "parse_mode" not in calls[-1][2]

    # Длинный текст обрезается
    long_text = "A" * 5000
    await ct.send_message("fake-token", 456, long_text)
    assert len(calls[-1][2]["text"]) == 4000


@pytest.mark.pure
async def test_set_webhook_calls_api(monkeypatch):
    import bothub.channels_telegram as ct

    calls = []

    async def mock_api_call(method, token, body, timeout=15.0):
        calls.append((method, token, body, timeout))
        return {"ok": True}

    monkeypatch.setattr(ct, "_api_call", mock_api_call)

    await ct.set_webhook("tok", "https://example.com/webhook", "secret123")
    assert calls[-1][0] == "setWebhook"
    assert calls[-1][2] == {"url": "https://example.com/webhook", "secret_token": "secret123"}


@pytest.mark.pure
async def test_delete_webhook_calls_api(monkeypatch):
    import bothub.channels_telegram as ct

    calls = []

    async def mock_api_call(method, token, body, timeout=15.0):
        calls.append((method, token, body, timeout))
        return {"ok": True}

    monkeypatch.setattr(ct, "_api_call", mock_api_call)

    await ct.delete_webhook("tok")
    assert calls[-1][0] == "deleteWebhook"
    assert calls[-1][2] == {}


@pytest.mark.pure
async def test_api_call_error_raises(monkeypatch):
    import bothub.channels_telegram as ct

    async def mock_post(*args, **kwargs):
        class MockResponse:
            def json(self):
                return {"ok": False, "description": "Unauthorized"}
        return MockResponse()

    monkeypatch.setattr(ct.httpx.AsyncClient, "post", mock_post)

    with pytest.raises(ct.TelegramError, match="Telegram API sendMessage failed"):
        await ct._api_call("sendMessage", "123456:bad-token", {})


@pytest.mark.pure
@pytest.mark.parametrize("token", ["123:abc_def-XYZ", "1:a"])
def test_valid_token_accepts_telegram_path_segment(token):
    import bothub.channels_telegram as ct
    assert ct.valid_token(token)


@pytest.mark.pure
@pytest.mark.parametrize("token", ["", "abc", "1:/sendMessage", "1:a?x=1", "1:a#x", "1:a%2Fb", "1:é", "1:a\n"])
def test_valid_token_rejects_url_control_characters(token):
    import bothub.channels_telegram as ct
    assert not ct.valid_token(token)


@pytest.mark.pure
async def test_api_call_hides_token_in_errors(monkeypatch):
    import bothub.channels_telegram as ct

    token = "123456:secret-canary"

    async def request_error(*args, **kwargs):
        raise ct.httpx.ConnectError(f"could not connect to https://api.telegram.org/bot{token}/sendMessage")

    monkeypatch.setattr(ct.httpx.AsyncClient, "post", request_error)
    with pytest.raises(ct.TelegramError) as caught:
        await ct._api_call("sendMessage", token, {})
    assert token not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.pure
async def test_api_call_hides_token_in_remote_description(monkeypatch):
    import bothub.channels_telegram as ct

    token = "123456:secret-canary"

    async def bad_reply(*args, **kwargs):
        class Response:
            def json(self):
                return {"ok": False, "description": f"bad token {token}"}
        return Response()

    monkeypatch.setattr(ct.httpx.AsyncClient, "post", bad_reply)
    with pytest.raises(ct.TelegramError) as caught:
        await ct._api_call("sendMessage", token, {})
    assert token not in str(caught.value)


@pytest.mark.pure
async def test_api_call_rejects_path_injection_before_network(monkeypatch):
    import bothub.channels_telegram as ct

    async def unexpected_post(*args, **kwargs):
        raise AssertionError("network call must not happen")

    monkeypatch.setattr(ct.httpx.AsyncClient, "post", unexpected_post)
    with pytest.raises(ct.TelegramError, match="Invalid Telegram API request"):
        await ct._api_call("sendMessage", "123:x/../../evil", {})
    with pytest.raises(ct.TelegramError, match="Invalid Telegram API request"):
        await ct._api_call("https://attacker.invalid", "123:valid", {})


@pytest.mark.pure
async def test_httpx_info_log_does_not_expose_bot_token(monkeypatch, caplog):
    import bothub.channels_telegram as ct

    token = "123456:secret-canary"
    client_class = ct.httpx.AsyncClient
    transport = ct.httpx.MockTransport(lambda request: ct.httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(ct.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))

    with caplog.at_level(logging.INFO, logger="httpx"):
        await ct.send_message(token, 42, "hi")

    assert "HTTP Request" in caplog.text
    assert token not in caplog.text
    assert "[redacted]" in caplog.text
