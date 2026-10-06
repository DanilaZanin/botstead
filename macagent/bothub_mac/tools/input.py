"""click, type_text через CGEvent (CoreGraphics) — требует разрешение Accessibility.

Никакой сторонней зависимости: связываемся с системным фреймворком через ctypes,
как и допускает контракт модуля (stdlib + websockets).
"""
from __future__ import annotations

import ctypes
import ctypes.util

from .errors import ToolError


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_MOUSE_EVENTS = {
    "left": (1, 2, 0),  # kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
    "right": (3, 4, 1),  # kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
}

_kCGHIDEventTap = 0


def _load(name: str) -> ctypes.CDLL:
    path = ctypes.util.find_library(name)
    if not path:
        raise ToolError(f"{name} framework недоступен")
    return ctypes.CDLL(path)


def _core_graphics() -> ctypes.CDLL:
    cg = _load("CoreGraphics")
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        CGPoint,
        ctypes.c_uint32,
    ]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventKeyboardSetUnicodeString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint16),
    ]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    # CFRelease живёт в CoreFoundation, не в CoreGraphics — грузим отдельно.
    cf = _load("CoreFoundation")
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cg.CFRelease = cf.CFRelease
    return cg


def click(args: dict) -> dict:
    try:
        x = float(args["x"])
        y = float(args["y"])
    except (KeyError, TypeError, ValueError) as e:
        raise ToolError("x и y обязательны и должны быть числами") from e
    button = args.get("button", "left")
    if button not in _MOUSE_EVENTS:
        raise ToolError(f"unknown button: {button}")
    down_type, up_type, cg_button = _MOUSE_EVENTS[button]
    cg = _core_graphics()
    point = CGPoint(x, y)
    for event_type in (down_type, up_type):
        event = cg.CGEventCreateMouseEvent(None, event_type, point, cg_button)
        if not event:
            raise ToolError("не удалось создать событие клика (нет разрешения Accessibility?)")
        cg.CGEventPost(_kCGHIDEventTap, event)
        cg.CFRelease(event)
    return {}


def type_text(args: dict) -> dict:
    text = args.get("text")
    if not text:
        raise ToolError("text обязателен")
    cg = _core_graphics()
    utf16 = text.encode("utf-16-le")
    count = len(utf16) // 2
    buf = (ctypes.c_uint16 * count).from_buffer_copy(utf16)
    for key_down in (True, False):
        event = cg.CGEventCreateKeyboardEvent(None, 0, key_down)
        if not event:
            raise ToolError("не удалось создать событие ввода (нет разрешения Accessibility?)")
        cg.CGEventKeyboardSetUnicodeString(event, count, buf)
        cg.CGEventPost(_kCGHIDEventTap, event)
        cg.CFRelease(event)
    return {}
