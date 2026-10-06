"""Служебный ход сжатия: раннеры запускают CLI без инструментов (docs/contracts.md, раздел 15). Без БД.
Флаги взяты из `claude --help`, `codex exec [resume] --help`, `codex features list`, `agy --help`; чего у CLI нет, там
запрет держит слой ядра (test_stage8_review_compact_pure.py)."""
import pytest

from bothub.runner import ClaudeRunner, CodexRunner, GeminiRunner
from bothub.runner.base import TurnContext

pytestmark = pytest.mark.pure


def runner_turn(turn_type='normal', session='sess-1', dry_run=False, mcp_allow=None):
    return TurnContext(turn_id='t1', thread_id='th1', bot={'model': 'm', 'instructions': 'be brief', 'mcp_allow': mcp_allow or []}, prompt='p',
                       cli_session_id=session, dry_run=dry_run, memory_md='', turn_type=turn_type)


def option(command, name):
    return command[command.index(name) + 1]


def disabled_features(command):
    return [command[i + 1] for i, part in enumerate(command) if part == '--disable']


def test_turn_context_defaults_to_a_normal_turn():
    assert runner_turn().turn_type == 'normal'


def test_claude_compact_turn_has_no_tools_and_no_mcp():
    command = ClaudeRunner().command(runner_turn('compact'))
    assert option(command, '--tools') == ''  # `--tools ""`: ни одного встроенного инструмента
    assert '--strict-mcp-config' in command  # чужие MCP-конфиги не подхватываются
    assert '--mcp-config' not in command and '--permission-prompt-tool' not in command
    assert option(command, '--resume') == 'sess-1' and option(command, '--model') == 'm'  # сессия и модель те же


def test_claude_normal_turn_keeps_tools_and_mcp():
    # с непустым mcp_allow чужие MCP-серверы из настроек CLI доступны (их инструменты режет approve); пустой список: test_mcp_allow_pure.py
    command = ClaudeRunner().command(runner_turn(mcp_allow=['mcp__github__*']))
    assert '--tools' not in command and '--strict-mcp-config' not in command
    assert '--mcp-config' in command and option(command, '--permission-prompt-tool') == 'mcp__bothub__approve'


def test_codex_compact_turn_is_read_only_and_without_shell_tools():
    resumed = CodexRunner().command(runner_turn('compact'))
    assert resumed[:4] == ['codex', 'exec', 'resume', 'sess-1'] and '-s' not in resumed  # у resume флага -s нет
    assert 'sandbox_mode="read-only"' in resumed  # то же значение через -c
    assert {'shell_tool', 'unified_exec'} <= set(disabled_features(resumed))
    fresh = CodexRunner().command(runner_turn('compact', session=None))
    assert option(fresh, '-s') == 'read-only' and {'shell_tool', 'unified_exec'} <= set(disabled_features(fresh))


def test_codex_normal_turn_is_unchanged():
    fresh = CodexRunner().command(runner_turn(session=None))
    assert option(fresh, '-s') == 'workspace-write' and not disabled_features(fresh)
    resumed = CodexRunner().command(runner_turn())
    assert not disabled_features(resumed) and 'sandbox_mode="read-only"' not in resumed


def test_gemini_compact_turn_runs_in_plan_mode_without_auto_approval():
    command = GeminiRunner().command(runner_turn('compact'))
    assert option(command, '--mode') == 'plan' and '--dangerously-skip-permissions' not in command
    assert command[-2] == '-p' and option(command, '--conversation') == 'sess-1'
    normal = GeminiRunner().command(runner_turn())
    assert '--mode' not in normal and '--dangerously-skip-permissions' in normal


# --- codex context window -------------------------------------------------------

def test_codex_command_has_no_context_window_options_when_unknown():
    command = CodexRunner().command(runner_turn())
    assert 'model_context_window' not in ' '.join(command)
    assert 'model_auto_compact_token_limit' not in ' '.join(command)


def test_codex_command_has_context_window_options_when_known():
    turn = TurnContext(turn_id='t1', thread_id='th1',
                       bot={'model': 'm', 'instructions': 'be brief',
                            '_gateway_kind': 'openai_compatible',
                            '_gateway_url': 'https://gw.example',
                            '_context_window': 128000},
                       prompt='p', cli_session_id=None, dry_run=False, memory_md='')
    command = CodexRunner().command(turn)
    joined = ' '.join(command)
    assert '-c model_context_window=128000' in joined
    assert '-c model_auto_compact_token_limit=102400' in joined


def test_codex_command_no_window_options_without_gateway():
    turn = TurnContext(turn_id='t1', thread_id='th1',
                       bot={'model': 'm', '_context_window': 64000},
                       prompt='p', cli_session_id=None, dry_run=False, memory_md='')
    command = CodexRunner().command(turn)
    joined = ' '.join(command)
    assert 'model_context_window' not in joined
    assert 'model_auto_compact_token_limit' not in joined
