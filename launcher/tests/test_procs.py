"""Настоящие процессы (sh/cat/sleep), без Docker: стриминг, stdin, kill, pty, runner команд."""
import asyncio
import os

import pytest

from bothub_launcher.procs import PtyProcess, SubprocessExec, SubprocessRunner


def _pty_available() -> bool:
    try:
        master, slave = os.openpty()
    except OSError:  # например, песочница запрещает /dev/ptmx
        return False
    os.close(master)
    os.close(slave)
    return True


needs_pty = pytest.mark.skipif(not _pty_available(), reason="pty недоступен в этом окружении")


async def drain(proc):
    out = {"stdout": b"", "stderr": b""}
    async for stream, chunk in proc.output():
        out[stream] += chunk
    return out


async def test_streams_stdout_stderr_and_exit_code():
    proc = await SubprocessExec.start(["sh", "-c", "printf out; printf err >&2; exit 3"], {})
    out = await asyncio.wait_for(drain(proc), 5)
    assert out == {"stdout": b"out", "stderr": b"err"}
    assert await proc.wait() == 3


async def test_stdin_is_written_and_closed():
    proc = await SubprocessExec.start(["cat"], {})
    await proc.write_stdin("привет".encode())
    out = await asyncio.wait_for(drain(proc), 5)
    assert out["stdout"] == "привет".encode()
    assert await proc.wait() == 0


async def test_large_output_is_chunked_and_complete():
    proc = await SubprocessExec.start(["sh", "-c", "head -c 300000 /dev/zero"], {})
    chunks = []
    async for stream, chunk in proc.output():
        assert len(chunk) <= 65536
        chunks.append(chunk)
    assert sum(map(len, chunks)) == 300000


async def test_silent_reader_backpressures_125_mib_output():
    proc = await SubprocessExec.start(["sh", "-c", "head -c 131072000 /dev/zero"], {})
    output = proc.output()
    await asyncio.wait_for(output.__anext__(), 5)
    await asyncio.sleep(0.2)
    assert proc._proc.returncode is None
    await output.aclose()
    await proc.kill()
    await asyncio.wait_for(proc._proc.stdout.read(), 5)
    await asyncio.wait_for(proc.wait(), 5)


async def test_kill_ends_process_group_quickly():
    proc = await SubprocessExec.start(["sh", "-c", "sleep 30 & wait"], {})
    await asyncio.sleep(0.1)
    await proc.kill()
    code = await asyncio.wait_for(proc.wait(), 5)
    assert code != 0


async def test_env_is_passed_but_parent_secrets_are_not(monkeypatch):
    monkeypatch.setenv("LAUNCHER_SECRET", "must-not-leak")
    proc = await SubprocessExec.start(["sh", "-c", 'printf "%s|%s" "$FOO" "${LAUNCHER_SECRET:-none}"'], {"FOO": "bar"})
    out = await drain(proc)
    assert out["stdout"] == b"bar|none"


async def test_missing_binary_raises_backend_error():
    from bothub_launcher.errors import BackendError
    with pytest.raises(BackendError):
        await SubprocessExec.start(["/nonexistent/docker-xyz"], {})


async def test_runner_collects_result():
    run = SubprocessRunner()
    res = await run(["sh", "-c", "echo hi; echo oops >&2; exit 2"])
    assert (res.rc, res.out, res.err) == (2, "hi\n", "oops\n")


async def test_runner_feeds_input_and_env():
    res = await SubprocessRunner()(["sh", "-c", 'cat; printf %s "$X"'], input="data", env={"X": "y"})
    assert res.out == "datay"


async def test_runner_missing_binary_is_127():
    res = await SubprocessRunner()(["/nonexistent/iptables-xyz", "-S"])
    assert res.rc == 127 and "not found" in res.err.lower()


async def test_runner_timeout_is_124():
    res = await SubprocessRunner(timeout=0.2)(["sleep", "5"])
    assert res.rc == 124


@needs_pty
async def test_pty_roundtrip_and_exit_code():
    pty = await PtyProcess.start(["sh", "-c", "read x; echo got:$x; exit 4"], 80, 24)
    pty.write(b"abc\n")
    seen = b""
    while True:
        data = await asyncio.wait_for(pty.read(), 5)
        if not data:
            break
        seen += data
    assert b"got:abc" in seen
    assert await pty.wait() == 4


@needs_pty
async def test_pty_size_and_resize():
    pty = await PtyProcess.start(["sh", "-c", "stty size; read x; stty size"], 100, 30)
    first = await asyncio.wait_for(pty.read(), 5)
    assert b"30 100" in first
    pty.resize(120, 40)
    pty.write(b"\n")
    seen = b""
    while True:
        data = await asyncio.wait_for(pty.read(), 5)
        if not data:
            break
        seen += data
    assert b"40 120" in seen


@needs_pty
async def test_pty_kill():
    pty = await PtyProcess.start(["sleep", "30"], 80, 24)
    pty.kill()
    code = await asyncio.wait_for(pty.wait(), 5)
    assert code != 0
