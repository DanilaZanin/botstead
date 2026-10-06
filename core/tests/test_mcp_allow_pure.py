"""bots.mcp_allow: список разрешённых чужих MCP-инструментов (docs/contracts.md, раздел 4). Без БД.

Слои: чистая политика (bothub.mcp_policy), шлюз ядра POST /api/approvals (reason mcp_not_allowed), approve в mcp_server,
раннеры (флаг claude `--strict-mcp-config` при пустом списке, остановка хода у codex и agy в потоке событий)."""
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest

from bothub import mcp_server as srv
from bothub.launcher_client import FakeBot, FakeLauncherClient
from bothub.main import BotIn, BotPatch, create_app
from bothub.mcp_policy import (NOT_ALLOWED, UNSUPPORTED, McpAllowUnsupported, McpNotAllowed, canonical_tool, is_foreign_mcp,
                               looks_like_mcp, mcp_allowed, server_of)
from bothub.runner import ClaudeRunner, CodexRunner, GeminiRunner
from bothub.runner.codex import BOTHUB_MCP_FLAGS
from bothub.runner.base import RunnerEvent, TurnContext

pytestmark = pytest.mark.pure

FOREIGN = "mcp__github__create_issue"


# ---- политика -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,expected", [
    ("mcp__github__create_issue", "mcp__github__create_issue"),
    ("github.create_issue", "mcp__github__create_issue"),  # так codex называет инструмент в событиях
    ("mcp__my-srv__a__b", "mcp__my-srv__a__b"),
    ("mcp__github", None), ("mcp__github__", None), ("mcp____x", None),
    ("Bash", None), ("Read", None), ("command_execution", None), ("", None), (None, None), (5, None),
])
def test_canonical_tool(tool, expected):
    assert canonical_tool(tool) == expected


def test_server_and_foreign():
    assert server_of("mcp__github__create_issue") == "github" and server_of("github.create_issue") == "github"
    assert server_of("Bash") is None
    assert is_foreign_mcp("mcp__github__x") and is_foreign_mcp("github.x")
    assert not is_foreign_mcp("mcp__bothub__browser") and not is_foreign_mcp("bothub.browser")
    assert not is_foreign_mcp("Bash") and not is_foreign_mcp(None)


@pytest.mark.parametrize("allow", [[], None, "mcp__github__create_issue", {"mcp__github__create_issue": 1}, [None, 5, "", "  "]])
def test_empty_or_malformed_list_allows_no_foreign_tool(allow):
    assert mcp_allowed(allow, FOREIGN) is False


@pytest.mark.parametrize("item", [
    "mcp__github__create_issue",  # точное имя
    "mcp__github__*",  # весь сервер по маске
    "mcp__*__create_*",  # маска по обоим полям
    "github",  # голое имя сервера
    "github.create_issue",  # вид codex
    "  github  ",
])
def test_list_items_that_allow_the_tool(item):
    assert mcp_allowed([item], FOREIGN) is True
    assert mcp_allowed([item], "github.create_issue") is True  # то же имя в виде codex


@pytest.mark.parametrize("item", [
    "mcp__github__delete_repo", "mcp__gitlab__*", "gitlab", "gitlab.create_issue", "mcp__github__create_issue2",
    "git", "mcp__git__*", "create_issue",
])
def test_list_items_that_do_not_allow_the_tool(item):
    assert mcp_allowed([item], FOREIGN) is False


def test_own_and_builtin_tools_are_not_subject_to_the_list():
    for tool in ("mcp__bothub__browser", "mcp__bothub__approve", "mcp__bothub__mac_shell", "Bash", "Read", "WebSearch",
                 "command_execution", "bothub.remember"):
        assert mcp_allowed([], tool) is True


def test_bare_server_name_does_not_allow_a_prefix_collision():
    assert mcp_allowed(["git"], "mcp__github__x") is False
    assert mcp_allowed(["github"], "mcp__github_enterprise__x") is False


