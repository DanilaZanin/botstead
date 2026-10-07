"""Отправка ответов бота в Slack: сетевой вызов отдельно от маршрутов и БД."""
from __future__ import annotations

from typing import Any

import httpx


SLACK_API_URL = "https://slack.com/api/chat.postMessage"
SLACK_AUTH_URL = "https://slack.com/api/auth.test"
SLACK_TIMEOUT = 15
SLACK_MESSAGE_LIMIT = 3900


class SlackError(RuntimeError):
    pass


async def slack_team_id(token: str) -> str:
    """Verify a Bot Token and return the workspace it belongs to."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=SLACK_TIMEOUT) as client:
            response = await client.post(SLACK_AUTH_URL, headers={"Authorization": f"Bearer {token}"})
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SlackError("slack_auth_failed") from exc
    team_id = result.get("team_id") if isinstance(result, dict) else None
    if response.status_code != 200 or not isinstance(result, dict) or not result.get("ok") or not isinstance(team_id, str) or not team_id:
        raise SlackError("slack_auth_failed")
    return team_id


def clean_event_id(payload: dict[str, Any]) -> str | None:
    """event_id из события Slack; None, если его нет или строка не годится для БД."""
    value = payload.get("event_id")
    if not isinstance(value, (str, int)) or value in ("", 0):
        return None
    event_id = str(value)
    try:
        event_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return event_id if "\x00" not in event_id else None


def slack_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Минимальная копия события для ответа и дедупликации; лишние данные Slack не храним."""
    return {"event_id": clean_event_id(payload), "event": payload.get("event") if isinstance(payload.get("event"), dict) else {}}


def reply_target(payload: dict[str, Any]) -> dict[str, str] | None:
    """Канал и тред ответа: channel обязателен, thread_ts имеет приоритет над ts."""
    event = payload.get("event")
    if not isinstance(event, dict) or not event.get("channel"):
        return None
    thread_ts = event.get("thread_ts") or event.get("ts")
    return {"channel": str(event["channel"]), "thread_ts": str(thread_ts) if thread_ts else ""}


def split_message(text: str, limit: int = SLACK_MESSAGE_LIMIT) -> list[str]:
    """Slack ограничивает текст сообщения, длинный ответ режем на части."""
    return [part for part in (text[index:index + limit] for index in range(0, len(text), limit)) if part]


async def send_slack_message(token: str, channel: str, thread_ts: str, text: str) -> None:
    """POST chat.postMessage с токеном приложения. Отдельная функция, чтобы подменять в тестах."""
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=SLACK_TIMEOUT) as client:
            response = await client.post(SLACK_API_URL, json=payload,
                                         headers={"Authorization": f"Bearer {token}"})
        try:
            result = response.json()
        except ValueError as exc:
            raise SlackError("invalid_slack_response") from exc
        if response.status_code != 200 or not result.get("ok"):
            raise SlackError(str(result.get("error") or "slack_request_failed"))
    except httpx.HTTPError as exc:
        raise SlackError("slack_unreachable") from exc
