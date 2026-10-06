"""open, applescript, shortcut, shell (раздел 5)."""
from __future__ import annotations

import os
import subprocess

from .errors import ToolError

_MAX_OUTPUT = 64 * 1024


def _truncate(text: str) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= _MAX_OUTPUT:
        return text
    return data[:_MAX_OUTPUT].decode("utf-8", errors="ignore") + "\n…(обрезано)"


def open_target(args: dict) -> dict:
    target = args.get("target")
    if not target:
        raise ToolError("target обязателен")
    cmd = ["open", "-a", target] if args.get("app") else ["open", target]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or "open failed")
    return {}


def applescript(args: dict) -> dict:
    script = args.get("script")
    if not script:
        raise ToolError("script обязателен")
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or "applescript failed")
    return {"stdout": _truncate(proc.stdout)}


def shortcut(args: dict) -> dict:
    name = args.get("name")
    if not name:
        raise ToolError("name обязателен")
    cmd = ["shortcuts", "run", name]
    input_text = args.get("input")
    kwargs = {}
    if input_text is not None:
        cmd += ["-i", "-"]
        kwargs["input"] = input_text
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kwargs)
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or "shortcut failed")
    return {"stdout": _truncate(proc.stdout)}


def shell(args: dict) -> dict:
    cmd = args.get("cmd")
    if not cmd:
        raise ToolError("cmd обязателен")
    cwd = os.path.expanduser(args["cwd"]) if args.get("cwd") else None
    timeout = int(args.get("timeout", 60))
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"timeout after {timeout}s") from e
    return {"code": proc.returncode, "stdout": _truncate(proc.stdout), "stderr": _truncate(proc.stderr)}
