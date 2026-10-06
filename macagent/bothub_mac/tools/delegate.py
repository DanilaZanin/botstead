"""delegate: субагенты gemini/codex/claude на Mac (раздел 5)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .errors import ToolError

_MAX_OUTPUT = 200 * 1024
_DEFAULT_TIMEOUT = 900
_MAX_TIMEOUT = 1800
_DEFAULT_CWD = Path.home() / "BotHub" / "work"
_MAX_INLINE_PROMPT = 100_000  # 100 КБ: agy -p не читает stdin, длинный промпт кладём в файл

# ponytail: 2 параллельных делегирования, остальные ждут своей очереди в acquire()
_SEMAPHORE = threading.Semaphore(2)

_ENGINE_BIN_NAME = {"gemini": "agy", "codex": "codex", "claude": "claude"}
_ABSOLUTE_BINS = {
    "gemini": os.path.expanduser("~/.local/bin/agy"),
    "codex": "/Applications/ChatGPT.app/Contents/Resources/codex",
    "claude": os.path.expanduser("~/.local/bin/claude"),
}


def _resolve_bin(engine: str) -> str:
    absolute = _ABSOLUTE_BINS[engine]
    if os.path.isfile(absolute) and os.access(absolute, os.X_OK):
        return absolute
    fallback = shutil.which(_ENGINE_BIN_NAME[engine])
    if not fallback:
        raise ToolError(f"{engine}: бинарь не найден ({absolute})")
    return fallback


def _truncate(text: str) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= _MAX_OUTPUT:
        return text
    return data[:_MAX_OUTPUT].decode("utf-8", errors="ignore") + "\n…(обрезано)"


def _gemini_prompt_arg(prompt: str, cwd: Path) -> str:
    if len(prompt.encode("utf-8")) <= _MAX_INLINE_PROMPT:
        return prompt
    prompt_path = cwd / f"delegate-prompt-{uuid.uuid4().hex}.txt"
    prompt_path.write_text(prompt)
    return f"прочитай задачу из файла {prompt_path} и выполни"


def _build_command(engine: str, prompt: str, cwd: Path, last_message_path: Path) -> list[str]:
    if engine == "gemini":
        return [_resolve_bin("gemini"), "-p", _gemini_prompt_arg(prompt, cwd), "--model", "gemini-3.1-pro-high"]
    if engine == "codex":
        # Решение владельца 2026-09-25: у Codex закончился лимит, вместо него код
        # пишет Claude Sonnet (acceptEdits — задача кодовая, правки cwd разрешены).
        # Настоящий Codex — только если явно попросили через переменную окружения.
        if os.environ.get("BOTHUB_CODE_ENGINE") == "codex":
            return [
                _resolve_bin("codex"), "exec", "--skip-git-repo-check", "-s", "workspace-write",
                "-C", str(cwd), "--output-last-message", str(last_message_path), prompt,
            ]
        return [
            _resolve_bin("claude"), "-p", prompt, "--model", "claude-sonnet-5",
            "--permission-mode", "acceptEdits", "--output-format", "text",
        ]
    if engine == "claude":
        return [_resolve_bin("claude"), "-p", prompt, "--model", "claude-sonnet-5", "--output-format", "text"]
    raise ToolError(f"unknown engine: {engine}")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def delegate(args: dict) -> dict:
    engine = args.get("engine")
    if engine not in _ABSOLUTE_BINS:
        raise ToolError(f"unknown engine: {engine}")
    prompt = args.get("prompt")
    if not prompt:
        raise ToolError("prompt обязателен")
    cwd = Path(os.path.expanduser(args.get("cwd") or str(_DEFAULT_CWD)))
    cwd.mkdir(parents=True, exist_ok=True)
    timeout = min(int(args.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)

    last_message_path = cwd / f"delegate-out-{uuid.uuid4().hex}.txt"
    cmd = _build_command(engine, prompt, cwd, last_message_path)

    _SEMAPHORE.acquire()
    try:
        started = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,  # своя группа процессов, чтобы убить её целиком по таймауту
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            _, stderr = proc.communicate()
            detail = f"timeout after {timeout}s"
            if stderr:
                detail += f"\n{_truncate(stderr)}"
            raise ToolError(detail)
        seconds = time.monotonic() - started
    finally:
        _SEMAPHORE.release()

    output = last_message_path.read_text() if engine == "codex" and last_message_path.is_file() else stdout
    if proc.returncode != 0 and stderr:
        output = f"{output}\n{stderr}"
    return {
        "engine": engine,
        "output": _truncate(output),
        "seconds": round(seconds, 3),
        "exit_code": proc.returncode,
    }
