"""Экспорт ленты активности в CSV: чистая сборка без БД."""
from datetime import datetime, timedelta, timezone

import pytest

from bothub import activity


pytestmark = pytest.mark.pure


T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def item(**fields):
    row = {
        'id': 'turn:1:start', 'at': T0, 'bot_id': 'alpha', 'kind': 'turn',
        'title': {'code': 'turn_started', 'params': {}}, 'detail': None,
    }
    row.update(fields)
    return row


@pytest.mark.parametrize('days', [1, 7, 30, 90])
def test_export_days_accepts_only_the_contract_values(days):
    assert activity.parse_export_days(str(days)) == days


def test_export_days_defaults_to_seven():
    assert activity.parse_export_days(None) == 7


@pytest.mark.parametrize('days', ['', '0', '8', '91', '-7', '1.0', 'x'])
def test_export_days_invalid_value_is_rejected(days):
    with pytest.raises(activity.ActivityError) as failure:
        activity.parse_export_days(days)
    assert str(failure.value) == 'days'


def test_csv_has_bom_header_and_rows():
    body = activity.build_csv([
        item(detail='Отправить отчёт'),
        item(bot_id=None, kind='procedure', title={'code': 'procedure_started', 'params': {}}),
    ], {'alpha': 'Скаут'})
    lines = body.decode('utf-8-sig').splitlines()
    assert body.startswith('﻿'.encode())
    assert lines[0] == 'time,bot,kind,code,title,detail'
    assert lines[1] == '2026-10-05T12:00:00.000000Z,Скаут,turn,turn_started,Task started,Отправить отчёт'
    assert lines[2] == '2026-10-05T12:00:00.000000Z,,procedure,procedure_started,Procedure started,'
    assert body.endswith(b'\r\n')


def test_csv_time_is_utc_with_microseconds():
    local = datetime(2026, 10, 5, 15, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))
    body = activity.build_csv([item(at=local)])
    assert body.decode('utf-8-sig').splitlines()[1].startswith('2026-10-05T12:00:00.123456Z,')


def test_csv_escapes_quotes_commas_and_newlines():
    body = activity.build_csv([item(detail='а"б,в\nг')])
    lines = body.decode('utf-8-sig').splitlines()
    assert lines[1] == '2026-10-05T12:00:00.000000Z,alpha,turn,turn_started,Task started,"а""б,в'
    assert lines[2] == 'г"'


@pytest.mark.parametrize('prefix', ['=', '+', '-', '@'])
def test_csv_neutralizes_formula_injection(prefix):
    body = activity.build_csv([item(bot_id=prefix + 'cmd(1)', detail=prefix + '1')])
    line = body.decode('utf-8-sig').splitlines()[1]
    assert line == f"2026-10-05T12:00:00.000000Z,'{prefix}cmd(1),turn,turn_started,Task started,'{prefix}1"


def test_csv_is_limited_to_10000_rows():
    body = activity.build_csv([item() for _ in range(10001)])
    assert len(body.decode('utf-8-sig').splitlines()) == 10001
