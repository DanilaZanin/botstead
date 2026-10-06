"""preview (qlmanage), screenshot (screencapture); обе ужимаются через sips
(JPEG по умолчанию, max_side настраивается): модель получает картинку в
разумном размере, а не png_b64 на несколько МБ текста."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile

from .errors import ToolError

_QUALITY = 70  # sips formatOptions для jpeg
_DIM_RE = {"width": re.compile(r"pixelWidth:\s*(\d+)"), "height": re.compile(r"pixelHeight:\s*(\d+)")}


def _pixel_size(path: str) -> tuple[int, int]:
    proc = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
        capture_output=True, text=True, timeout=10,
    )
    w = _DIM_RE["width"].search(proc.stdout)
    h = _DIM_RE["height"].search(proc.stdout)
    return (int(w.group(1)) if w else 0, int(h.group(1)) if h else 0)


def _resize(src_path: str, tmpdir: str, max_side: int, fmt: str) -> dict:
    ext = "jpg" if fmt == "jpeg" else fmt
    out_path = os.path.join(tmpdir, f"resized.{ext}")
    proc = subprocess.run(
        [
            "sips", "-Z", str(max_side), "-s", "format", fmt, "-s", "formatOptions", str(_QUALITY),
            src_path, "-o", out_path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise ToolError(proc.stderr.strip() or "sips failed")
    with open(out_path, "rb") as f:
        data = f.read()
    width, height = _pixel_size(out_path)
    return {
        f"{fmt}_b64": base64.b64encode(data).decode("ascii"),
        "width": width,
        "height": height,
        "bytes": len(data),
    }


def preview(args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise ToolError("path обязателен")
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise ToolError(f"путь не найден: {path}")
    max_side = int(args.get("max_side", 800))
    fmt = args.get("format", "jpeg")
    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.run(
            ["qlmanage", "-t", "-s", "1024", "-o", tmpdir, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise ToolError(proc.stderr.strip() or "qlmanage failed")
        stem = os.path.basename(path)
        png_path = os.path.join(tmpdir, f"{stem}.png")
        if not os.path.isfile(png_path):
            raise ToolError("qlmanage не создал превью")
        return _resize(png_path, tmpdir, max_side, fmt)


def screenshot(args: dict) -> dict:
    max_side = int(args.get("max_side", 1440))
    fmt = args.get("format", "jpeg")
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "screenshot.png")
        proc = subprocess.run(["screencapture", "-x", raw_path], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0 or not os.path.isfile(raw_path):
            raise ToolError(proc.stderr.strip() or "screencapture failed")
        return _resize(raw_path, tmpdir, max_side, fmt)
