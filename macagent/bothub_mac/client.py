"""Соединение с ядром: hello, heartbeat 30 с, обработка call/result, реконнект 1–30 с.

Несколько адресов ядра (config.urls, по приоритету): каждая попытка подключения
пробует их по порядку, основной первым, с таймаутом CONNECT_TIMEOUT на адрес.
Сидя на запасном адресе, раз в PRIMARY_RECHECK_INTERVAL проверяет доступность
основного отдельным HTTP GET (не рвёт рабочее соединение без нужды) и, если он
жив, закрывает WS, чтобы следующая попытка подключения снова начала с основного.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request

import websockets

from . import state
from .config import Config
from .protocol import heartbeat_message, hello_message, result_message
from .tools import dispatch
from .tools.errors import ToolError

log = logging.getLogger("bothub_mac")

HEARTBEAT_INTERVAL = 30
MIN_BACKOFF = 1
MAX_BACKOFF = 30
DEFAULT_TIMEOUT = 60
CONNECT_TIMEOUT = 10
HEALTH_TIMEOUT = 5
PRIMARY_RECHECK_INTERVAL = 300  # 5 минут


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # не следуем за 302 (SSO-прокси) — сам статус и есть ответ


def primary_available(url: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    """GET <url>/api/health, 2xx/3xx считаем «доступен» (за SSO-прокси отдаёт 302)."""
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        resp = opener.open(f"{url}/api/health", timeout=timeout)
        return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 400
    except Exception:
        return False


async def _try_connect(url: str, config: Config, timeout: float = CONNECT_TIMEOUT):
    try:
        return await asyncio.wait_for(
            websockets.connect(
                config.ws_url_for(url),
                additional_headers={"Authorization": f"Bearer {config.mac_agent_token}"},
                ping_interval=20,
                ping_timeout=20,
            ),
            timeout=timeout,
        )
    except Exception as e:
        log.warning("connect to %s failed: %s", url, e)
        return None


async def _connect_first_available(urls: tuple[str, ...], config: Config):
    """Пробует адреса по порядку (основной первым); первый успешный — (индекс, ws)."""
    for index, url in enumerate(urls):
        ws = await _try_connect(url, config)
        if ws is not None:
            return index, ws
    return None, None


async def _recheck_primary_loop(ws, primary: str) -> None:
    """Пока сидим на запасном: раз в 5 минут проверяет основной отдельным GET и,
    если он жив, закрывает текущий ws — следующая попытка подключения начнётся
    с основного адреса."""
    while True:
        await asyncio.sleep(PRIMARY_RECHECK_INTERVAL)
        if await asyncio.to_thread(primary_available, primary):
            log.info("primary %s снова доступен, переключаюсь", primary)
            with contextlib.suppress(Exception):
                await ws.close()
            return


async def run_forever(config: Config) -> None:
    urls = config.urls
    backoff = MIN_BACKOFF
    while True:
        index, ws = await _connect_first_available(urls, config)
        if ws is None:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        url = urls[index]
        config.current.url = url
        log.info("connected to %s", url)
        backoff = MIN_BACKOFF
        recheck_task = asyncio.create_task(_recheck_primary_loop(ws, urls[0])) if index != 0 else None
        try:
            await _session(ws, config)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # соединение оборвалось — переподключаемся с задержкой
            log.warning("connection lost: %s", e)
        finally:
            if recheck_task is not None:
                recheck_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await recheck_task
            with contextlib.suppress(Exception):
                await ws.close()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


async def _session(ws, config: Config) -> None:
    await ws.send(json.dumps(hello_message()))
    heartbeat_task = asyncio.create_task(_heartbeat_loop(ws))
    try:
        async for raw in ws:
            _handle_message(ws, raw, config)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass


async def _heartbeat_loop(ws) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        locked, battery = await asyncio.to_thread(_read_state)
        await ws.send(json.dumps(heartbeat_message(locked=locked, battery=battery)))


def _read_state() -> tuple[bool, int | None]:
    return state.is_locked(), state.battery_percent()


def _handle_message(ws, raw: str | bytes, config: Config) -> None:
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("bad message: %r", raw)
        return
    if msg.get("type") != "call":
        return
    call_id = msg.get("id")
    tool = msg.get("tool")
    args = msg.get("args") or {}
    timeout = msg.get("timeout") or DEFAULT_TIMEOUT
    asyncio.create_task(_run_call(ws, call_id, tool, args, timeout, config))


async def _run_call(ws, call_id: str, tool: str, args: dict, timeout: float, config: Config) -> None:
    try:
        data = await asyncio.wait_for(dispatch(tool, args, config), timeout=timeout)
        response = result_message(call_id, ok=True, data=data)
    except asyncio.TimeoutError:
        response = result_message(call_id, ok=False, error=f"timeout after {timeout}s")
    except ToolError as e:
        response = result_message(call_id, ok=False, error=str(e))
    except Exception as e:
        log.exception("tool %s failed", tool)
        response = result_message(call_id, ok=False, error=f"{type(e).__name__}: {e}")
    await ws.send(json.dumps(response))
