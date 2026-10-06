"""Несколько адресов ядра: порядок, откат на запасной, возврат на основной."""
import asyncio
import urllib.error

import pytest

from bothub_mac import client
from bothub_mac.config import Config


@pytest.fixture
def config():
    return Config(
        bothub_url="https://a", mac_agent_token="t",
        bothub_urls=("https://a", "https://b"),
    )


# --- primary_available: HTTP GET .../api/health, 2xx/3xx = доступен ---

def test_primary_available_true_for_2xx(monkeypatch):
    class FakeOpener:
        def open(self, url, timeout=None):
            class R:
                status = 200
            return R()

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *_: FakeOpener())
    assert client.primary_available("https://x") is True


def test_primary_available_true_for_3xx_behind_sso_proxy(monkeypatch):
    class FakeOpener:
        def open(self, url, timeout=None):
            class R:
                status = 302
            return R()

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *_: FakeOpener())
    assert client.primary_available("https://x") is True


def test_primary_available_false_for_5xx(monkeypatch):
    class FakeOpener:
        def open(self, url, timeout=None):
            raise urllib.error.HTTPError(url, 502, "bad gateway", None, None)

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *_: FakeOpener())
    assert client.primary_available("https://x") is False


def test_primary_available_false_on_connection_error(monkeypatch):
    class FakeOpener:
        def open(self, url, timeout=None):
            raise OSError("no route to host")

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *_: FakeOpener())
    assert client.primary_available("https://x") is False


# --- _connect_first_available: порядок и откат на запасной ---

async def test_connect_first_available_tries_primary_first(monkeypatch, config):
    attempts = []

    async def fake_try_connect(url, cfg, timeout=client.CONNECT_TIMEOUT):
        attempts.append(url)
        return "WS-A" if url == "https://a" else None

    monkeypatch.setattr(client, "_try_connect", fake_try_connect)
    index, ws = await client._connect_first_available(("https://a", "https://b"), config)
    assert (index, ws) == (0, "WS-A")
    assert attempts == ["https://a"]


async def test_connect_first_available_falls_back_when_primary_unresponsive(monkeypatch, config):
    async def fake_try_connect(url, cfg, timeout=client.CONNECT_TIMEOUT):
        return None if url == "https://a" else "WS-B"

    monkeypatch.setattr(client, "_try_connect", fake_try_connect)
    index, ws = await client._connect_first_available(("https://a", "https://b"), config)
    assert (index, ws) == (1, "WS-B")


async def test_connect_first_available_none_when_all_fail(monkeypatch, config):
    async def fake_try_connect(url, cfg, timeout=client.CONNECT_TIMEOUT):
        return None

    monkeypatch.setattr(client, "_try_connect", fake_try_connect)
    index, ws = await client._connect_first_available(("https://a", "https://b"), config)
    assert (index, ws) == (None, None)


# --- _recheck_primary_loop: возврат на основной без обрыва без нужды ---

async def test_recheck_primary_loop_closes_ws_once_primary_is_back(monkeypatch):
    monkeypatch.setattr(client, "PRIMARY_RECHECK_INTERVAL", 0)
    monkeypatch.setattr(client, "primary_available", lambda url, timeout=client.HEALTH_TIMEOUT: True)
    closed = {"n": 0}

    class FakeWS:
        async def close(self):
            closed["n"] += 1

    await client._recheck_primary_loop(FakeWS(), "https://a")
    assert closed["n"] == 1


async def test_recheck_primary_loop_keeps_running_while_primary_down(monkeypatch):
    monkeypatch.setattr(client, "PRIMARY_RECHECK_INTERVAL", 0)
    calls = {"n": 0}

    def fake_available(url, timeout=client.HEALTH_TIMEOUT):
        calls["n"] += 1
        return calls["n"] >= 3  # доступен только с третьей проверки

    monkeypatch.setattr(client, "primary_available", fake_available)
    closed = {"n": 0}

    class FakeWS:
        async def close(self):
            closed["n"] += 1

    await client._recheck_primary_loop(FakeWS(), "https://a")
    assert calls["n"] == 3
    assert closed["n"] == 1


# --- run_forever: подключается к текущему адресу и обновляет config.current.url ---

async def test_run_forever_sets_current_url_to_fallback_when_that_is_connected(monkeypatch, config):
    class FakeWS:
        async def close(self):
            pass

    async def fake_connect_first_available(urls, cfg):
        return 1, FakeWS()

    async def fake_session(ws, cfg):
        raise asyncio.CancelledError()

    monkeypatch.setattr(client, "_connect_first_available", fake_connect_first_available)
    monkeypatch.setattr(client, "_session", fake_session)

    with pytest.raises(asyncio.CancelledError):
        await client.run_forever(config)

    assert config.current.url == "https://b"


async def test_run_forever_sets_current_url_to_primary_when_that_is_connected(monkeypatch, config):
    class FakeWS:
        async def close(self):
            pass

    async def fake_connect_first_available(urls, cfg):
        return 0, FakeWS()

    async def fake_session(ws, cfg):
        raise asyncio.CancelledError()

    monkeypatch.setattr(client, "_connect_first_available", fake_connect_first_available)
    monkeypatch.setattr(client, "_session", fake_session)

    with pytest.raises(asyncio.CancelledError):
        await client.run_forever(config)

    assert config.current.url == "https://a"
