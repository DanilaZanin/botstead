"""Парсеры раннеров против РЕАЛЬНОГО вывода CLI, снятого на этой машине:

- claude: `claude -p "..." --output-format stream-json --verbose
  --model claude-haiku-4-5-20251001 --allowedTools Bash`
- codex: `codex exec --json --skip-git-repo-check -s read-only -m gpt-6-luna "..."`
- gemini (agy): `agy --output-format stream-json --model gemini-3.8-flash-low -p "..."`
  (+ отдельный реальный ERROR-ответ agy на пустой промпт, gemini_error.jsonl)

Плюс fake-раннер и stop() у SubprocessRunner (реально убивает процесс).
"""
import asyncio
import json
import signal
from pathlib import Path

import pytest

from bothub.runner import ClaudeRunner, CodexRunner, FakeRunner, GeminiRunner, get_runner
from bothub.runner.base import TurnContext
from bothub.runner.subprocess import SubprocessRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def test_get_runner_returns_expected_classes():
    assert isinstance(get_runner("claude"), ClaudeRunner)
    assert isinstance(get_runner("codex"), CodexRunner)
    assert isinstance(get_runner("gemini"), GeminiRunner)
    assert isinstance(get_runner("fake"), FakeRunner)
    with pytest.raises(ValueError):
        get_runner("nope")


# --- claude -----------------------------------------------------------------

def test_claude_parser_on_real_stream():
    runner = ClaudeRunner()
    messages = _load("claude_stream.jsonl")
    sid = messages[0]["session_id"]
    events = [e for m in messages for e in runner.parse(m)]

    kinds = [e.kind for e in events]
    assert kinds == ["system", "tool_call", "tool_result", "assistant_msg",
                      "assistant_msg", "usage"]
    assert all(e.cli_session_id == sid for e in events)

    tool_call = events[1]
    assert tool_call.payload["tool"] == "Bash"
    assert tool_call.payload["args"]["command"] == "ls -la"
    call_id = tool_call.payload["call_id"]

    tool_result = events[2]
    assert tool_result.payload["call_id"] == call_id
    assert tool_result.payload["ok"] is True
    assert "a.txt" in tool_result.payload["summary"]

    final_text = events[3]
    assert final_text.payload["final"] is False
    assert "a.txt" in final_text.payload["text"]

    final_marker = events[4]
    assert final_marker.kind == "assistant_msg"
    assert final_marker.payload == {"text": "", "final": True}


def test_claude_parser_ignores_hook_noise_as_system():
    # Черновик триггерил system-event на любом message с session_id (hook_started,
    # thinking_tokens, ...), а не только на subtype=="init". Настоящий вывод claude
    # такие сообщения тоже шлёт — здесь их нет в фикстуре (уже отфильтрованы), но
    # регрессию по subtype фиксируем отдельно, синтетическим сообщением.
    runner = ClaudeRunner()
    noise = {"type": "system", "subtype": "hook_started", "session_id": "s1", "hook_id": "h1"}
    assert runner.parse(noise) == []


def test_claude_result_usage_matches_real_fixture():
    runner = ClaudeRunner()
    result = _load("claude_stream.jsonl")[-1]
    events = runner.parse(result)
    usage = [e for e in events if e.kind == "usage"][0]
    assert usage.payload["tokens_in"] == result["usage"]["input_tokens"]
    assert usage.payload["tokens_out"] == result["usage"]["output_tokens"]
    # claude в stream-json не кладёt "model" на верхний уровень result — заполняется
    # в subprocess.py из turn.bot["model"], здесь парсер честно отдаёт "".
    assert usage.payload["model"] == ""


# --- codex --------------------------------------------------------------------

def _parse_with_carried_session(runner, messages):
    # Реплика carry-forward логики из subprocess.py.run(): большинство сообщений
    # codex (item.started/item.completed/turn.completed) не несут thread_id вовсе —
    # только "thread.started" его знает. subprocess.py компенсирует это, протягивая
    # последний известный session_id на события, где парсер его не дал.
    sid = None
    out = []
    for message in messages:
        for event in runner.parse(message):
            sid = event.cli_session_id or sid
            event.cli_session_id = sid
            out.append(event)
    return out


