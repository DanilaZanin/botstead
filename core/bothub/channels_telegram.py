"""Сетевые вызовы Telegram Bot API (httpx). Вынесены в отдельный модуль для подмены в тестах."""
from __future__ import annotations

import httpx
import logging
import re

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT = 15.0
TELEGRAM_MESSAGE_MAX = 4000


class TelegramError(Exception):
    """Ошибка вызова Telegram API."""


_TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+", re.ASCII)
_TOKEN_IN_URL = re.compile(r"(/bot)[0-9]+:[A-Za-z0-9_-]+(?=/)", re.ASCII)


class _RedactTelegramToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_IN_URL.sub(r"\1[redacted]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# httpx INFO logs include request URLs. Bot API credentials are part of the path.
logging.getLogger('httpx').addFilter(_RedactTelegramToken())


def valid_token(token: str) -> bool:
    """Токен может занимать ровно один безопасный сегмент пути Bot API."""
    return isinstance(token, str) and _TOKEN.fullmatch(token) is not None


async def _api_call(method: str, token: str, body: dict, *, timeout: float = TELEGRAM_TIMEOUT) -> dict:
    """POST https://api.telegram.org/bot<token>/<method>, возвращает распарсенный JSON ответа."""
    if not valid_token(token) or method not in {'setWebhook', 'deleteWebhook', 'sendMessage'}:
        raise TelegramError('Invalid Telegram API request')
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=body)
            data = response.json()
    except Exception:
        raise TelegramError(f"Telegram API {method} failed") from None
    if not isinstance(data, dict) or data.get('ok') is not True:
        raise TelegramError(f"Telegram API {method} failed")
    return data


async def set_webhook(token: str, url: str, secret_token: str) -> dict:
    """Установка webhook через setWebhook."""
    return await _api_call("setWebhook", token, {"url": url, "secret_token": secret_token})


async def delete_webhook(token: str) -> dict:
    """Удаление webhook через deleteWebhook."""
    return await _api_call("deleteWebhook", token, {})


async def send_message(token: str, chat_id: int, text: str) -> dict:
    """Отправка сообщения через sendMessage. Длинный текст режется по 4000 символов."""
    return await _api_call("sendMessage", token, {
        "chat_id": chat_id,
        "text": text[:TELEGRAM_MESSAGE_MAX],
    })
