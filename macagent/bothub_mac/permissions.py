"""Best-effort проверка TCC-разрешений для `hello.permissions` (раздел 5)."""
from __future__ import annotations

import ctypes
import ctypes.util
import os


def _load(name: str) -> ctypes.CDLL | None:
    path = ctypes.util.find_library(name)
    if not path:
        return None
    try:
        return ctypes.CDLL(path)
    except OSError:
        return None


def accessibility_trusted() -> bool:
    lib = _load("ApplicationServices")
    if lib is None:
        return False
    try:
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except AttributeError:
        return False


def screen_capture_allowed() -> bool:
    lib = _load("CoreGraphics")
    if lib is None:
        return False
    try:
        lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(lib.CGPreflightScreenCaptureAccess())
    except AttributeError:
        # Недоступно на старых macOS: скриншот по факту не запрещён Screen Recording TCC.
        return True


def files_allowed() -> bool:
    for candidate in ("Desktop", "Documents", "Downloads"):
        target = os.path.expanduser(f"~/{candidate}")
        if os.path.isdir(target):
            try:
                os.listdir(target)
            except PermissionError:
                return False
    return True


def detect_permissions() -> dict[str, bool]:
    return {
        "files": files_allowed(),
        "screen": screen_capture_allowed(),
        # Automation (per-app TCC) не проверяется без реального вызова: считаем разрешённым,
        # ошибка всплывёт как error результата конкретного tool_call.
        "automation": True,
        "accessibility": accessibility_trusted(),
    }
