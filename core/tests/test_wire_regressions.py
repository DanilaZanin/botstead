"""Checks that do not require the database or Docker daemon."""
import sys

import pytest

from bothub.main import BotIn, BotPatch, runner_provider
from pydantic import ValidationError
from bothub.runner.base import RunnerEvent, TurnContext
from bothub.runner.subprocess import SubprocessRunner

pytestmark = pytest.mark.pure


def test_subscription_cli_maps_to_supported_runner():
    assert runner_provider('cli_subscription', 'agy') == 'gemini'
    assert runner_provider('cli_subscription', 'claude') == 'claude'
    assert runner_provider('openai_api', None) == 'codex'


@pytest.mark.parametrize('field,value', [('mac_full_control','yes'), ('budget_daily_tokens','100'),
                                          ('max_turn_seconds',True)])
def test_bot_models_reject_coerced_control_types(field, value):
    base = {'name':'Scout','provider':'claude','model':'sonnet'}
    with pytest.raises(ValidationError):
        BotIn.model_validate(base | {field:value})
    with pytest.raises(ValidationError):
        BotPatch.model_validate({field:value})


@pytest.mark.parametrize('field,value', [('start_container','false'), ('skip_container',1)])
def test_bot_creation_rejects_coerced_container_flags(field, value):
    with pytest.raises(ValidationError):
        BotIn.model_validate({'name':'Scout','provider':'claude','model':'sonnet',field:value})


@pytest.mark.parametrize('field,value', [
    ('name','x'*81), ('instructions','x'*(64*1024+1)),
    ('budget_daily_tokens',-1), ('budget_daily_tokens',10**12+1),
    ('max_turn_seconds',29), ('max_turn_seconds',86401),
    ('mcp_allow',['x']*201), ('auto_allow',[{}]*201),
])
def test_bot_models_enforce_input_bounds(field, value):
    base = {'name':'Scout','provider':'claude','model':'sonnet'}
    with pytest.raises(ValidationError):
        BotIn.model_validate(base | {field:value})
    with pytest.raises(ValidationError):
        BotPatch.model_validate({field:value})


@pytest.mark.parametrize('field,value', [
    ('budget_daily_tokens',0), ('budget_daily_tokens',10**12),
    ('max_turn_seconds',30), ('max_turn_seconds',86400),
    ('name','x'*80), ('instructions','x'*(64*1024)),
    ('mcp_allow',['x']*200), ('auto_allow',[{}]*200),
])
def test_bot_models_accept_bounds(field, value):
    base = {'name':'Scout','provider':'claude','model':'sonnet'}
    BotIn.model_validate(base | {field:value})
    BotPatch.model_validate({field:value})


@pytest.mark.asyncio
async def test_local_runner_receives_gateway_env_without_upstream_key(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'upstream-secret')

    class ProbeRunner(SubprocessRunner):
        provider = 'probe'

        def command(self, turn):
            script = ('import json,os; print(json.dumps({"token":os.getenv("BOTHUB_GATEWAY_TOKEN"),'
                      '"upstream":os.getenv("OPENAI_API_KEY")}))')
            return [sys.executable, '-c', script]

        def parse(self, message):
            return [RunnerEvent('system', message)]

    turn = TurnContext('local-env', 'thread', {'max_turn_seconds': 5, '_gateway_kind': 'openai_api'},
                       '', None, False, '', exec_env={'BOTHUB_GATEWAY_TOKEN': 'turn-token'})
    events = [event async for event in ProbeRunner().run(turn)]
    assert events[0].payload == {'token': 'turn-token', 'upstream': None}
