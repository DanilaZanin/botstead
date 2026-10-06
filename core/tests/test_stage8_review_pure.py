"""Правки по ревью этапа 8 без БД: даты от клиента, которые не помещаются в UTC (docs/contracts.md, раздел 16).
Остальные правки: test_stage8_review_{runner,compact,procedures,feed,triggers}_pure.py и test_context_pure.py; пробы оценщика (s8r),
превращённые в постоянные тесты с Postgres: test_stage8_review_db.py и test_stage8_review_ctx_db.py."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from bothub import main as hub

pytestmark = pytest.mark.pure
EDGE_DATES = ['0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00', '9999-12-31T23:59:59.999999-23:59',
              '0001-01-01T00:00:00+23:59']


# ---- пункт 3: даты от клиента, которые не помещаются в UTC ----

@pytest.mark.parametrize('value', EDGE_DATES)
def test_memory_expires_at_that_overflows_utc_is_a_validation_error(value):
    with pytest.raises(ValidationError):
        hub.MemoryIn(text='fact', expires_at=value)


def test_memory_expires_at_is_normalised_to_utc_and_naive_means_utc():
    zone = timezone(timedelta(hours=3))
    aware = hub.MemoryIn(text='fact', expires_at=datetime(2030, 1, 1, 12, 0, tzinfo=zone))
    assert aware.expires_at == datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc) and aware.expires_at.utcoffset() == timedelta(0)
    naive = hub.MemoryIn(text='fact', expires_at='2030-01-01T12:00:00')
    assert naive.expires_at == datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert hub.MemoryIn(text='fact').expires_at is None


@pytest.mark.parametrize('value', EDGE_DATES)
def test_client_datetime_rejects_dates_outside_utc(value):
    with pytest.raises(ValueError):
        hub.client_datetime(datetime.fromisoformat(value))


def test_client_datetime_keeps_the_edge_that_fits():
    edge = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert hub.client_datetime(edge) == edge
    assert hub.client_datetime(datetime(2030, 1, 1)) == datetime(2030, 1, 1, tzinfo=timezone.utc)
