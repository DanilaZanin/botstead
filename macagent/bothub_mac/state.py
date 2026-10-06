"""Состояние Mac для heartbeat: экран заблокирован, заряд батареи."""
from __future__ import annotations

import ctypes
import ctypes.util
import re
import subprocess

_BATTERY_RE = re.compile(r"(\d+)%")


def is_locked() -> bool:
    lib = None
    path = ctypes.util.find_library("CoreGraphics")
    if path:
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            lib = None
    if lib is None:
        return False
    try:
        lib.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        info = lib.CGSessionCopyCurrentDictionary()
        if not info:
            # Нет активной сессии GUI (fast user switching) — считаем экран заблокированным.
            return True
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        # CFSTR("CGSSessionScreenIsLocked") строится через CFStringCreateWithCString.
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        key = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
        try:
            value = cf.CFDictionaryGetValue(info, key)
            return bool(value)
        finally:
            cf.CFRelease(key)
            cf.CFRelease(info)
    except (AttributeError, OSError):
        return False


def battery_percent() -> int | None:
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _BATTERY_RE.search(out)
    return int(match.group(1)) if match else None
