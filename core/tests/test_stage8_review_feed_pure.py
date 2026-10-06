"""Выборки ленты идут от владельца (docs/contracts.md, раздел 16, миграция 023). Без БД: форма SQL и состав индексов.

Проба оценщика: на 100 тыс. чужих событий выборка читала их все (`Rows Removed by Join Filter: 100000`), потому что индексы
миграции 021 шли по времени всей системы. Теперь выборка берёт сначала свои треды, боты и процедуры (`threads.owner_id`,
`procedures.owner_id`), затем по каждому из них `lateral` с индексом, начинающимся с ключа родителя. Реальный план на данных двух
пользователей: test_stage8_review_db.py (нужен Postgres)."""
import re
from pathlib import Path

import pytest

from bothub import activity

pytestmark = pytest.mark.pure
MIGRATIONS = Path(__file__).resolve().parent.parent / 'bothub' / 'migrations'


def migration(name):
    return (MIGRATIONS / name).read_text()


def indexes():
    """Все индексы, которые остаются после всех миграций по порядку: {имя: (таблица, колонки до where, где)}."""
    found = {}
    for path in sorted(MIGRATIONS.glob('[0-9][0-9][0-9]_*.sql')):
        text = re.sub(r'--[^\n]*', '', path.read_text())
        for match in re.finditer(r'create\s+(?:unique\s+)?index\s+(?:if not exists\s+)?(\w+)\s+on\s+(?:bothub\.)?(\w+)\s*\(([^;]*?)\)\s*(where[^;]*)?;',
                                 text, re.I | re.S):
            name, table, columns, where = match.groups()
            found[name] = (table, ' '.join(columns.split()), ' '.join((where or '').split()))
        for match in re.finditer(r'drop\s+index\s+(?:if exists\s+)?(?:bothub\.)?(\w+)', text, re.I):
            found.pop(match.group(1), None)
    return found


THREAD_SOURCES = ('turn', 'approval', 'browser_step', 'browser_control', 'schedule_run')


@pytest.mark.parametrize('source', THREAD_SOURCES)
def test_thread_sources_start_from_the_owners_threads_and_probe_each_with_lateral(source):
    sql = activity.SQL[source]
    assert 'from bothub.threads th cross join lateral' in sql and 'th.owner_id = $1' in sql
    assert 'join bothub.threads th on th.id = ' not in sql  # прежняя форма: все события системы, потом отбор по владельцу
    for part in sql.split(' union all '):
        assert part.count('cross join lateral') == 1 and 'limit $5' in part


def test_the_procedure_source_starts_from_the_owners_procedures():
    sql = activity.SQL['procedure']
    assert 'from bothub.procedures p cross join lateral' in sql and 'p.owner_id = $1' in sql
    assert 'join bothub.procedures p on p.id = r.procedure_id' not in sql


def test_the_cursor_is_pushed_into_every_lateral_probe_so_each_reads_only_its_newest_rows():
    for source in THREAD_SOURCES + ('procedure',):
        for part in activity.SQL[source].split(' union all '):
            probe = part.split('cross join lateral', 1)[1].split(' limit $5')[0]
            assert '<= $3::timestamptz' in probe, source
            assert 'collate "C" desc' in probe, source  # тот же порядок, что у слияния: страницы не теряют строк на равных временах
            # точная пара (время, id) строго раньше курсора, а не только время: иначе limit берёт строки «не раньше курсора»,
            # внешний фильтр их отбрасывает, и на равных временах страница теряет строки
            assert 'collate "C") < ($3::timestamptz, $4::text collate "C")' in probe, source


def test_the_probes_order_by_the_total_order_of_the_merge_so_ties_cannot_drop_rows():
    # id элемента сравнивается как строка по кодовым точкам; у события это seq как текст ('10' раньше '9'), а не как число
    event = activity.SQL['browser_step']
    assert "order by x.ts desc, ('event:' || x.thread_id::text || ':' || x.seq::text) collate \"C\" desc" in event


def test_the_schedule_id_lookup_runs_after_the_page_limit_not_before_it():
    sql = activity.SQL['schedule_run']
    assert sql.startswith('(select p.*, (select s.id from bothub.schedules s where s.last_turn_id = p.turn_id limit 1) as schedule_id from (')
    assert 'bothub.schedules' not in sql.split(') p)', 1)[0].split(' from (', 1)[1]  # внутри страницы поиска расписания нет


def test_every_source_still_returns_only_the_owners_rows_by_bot_filter():
    for source, sql in activity.SQL.items():
        assert '$2::text is null or' in sql, source


def test_migration_023_builds_the_indexes_the_new_selects_start_with():
    found = indexes()
    wanted = {
        'turns': [('thread_id, started_at desc', 'where started_at is not null'),
                  ('thread_id, finished_at desc', 'where finished_at is not null'),
                  ('thread_id, created_at desc', "where client in ('schedule','hook')")],
        'approvals': [('thread_id, created_at desc', ''),
                      ('thread_id, (coalesce(decided_at, expires_at)) desc', "where status in ('approved','rejected','expired')")],
        'events': [('thread_id, ts desc', "where kind in ('browser_step','browser_control')")],
        'procedure_runs': [('procedure_id, created_at desc', ''), ('procedure_id, finished_at desc', 'where finished_at is not null')],
    }
    for table, specs in wanted.items():
        have = [(columns, where) for t, columns, where in found.values() if t == table]
        for spec in specs:
            assert spec in have, (table, spec)


def test_the_old_system_wide_time_indexes_of_021_are_gone_and_owner_keyed_ones_stay():
    found = indexes()
    for name in ('turns_started_idx', 'turns_finished_idx', 'turns_schedule_created_idx', 'approvals_created_idx',
                 'approvals_decided_idx', 'events_activity_idx', 'procedure_runs_created_idx', 'procedure_runs_finished_idx'):
        assert name not in found, name
    assert found['threads_owner_bot_idx'][1] == 'owner_id, bot_id'
    assert found['activity_log_owner_at_idx'][1].startswith('owner_id, at desc')
    assert found['memory_proposed_idx'][1].startswith('owner_id, created_at desc')


def test_migration_023_has_no_concurrent_index_builds_because_the_runner_wraps_a_file_in_a_transaction():
    text = re.sub(r'--[^\n]*', '', migration('023_review_fixes.sql'))
    assert 'concurrently' not in text.lower() and 'create index' in text.lower()
    from bothub import db
    import inspect
    assert 'con.transaction()' in inspect.getsource(db.open_pool)  # create index concurrently внутри транзакции не бывает


def test_migration_023_extends_the_skip_reason_check_for_check_failed():
    text = migration('023_review_fixes.sql')
    assert 'schedules_last_skip_reason_check' in text and "'check_failed'" in text
    assert 'check_failed' in activity.SKIP_REASONS
