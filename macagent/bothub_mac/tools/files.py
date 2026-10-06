"""find_files, read_file, move_to_trash (раздел 5)."""
from __future__ import annotations

import base64
import mimetypes
import os
import subprocess
from datetime import datetime, timezone

from .errors import ToolError

_KIND_PREDICATES = {
    "pdf": 'kMDItemContentType == "com.adobe.pdf"',
    "image": 'kMDItemContentTypeTree == "public.image"',
    "doc": (
        '(kMDItemContentTypeTree == "public.text" || '
        'kMDItemContentTypeTree == "org.openxmlformats.wordprocessingml.document" || '
        'kMDItemContentTypeTree == "com.microsoft.word.doc")'
    ),
}


def _escape_predicate_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_predicate(query: str, kind: str = "any", since: str | None = None, content: bool = False) -> str:
    field = "kMDItemTextContent" if content else "kMDItemFSName"
    clauses = [f'{field} == "*{_escape_predicate_literal(query)}*"cd']
    if kind and kind != "any":
        predicate = _KIND_PREDICATES.get(kind)
        if predicate is None:
            raise ToolError(f"unknown kind: {kind}")
        clauses.append(predicate)
    if since:
        clauses.append(f'kMDItemFSContentChangeDate >= $time.iso("{since}")')
    return " && ".join(clauses)


def _guess_kind(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        return "any"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/") or "word" in mime or "officedocument" in mime:
        return "doc"
    return "any"


def _run_mdfind(predicate: str) -> list[str]:
    proc = subprocess.run(["mdfind", predicate], capture_output=True, text=True, timeout=30)
    return [line for line in proc.stdout.splitlines() if line]


def find_files(args: dict) -> list[dict]:
    query = args.get("query")
    if not query:
        raise ToolError("query обязателен")
    limit = int(args.get("limit", 20))
    predicate = build_predicate(
        query, kind=args.get("kind", "any"), since=args.get("since"), content=bool(args.get("content", False))
    )
    results = []
    for path in _run_mdfind(predicate)[:limit]:
        try:
            st = os.stat(path)
        except OSError:
            continue
        results.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "kind": _guess_kind(path),
            }
        )
    return results


def read_file(args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise ToolError("path обязателен")
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise ToolError(f"файл не найден: {path}")
    max_bytes = int(args.get("max_bytes", 1_048_576))
    st = os.stat(path)
    with open(path, "rb") as f:
        content = f.read(max_bytes)
    mime, _ = mimetypes.guess_type(path)
    return {
        "path": path,
        "size": st.st_size,
        "mime": mime or "application/octet-stream",
        "content_b64": base64.b64encode(content).decode("ascii"),
    }


def move_to_trash(args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise ToolError("path обязателен")
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise ToolError(f"путь не найден: {path}")
    # path идёт как отдельный argv-элемент (не подставляется в текст скрипта),
    # иначе кавычка или обратный слэш в имени файла ломают/дополняют AppleScript.
    proc = subprocess.run(
        [
            "osascript",
            "-e", "on run argv",
            "-e", 'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
            "-e", "end run",
            "--", path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or "move_to_trash failed")
    return {}
