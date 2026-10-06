"""Самопробуждение бота (docs/contracts.md, раздел 17): проверка входа, пределы, окно повтора, решение планировщика,
лента и MCP-инструмент `schedule_wakeup`. Без БД. Маршруты и планировщик с Postgres: test_wakeups_db.py."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from bothub import activity, mcp_server, wakeups
from bothub import main as hub

pytestmark = pytest.mark.pure

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def refusal(call, *args):
    with pytest.raises(wakeups.WakeupError) as failure:
        call(*args)
    return failure.value


# ---- срок ----

def test_in_minutes_gives_now_plus_minutes():
    assert wakeups.resolve_time(None, 90, NOW) == NOW + timedelta(minutes=90)


def test_in_minutes_bounds_are_one_minute_and_thirty_days():
    assert wakeups.resolve_time(None, 1, NOW) == NOW + timedelta(minutes=1)
    assert wakeups.resolve_time(None, 30 * 24 * 60, NOW) == NOW + timedelta(days=30)
    for value, code in ((0, 'too_soon'), (-5, 'too_soon'), (30 * 24 * 60 + 1, 'too_far'), (10**12, 'too_far')):
        assert refusal(wakeups.resolve_time, None, value, NOW).code == code


@pytest.mark.parametrize('value', [True, 1.5, '10', [10]])
def test_in_minutes_must_be_a_plain_integer(value):
    assert refusal(wakeups.resolve_time, None, value, NOW).code == 'in_minutes_invalid'


def test_exactly_one_of_at_and_in_minutes():
    assert refusal(wakeups.resolve_time, None, None, NOW).code == 'time_required'
    assert refusal(wakeups.resolve_time, '2026-10-07T12:00:00Z', 5, NOW).code == 'time_conflict'


def test_at_bounds_are_one_minute_and_thirty_days_from_now():
    assert wakeups.resolve_time('2026-10-06T12:01:00Z', None, NOW) == NOW + timedelta(minutes=1)
    assert wakeups.resolve_time('2026-11-05T12:00:00Z', None, NOW) == NOW + timedelta(days=30)
    assert refusal(wakeups.resolve_time, '2026-10-06T12:00:59Z', None, NOW).code == 'too_soon'
    assert refusal(wakeups.resolve_time, '2026-10-01T00:00:00Z', None, NOW).code == 'too_soon'
    assert refusal(wakeups.resolve_time, '2026-11-05T12:00:01Z', None, NOW).code == 'too_far'


def test_at_zone_handling():
    assert wakeups.resolve_time('2026-10-06T15:30:00+03:00', None, NOW) == datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)
    assert wakeups.resolve_time('2026-10-06T12:30:00', None, NOW) == datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)  # без пояса это UTC
    assert wakeups.resolve_time('2026-10-06T12:30:00z', None, NOW) == datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)
    assert wakeups.resolve_time(' 2026-10-06T12:30:00Z ', None, NOW).tzinfo == timezone.utc


@pytest.mark.parametrize('value', ['', '   ', 'завтра', 'tomorrow 9am', '2026-13-45T00:00:00Z', 5, ['x'], 'x' * 65,
                                   '0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00'])
def test_at_garbage_is_rejected_without_echo(value):
    error = refusal(wakeups.resolve_time, value, None, NOW)
    assert (error.code, error.status) == ('at_invalid', 400)
    assert str(error) == 'at_invalid'  # значение клиента в сообщении не повторяется


# ---- prompt, reason, предел ----

def test_prompt_limits():
    assert wakeups.clean_prompt('x' * 2000) == 'x' * 2000
    too_long = refusal(wakeups.clean_prompt, 'x' * 2001)
    assert (too_long.code, too_long.status) == ('prompt_too_long', 422)
    for value in ('', '   \n', None, 5):
        assert refusal(wakeups.clean_prompt, value).code == 'prompt_empty'


def test_prompt_is_kept_as_written():
    assert wakeups.clean_prompt('  Проверь сборку\nи напиши  ') == '  Проверь сборку\nи напиши  '


def test_reason_limits():
    assert wakeups.clean_reason(None) == '' and wakeups.clean_reason('') == ''
    assert wakeups.clean_reason('r' * 200) == 'r' * 200
    too_long = refusal(wakeups.clean_reason, 'r' * 201)
    assert (too_long.code, too_long.status) == ('reason_too_long', 422)
    assert refusal(wakeups.clean_reason, 5).code == 'reason_invalid'


def test_active_limit_is_twenty():
    wakeups.check_active_limit(19)
    error = refusal(wakeups.check_active_limit, 20)
    assert (error.code, error.status) == ('wakeup_limit', 409)
    assert refusal(wakeups.check_active_limit, 21).code == 'wakeup_limit'


# ---- повтор ----

def rows(*pairs):
    return [{'scheduled_at': at, 'prompt': prompt} for at, prompt in pairs]


def test_same_prompt_within_a_minute_is_the_same_wakeup():
    at = NOW + timedelta(hours=1)
    assert wakeups.is_duplicate(at, 'p', at, 'p')
    assert wakeups.is_duplicate(at, 'p', at + timedelta(minutes=1), 'p')
    assert wakeups.is_duplicate(at, 'p', at - timedelta(minutes=1), 'p')
    assert not wakeups.is_duplicate(at, 'p', at + timedelta(minutes=1, seconds=1), 'p')
    assert not wakeups.is_duplicate(at, 'p', at - timedelta(minutes=2), 'p')


def test_other_prompt_is_never_a_duplicate():
    at = NOW + timedelta(hours=1)
    assert not wakeups.is_duplicate(at, 'p', at, 'q')
    assert not wakeups.is_duplicate(at, 'p', at, 'p ')  # другой текст: разный prompt


def test_find_duplicate_returns_the_matching_row_or_none():
    at = NOW + timedelta(hours=1)
    found = rows((at + timedelta(days=1), 'p'), (at + timedelta(seconds=30), 'p'), (at, 'q'))
    assert wakeups.find_duplicate(found, at, 'p') is found[1]
    assert wakeups.find_duplicate(found, at, 'z') is None
    assert wakeups.find_duplicate([], at, 'p') is None


# ---- решение планировщика ----

def test_decide_fires_when_nothing_blocks():
    assert wakeups.decide(None, NOW - timedelta(seconds=1), NOW) == 'fire'
    assert wakeups.decide(None, NOW - timedelta(days=3), NOW) == 'fire'


def test_decide_skips_paused_bot_at_once():
    assert wakeups.decide('bot_paused', NOW, NOW) == 'skip'


@pytest.mark.parametrize('block', ['executor_unavailable', 'provider_unavailable', 'check_failed'])
def test_decide_waits_for_the_executor_then_skips_after_grace(block):
    assert wakeups.decide(block, NOW - timedelta(minutes=14, seconds=59), NOW) == 'wait'
    assert wakeups.decide(block, NOW - wakeups.GRACE, NOW) == 'skip'
    assert wakeups.decide(block, NOW - timedelta(hours=2), NOW) == 'skip'


def test_every_skip_reason_has_a_column_value_and_a_text():
    for reason in activity.SKIP_REASONS:
        assert wakeups.skipped_text(reason).startswith('Пробуждение пропущено:')
        assert wakeups.skipped_text(reason) != wakeups.skipped_text('unknown')


def test_turn_text_names_the_reason_and_keeps_the_prompt():
    assert wakeups.fire_text('Проверь сборку', '').endswith('\n\nПроверь сборку')
    text = wakeups.fire_text('Проверь сборку', 'жду CI')
    assert 'Самопробуждение' in text and 'Причина: жду CI' in text and text.endswith('Проверь сборку')


def test_scheduled_text_is_in_utc():
    assert wakeups.scheduled_text(datetime(2026, 10, 7, 9, 5, tzinfo=timezone(timedelta(hours=3)))) == \
        'Бот запланировал пробуждение на 2026-10-07 06:05 UTC.'


# ---- модель запроса ----

def test_request_model_rejects_unknown_fields_and_non_integer_minutes():
    ok = hub.WakeupIn(prompt='p', in_minutes=5, reason='r', thread_id=str(uuid.uuid4()))
    assert ok.in_minutes == 5 and ok.at is None
    for bad in ({'prompt': 'p', 'in_minutes': '5'}, {'prompt': 'p', 'in_minutes': 5.5}, {'prompt': 'p', 'bot_id': 'other'},
                {'in_minutes': 5}, {'prompt': 'p', 'thread_id': 'not-a-uuid'}):
        with pytest.raises(ValidationError):
            hub.WakeupIn(**bad)


# ---- лента ----

def log_row(code, params):
    return {'id': 'log:1', 'at': NOW, 'bot_id': 'alpha', 'thread_id': None, 'log_kind': 'schedule', 'code': code, 'params': params}


def test_feed_shows_wakeup_codes_with_the_bot_note_as_detail():
    for code in wakeups.CODES:
        built = activity.build_item('log', log_row(code, {'wakeup_id': 'w1', 'scheduled_at': '2026-10-06T13:00:00+00:00',
                                                          'note': 'жду CI', 'reason': 'bot_paused', 'prompt': 'SECRET'}))
        assert built['kind'] == 'schedule' and built['title']['code'] == code and built['detail'] == 'жду CI'
        assert built['title']['params'] == {'wakeup_id': 'w1', 'scheduled_at': '2026-10-06T13:00:00+00:00', 'reason': 'bot_paused'}
        assert 'SECRET' not in repr(built)


def test_feed_note_is_only_for_wakeup_codes():
    built = activity.build_item('log', log_row('schedule_skipped', {'note': 'чужой текст', 'reason': 'bot_paused'}))
    assert built['detail'] is None


def test_feed_note_is_shortened():
    built = activity.build_item('log', log_row('wakeup_scheduled', {'note': 'n' * 500}))
    assert len(built['detail']) == activity.TEXT_MAX


def test_activity_codes_match_the_wakeup_codes():
    assert set(wakeups.CODES) == {'wakeup_scheduled', 'wakeup_fired', 'wakeup_skipped'}
    assert set(wakeups.STATUSES) == {'active', 'fired', 'skipped'}


# ---- MCP-инструмент ----

class FakeCore:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.seen = status, body if body is not None else {'id': 'w1', 'scheduled_at': 'x'}, []

    def client(self):
        def handler(request: httpx.Request):
            import json
            self.seen.append((request.method, request.url.path, json.loads(request.content), request.headers.get('authorization')))
            return httpx.Response(self.status, json=self.body)
        return httpx.AsyncClient(base_url='http://core', transport=httpx.MockTransport(handler),
                                 headers={'Authorization': 'Bearer bot:alpha:sig'})


@pytest.fixture
def core(monkeypatch):
    monkeypatch.setenv('BOTHUB_THREAD_ID', 'thread-1')
    fake = FakeCore()
    monkeypatch.setattr(mcp_server, '_client', fake.client)
    return fake


async def test_tool_posts_to_the_bot_route_with_the_current_thread(core):
    result = await mcp_server.schedule_wakeup('Проверь сборку', reason='жду CI', in_minutes=30)
    assert result == {'ok': True, 'id': 'w1', 'scheduled_at': 'x'}
    method, path, body, auth_header = core.seen[0]
    assert (method, path, auth_header) == ('POST', '/api/bots/wakeups', 'Bearer bot:alpha:sig')
    assert body == {'prompt': 'Проверь сборку', 'reason': 'жду CI', 'thread_id': 'thread-1', 'in_minutes': 30}


async def test_tool_passes_at_instead_of_in_minutes(core):
    await mcp_server.schedule_wakeup('p', at='2026-10-07T09:00:00+03:00')
    assert core.seen[0][2] == {'prompt': 'p', 'reason': '', 'thread_id': 'thread-1', 'at': '2026-10-07T09:00:00+03:00'}


@pytest.mark.parametrize('kwargs,code', [({'prompt': ''}, 'prompt_empty'), ({'prompt': 'x' * 2001}, 'prompt_too_long'),
                                         ({'prompt': 'p', 'reason': 'r' * 201}, 'reason_too_long')])
async def test_tool_refuses_bad_text_before_calling_the_core(core, kwargs, code):
    assert await mcp_server.schedule_wakeup(**kwargs, in_minutes=5) == {'ok': False, 'error': code}
    assert core.seen == []


async def test_tool_reports_core_refusals_as_data(monkeypatch):
    monkeypatch.setenv('BOTHUB_THREAD_ID', 'thread-1')
    fake = FakeCore(409, {'detail': 'wakeup_limit'})
    monkeypatch.setattr(mcp_server, '_client', fake.client)
    assert await mcp_server.schedule_wakeup('p', in_minutes=5) == {'ok': False, 'error': 'wakeup_limit'}
    fake = FakeCore(422, {'detail': [{'msg': 'x'}]})
    monkeypatch.setattr(mcp_server, '_client', fake.client)
    assert await mcp_server.schedule_wakeup('p', in_minutes=5) == {'ok': False, 'error': 'http_422'}


async def test_tool_is_registered_next_to_remember():
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert {'remember', 'schedule_wakeup'} <= names