def test_codex_parser_on_real_stream():
    runner = CodexRunner()
    messages = _load("codex_stream.jsonl")
    sid = messages[0]["thread_id"]
    events = _parse_with_carried_session(runner, messages)

    kinds = [e.kind for e in events]
    assert kinds == ["system", "assistant_msg", "tool_call", "tool_result",
                      "assistant_msg", "assistant_msg", "usage"]
    assert all(e.cli_session_id == sid for e in events)

    tool_call = events[2]
    assert tool_call.payload["tool"] == "command_execution"
    assert "awk" in tool_call.payload["args"]["command"]
    call_id = tool_call.payload["call_id"]

    tool_result = events[3]
    assert tool_result.payload["call_id"] == call_id
    assert tool_result.payload["ok"] is True
    assert tool_result.payload["summary"] == "4\n"

    usage = events[-1]
    assert usage.payload["tokens_in"] == 62770
    assert usage.payload["tokens_out"] == 422


def test_codex_ignores_error_items():
    # Реальный вывод codex шлёт item.completed{type:"error"} для варнингов
    # (unstable feature, hook timeout) — парсер не обязан их отражать в событиях.
    runner = CodexRunner()
    error_item = {"type": "item.completed",
                  "item": {"id": "item_1", "type": "error", "message": "warning"}}
    assert runner.parse(error_item) == []


def test_codex_resume_command_has_no_sandbox_flag():
    # codex exec resume не принимает -s/--sandbox (проверено `codex exec resume --help`
    # на этой машине) — черновик клеил "-s ... resume <sid>" и это ломало вызов.
    runner = CodexRunner()
    turn = TurnContext(turn_id="t1", thread_id="th1", bot={"model": "gpt-6"},
                        prompt="p", cli_session_id="sid-1", dry_run=False, memory_md="")
    command = runner.command(turn)
    assert command[:4] == ["codex", "exec", "resume", "sid-1"]
    assert "-s" not in command


# --- gemini (agy) ---------------------------------------------------------------

def test_gemini_parser_on_real_stream():
    runner = GeminiRunner()
    messages = _load("gemini_stream.jsonl")
    sid = messages[0]["conversation_id"]
    events = [e for m in messages for e in runner.parse(m)]

    kinds = [e.kind for e in events]
    assert kinds == ["system", "assistant_msg", "assistant_msg", "usage"]
    assert all(e.cli_session_id == sid for e in events)

    text_event = events[1]
    assert text_event.payload == {"text": "Привет\n", "final": False}

    final_marker = events[2]
    assert final_marker.payload == {"text": "", "final": True}

    usage = events[3]
    assert usage.payload["tokens_in"] == 21366
    assert usage.payload["tokens_out"] == 57


def test_gemini_parser_surfaces_error_result():
    # Реальный agy на пустой промпт (-p='') отвечает result.status=="ERROR" без
    # единого предшествующего text_delta — если тут отдать text="", ответ бота
    # молча исчезнет. Проверяем, что error всё-таки долетает как final assistant_msg.
    runner = GeminiRunner()
    (error_result,) = _load("gemini_error.jsonl")
    events = runner.parse(error_result)
    assert [e.kind for e in events] == ["assistant_msg", "usage"]
    assert events[0].payload["final"] is True
    assert "empty prompt" in events[0].payload["text"]
    assert events[1].payload["tokens_in"] == 0  # неудачный вызов, usage нулевой, но есть


def test_gemini_command_puts_prompt_as_trailing_p_argument():
    # agy -p требует значение сразу в аргументе: `-p` без значения или с флагом
    # сразу после него падает ("empty prompt" / флаг съедается как промпт).
    # Подтверждено реальным вызовом agy на этой машине.
    runner = GeminiRunner()
    turn = TurnContext(turn_id="t1", thread_id="th1", bot={"model": "gemini-3.8-flash-low"},
                        prompt="привет", cli_session_id=None, dry_run=False, memory_md="")
    command = runner.command(turn)
    assert command[-2] == "-p"
    assert command[-1] == "привет"
    assert runner.prompt(turn) == ""  # promt уже в аргументе, stdin agy не читает


# --- fake ------------------------------------------------------------------------

async def test_fake_runner_events():
    runner = FakeRunner()
    turn = TurnContext(turn_id="t1", thread_id="th1", bot={"model": "fake"},
                        prompt="hello", cli_session_id=None, dry_run=False, memory_md="")
    events = [e async for e in runner.run(turn)]
    assert [e.kind for e in events] == ["plan", "assistant_msg", "usage"]
    assert events[1].payload == {"text": "ok: hello", "final": True}
    assert events[2].payload["tokens_in"] == 10 and events[2].payload["tokens_out"] == 20
    await runner.stop("t1")  # no-op, must not raise


