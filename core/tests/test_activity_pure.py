"""Лента активности: фильтры, курсор, сборка элементов, слияние страниц, решения про паузу триггеров. Без БД."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bothub import activity

pytestmark = pytest.mark.pure
T0 = datetime(2026, 10, 5, 12, 0, 0, 123456, tzinfo=timezone.utc)


def row(item_id, at=T0, **fields):
    return {'id': item_id, 'at': at, 'bot_id': 'scout', 'thread_id': None, 'turn_id': None} | fields


# ---- параметры ----

def test_kinds_default_is_all_and_values_are_ordered_without_repeats():
    assert activity.parse_kinds(None) is None and activity.parse_kinds('') is None
    assert activity.parse_kinds('turn') == ['turn']
    assert activity.parse_kinds('approval, turn,approval') == ['approval', 'turn']


@pytest.mark.parametrize('value', ['bogus', 'turn,bogus', 'turn,,approval', 'turn,', ',', 'TURN', 'turn;approval'])
def test_unknown_kind_is_rejected_without_echo(value):
    with pytest.raises(activity.ActivityError) as failure:
        activity.parse_kinds(value)
    assert str(failure.value) == 'kind' and 'bogus' not in str(failure.value)


def test_every_documented_kind_is_accepted():
    assert activity.parse_kinds(','.join(activity.KINDS)) == list(activity.KINDS)
    assert activity.KINDS == ('turn', 'approval', 'browser', 'takeover', 'schedule', 'procedure', 'memory', 'pause')


def test_limit_defaults_to_50_and_is_bounded_1_to_100():
    assert activity.parse_limit(None) == 50
    assert activity.parse_limit('1') == 1 and activity.parse_limit('100') == 100 and activity.parse_limit(25) == 25
    for bad in ('0', '101', '-1', 'abc', '7.5', '', ' ', '+7', True, 0, 101, 1.5, '١٢'):
        with pytest.raises(activity.ActivityError):
            activity.parse_limit(bad)


# ---- курсор ----

def test_cursor_roundtrips_microseconds_and_id_with_separators():
    cursor = activity.encode_cursor(T0, 'turn:5b0e:start')
    assert cursor.isascii() and '=' not in cursor and 'turn' not in cursor  # непрозрачная строка
    assert activity.decode_cursor(cursor) == (T0, 'turn:5b0e:start')
    other_zone = T0.astimezone(timezone(timedelta(hours=3)))
    assert activity.decode_cursor(activity.encode_cursor(other_zone, 'event:5b0e:7')) == (T0, 'event:5b0e:7')


@pytest.mark.parametrize('bad', ['', 'not base64!', 'YQ', None, 5, 'x' * 500])
def test_garbage_cursor_is_rejected(bad):
    with pytest.raises(activity.ActivityError) as failure:
        activity.decode_cursor(bad)
    assert str(failure.value) == 'before'


def test_cursor_without_timezone_or_id_is_rejected():
    import base64
    def forge(text):
        return base64.urlsafe_b64encode(text.encode()).rstrip(b'=').decode()
    for text in ('2026-10-05T12:00:00|turn:x', '2026-10-05T12:00:00+00:00|', '2026-10-05T12:00:00+00:00', 'garbage|x',
                 '2026-10-05T12:00:00+00:00|тест'):
        with pytest.raises(activity.ActivityError):
            activity.decode_cursor(forge(text))


@pytest.mark.parametrize('text', [
    "2026-10-05T12:00:00+00:00|log:x' or '1'='1",  # SQL-фрагмент в id
    '2026-10-05T12:00:00+00:00|log:a\x00b', '2026-10-05T12:00:00+00:00|log:a\nb', '2026-10-05T12:00:00+00:00|log:',
    '2026-10-05T12:00:00+00:00|zzz', '2026-10-05T12:00:00+00:00|x', '2026-10-05T12:00:00+00:00|other:1',  # нет известного вида
    '2026-10-05T12:00:00+00:00|log:a|b', '2026-10-05T12:00:00+00:00|log:' + 'a' * 121,
    '2026-10-05 12:00:00+00:00|log:1', '2026-10-05T12:00:00+0000|log:1', '20261005T120000+00:00|log:1',  # не строгая ISO-дата
    '2026-13-45T12:00:00+00:00|log:1', '2026-10-05T12:00:00+00:00\n|log:1', ' 2026-10-05T12:00:00+00:00|log:1',
])
def test_cursor_is_parsed_strictly(text):
    import base64
    with pytest.raises(activity.ActivityError) as failure:
        activity.decode_cursor(base64.urlsafe_b64encode(text.encode()).rstrip(b'=').decode())
    assert str(failure.value) == 'before'


@pytest.mark.parametrize('item_id', ['turn:5b0e:start', 'approval:5b0e-1:dec', 'event:5b0e:12', 'run:1', 'procedure:1_2:end', 'memory:7',
                                     'log:' + 'a' * 120])
def test_every_item_id_format_the_feed_issues_is_a_valid_cursor_id(item_id):
    assert activity.decode_cursor(activity.encode_cursor(T0, item_id)) == (T0, item_id)


def test_a_cursor_from_the_future_with_a_valid_id_is_valid():
    import base64
    forged = base64.urlsafe_b64encode(b'2999-01-01T00:00:00Z|log:zzz').rstrip(b'=').decode()
    assert activity.decode_cursor(forged) == (datetime(2999, 1, 1, tzinfo=timezone.utc), 'log:zzz')


@pytest.mark.parametrize('stamp', ['0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00', '0001-01-01T00:00:00.000001+23:59',
                                   '9999-12-31T23:59:59.999999-23:59'])
def test_cursor_at_the_edge_of_the_calendar_is_rejected_not_crashed(stamp):
    """В UTC такое время не помещается (OverflowError): это неверный курсор (422), а не 500."""
    import base64
    forged = base64.urlsafe_b64encode(f'{stamp}|log:x'.encode()).rstrip(b'=').decode()
    with pytest.raises(activity.ActivityError) as failure:
        activity.decode_cursor(forged)
    assert str(failure.value) == 'before'


def test_cursor_at_the_edge_in_utc_still_roundtrips():
    edge = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert activity.decode_cursor(activity.encode_cursor(edge, 'log:x')) == (edge, 'log:x')
    first = datetime(1, 1, 1, tzinfo=timezone.utc)
    assert activity.decode_cursor(activity.encode_cursor(first, 'log:x')) == (first, 'log:x')


# ---- слияние и страницы ----

def walk(groups, limit):
    """Проходит все страницы так, как это делает маршрут: источник отдаёт limit+1 своих строк после курсора."""
    seen, before, pages = [], None, 0
    while True:
        offered = [sorted((r for r in group if before is None or activity.sort_key(r) < before),
                          key=activity.sort_key, reverse=True)[:limit + 1] for group in groups]
        page, cursor = activity.merge_page(offered, limit, before)
        seen.extend(r['id'] for r in page)
        pages += 1
        assert pages < 100
        if cursor is None:
            return seen, pages
        before = activity.decode_cursor(cursor)


def test_ties_on_equal_time_page_without_duplicates_or_gaps():
    first = [row(f'log:a{n}') for n in range(7)]
    second = [row(f'log:b{n}') for n in range(7)] + [row(f'log:c{n}', T0 - timedelta(seconds=1)) for n in range(4)]
    seen, pages = walk([first, second], 3)
    expected = [r['id'] for r in sorted(first + second, key=activity.sort_key, reverse=True)]
    assert seen == expected and len(set(seen)) == 18 and pages == 6


def test_order_is_newest_first_and_id_descending_on_equal_time():
    page, cursor = activity.merge_page([[row('x:1'), row('x:3')], [row('x:2'), row('y:0', T0 + timedelta(seconds=5))]], 10)
    assert [r['id'] for r in page] == ['y:0', 'x:3', 'x:2', 'x:1'] and cursor is None


def test_exactly_limit_rows_has_no_next_and_one_more_has():
    rows = [row(f'log:r{n}') for n in range(5)]
    assert activity.merge_page([rows], 5)[1] is None
    page, cursor = activity.merge_page([rows + [row('log:r5')]], 5)
    assert len(page) == 5 and cursor is not None
    assert activity.decode_cursor(cursor) == (T0, page[-1]['id'])


def test_same_row_from_two_groups_is_listed_once_and_empty_input_is_an_empty_page():
    page, cursor = activity.merge_page([[row('d:1')], [row('d:1')]], 10)
    assert [r['id'] for r in page] == ['d:1'] and cursor is None
    assert activity.merge_page([[], []], 10) == ([], None)


def test_rows_at_or_after_the_cursor_are_dropped():
    page, _ = activity.merge_page([[row('a'), row('b'), row('c')]], 10, (T0, 'b'))
    assert [r['id'] for r in page] == ['a']


def test_many_equal_timestamps_across_all_page_sizes():
    rows = [row(f'log:e{n:03d}') for n in range(40)]
    for limit in (1, 2, 7, 39, 40, 41, 100):
        seen, _ = walk([rows[:20], rows[20:]], limit)
        assert seen == sorted((r['id'] for r in rows), reverse=True)


# ---- источники и виды ----

def test_sources_follow_the_requested_kinds():
    assert activity.sources_for(None) == ['turn', 'approval', 'browser_step', 'browser_control', 'schedule_run', 'log',
                                          'procedure', 'memory']
    assert activity.sources_for(['pause']) == ['log'] and activity.log_kinds_for(['pause']) == ['pause']
    assert activity.sources_for(['schedule']) == ['schedule_run', 'log'] and activity.log_kinds_for(['schedule']) == ['schedule']
    assert activity.sources_for(['turn', 'memory']) == ['turn', 'memory'] and activity.log_kinds_for(['turn', 'memory']) == []
    assert set(activity.SQL) == set(activity.BUILDERS)


def test_every_query_is_owner_scoped_and_cursor_paged():
    for source, sql in activity.SQL.items():
        assert '$1' in sql and 'owner_id' in sql, source
        assert 'limit $5' in sql and 'collate "C"' in sql and '$3::timestamptz' in sql, source


# ---- элементы ----

def test_turn_items_carry_status_and_code():
    start = activity.build_item('turn', row('turn:1:start', phase='start', status='running', client='iphone', thread_id='t', turn_id='u'))
    assert start['kind'] == 'turn' and start['title'] == {'code': 'turn_started', 'params': {'client': 'iphone'}}
    assert start['status'] == 'running' and start['thread_id'] == 't' and start['turn_id'] == 'u'
    for status, code in (('done', 'turn_done'), ('error', 'turn_error'), ('stopped', 'turn_stopped')):
        end = activity.build_item('turn', row('turn:1:end', phase='end', status=status, client='api'))
        assert end['title']['code'] == code and end['status'] == status


def test_a_compact_turn_has_its_own_codes_and_does_not_look_like_a_normal_turn():
    start = activity.build_item('turn', row('turn:2:start', phase='start', status='running', client='system', turn_type='compact'))
    assert start['kind'] == 'turn' and start['title']['code'] == 'compact_started' and start['title']['params']['auto'] is True
    for status, code in (('done', 'compact_done'), ('error', 'compact_failed'), ('stopped', 'compact_failed')):
        end = activity.build_item('turn', row('turn:2:end', phase='end', status=status, client='api', turn_type='compact'))
        assert end['title']['code'] == code and end['status'] == status and end['title']['params']['auto'] is False
    normal = activity.build_item('turn', row('turn:3:start', phase='start', status='running', client='api', turn_type='normal'))
    assert normal['title'] == {'code': 'turn_started', 'params': {'client': 'api'}}


def test_both_turn_selects_return_the_turn_type():
    sql = activity.SQL['turn']
    assert sql.count('t.turn_type') == 2


def test_approval_items_have_risk_and_no_arguments():
    raw = row('approval:1:req', phase='req', status='pending', risk='pay', title='Оплатить заказ', tool='mcp__bothub__mac_shell',
              args={'cmd': 'curl -H "Authorization: Bearer SECRET-TOKEN"'})
    built = activity.build_item('approval', raw)
    assert built['risk'] == 'pay' and built['title']['code'] == 'approval_requested' and built['detail'] == 'Оплатить заказ'
    assert 'SECRET-TOKEN' not in json.dumps(built, default=str) and 'args' not in built
    for status, code in (('approved', 'approval_approved'), ('rejected', 'approval_rejected'), ('expired', 'approval_expired')):
        assert activity.build_item('approval', row('approval:1:dec', phase='dec', status=status, risk='other', title='t', tool='x'))['title']['code'] == code


def test_approval_title_is_masked_and_shortened():
    built = activity.build_item('approval', row('approval:2:req', phase='req', status='pending', risk='other',
                                                title='Открыть https://user:pw@example.com/a?token=SECRET ' + 'я' * 400, tool='x'))
    assert 'pw@' not in built['detail'] and 'SECRET' not in built['detail'] and len(built['detail']) <= activity.TEXT_MAX


def test_browser_step_never_exposes_typed_values_or_url_secrets():
    payload = {'action': 'fill', 'target': '[redacted]', 'url': None, 'value': 'hunter2', 'result': 'ok', 'role': 'textbox',
               'name': 'Password', 'secret': True}
    built = activity.build_item('browser_step', row('event:t:3', payload=payload))
    assert 'hunter2' not in json.dumps(built, default=str) and 'value' not in built['title']['params']
    assert built['title'] == {'code': 'browser_step', 'params': {'action': 'fill', 'role': 'textbox', 'name': 'Password'}} and built['status'] == 'ok'
    nav = activity.build_item('browser_step', row('event:t:4', payload=json.dumps(
        {'action': 'navigate', 'target': '', 'url': 'https://u:p@shop.example/cart?token=ABC#frag', 'result': 'error'})))
    assert nav['title']['params']['url'] == 'https://shop.example/cart' and nav['status'] == 'error'
    assert 'ABC' not in json.dumps(nav, default=str) and 'p@' not in json.dumps(nav, default=str)


def test_browser_step_does_not_expose_the_old_target_field_only_role_and_name():
    # у записей до разбора на role и name в target лежал сырой селектор или текст поля с введённым значением
    payload = {'action': 'click', 'target': 'input[name=password] value=hunter2', 'url': None, 'result': 'ok',
               'role': 'button', 'name': 'Sign in at https://u:pw@shop.example/a?token=SECRET123'}
    built = activity.build_item('browser_step', row('event:t:5', payload=payload))
    dump = json.dumps(built, default=str)
    assert 'target' not in built['title']['params'] and 'hunter2' not in dump and 'SECRET123' not in dump and 'pw@' not in dump
    assert built['title']['params']['role'] == 'button' and built['title']['params']['name'] == 'Sign in at https://shop.example/a'
    legacy = activity.build_item('browser_step', row('event:t:6', payload={'action': 'click', 'target': 'value=hunter2', 'result': 'ok'}))
    assert legacy['title']['params'] == {'action': 'click'} and 'hunter2' not in json.dumps(legacy, default=str)
    odd = activity.build_item('browser_step', row('event:t:7', payload={'action': 'click', 'role': 'bad role <x>', 'name': {'a': 1}}))
    assert odd['title']['params'] == {'action': 'click'}


def test_browser_step_tolerates_broken_payloads():
    for payload in (None, 'not json', '[]', 5, {'action': 7, 'target': 9, 'url': 3, 'result': 'weird'}):
        built = activity.build_item('browser_step', row('event:t:9', payload=payload))
        assert built['title']['code'] == 'browser_step' and built['status'] is None


def test_takeover_items_map_the_transition():
    for new, code in (('human', 'takeover_started'), ('returning', 'takeover_returned'), ('bot', 'takeover_bot')):
        built = activity.build_item('browser_control', row('event:t:1', payload={'from': 'bot', 'to': new, 'by': 'user-id', 'reason': 'private reason'}))
        assert built['kind'] == 'takeover' and built['title']['code'] == code
        assert 'user-id' not in json.dumps(built, default=str) and 'private reason' not in json.dumps(built, default=str)


def test_schedule_run_and_hook_run():
    built = activity.build_item('schedule_run', row('run:1', client='schedule', name='Утренний отчёт', status='running', schedule_id='s1'))
    assert built['kind'] == 'schedule' and built['title'] == {'code': 'schedule_run', 'params': {'name': 'Утренний отчёт', 'schedule_id': 's1'}}
    assert activity.build_item('schedule_run', row('run:2', client='hook', name='n', status='done', schedule_id=None))['title']['code'] == 'hook_run'


def test_procedure_items_have_run_link_and_no_parameter_values():
    raw = row('procedure:1:start', phase='start', status='running', name='Вход', procedure_id='p1', run_id='r1',
              params={'password': 'hunter2'}, step_log=[{'value': 'hunter2'}])
    built = activity.build_item('procedure', raw)
    assert built['kind'] == 'procedure' and built['title']['code'] == 'procedure_started' and built['status'] == 'running'
    assert built['title']['params'] == {'name': 'Вход', 'procedure_id': 'p1', 'run_id': 'r1'} and 'hunter2' not in json.dumps(built, default=str)
    assert activity.build_item('procedure', row('procedure:1:end', phase='end', status='failed', name='n', procedure_id='p', run_id='r'))['title']['code'] == 'procedure_finished'


def test_memory_item_shortens_text():
    built = activity.build_item('memory', row('memory:1', text='x' * 500, status='proposed', memory_id='m1'))
    assert built['kind'] == 'memory' and built['title']['code'] == 'memory_proposed' and len(built['detail']) == activity.TEXT_MAX
    assert built['detail'].endswith('…') and built['status'] == 'proposed'


def test_log_items_keep_only_known_scalar_params():
    built = activity.build_item('log', row('log:1', log_kind='schedule', code='schedule_skipped',
                                           params={'reason': 'executor_unavailable', 'count': 5, 'paused': True, 'schedule_id': 's', 'prompt': 'SECRET', 'nested': {'a': 1}}))
    assert built['kind'] == 'schedule' and built['title']['code'] == 'schedule_skipped'
    assert built['title']['params'] == {'reason': 'executor_unavailable', 'count': 5, 'paused': True, 'schedule_id': 's'}
    pause = activity.build_item('log', row('log:2', log_kind='pause', code='bot_paused', params=json.dumps({'reason': 'отпуск'})))
    assert pause['kind'] == 'pause' and pause['title']['params'] == {'reason': 'отпуск'}


def test_finish_page_serialises_for_json():
    import uuid
    thread = uuid.uuid4()
    entry = activity.build_item('log', row('log:3', at=T0, log_kind='pause', code='bot_paused', params={}, thread_id=thread))
    out = activity.finish_page([entry], 'abc')
    assert out['next'] == 'abc' and out['items'][0]['at'] == '2026-10-05T12:00:00.123456Z' and out['items'][0]['thread_id'] == str(thread)
    json.dumps(out)
    assert 'turn_id' not in out['items'][0] and 'risk' not in out['items'][0] and out['items'][0]['bot_id'] == 'scout'
    bot_less = activity.finish_page([activity.build_item('procedure', row('procedure:9:start', bot_id=None, phase='start', status='done', name='n', procedure_id='p', run_id='r'))], None)
    assert bot_less['items'][0]['bot_id'] is None


# ---- пауза триггеров ----

BOT = {'paused': False, 'status': 'idle', 'provider_id': None, 'registry_bound': False, 'executor': 'container'}


def test_skip_reason_priority_and_cases():
    assert activity.skip_reason(BOT) is None
    assert activity.skip_reason(BOT | {'paused': True}, container_running=False) == 'bot_paused'
    assert activity.skip_reason(BOT | {'provider_id': 'p'}, provider_status='pending_admin') == 'provider_unavailable'
    assert activity.skip_reason(BOT | {'provider_id': 'p'}, provider_status='error') == 'provider_unavailable'
    assert activity.skip_reason(BOT | {'provider_id': 'p'}, provider_status='ok') is None
    assert activity.skip_reason(BOT | {'provider_id': 'p'}, provider_status=None) == 'provider_unavailable'
    assert activity.skip_reason(BOT | {'status': 'no_model'}) == 'provider_unavailable'
    assert activity.skip_reason(BOT | {'registry_bound': True, 'provider_id': None}) == 'provider_unavailable'
    assert activity.skip_reason(BOT | {'status': 'error_starting'}) == 'executor_unavailable'
    assert activity.skip_reason(BOT, container_running=False) == 'executor_unavailable'
    assert activity.skip_reason(BOT, container_running=True) is None
    assert activity.skip_reason(BOT | {'executor': 'mac'}) == 'executor_unavailable'
    assert activity.skip_reason(BOT | {'executor': 'mac'}, mac_online=False) == 'executor_unavailable'
    assert activity.skip_reason(BOT | {'executor': 'mac'}, mac_online=True, container_running=False) is None
    assert activity.skip_reason(BOT | {'provider_id': 'p'}, provider_status='error', container_running=False) == 'provider_unavailable'


def test_five_skips_in_a_row_pause_the_schedule_and_probe_every_15_minutes():
    now = T0
    count, paused, last = 0, False, None
    for number in range(1, 7):
        plan = activity.plan_skip(count, paused, last, now)
        count, paused = plan.count, plan.paused
        assert plan.count == number
        assert plan.paused is (number >= 5)
        assert plan.probe_at == (now + timedelta(minutes=15) if number >= 5 else None)


def test_skip_event_is_written_at_most_once_an_hour():
    assert activity.plan_skip(0, False, None, T0).write_event is True
    assert activity.plan_skip(1, False, T0, T0 + timedelta(minutes=59, seconds=59)).write_event is False
    assert activity.plan_skip(2, False, T0, T0 + timedelta(hours=1)).write_event is True


def test_resume_plan_never_replays_a_burst():
    assert activity.plan_resume(0, False, False) == activity.ResumePlan(False, True)
    assert activity.plan_resume(3, False, False) == activity.ResumePlan(True, True)  # срок cron настал: обычный запуск
    assert activity.plan_resume(5, True, False) == activity.ResumePlan(True, False)  # в паузе: ждёт cron
    assert activity.plan_resume(9, True, True) == activity.ResumePlan(True, True)  # один запуск при catch_up


def test_skip_text_names_the_cause_and_count():
    assert 'компьютер бота недоступен' in activity.skip_text('executor_unavailable', 2, False)
    assert 'на паузе' in activity.skip_text('bot_paused', 1, False)
    assert 'модель' in activity.skip_text('provider_unavailable', 1, False)
    assert 'возобновится само' in activity.skip_text('executor_unavailable', 5, True)
    assert '—' not in activity.skip_text('executor_unavailable', 5, True) + activity.RESUME_TEXT + activity.BOT_PAUSED_TEXT
