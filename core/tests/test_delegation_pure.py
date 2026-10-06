"""Поручения бота боту (docs/contracts.md, раздел 20): проверка входа, пределы, выбор получателя, текст результата,
лента, риск и MCP-инструменты. Без БД. Маршруты с Postgres: test_delegation_db.py."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from bothub import activity, delegation, mcp_server
from bothub.risk import READ_ONLY_TOOLS, decide, remember_rule

pytestmark = pytest.mark.pure


def refusal(call, *args):
    with pytest.raises(delegation.DelegationError) as failure:
        call(*args)
    return failure.value


def test_task_is_one_to_four_thousand_characters():
    assert delegation.clean_task('x') == 'x' and delegation.clean_task('x' * 4000)
    assert refusal(delegation.clean_task, '').code == 'task_empty' and refusal(delegation.clean_task, '  \n').code == 'task_empty'
    assert refusal(delegation.clean_task, None).code == 'task_empty'
    err = refusal(delegation.clean_task, 'x' * 4001)
    assert (err.code, err.status) == ('task_too_long', 422)


def test_target_is_a_short_non_empty_string():
    assert delegation.clean_target('  beta ') == 'beta'
    for bad in ('', '  ', None, 5, 'x' * 201):
        assert refusal(delegation.clean_target, bad).code == 'bot_invalid'


def test_self_delegation_and_depth_are_refused():
    assert refusal(delegation.check_not_self, 'a', 'a').code == 'self_delegation'
    delegation.check_not_self('a', 'b')
    assert refusal(delegation.check_depth, True).code == 'delegation_depth'
    delegation.check_depth(False)


def test_active_limit_is_five():
    delegation.check_active_limit(4)
    err = refusal(delegation.check_active_limit, 5)
    assert (err.code, err.status) == ('delegation_limit', 409)


@pytest.mark.parametrize('bot,block', [
    ({'paused': False, 'status': 'idle', 'registry_bound': False, 'provider_id': None}, None),
    ({'paused': True, 'status': 'idle', 'registry_bound': False, 'provider_id': None}, 'target_paused'),
    ({'paused': False, 'status': 'no_model', 'registry_bound': False, 'provider_id': None}, 'target_no_model'),
    ({'paused': False, 'status': 'idle', 'registry_bound': True, 'provider_id': None}, 'target_no_model'),
    ({'paused': False, 'status': 'error_starting', 'registry_bound': False, 'provider_id': None}, 'target_error_starting'),
])
def test_target_availability(bot, block):
    assert delegation.target_block(bot) == block


def test_message_and_thread_title():
    assert delegation.message_text('Alpha', 'Сделай') == 'Поручение от бота Alpha:\n\nСделай'
    assert delegation.thread_title('Alpha') == 'Поручения от Alpha'


def test_final_text_skips_text_before_the_last_tool_and_is_cut():
    events = [('assistant_msg', 'Сейчас'), ('tool_call', 'x'), ('assistant_msg', 'Между'), ('tool_result', 'y'), ('assistant_msg', 'Итог'), ('assistant_msg', 'тут')]
    assert delegation.final_text(events, 'claude') == 'Итог\n\nтут'
    assert delegation.final_text(events, 'gemini') == 'Итогтут'
    assert delegation.final_text([('assistant_msg', 'a'), ('tool_call', 'x')], 'claude') == 'a'  # после инструментов текста нет: весь текст
    assert delegation.final_text([], 'claude') == ''
    assert len(delegation.final_text([('assistant_msg', 'я' * 9000)], 'claude')) == 8000


def test_delegate_needs_the_owner_but_the_result_is_read_only():
    assert 'mcp__bothub__delegation_result' in READ_ONLY_TOOLS and 'mcp__bothub__delegate_to_bot' not in READ_ONLY_TOOLS
    args = {'bot': 'beta', 'task': 'x'}
    tool = 'mcp__bothub__delegate_to_bot'
    assert decide({'auto_allow': []}, tool, args) == ('other', False)
    assert decide({'auto_allow': [{'tool': 'mcp__bothub__delegation_result'}]}, 'mcp__bothub__delegation_result', {'turn_id': 'x'}) == ('other', True)
    # правило без точных аргументов поручение не разрешает: только закреплённое
    assert decide({'auto_allow': [{'tool': tool}]}, tool, args)[1] is False
    rule = remember_rule(tool, args)
    assert rule and decide({'auto_allow': [rule]}, tool, args)[1] is True
    assert decide({'auto_allow': [rule]}, tool, {'bot': 'beta', 'task': 'другое'})[1] is False


def test_feed_item_for_delegation_events():
    row = {'id': 'log:1', 'at': datetime(2026, 10, 6, tzinfo=timezone.utc), 'bot_id': 'alpha', 'thread_id': None,
           'log_kind': 'schedule', 'code': 'delegation_sent',
           'params': json.dumps({'from_bot': 'Alpha', 'to_bot': 'Beta', 'to_bot_id': 'beta', 'turn_id': 't1', 'task': 'Собери  отчёт', 'secret': 'x'})}
    entry = activity.build_item('log', row)
    assert entry['kind'] == 'schedule' and entry['title']['code'] == 'delegation_sent' and entry['detail'] == 'Собери отчёт'
    assert entry['title']['params'] == {'from_bot': 'Alpha', 'to_bot': 'Beta', 'to_bot_id': 'beta', 'turn_id': 't1'}
    done = activity.build_item('log', {**row, 'code': 'delegation_done', 'params': json.dumps({'outcome': 'done', 'task': 'не показываем'})})
    assert done['title']['params'] == {'outcome': 'done'} and done.get('detail') is None


# ---- MCP-инструменты ----

class FakeCore:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.seen = status, body if body is not None else {'turn_id': 't', 'thread_id': 'th'}, []

    def client(self):
        def handler(request: httpx.Request):
            self.seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None, request.headers.get('authorization')))
            return httpx.Response(self.status, json=self.body)
        return httpx.AsyncClient(base_url='http://core', transport=httpx.MockTransport(handler), headers={'Authorization': 'Bearer bot:alpha:sig'})


@pytest.fixture
def core(monkeypatch):
    monkeypatch.setenv('BOTHUB_TURN_ID', 'turn-1')
    fake = FakeCore()
    monkeypatch.setattr(mcp_server, '_client', fake.client)
    return fake


async def test_delegate_posts_with_the_current_turn(core):
    assert await mcp_server.delegate_to_bot('beta', 'Собери отчёт') == {'ok': True, 'turn_id': 't', 'thread_id': 'th'}
    assert core.seen == [('POST', '/api/bots/delegations', {'bot': 'beta', 'task': 'Собери отчёт', 'turn_id': 'turn-1'}, 'Bearer bot:alpha:sig')]


@pytest.mark.parametrize('bot,task,code', [('beta', '', 'task_empty'), ('beta', 'x' * 4001, 'task_too_long'), ('', 'x', 'bot_invalid')])
async def test_delegate_refuses_bad_input_before_calling_the_core(core, bot, task, code):
    assert await mcp_server.delegate_to_bot(bot, task) == {'ok': False, 'error': code}
    assert core.seen == []


async def test_core_refusals_come_back_as_data(monkeypatch):
    monkeypatch.setenv('BOTHUB_TURN_ID', 'turn-1')
    monkeypatch.setattr(mcp_server, '_client', FakeCore(409, {'error': 'conflict', 'detail': 'delegation_limit'}).client)
    assert await mcp_server.delegate_to_bot('beta', 'x') == {'ok': False, 'error': 'delegation_limit'}
    monkeypatch.setattr(mcp_server, '_client', FakeCore(500, {'x': 1}).client)
    assert await mcp_server.delegation_result('t') == {'ok': False, 'error': 'http_500'}


async def test_result_reads_the_turn(monkeypatch):
    fake = FakeCore(200, {'turn_id': 't', 'status': 'done', 'result': 'ok'})
    monkeypatch.setattr(mcp_server, '_client', fake.client)
    assert await mcp_server.delegation_result('t') == {'ok': True, 'turn_id': 't', 'status': 'done', 'result': 'ok'}
    assert fake.seen[0][:2] == ('GET', '/api/bots/delegations/t')


async def test_tools_are_registered():
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert {'remember', 'schedule_wakeup', 'delegate_to_bot', 'delegation_result'} <= names
