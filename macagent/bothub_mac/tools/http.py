"""upload_file: POST /api/files через stdlib urllib (без внешних HTTP-библиотек)."""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid

from ..config import Config
from .errors import ToolError


def build_multipart(fields: dict[str, str], file_field: tuple[str, str, str, bytes]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    field_name, filename, content_type, content = file_field
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def upload_file(args: dict, config: Config) -> dict:
    path = args.get("path")
    thread_id = args.get("thread_id")
    if not path or not thread_id:
        raise ToolError("path и thread_id обязательны")
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise ToolError(f"файл не найден: {path}")
    with open(path, "rb") as f:
        content = f.read()
    mime, _ = mimetypes.guess_type(path)
    body, boundary = build_multipart(
        {"thread_id": thread_id, "origin": f"mac:{path}"},
        ("file", os.path.basename(path), mime or "application/octet-stream", content),
    )
    base_url = config.current.url or config.bothub_url
    req = urllib.request.Request(f"{base_url}/api/files", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {config.mac_agent_token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        raise ToolError(f"upload_file: {e}") from e
