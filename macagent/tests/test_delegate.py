import subprocess
import threading
import time
from pathlib import Path

import pytest

from bothub_mac.tools import delegate
from bothub_mac.tools.errors import ToolError


class _FakeProc:
    pid = 999

    def __init__(self, stdout="", stderr="", returncode=0, on_wait=None):
        self._stdout, self._stderr, self.returncode = stdout, stderr, returncode
        self._on_wait = on_wait

    def communicate(self, timeout=None):
        if self._on_wait:
            self._on_wait()
        return self._stdout, self._stderr


# --- построение команды ---

def test_build_command_gemini(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: f"/bin/{engine}")
    cmd = delegate._build_command("gemini", "hello", tmp_path, tmp_path / "out.txt")
    assert cmd == ["/bin/gemini", "-p", "hello", "--model", "gemini-3.1-pro-high"]


def test_build_command_codex_defaults_to_claude_sonnet_with_accept_edits(tmp_path, monkeypatch):
    """Решение владельца 2026-09-25: Codex безлимитный, вместо него код пишет Claude Sonnet."""
    monkeypatch.delenv("BOTHUB_CODE_ENGINE", raising=False)
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: f"/bin/{engine}")
    cmd = delegate._build_command("codex", "do it", tmp_path, tmp_path / "out.txt")
    assert cmd == [
        "/bin/claude", "-p", "do it", "--model", "claude-sonnet-5",
        "--permission-mode", "acceptEdits", "--output-format", "text",
    ]


def test_build_command_codex_uses_real_codex_when_env_opts_in(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTHUB_CODE_ENGINE", "codex")
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: f"/bin/{engine}")
    out = tmp_path / "out.txt"
    cmd = delegate._build_command("codex", "do it", tmp_path, out)
    assert cmd == [
        "/bin/codex", "exec", "--skip-git-repo-check", "-s", "workspace-write",
        "-C", str(tmp_path), "--output-last-message", str(out), "do it",
    ]


def test_build_command_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: f"/bin/{engine}")
    cmd = delegate._build_command("claude", "hi", tmp_path, tmp_path / "out.txt")
    assert cmd == ["/bin/claude", "-p", "hi", "--model", "claude-sonnet-5", "--output-format", "text"]


def test_build_command_unknown_engine_raises(tmp_path):
    with pytest.raises(ToolError):
        delegate._build_command("mystery", "x", tmp_path, tmp_path / "out.txt")


def test_gemini_long_prompt_goes_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/agy")
    big_prompt = "x" * (delegate._MAX_INLINE_PROMPT + 1)
    cmd = delegate._build_command("gemini", big_prompt, tmp_path, tmp_path / "out.txt")
    assert cmd[1] == "-p"
    assert "прочитай задачу из файла" in cmd[2]
    saved = list(tmp_path.glob("delegate-prompt-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text() == big_prompt


# --- delegate(): валидация, вызов, обрезка, таймаут, семафор ---

def test_delegate_requires_known_engine(tmp_path):
    with pytest.raises(ToolError):
        delegate.delegate({"engine": "gpt", "prompt": "x", "cwd": str(tmp_path)})


def test_delegate_requires_prompt(tmp_path):
    with pytest.raises(ToolError):
        delegate.delegate({"engine": "claude", "prompt": "", "cwd": str(tmp_path)})


def test_delegate_creates_cwd_if_missing(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "work"
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/claude")
    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: _FakeProc("ok"))
    delegate.delegate({"engine": "claude", "prompt": "hi", "cwd": str(target)})
    assert target.is_dir()


def test_delegate_returns_output_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/claude")
    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: _FakeProc("hello world"))
    result = delegate.delegate({"engine": "claude", "prompt": "hi", "cwd": str(tmp_path)})
    assert result["engine"] == "claude"
    assert result["output"] == "hello world"
    assert result["exit_code"] == 0
    assert result["seconds"] >= 0


def test_delegate_reads_codex_last_message_file_not_stdout_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTHUB_CODE_ENGINE", "codex")
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/codex")

    def fake_popen(cmd, **kwargs):
        out_path = Path(cmd[cmd.index("--output-last-message") + 1])
        out_path.write_text("final answer")
        return _FakeProc("echo of the prompt that codex printed")

    monkeypatch.setattr(delegate.subprocess, "Popen", fake_popen)
    result = delegate.delegate({"engine": "codex", "prompt": "do it", "cwd": str(tmp_path)})
    assert result["output"] == "final answer"


def test_delegate_truncates_output_to_200kb(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/claude")
    big = "y" * (delegate._MAX_OUTPUT + 5000)
    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: _FakeProc(big))
    result = delegate.delegate({"engine": "claude", "prompt": "hi", "cwd": str(tmp_path)})
    assert len(result["output"].encode("utf-8")) <= delegate._MAX_OUTPUT + 32


def test_delegate_appends_stderr_only_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/claude")
    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: _FakeProc("partial", "boom", returncode=1))
    result = delegate.delegate({"engine": "claude", "prompt": "hi", "cwd": str(tmp_path)})
    assert result["exit_code"] == 1
    assert "boom" in result["output"]

    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: _FakeProc("ok", "", returncode=0))
    result_ok = delegate.delegate({"engine": "claude", "prompt": "hi", "cwd": str(tmp_path)})
    assert result_ok["output"] == "ok"


def test_delegate_clamps_timeout_to_max_and_passes_to_communicate(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/claude")
    captured = {}

    def on_wait():
        pass

    class RecordingProc(_FakeProc):
        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return super().communicate(timeout)

    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: RecordingProc("ok"))
    delegate.delegate({"engine": "claude", "prompt": "hi", "cwd": str(tmp_path), "timeout": 999999})
    assert captured["timeout"] == delegate._MAX_TIMEOUT


def test_delegate_timeout_kills_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/claude")
    killed = {}

    class HangingProc:
        pid = 4321
        returncode = -9

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return "", "killed"

    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: HangingProc())
    monkeypatch.setattr(delegate, "_kill_group", lambda proc: killed.setdefault("done", True))
    with pytest.raises(ToolError, match="timeout after 1s"):
        delegate.delegate({"engine": "claude", "prompt": "hi", "cwd": str(tmp_path), "timeout": 1})
    assert killed.get("done") is True


def test_delegate_limits_to_two_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_resolve_bin", lambda engine: "/bin/claude")
    lock = threading.Lock()
    state = {"current": 0, "max": 0}

    def slow_wait():
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.1)
        with lock:
            state["current"] -= 1

    monkeypatch.setattr(delegate.subprocess, "Popen", lambda *a, **k: _FakeProc("ok", on_wait=slow_wait))

    threads = [
        threading.Thread(target=delegate.delegate, args=({"engine": "claude", "prompt": "x", "cwd": str(tmp_path)},))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] <= 2
