"""Чистая логика шаблона бота (раздел 9, экспорт/импорт файлом): разбор документа, отказ лишних полей и плохих форм,
тот же набор валидаторов, что у BotIn/ScheduleIn/процедуры. Без базы и FastAPI."""
import pytest

from bothub import main

pytestmark = pytest.mark.pure

GOOD_DOC = {
    'format': 'botstead-bot', 'version': 1,
    'name': 'Скаут', 'role': 'Ищет вакансии', 'instructions': 'Смотри hh',
    'avatar': 'scout', 'executor': 'container',
    'auto_allow': [{'tool': 'mac_find_files'}],
    'mcp_allow': ['mcp__github__mac_find_files'],
    'budget_daily_tokens': 200000, 'auto_compact_percent': 80,
    'schedules': [{'cron': '0 9 * * 1-5', 'timezone': 'Europe/Moscow', 'prompt': 'проверь вакансии', 'enabled': True, 'name': 'утро'}],
    'procedures': [{'format': 'bothub-procedure/1', 'name': 'Вход', 'params': [], 'steps': [{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Войти'}}]}],
}


def test_roundtrip():
    parsed = main.parse_bot_template(GOOD_DOC)
    assert parsed['name'] == 'Скаут' and parsed['auto_compact_percent'] == 80
    assert parsed['auto_allow'] == [{'tool': 'mac_find_files'}]
    assert parsed['schedules'][0]['cron'] == '0 9 * * 1-5' and parsed['schedules'][0]['name'] == 'утро'
    assert parsed['procedures'][0]['name'] == 'Вход'


def test_defaults_when_optional_fields_missing():
    parsed = main.parse_bot_template({'format': 'botstead-bot', 'version': 1, 'name': 'Б'})
    assert parsed['role'] == '' and parsed['instructions'] == ''
    assert parsed['avatar'] == 'robot' and parsed['executor'] == 'container'
    assert parsed['auto_allow'] == [] and parsed['mcp_allow'] == []
    assert parsed['budget_daily_tokens'] == 200000 and parsed['auto_compact_percent'] == 80
    assert parsed['schedules'] == [] and parsed['procedures'] == []


def test_explicit_null_auto_compact_passes_through():
    parsed = main.parse_bot_template({'name': 'Б', 'auto_compact_percent': None})
    assert parsed['auto_compact_percent'] is None


@pytest.mark.parametrize('field,value,match', [
    ('id', 'x', 'unknown_field'),
    ('owner_id', 'x', 'unknown_field'),
    ('provider', 'fake', 'unknown_field'),
    ('model', 'fake', 'unknown_field'),
    ('mac_full_control', True, 'unknown_field'),
    ('max_turn_seconds', 1800, 'unknown_field'),
    ('schedule', {'cron': '0 9 * * *'}, 'unknown_field'),
    ('start_container', True, 'unknown_field'),
    ('skip_container', False, 'unknown_field'),
])
def test_rejects_fields_not_in_format(field, value, match):
    doc = dict(GOOD_DOC, **{field: value})
    with pytest.raises(main.BotTemplateError) as caught:
        main.parse_bot_template(doc)
    assert match in str(caught.value)


@pytest.mark.parametrize('doc,match', [
    ([], 'object'),
    ('{"name": "x"}', 'object'),
    ({}, 'name'),
    ({'name': ''}, 'empty'),
    ({'name': 'x' * 81}, 'too_long'),
    ({'name': 5}, 'type'),
    ({'name': 'x', 'role': 5}, 'type'),
    ({'name': 'x', 'instructions': 'x' * (64 * 1024 + 1)}, 'too_long'),
    ({'name': 'x', 'avatar': 'x' * 129}, 'too_long'),
    ({'name': 'x', 'executor': 'x' * 129}, 'too_long'),
    ({'name': 'x', 'auto_allow': 'no'}, 'auto_allow'),
    ({'name': 'x', 'mcp_allow': 'no'}, 'mcp_allow'),
    ({'name': 'x', 'budget_daily_tokens': -1}, 'budget_daily_tokens'),
    ({'name': 'x', 'budget_daily_tokens': '200'}, 'budget_daily_tokens'),
    ({'name': 'x', 'budget_daily_tokens': 10 ** 13}, 'budget_daily_tokens'),
    ({'name': 'x', 'auto_compact_percent': 49}, 'auto_compact_percent'),
    ({'name': 'x', 'auto_compact_percent': 96}, 'auto_compact_percent'),
    ({'name': 'x', 'auto_compact_percent': '80'}, 'auto_compact_percent'),
    ({'name': 'x', 'auto_compact_percent': True}, 'auto_compact_percent'),
    ({'name': 'x', 'schedules': 'no'}, 'schedules'),
    ({'name': 'x', 'procedures': 'no'}, 'procedures'),
    ({'name': 'x', 'format': 'other/1'}, 'unsupported'),
    ({'name': 'x', 'version': 2}, 'unsupported'),
])
def test_rejects_bad_shapes(doc, match):
    with pytest.raises(main.BotTemplateError) as caught:
        main.parse_bot_template(doc)
    assert match in str(caught.value)


@pytest.mark.parametrize('doc,match', [
    ({'name': 'x', 'schedules': [{'cron': '0 9 * * 1-5'}]}, 'timezone'),
    ({'name': 'x', 'schedules': [{'cron': '0 9 * * 1-5', 'timezone': 'Nowhere/Nowhere'}]}, 'unknown_timezone'),
    ({'name': 'x', 'schedules': [{'cron': '', 'timezone': 'Europe/Moscow', 'prompt': 'p'}]}, 'empty'),
    ({'name': 'x', 'schedules': [{'cron': '0 9 * * 1-5', 'timezone': 'Europe/Moscow', 'prompt': ''}]}, 'empty'),
    ({'name': 'x', 'schedules': [{'cron': 'oops', 'timezone': 'Europe/Moscow', 'prompt': 'p'}]}, 'invalid'),
    ({'name': 'x', 'schedules': [{'cron': '0 9 * * 1-5', 'timezone': 'Europe/Moscow', 'prompt': 'p', 'enabled': 'yes'}]}, 'enabled'),
    ({'name': 'x', 'schedules': [{'cron': '0 9 * * 1-5', 'timezone': 'Europe/Moscow', 'prompt': 'p', 'hook_token': 'x'}]}, 'unknown_field'),
])
def test_rejects_bad_schedule(doc, match):
    with pytest.raises(main.BotTemplateError) as caught:
        main.parse_bot_template(doc)
    assert match in str(caught.value)


def test_rejects_too_many_schedules_and_procedures():
    with pytest.raises(main.BotTemplateError):
        main.parse_bot_template({'name': 'x', 'schedules': [{'cron': '0 9 * * *', 'timezone': 'Europe/Moscow', 'prompt': 'p'}] * (main.BOT_TEMPLATE_SCHEDULES_MAX + 1)})
    with pytest.raises(main.BotTemplateError):
        main.parse_bot_template({'name': 'x', 'procedures': [{}] * (main.BOT_TEMPLATE_PROCEDURES_MAX + 1)})


def test_rejects_bad_auto_allow_rule():
    with pytest.raises(main.BotTemplateError) as caught:
        main.parse_bot_template({'name': 'x', 'auto_allow': [{'tool': 'x' * 1000}]})
    assert 'auto_allow' in str(caught.value)


def test_procedures_kept_raw_for_later_parse():
    raw_proc = {'name': 'P', 'steps': []}
    parsed = main.parse_bot_template({'name': 'x', 'procedures': [raw_proc]})
    assert parsed['procedures'] == [raw_proc]


@pytest.mark.parametrize('field,value', [
    ('auto_allow', [5]),
    ('auto_allow', ['tool']),
    ('mcp_allow', [5]),
    ('mcp_allow', [None]),
])
def test_rejects_non_object_and_non_string_rules(field, value):
    with pytest.raises(main.BotTemplateError) as caught:
        main.parse_bot_template({'name': 'x', field: value})
    assert field in str(caught.value) and 'type' in str(caught.value)


def test_rejects_oversized_rule_lists():
    with pytest.raises(main.BotTemplateError):
        main.parse_bot_template({'name': 'x', 'mcp_allow': ['a'] * 100000})
    with pytest.raises(main.BotTemplateError):
        main.parse_bot_template({'name': 'x', 'auto_allow': [{'tool': 'a'}] * 100000})


def test_schedule_gets_next_run_in_the_future():
    parsed = main.parse_bot_template(GOOD_DOC)
    assert parsed['schedules'][0]['next_run_at'] > main.datetime.now(main.NOW)


def test_executor_limited_to_container_and_mac():
    assert main.parse_bot_template({'name': 'x', 'executor': 'mac'})['executor'] == 'mac'
    with pytest.raises(main.BotTemplateError) as caught:
        main.parse_bot_template({'name': 'x', 'executor': 'docker'})
    assert 'executor' in str(caught.value) and 'unsupported' in str(caught.value)


def test_unique_name_appends_counter_and_fits_limit():
    assert main.unique_name('Скаут', set(), 80) == 'Скаут'
    assert main.unique_name('Скаут', {'Скаут'}, 80) == 'Скаут (2)'
    assert main.unique_name('Скаут', {'Скаут', 'Скаут (2)'}, 80) == 'Скаут (3)'
    long = 'x' * 80
    result = main.unique_name(long, {long}, 80)
    assert result.endswith(' (2)') and len(result) <= 80