def test_second_item_can_allow():
    assert mcp_allowed(["mcp__slack__post", "mcp__github__*"], FOREIGN) is True


# ---- шлюз ядра: POST /api/approvals ------------------------------------------------------------------------------

OWNER = uuid.UUID(int=1)
THREAD = uuid.UUID(int=3)
TURN = uuid.UUID(int=4)


class Con:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, query, *args):
        if "from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and t.thread_id=$2" in query:
            return "normal"
        if "update bothub.threads set last_seq" in query:
            self.pool.seq += 1
            return self.pool.seq
        return None

    async def fetchrow(self, query, *args):
        if "from bothub.bots b join bothub.users u" in query:
            return {"owner_id": OWNER}
        if "from bothub.threads where id=$1 and owner_id=$2" in query:
            return {"id": THREAD, "bot_id": "alpha", "owner_id": OWNER}
        if "select * from bothub.bots where id=$1 and owner_id=$2" in query:
            return {"id": "alpha", "owner_id": OWNER, "auto_allow": self.pool.auto_allow, "mcp_allow": self.pool.mcp_allow,
                    "mac_full_control": False}
        if "insert into bothub.approvals" in query:
            keys = ("thread_id", "turn_id", "bot_id", "risk", "title", "tool", "args", "args_hash", "op_hash", "status", "expires_at")
            row = dict(zip(keys, args)) | {"id": uuid.uuid4(), "args": json.loads(args[6]), "remember": False,
                                            "created_at": datetime.now(timezone.utc)}
            self.pool.approvals.append(row)
            return row
        if "insert into bothub.events" in query:
            event = {"id": uuid.uuid4(), "thread_id": args[0], "seq": args[1], "turn_id": args[2], "kind": args[3],
                     "actor": args[4], "client": args[5], "payload": json.loads(args[6]),
                     "created_at": datetime.now(timezone.utc)}
            self.pool.events.append(event)
            return event
        raise AssertionError(f"unexpected SQL: {query}")

    async def fetch(self, query, *args):
        raise AssertionError(f"unexpected SQL: {query}")

    async def execute(self, query, *args):
        return "OK"


class Pool:
    def __init__(self, mcp_allow, auto_allow):
        self.approvals, self.events, self.seq = [], [], 0
        self.mcp_allow, self.auto_allow = mcp_allow, auto_allow
        self.connection = Con(self)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def no_lifespan(app):
    yield


def _app(monkeypatch, mcp_allow=(), auto_allow=()):
    monkeypatch.setenv("BOTHUB_BASE_PATH", "/")
    monkeypatch.setenv("BOTHUB_LEGACY_AUTH", "false")
    monkeypatch.setenv("BOT_TOKEN_SECRET", "mcp-allow-secret")
    launcher = FakeLauncherClient()
    launcher.bots["alpha"] = FakeBot("alpha", str(OWNER))
    app = create_app(launcher=launcher)
    app.state.pool = Pool(list(mcp_allow), list(auto_allow))
    app.router.lifespan_context = no_lifespan
    return app


