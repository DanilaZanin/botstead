"""Формы сообщений WebSocket `/agent/mac` (раздел 5)."""
from __future__ import annotations

import platform

from . import __version__
from .permissions import detect_permissions


def hello_message() -> dict:
    return {
        "type": "hello",
        "host": platform.node(),
        "os": f"macOS {platform.mac_ver()[0]}",
        "agent_version": __version__,
        "permissions": detect_permissions(),
    }


def heartbeat_message(locked: bool, battery: int | None) -> dict:
    return {"type": "heartbeat", "locked": locked, "battery": battery}


def result_message(call_id: str, ok: bool, data: dict | None = None, error: str | None = None) -> dict:
    msg: dict = {"type": "result", "id": call_id, "ok": ok}
    if ok:
        msg["data"] = data or {}
    else:
        msg["error"] = error or "unknown_error"
    return msg
