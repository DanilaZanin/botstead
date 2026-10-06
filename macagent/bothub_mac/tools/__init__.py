"""Реестр инструментов Mac-агента (раздел 5 контракта)."""
from __future__ import annotations

import asyncio
from typing import Callable

from ..config import Config
from . import automation, delegate, files, input as input_tools, media
from .errors import ToolError
from .http import upload_file

# Инструменты без config: args -> dict
_PLAIN: dict[str, Callable[[dict], dict]] = {
    "find_files": files.find_files,
    "read_file": files.read_file,
    "move_to_trash": files.move_to_trash,
    "delegate": delegate.delegate,
    "preview": media.preview,
    "screenshot": media.screenshot,
    "open": automation.open_target,
    "applescript": automation.applescript,
    "shortcut": automation.shortcut,
    "shell": automation.shell,
    "click": input_tools.click,
    "type_text": input_tools.type_text,
}

# Инструменты, которым нужен Config (обращаются к ядру): args, config -> dict
_WITH_CONFIG: dict[str, Callable[[dict, Config], dict]] = {
    "upload_file": upload_file,
}


def known_tools() -> set[str]:
    return set(_PLAIN) | set(_WITH_CONFIG)


async def dispatch(tool: str, args: dict, config: Config) -> dict:
    if tool in _PLAIN:
        return await asyncio.to_thread(_PLAIN[tool], args)
    if tool in _WITH_CONFIG:
        return await asyncio.to_thread(_WITH_CONFIG[tool], args, config)
    raise ToolError(f"unknown tool: {tool}")