# --- stop() реально убивает процесс ---------------------------------------------

class _SleepRunner(SubprocessRunner):
    provider = "sleep-test"

    def command(self, turn: TurnContext) -> list[str]:
        return ["sleep", "30"]

    def parse(self, message: dict) -> list:
        return []


async def test_subprocess_runner_stop_kills_the_process():
    runner = _SleepRunner()
    turn = TurnContext(turn_id="t1", thread_id="th1", bot={"max_turn_seconds": 30},
                        prompt="", cli_session_id=None, dry_run=False, memory_md="")

    events = []

    async def consume():
        async for event in runner.run(turn):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    process = runner._processes["t1"]
    assert process.returncode is None  # ещё жив

    await runner.stop("t1")
    await asyncio.wait_for(task, timeout=5)

    assert process.returncode is not None  # реально убит
    assert process.returncode != 0 or process.returncode == -signal.SIGTERM
    assert events == []


# Launcher owns process groups and stop semantics for Docker-mode turns.
async def test_launcher_run_and_stop():
    from bothub.launcher_client import FakeLauncherClient
    launcher = FakeLauncherClient()
    await launcher.create_bot('scout', 'owner')
    launcher.script_exec(stdout=[b'{"type":"result","usage":{"input_tokens":2,"output_tokens":3}}\n'])
    runner = _SleepRunner()
    turn = TurnContext(turn_id='t-launcher', thread_id='th1', bot={'max_turn_seconds':30},
                       prompt='hello', cli_session_id=None, dry_run=False, memory_md='',
                       launcher=launcher, bot_container_id='scout', exec_env={'BOTHUB_TURN_ID':'t-launcher'})
    list_events = [event async for event in runner.run(turn)]
    assert launcher.execs[0]['bot_id'] == 'scout'
    assert launcher.execs[0]['argv'] == ['sleep','30']
    assert launcher.execs[0]['env']['BOTHUB_TURN_ID'] == 't-launcher'


async def test_launcher_stop_calls_stop_exec():
    from bothub.launcher_client import FakeLauncherClient
    launcher=FakeLauncherClient()
    await launcher.create_bot('scout','owner')
    launcher.script_exec(hang=True)
    runner=_SleepRunner()
    turn=TurnContext(turn_id='t-launcher',thread_id='th1',bot={'max_turn_seconds':30},
                     prompt='',cli_session_id=None,dry_run=False,memory_md='',
                     launcher=launcher,bot_container_id='scout')
    async def consume():
        return [event async for event in runner.run(turn)]
    task=asyncio.create_task(consume())
    for _ in range(100):
        if launcher.execs: break
        await asyncio.sleep(.01)
    await runner.stop('t-launcher')
    await task
    assert launcher.stopped == ['t-launcher']


async def test_runner_reads_multi_megabyte_json_line():
    """Живой баг 2026-09-25: результат скриншота одной строкой 2.5 МБ ронял чтение (лимит 64 КБ)."""
    import sys
    from bothub.runner.subprocess import SubprocessRunner
    from bothub.runner.base import RunnerEvent, TurnContext

    class BigLine(SubprocessRunner):
        provider = "fake"
        def command(self, turn):
            return [sys.executable, "-c", "import json;print(json.dumps({'big':'x'*3000000}))"]
        def parse(self, message):
            return [RunnerEvent("assistant_msg", {"text": str(len(message["big"])), "final": True})]

    turn = TurnContext(turn_id="t-big", thread_id="th", bot={"max_turn_seconds": 30}, prompt="",
                       cli_session_id=None, dry_run=False, memory_md="")
    events = [e async for e in BigLine().run(turn)]
    assert events and events[0].payload["text"] == "3000000"


async def test_launcher_runner_flushes_final_json_without_newline():
    from bothub.launcher_client import FakeLauncherClient
    from bothub.runner.base import RunnerEvent
    class One(SubprocessRunner):
        provider='one'
        def command(self, turn): return ['echo','x']
        def parse(self, message): return [RunnerEvent('assistant_msg', {'text':message['text']})]
    launcher=FakeLauncherClient()
    await launcher.create_bot('scout','owner')
    launcher.script_exec(stdout=[b'{"text":"last"}'])
    turn=TurnContext(turn_id='t-last',thread_id='th',bot={'max_turn_seconds':30},
        prompt='',cli_session_id=None,dry_run=False,memory_md='',launcher=launcher,bot_container_id='scout')
    events=[event async for event in One().run(turn)]
    assert events[0].payload['text']=='last'
