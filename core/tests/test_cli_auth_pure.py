"""Истёкший вход подписочных CLI не превращается в ответ бота."""
import json
import sys

import pytest

from bothub.launcher_client import ExecChunk, ExecExit
from bothub.runner.base import TurnContext
from bothub.runner.claude import ClaudeRunner
from bothub.runner.codex import CodexRunner
from bothub.runner.gemini import GeminiRunner
from bothub.runner.subprocess import CliAuthExpired, SubprocessRunner, is_cli_auth_error


pytestmark = pytest.mark.pure


@pytest.mark.parametrize('text', [
    'Failed to authenticate: OAuth session expired and could not be refreshed',
    'OAuth session expired', 'Not logged in', 'Please run /login',
    'login required', 'invalid_grant', 'HTTP 401 Unauthorized', 'status code: 401', '401 Unauthorized', '401',
])
def test_cli_auth_errors(text):
    assert is_cli_auth_error(text)


@pytest.mark.parametrize('text', [
    '', 'Found 401 files', 'Use /login to authenticate', 'login page is ready',
])
def test_regular_reply_is_not_auth_error(text):
    assert not is_cli_auth_error(text)


def test_ordinary_assistant_text_about_http_401_is_not_an_auth_failure():
    assert not is_cli_auth_error('The HTTP 401 response is documented here', diagnostic=False)
    assert not is_cli_auth_error('The message "Not logged in" means you need to sign in.', diagnostic=False)


class Launcher:
    def __init__(self, message):
        self.message = message
        self.stopped = False

    async def exec(self, *args, **kwargs):
        yield ExecChunk('stdout', (json.dumps(self.message) + '\n').encode())
        yield ExecExit(1, 'exit')

    async def stop_exec(self, *args, **kwargs):
        self.stopped = True


@pytest.mark.parametrize(('runner', 'message'), [
    (ClaudeRunner, {'type': 'assistant', 'message': {'content': [
        {'type': 'text', 'text': 'Failed to authenticate: OAuth session expired and could not be refreshed'}]}}),
    (CodexRunner, {'type': 'turn.failed', 'error': {'message': 'Not logged in'}}),
    (CodexRunner, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'login required'}}),
    (GeminiRunner, {'event': 'result', 'result': {'status': 'ERROR', 'error': 'invalid_grant'}}),
    (GeminiRunner, {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': 'Not logged in'}}),
])
@pytest.mark.asyncio
async def test_auth_diagnostic_is_not_published(runner, message):
    launcher = Launcher(message)
    turn = TurnContext('turn', 'thread', {'model': '', 'max_turn_seconds': 5}, 'prompt', None, False, '',
                       launcher=launcher, bot_container_id='bot')
    with pytest.raises(CliAuthExpired, match='Provider sign-in expired'):
        assert [event async for event in runner().run(turn)] == []
    assert launcher.stopped


@pytest.mark.asyncio
async def test_local_stderr_auth_error_is_reported_without_raw_text():
    class Local(SubprocessRunner):
        provider = 'claude'

        def command(self, turn):
            return [sys.executable, '-c',
                    'import sys; sys.stderr.write("OAuth session expired\\n"); sys.exit(1)']

        def parse(self, message):
            return []

    turn = TurnContext('turn', 'thread', {'max_turn_seconds': 5}, '', None, False, '')
    with pytest.raises(CliAuthExpired, match='Provider sign-in expired'):
        assert [event async for event in Local().run(turn)] == []