async def _ask(app, tool, args=None):
    signature = hmac.new(b"mcp-allow-secret", b"alpha", hashlib.sha256).hexdigest()
    body = {"thread_id": str(THREAD), "turn_id": str(TURN), "risk": "other", "title": tool, "tool": tool, "args": args or {"title": "x"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        return await client.post("/api/approvals", json=body, headers={"Authorization": f"Bearer bot:alpha:{signature}"})


async def test_foreign_tool_with_an_empty_list_is_rejected_at_once(monkeypatch):
    app = _app(monkeypatch)
    response = await _ask(app, FOREIGN)
    assert response.status_code == 200
    assert response.json()["status"] == "rejected" and response.json()["reason"] == NOT_ALLOWED
    pool = app.state.pool
    assert [event["kind"] for event in pool.events] == ["approval_dec"]  # вопроса владельцу нет
    assert pool.events[0]["actor"] == "system"
    assert pool.events[0]["payload"]["decision"] == "rejected" and pool.events[0]["payload"]["reason"] == NOT_ALLOWED
    assert pool.approvals[0]["status"] == "rejected"


async def test_auto_allow_does_not_open_a_tool_outside_the_list(monkeypatch):
    app = _app(monkeypatch, auto_allow=[{"tool": "mcp__github__*"}])
    response = await _ask(app, FOREIGN)
    assert response.json()["status"] == "rejected" and response.json()["reason"] == NOT_ALLOWED


async def test_tool_on_the_list_still_asks_the_owner(monkeypatch):
    app = _app(monkeypatch, mcp_allow=["mcp__github__*"])
    response = await _ask(app, FOREIGN)
    assert response.status_code == 200
    assert response.json()["status"] == "pending" and "reason" not in response.json()
    kinds = [event["kind"] for event in app.state.pool.events]
    assert "approval_req" in kinds and "approval_dec" not in kinds


async def test_listing_a_tool_does_not_auto_approve_it(monkeypatch):
    # неизвестный инструмент проходит без вопроса только по точному правилу с непустым match: список разрешает, не одобряет
    app = _app(monkeypatch, mcp_allow=["github"], auto_allow=[{"tool": FOREIGN}])
    response = await _ask(app, FOREIGN)
    assert response.json()["status"] == "pending"


async def test_another_server_is_not_opened_by_the_list(monkeypatch):
    app = _app(monkeypatch, mcp_allow=["mcp__github__*"])
    response = await _ask(app, "mcp__slack__post_message")
    assert response.json()["status"] == "rejected" and response.json()["reason"] == NOT_ALLOWED


@pytest.mark.parametrize("tool", ["Read", "Bash", "mcp__bothub__remember", "mcp__bothub__mac_find_files"])
async def test_own_and_builtin_tools_ignore_the_list(monkeypatch, tool):
    app = _app(monkeypatch)
    response = await _ask(app, tool, {"file_path": "/home/bot/a.txt"} if tool == "Read" else {"command": "ls"} if tool == "Bash" else {})
    assert response.status_code == 200
    assert response.json().get("reason") != NOT_ALLOWED and response.json()["status"] != "rejected"


# ---- approve в mcp_server --------------------------------------------------------------------------------------------

def _patch_client(monkeypatch, handler):
    monkeypatch.setenv("BOTHUB_URL", "http://core.test")
    monkeypatch.setenv("BOTHUB_TOKEN", "bot:scout:deadbeef")
    monkeypatch.setenv("BOTHUB_THREAD_ID", "thread-1")
    monkeypatch.setenv("BOTHUB_TURN_ID", "turn-1")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(srv, "_client", lambda: httpx.AsyncClient(base_url="http://core.test", transport=transport))


async def test_approve_denies_with_the_reason_mcp_not_allowed(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": "a1", "status": "rejected", "reason": NOT_ALLOWED})

    _patch_client(monkeypatch, handler)
    result = await srv.approve(FOREIGN, {"title": "x"})
    assert result == {"behavior": "deny", "message": NOT_ALLOWED}
    assert calls == ["/api/approvals"]  # ждать нечего: без long-poll


async def test_approve_keeps_the_plain_message_for_an_owner_rejection(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, json={"id": "a1", "status": "rejected"}))
    assert await srv.approve("git_push", {}) == {"behavior": "deny", "message": "approval rejected"}


async def test_approve_does_not_take_another_rejection_reason_for_mcp_not_allowed(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, json={"id": "a1", "status": "rejected", "reason": "private_address"}))
    assert (await srv.approve("mcp__bothub__browser", {"action": "navigate", "url": "http://10.0.0.1"}))["message"] == "approval rejected"


# ---- раннеры -----------------------------------------------------------------------------------------------------------

def turn_for(mcp_allow=None, turn_type="normal", launcher=None, session=None):
    return TurnContext(turn_id=str(uuid.uuid4()), thread_id="th1", bot={"model": "m", "instructions": "", "mcp_allow": mcp_allow or []},
                       prompt="p", cli_session_id=session, dry_run=False, memory_md="", turn_type=turn_type,
                       launcher=launcher, bot_container_id="alpha" if launcher else None)


def test_claude_with_an_empty_list_starts_only_the_bothub_server():
    command = ClaudeRunner().command(turn_for([]))
    assert "--strict-mcp-config" in command and "--mcp-config" in command
    assert json.loads(command[command.index("--mcp-config") + 1])["mcpServers"].keys() == {"bothub"}
    assert command[command.index("--permission-prompt-tool") + 1] == "mcp__bothub__approve"


def test_claude_with_a_list_leaves_cli_servers_to_the_approve_gate():
    command = ClaudeRunner().command(turn_for(["mcp__github__*"]))
    assert "--strict-mcp-config" not in command and "--tools" not in command
    assert command[command.index("--permission-prompt-tool") + 1] == "mcp__bothub__approve"


def test_claude_does_not_stop_the_turn_by_itself():
    runner = ClaudeRunner()
    assert runner.stream_guard is False
    runner.guard(turn_for([]), RunnerEvent("tool_call", {"tool": FOREIGN, "args": {}}))  # не бросает: решает approve


def test_codex_and_gemini_stop_on_a_foreign_tool_call():
    for runner in (CodexRunner(), GeminiRunner()):
        assert runner.stream_guard is True
        with pytest.raises(McpNotAllowed) as caught:
            runner.guard(turn_for([]), RunnerEvent("tool_call", {"tool": "github.create_issue", "args": {}}))
        assert caught.value.tool == "github.create_issue" and NOT_ALLOWED in str(caught.value)
        with pytest.raises(McpNotAllowed):
            runner.guard(turn_for(["mcp__slack__*"]), RunnerEvent("tool_call", {"tool": FOREIGN, "args": {}}))


def test_guard_lets_through_allowed_own_builtin_and_non_call_events():
    runner = CodexRunner()
    runner.guard(turn_for(["github"]), RunnerEvent("tool_call", {"tool": "github.create_issue", "args": {}}))
    runner.guard(turn_for([]), RunnerEvent("tool_call", {"tool": "bothub.remember", "args": {}}))
    runner.guard(turn_for([]), RunnerEvent("tool_call", {"tool": "command_execution", "args": {"command": "ls"}}))
    runner.guard(turn_for([]), RunnerEvent("tool_result", {"call_id": "1", "ok": True, "summary": FOREIGN}))
    runner.guard(turn_for([]), RunnerEvent("assistant_msg", {"text": FOREIGN, "final": False}))


def test_guard_leaves_the_compact_turn_to_the_core():
    CodexRunner().guard(turn_for([], turn_type="compact"), RunnerEvent("tool_call", {"tool": FOREIGN, "args": {}}))


def codex_lines(*items):
    lines = [{"type": "thread.started", "thread_id": "s1"}]
    lines += [{"type": "item.started", "item": item} for item in items]
    lines += [{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
    return [json.dumps(line).encode() + b"\n" for line in lines]


async def run_codex(items, mcp_allow):
    launcher = FakeLauncherClient()
    await launcher.create_bot("alpha", "owner-a")
    launcher.script_exec(stdout=codex_lines(*items))
    turn = turn_for(mcp_allow, launcher=launcher)
    events = []
    try:
        async for event in CodexRunner().run(turn):
            events.append(event)
    except McpNotAllowed as exc:
        return events, exc, launcher, turn
    return events, None, launcher, turn


async def test_codex_stream_with_a_foreign_tool_is_stopped_and_the_process_is_killed():
    item = {"id": "i1", "type": "mcp_tool_call", "server": "github", "tool": "create_issue", "arguments": {"title": "x"}}
    events, exc, launcher, turn = await run_codex([item], mcp_allow=[])
    assert exc is not None and exc.tool == "github.create_issue"
    assert [event.kind for event in events] == ["system"]  # вызов в ленту не попал
    assert launcher.stopped == [turn.turn_id]  # процесс в контейнере остановлен явно


async def test_codex_stream_with_an_allowed_tool_runs_to_the_end():
    item = {"id": "i1", "type": "mcp_tool_call", "server": "github", "tool": "create_issue", "arguments": {"title": "x"}}
    events, exc, launcher, _ = await run_codex([item], mcp_allow=["github"])
    assert exc is None and [event.kind for event in events] == ["system", "tool_call", "assistant_msg", "usage"]
    assert launcher.stopped == []


async def test_codex_stream_with_bothub_and_shell_calls_needs_no_list():
    own = {"id": "i1", "type": "mcp_tool_call", "server": "bothub", "tool": "remember", "arguments": {"text": "x"}}
    shell = {"id": "i2", "type": "command_execution", "command": "ls"}
    events, exc, _, _ = await run_codex([own, shell], mcp_allow=[])
    assert exc is None and [event.kind for event in events].count("tool_call") == 2


# ---- разбор имён: свой сервер только ровно bothub, неразобранное закрыто -----------------------------------------------

@pytest.mark.parametrize("tool,server", [
    ("mcp__bothub__remember", "bothub"),
    ("mcp__bothub_x__remember", "bothub_x"),  # сервер с общим началом
    ("mcp__bothub___remember", "bothub_"),  # лишний `_` отходит серверу, это не свой bothub с инструментом `_remember`
    ("mcp__bothub____remember", "bothub__"),
    ("bothub_.remember", "bothub_"),
    ("mcp__Bothub__remember", "Bothub"),
])
def test_server_name_is_parsed_exactly(tool, server):
    assert server_of(tool) == server
    assert is_foreign_mcp(tool) is (server != "bothub")


def test_bothub_lookalike_servers_need_the_list():
    for tool in ("mcp__bothub_x__remember", "mcp__bothub___remember", "bothub_.remember", "mcp__Bothub__remember"):
        assert mcp_allowed([], tool) is False
        assert mcp_allowed(["mcp__bothub__*"], tool) is False  # список своего сервера их не открывает
    assert mcp_allowed(["bothub_"], "mcp__bothub___remember") is True  # голое имя сервера с `_` на конце
    assert mcp_allowed(["bothub_x"], "mcp__bothub_x__remember") is True
    assert mcp_allowed(["bothub_x"], "mcp__bothub__remember") is True  # свой сервер проходит и так


def test_masks_compare_server_and_tool_separately():
    # сервер `github_` с инструментом `x` не подходит под `mcp__github__*`, хотя строка `mcp__github___x` на эту маску похожа
    assert mcp_allowed(["mcp__github__*"], "mcp__github___x") is False
    assert mcp_allowed(["github"], "mcp__github___x") is False
    assert mcp_allowed(["mcp__github_*__x"], "mcp__github___x") is True  # маска сервера может это покрыть явно
    assert mcp_allowed(["mcp__git*__create_*"], "mcp__github__create_issue") is True
    assert mcp_allowed(["mcp__git*"], "mcp__github__create_issue") is True  # без `__`: все серверы по маске
    assert mcp_allowed(["mcp__*"], "mcp__github__create_issue") is True
    assert mcp_allowed(["*"], "mcp__github__create_issue") is True  # явный «всё»
    assert mcp_allowed(["mcp__github__"], "mcp__github__create_issue") is False  # без инструмента ничего не значит


@pytest.mark.parametrize("tool", [
    "mcp__github",  # нет инструмента
    "mcp__github__",
    "mcp__",
    "mcp____x",  # нет сервера
    "mcp__bothub__",  # даже у своего сервера пустое имя не разобрать
    "mcp_tool_call",  # codex без server и tool в событии
    "github.create issue",  # пробел в имени инструмента
    "github.create/issue",
    "github.",
    ".create",
])
def test_unparsed_mcp_like_name_is_foreign_and_never_allowed(tool):
    assert canonical_tool(tool) is None
    assert looks_like_mcp(tool) and is_foreign_mcp(tool)
    for allow in ([], ["github"], ["mcp__github__*"], ["*"], ["mcp__*"], [tool]):
        assert mcp_allowed(allow, tool) is False


@pytest.mark.parametrize("tool", ["Bash", "Read", "WebSearch", "command_execution", "git_push", "delegate", "", None, 5])
def test_names_without_the_mcp_marks_are_not_mcp(tool):
    assert looks_like_mcp(tool) is False and is_foreign_mcp(tool) is False and mcp_allowed([], tool) is True


# ---- valid mcp_allow в теле запроса ------------------------------------------------------------------------------------

@pytest.mark.parametrize("model", [BotIn, BotPatch])
def test_mcp_allow_is_trimmed_deduplicated_and_keeps_the_order(model):
    base = {"name": "Scout", "provider": "claude", "model": "sonnet"} if model is BotIn else {}
    value = model.model_validate(base | {"mcp_allow": ["  mcp__github__*  ", "slack", "mcp__github__*", "slack "]}).mcp_allow
    assert value == ["mcp__github__*", "slack"]
    assert model.model_validate(base | {"mcp_allow": []}).mcp_allow == []


@pytest.mark.parametrize("model", [BotIn, BotPatch])
@pytest.mark.parametrize("bad", [["   "], ["a b"], ["a\tb"], ["a\nb"], ["mcp__x__y\x00"], ["ok", "\u200bx\u2028"], [" " * 300]])
def test_mcp_allow_rejects_blank_names_and_names_with_spaces_or_control_characters(model, bad):
    base = {"name": "Scout", "provider": "claude", "model": "sonnet"} if model is BotIn else {}
    with pytest.raises(ValueError):
        model.model_validate(base | {"mcp_allow": bad})


def test_mcp_allow_length_is_counted_after_trimming():
    BotPatch.model_validate({"mcp_allow": ["  " + "x" * 200 + "  "]})
    with pytest.raises(ValueError):
        BotPatch.model_validate({"mcp_allow": ["x" * 201]})


# ---- codex: bothub MCP через -c, без ~/.codex/config.toml -------------------------------------------------------------

def _config_values(command):
    return [command[i + 1] for i, part in enumerate(command) if part == "-c"]


@pytest.mark.parametrize("session", [None, "sess-1"])
def test_codex_declares_the_bothub_server_with_c_flags(session):
    command = CodexRunner().command(turn_for([], session=session))
    values = _config_values(command)
    assert 'mcp_servers.bothub.command="python"' in values
    assert 'mcp_servers.bothub.args=["-m","bothub.mcp_server"]' in values
    env_vars = next(v for v in values if v.startswith("mcp_servers.bothub.env_vars="))
    for name in ("BOTHUB_URL", "BOTHUB_TOKEN", "BOTHUB_THREAD_ID", "BOTHUB_TURN_ID"):
        assert f'"{name}"' in env_vars
    # без required=true codex не ждёт старта сервера перед первым запросом к модели и часть ходов идёт без инструментов bothub
    assert "mcp_servers.bothub.required=true" in values
    assert command[-1] == "-"  # промпт по-прежнему из stdin; флаги стоят до него


def test_codex_c_flags_pass_variable_names_not_values():
    # токен не должен попадать в argv: ps в контейнере его бы показал
    command = CodexRunner().command(turn_for([]))
    assert "bot:" not in " ".join(command) and "http" not in " ".join(BOTHUB_MCP_FLAGS)
    assert "env_vars" in " ".join(BOTHUB_MCP_FLAGS) and ".env=" not in " ".join(BOTHUB_MCP_FLAGS)


def test_codex_compact_turn_does_not_get_the_bothub_server():
    command = CodexRunner().command(turn_for([], turn_type="compact"))
    assert not any(value.startswith("mcp_servers.") for value in _config_values(command))


def test_codex_command_does_not_mutate_the_shared_flags():
    before = list(BOTHUB_MCP_FLAGS)
    runner = CodexRunner()
    runner.command(turn_for([]))
    runner.command(turn_for([], session="s"))
    assert BOTHUB_MCP_FLAGS == before


# ---- agy: непустой список это отказ -----------------------------------------------------------------------------------

def test_agy_refuses_a_turn_with_a_non_empty_list():
    with pytest.raises(McpAllowUnsupported) as caught:
        GeminiRunner().command(turn_for(["mcp__github__*"]))
    assert caught.value.provider == "gemini" and UNSUPPORTED in str(caught.value)
    assert not isinstance(caught.value, McpNotAllowed)


def test_agy_runs_with_an_empty_list_and_for_a_compact_turn():
    assert GeminiRunner().command(turn_for([]))[0] == "agy"
    assert GeminiRunner().command(turn_for(["github"], turn_type="compact"))[0] == "agy"


async def test_agy_refusal_happens_before_any_process_starts():
    launcher = FakeLauncherClient()
    await launcher.create_bot("alpha", "owner-a")
    launcher.script_exec(stdout=[])
    turn = turn_for(["github"], launcher=launcher)
    with pytest.raises(McpAllowUnsupported):
        async for _ in GeminiRunner().run(turn):
            pass
    assert launcher.stopped == [] and launcher.execs == []


async def test_other_runners_do_not_refuse_a_non_empty_list():
    for runner in (ClaudeRunner(), CodexRunner()):
        assert runner.command(turn_for(["github"]))


@pytest.mark.pure
@pytest.mark.parametrize('name', ['mcp__bothub__gh__delete_repo', 'bothub.gh__delete_repo', 'mcp__bothub__a__b__c'])
def test_server_names_starting_with_bothub_are_foreign(name):
    # сервер `bothub__gh` (codex принимает такие имена) не должен выдавать свои инструменты за встроенные
    from bothub.mcp_policy import is_foreign_mcp, mcp_allowed
    assert is_foreign_mcp(name) and not mcp_allowed([], name)


@pytest.mark.pure
@pytest.mark.parametrize('name', ['mcp__bothub__remember', 'mcp__bothub__schedule_wakeup', 'bothub.browser'])
def test_own_tools_stay_own(name):
    from bothub.mcp_policy import is_foreign_mcp, mcp_allowed
    assert not is_foreign_mcp(name) and mcp_allowed([], name)


@pytest.mark.pure
@pytest.mark.parametrize('item', ['bothub', 'mcp__bothub__*', 'Bothub__gh', 'github.*'])
def test_confusing_list_items_are_rejected(item):
    from bothub.main import check_mcp_allow
    with pytest.raises(Exception):
        check_mcp_allow([item])


@pytest.mark.pure
def test_codex_ignores_user_config_when_list_is_empty():
    from bothub.runner.base import TurnContext
    from bothub.runner.codex import CodexRunner
    def ctx(allow):
        return TurnContext(turn_id='t1', thread_id='th1', bot={'model': 'm', 'mcp_allow': allow}, prompt='p', cli_session_id=None, dry_run=False, memory_md='')
    assert '--ignore-user-config' in CodexRunner().command(ctx([]))
    assert '--ignore-user-config' not in CodexRunner().command(ctx(['github']))


@pytest.mark.pure
def test_codex_mcp_call_without_server_is_foreign():
    from bothub.runner.codex import CodexRunner
    from bothub.mcp_policy import mcp_allowed
    events = CodexRunner().parse({'type': 'item.started', 'item': {'id': 'x', 'type': 'mcp_tool_call', 'tool': 'delete_repo'}})
    assert events and not mcp_allowed([], events[0].payload['tool'])
