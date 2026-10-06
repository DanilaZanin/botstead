"""Лента активности (docs/contracts.md, раздел 16): чистая логика без БД.

Здесь: фильтры и курсор запроса, SQL выборок по источникам, сборка элементов ленты из строк, слияние страниц,
и решения про паузу триггеров (причина пропуска, счётчик пропусков, возобновление). Ядро только исполняет SQL и
применяет решения. Подписи для человека модуль не строит: `title` это `{code, params}`, строку собирает PWA.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from bothub.browser_control import mask_browser_text, mask_url

KINDS = ('turn', 'approval', 'browser', 'takeover', 'schedule', 'procedure', 'memory', 'pause')
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

SKIP_REASONS = ('executor_unavailable', 'bot_paused', 'provider_unavailable', 'check_failed')
SKIP_PAUSE_AFTER = 5  # столько пропусков подряд ставят расписание в paused_by_unavailable
PROBE_INTERVAL = timedelta(minutes=15)  # так часто расписание в паузе проверяет исполнителя
SKIP_EVENT_INTERVAL = timedelta(hours=1)  # не чаще одного события о пропуске на расписание

TEXT_MAX = 200
CURSOR_MAX = 400
# Курсор разбирается строго: дата ISO 8601 с зоной и id элемента ленты (prefix:uuid[:суффикс]). Всё остальное: 422 `before`.
CURSOR_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})')
CURSOR_ID = re.compile(r'(?:turn|approval|event|run|procedure|memory|log):[A-Za-z0-9:_-]{1,120}')
ROLE_RE = re.compile(r'[A-Za-z][A-Za-z0-9-]{0,63}')  # роль элемента браузера (раздел 13)


class ActivityError(ValueError):
    """Неверный параметр запроса; в сообщении только имя поля, значение не повторяется."""


# ---- параметры запроса ----

def parse_kinds(value) -> list[str] | None:
    """`kind=turn,approval` -> ['turn','approval'] без повторов в порядке запроса; пусто -> None (все виды)."""
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ActivityError('kind')
    result: list[str] = []
    for part in value.split(','):
        part = part.strip()
        if part not in KINDS:
            raise ActivityError('kind')
        if part not in result:
            result.append(part)
    return result


def parse_limit(value) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool):
        raise ActivityError('limit')
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdigit():
            raise ActivityError('limit')
        value = int(text)
    if not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise ActivityError('limit')
    return value


def encode_cursor(at: datetime, item_id: str) -> str:
    """Курсор: непрозрачная строка из времени (UTC, микросекунды) и id последнего элемента страницы."""
    stamp = at.astimezone(timezone.utc).isoformat(timespec='microseconds')
    return base64.urlsafe_b64encode(f'{stamp}|{item_id}'.encode()).rstrip(b'=').decode()


def decode_cursor(value) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value or len(value) > CURSOR_MAX or not value.isascii():
        raise ActivityError('before')
    try:
        raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode()
        stamp, item_id = raw.split('|', 1)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ActivityError('before') from None
    if not CURSOR_STAMP.fullmatch(stamp) or not CURSOR_ID.fullmatch(item_id):
        raise ActivityError('before')
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc), item_id
    except (ValueError, OverflowError):  # 0001-01-01+14:00 или 9999-12-31-14:00: в UTC выходит за календарь; 2026-13-45: не дата
        raise ActivityError('before') from None


# ---- слияние страниц ----

def sort_key(item: Mapping) -> tuple[datetime, str]:
    return item['at'], item['id']


def merge_page(groups: Iterable[Iterable[Mapping]], limit: int, before: tuple[datetime, str] | None = None):
    """Новые сверху, порядок полный: (время, id) по убыванию, id сравнивается как строка по кодовым точкам
    (то же, что collate "C" в SQL). Строка не раньше курсора отбрасывается, повтор id схлопывается: страницы
    не пересекаются и ничего не теряют на одинаковом времени. Каждый источник должен отдать не меньше
    limit + 1 своих самых новых строк после курсора: тогда `next` не None ровно когда есть что показать дальше."""
    seen: set[str] = set()
    rows = []
    for group in groups:
        for row in group:
            if row['id'] in seen or (before is not None and sort_key(row) >= before):
                continue
            seen.add(row['id'])
            rows.append(row)
    rows.sort(key=sort_key, reverse=True)
    page = rows[:limit]
    more = len(rows) > limit
    return page, encode_cursor(*sort_key(page[-1])) if more else None


# ---- сборка элементов ----

def short(value, limit: int = TEXT_MAX) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    value = ' '.join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + '…'


def item(row: Mapping, kind: str, code: str, params: dict | None = None, *, detail: str | None = None,
         risk: str | None = None, status: str | None = None) -> dict:
    return {
        'id': row['id'], 'at': row['at'], 'bot_id': row['bot_id'], 'thread_id': row.get('thread_id'),
        'turn_id': row.get('turn_id'), 'kind': kind,
        'title': {'code': code, 'params': {key: val for key, val in (params or {}).items() if val is not None}},
        'detail': detail, 'risk': risk, 'status': status,
    }


TURN_END = {'done': 'turn_done', 'stopped': 'turn_stopped', 'error': 'turn_error'}


COMPACT_END = {'done': 'compact_done', 'stopped': 'compact_failed', 'error': 'compact_failed'}


def turn_item(row):
    start = row['phase'] == 'start'
    if row.get('turn_type') == 'compact':
        # служебный ход сжатия контекста (раздел 15): свой код, в ленте не выглядит обычной задачей бота
        code = 'compact_started' if start else COMPACT_END.get(row['status'], 'compact_done')
        return item(row, 'turn', code, {'auto': row.get('client') == 'system'}, status=row['status'])
    code = 'turn_started' if start else TURN_END.get(row['status'], 'turn_done')
    return item(row, 'turn', code, {'client': short(row.get('client'), 64)}, status=row['status'])


APPROVAL_DECISION = {'approved': 'approval_approved', 'rejected': 'approval_rejected', 'expired': 'approval_expired'}


def approval_item(row):
    # args в ленту не попадают совсем: только заголовок и имя инструмента, как их видит экран подтверждений
    code = 'approval_requested' if row['phase'] == 'req' else APPROVAL_DECISION.get(row['status'], 'approval_expired')
    if row['phase'] != 'req' and row['status'] == 'rejected' and row.get('checker_verdict') == 'deny':
        code = 'checker_denied'  # раздел 20: проверяющая модель отклонила действие сама
    return item(row, 'approval', code, {'tool': short(row.get('tool'), 120)},
                detail=short(mask_browser_text(row.get('title'))), risk=row.get('risk'), status=row['status'])


def payload_of(row) -> dict:
    payload = row.get('payload')
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def browser_item(row):
    """browser_step: действие, роль и имя элемента, адрес. Старое поле `target` и `value` не читаются вообще: в `target` у
    записей до разбора на `role` и `name` лежал сырой селектор или текст поля, в том числе введённое значение. Имя
    маскируется как текст браузера, адрес сводится к схема://хост/путь (как `browser_safe_url` ядра)."""
    payload = payload_of(row)
    action, role, name, url = payload.get('action'), payload.get('role'), payload.get('name'), payload.get('url')
    return item(row, 'browser', 'browser_step', {
        'action': short(action, 20),
        'role': role if isinstance(role, str) and ROLE_RE.fullmatch(role) else None,
        'name': short(mask_browser_text(name)) if isinstance(name, str) else None,
        'url': short(mask_url(url) or None, 300) if isinstance(url, str) else None,
    }, status=payload.get('result') if payload.get('result') in ('ok', 'error') else None)


def takeover_item(row):
    payload = payload_of(row)
    old, new = payload.get('from'), payload.get('to')
    code = {'human': 'takeover_started', 'returning': 'takeover_returned', 'bot': 'takeover_bot'}.get(new, 'takeover_changed')
    return item(row, 'takeover', code, {'from': short(old, 20), 'to': short(new, 20)})


def schedule_run_item(row):
    code = 'hook_run' if row['client'] == 'hook' else 'schedule_run'
    return item(row, 'schedule', code, {'name': short(row.get('name'), 120), 'schedule_id': row.get('schedule_id')},
                status=row['status'])


PROCEDURE_END = ('done', 'failed', 'stopped')


def procedure_item(row):
    # params и step_log запуска в выборку не входят: значения параметров ленте не нужны
    code = 'procedure_started' if row['phase'] == 'start' else 'procedure_finished'
    return item(row, 'procedure', code, {'name': short(row.get('name'), 120), 'procedure_id': row.get('procedure_id'),
                                          'run_id': row.get('run_id')}, status=row['status'])


def memory_item(row):
    return item(row, 'memory', 'memory_proposed', {'memory_id': row.get('memory_id')},
                detail=short(row.get('text')), status=row.get('status'))


def log_item(row):
    params = payload_of({'payload': row.get('params')})
    kind = row['log_kind']
    safe = {key: params[key] for key in ('reason', 'count', 'paused', 'schedule_id', 'name', 'catch_up', 'by_all',
                                         'wakeup_id', 'scheduled_at', 'from_bot', 'to_bot', 'to_bot_id', 'turn_id', 'outcome')
            if key in params and isinstance(params[key], (str, int, bool))}
    for key in ('reason', 'name', 'from_bot', 'to_bot'):
        if key in safe and isinstance(safe[key], str):
            safe[key] = short(safe[key], 120)
    # Самопробуждение (раздел 17): зачем бот его запросил (`note`, свободный текст бота) идёт в detail, как текст памяти
    detail = short(params.get('note')) if str(row['code']).startswith('wakeup_') else None
    # Поручение боту (раздел 19): текст задачи, который отправитель дал получателю, идёт в detail
    if row['code'] == 'delegation_sent':
        detail = short(params.get('task'))
    return item(row, kind, row['code'], safe, detail=detail)


BUILDERS = {
    'turn': turn_item, 'approval': approval_item, 'browser_step': browser_item, 'browser_control': takeover_item,
    'schedule_run': schedule_run_item, 'procedure': procedure_item, 'memory': memory_item, 'log': log_item,
}

# вид ленты -> источники (SQL ниже); у schedule и pause общий источник activity_log с отбором по виду
KIND_SOURCES = {
    'turn': ('turn',), 'approval': ('approval',), 'browser': ('browser_step',), 'takeover': ('browser_control',),
    'schedule': ('schedule_run', 'log'), 'procedure': ('procedure',), 'memory': ('memory',), 'pause': ('log',),
}
LOG_KINDS = ('schedule', 'pause')


def sources_for(kinds: list[str] | None) -> list[str]:
    wanted = kinds or list(KINDS)
    result: list[str] = []
    for kind in wanted:
        for source in KIND_SOURCES[kind]:
            if source not in result:
                result.append(source)
    return result


def log_kinds_for(kinds: list[str] | None) -> list[str]:
    return [kind for kind in (kinds or KINDS) if kind in LOG_KINDS]


def build_item(source: str, row: Mapping) -> dict:
    return BUILDERS[source](dict(row))


def finish_page(page: list[dict], next_cursor: str | None) -> dict:
    out = []
    for entry in page:
        entry = dict(entry)
        entry['at'] = entry['at'].astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        for key in ('thread_id', 'turn_id'):
            if entry[key] is not None:
                entry[key] = str(entry[key])
        for key, value in tuple(entry['title']['params'].items()):
            if not isinstance(value, (str, int, bool)):
                entry['title']['params'][key] = str(value)
        out.append({key: value for key, value in entry.items() if value is not None or key in ('bot_id', 'thread_id')})
    return {'items': out, 'next': next_cursor}


# ---- SQL: каждая выборка отдаёт строки одного вида события, уже после курсора, не больше $5 штук ----
# $1 owner_id, $2 bot_id или null, $3 время курсора или null, $4 id курсора, $5 лимит (limit + 1).
# Порядок и сравнение id: collate "C", как сравнивает строки Python в merge_page.
#
# Выборка идёт от владельца (миграция 023): сначала его треды (`threads.owner_id`, индекс threads_owner_bot_idx) или процедуры,
# затем `cross join lateral` берёт у каждого не больше $5 самых новых строк по индексу, начинающемуся с ключа родителя
# (`thread_id`, `procedure_id`). Чужие строки не читаются вовсе: раньше выборка шла по времени всей системы и отбрасывала
# чужое фильтром соединения (на 100 тыс. чужих событий читались все 100 тыс.).
# Курсор целиком (время и id) применяется ВНУТРИ lateral: ключ (время, id) там собран тем же выражением, что `id` элемента
# снаружи, и порядок тот же (время, затем id как строка по кодовым точкам). Иначе limit брал бы первые $5 строк родителя
# вместе со строками «не раньше курсора», внешний фильтр их отбрасывал, и на равных временах страница теряла строки.
# `<= $3` в условии оставлен для индекса: по строке (время, id) индекс по времени не ходит. Внешняя ветка (_branch) ещё раз
# применяет курсор и общий порядок: так каждая ветка отдаёт не больше $5 строк, уже после курсора и в порядке слияния.

def _after(at: str, key: str) -> str:
    """Курсор внутри lateral: время не позже курсора (для индекса) и пара (время, id) строго раньше курсора."""
    return (f'($3::timestamptz is null or ({at} <= $3::timestamptz '
            f'and ({at}, {key} collate "C") < ($3::timestamptz, $4::text collate "C")))')


def _probe(table: str, where: str, pair: tuple[str, str]) -> str:
    """Подзапрос lateral: до $5 самых новых строк родителя после курсора. pair: (время, id) строки, как в `id` и `at` ветки."""
    at, key = pair
    return (f'select x.* from bothub.{table} x where {where} and {_after(at, key)} '
            f'order by {at} desc, {key} collate "C" desc limit $5')


def _branch(inner: str, post: str = '') -> str:
    """Ветка выборки: точный курсор, общий порядок и limit. post: столбец, который считается по строкам уже после limit."""
    page = ('select * from (' + inner + ') b '
            'where ($3::timestamptz is null or (b.at, b.id collate "C") < ($3::timestamptz, $4::text collate "C")) '
            'order by b.at desc, b.id collate "C" desc limit $5')
    return '(' + (f'select p.*, {post} from ({page}) p' if post else page) + ')'


KEY_TURN_START = ('x.started_at', "('turn:' || x.id::text || ':start')")
KEY_TURN_END = ('x.finished_at', "('turn:' || x.id::text || ':end')")
KEY_APPROVAL_REQ = ('x.created_at', "('approval:' || x.id::text || ':req')")
KEY_APPROVAL_DEC = ('coalesce(x.decided_at, x.expires_at)', "('approval:' || x.id::text || ':dec')")
KEY_EVENT = ('x.ts', "('event:' || x.thread_id::text || ':' || x.seq::text)")
KEY_SCHEDULE_RUN = ('x.created_at', "('run:' || x.id::text)")
KEY_PROCEDURE_START = ('x.created_at', "('procedure:' || x.id::text || ':start')")
KEY_PROCEDURE_END = ('x.finished_at', "('procedure:' || x.id::text || ':end')")

SQL = {
    'turn': _branch(
        "select 'turn:' || t.id::text || ':start' as id, t.started_at as at, th.bot_id, t.thread_id, t.id as turn_id, "
        "'start'::text as phase, t.status, t.client, t.turn_type "
        "from bothub.threads th cross join lateral ("
        + _probe('turns', 'x.thread_id = th.id and x.started_at is not null', KEY_TURN_START) + ") t "
        "where th.owner_id = $1 and ($2::text is null or th.bot_id = $2)") +
        ' union all ' + _branch(
        "select 'turn:' || t.id::text || ':end' as id, t.finished_at as at, th.bot_id, t.thread_id, t.id as turn_id, "
        "'end'::text as phase, t.status, t.client, t.turn_type "
        "from bothub.threads th cross join lateral ("
        + _probe('turns', "x.thread_id = th.id and x.finished_at is not null and x.status in ('done','stopped','error')", KEY_TURN_END) + ") t "
        "where th.owner_id = $1 and ($2::text is null or th.bot_id = $2)"),
    'approval': _branch(
        "select 'approval:' || a.id::text || ':req' as id, a.created_at as at, a.bot_id, a.thread_id, a.turn_id, "
        "'req'::text as phase, a.status, a.risk, a.title, a.tool, null::text as checker_verdict "
        "from bothub.threads th cross join lateral ("
        + _probe('approvals', 'x.thread_id = th.id and ($2::text is null or x.bot_id = $2)', KEY_APPROVAL_REQ) + ") a "
        "where th.owner_id = $1") +
        ' union all ' + _branch(
        "select 'approval:' || a.id::text || ':dec' as id, coalesce(a.decided_at, a.expires_at) as at, a.bot_id, "
        "a.thread_id, a.turn_id, 'dec'::text as phase, a.status, a.risk, a.title, a.tool, a.checker_verdict "
        "from bothub.threads th cross join lateral ("
        + _probe('approvals', "x.thread_id = th.id and ($2::text is null or x.bot_id = $2) "
                              "and x.status in ('approved','rejected','expired')", KEY_APPROVAL_DEC) + ") a "
        "where th.owner_id = $1"),
    'browser_step': _branch(
        "select 'event:' || e.thread_id::text || ':' || e.seq::text as id, e.ts as at, th.bot_id, e.thread_id, e.turn_id, e.payload "
        "from bothub.threads th cross join lateral ("
        + _probe('events', "x.thread_id = th.id and x.kind = 'browser_step'", KEY_EVENT) + ") e "
        "where th.owner_id = $1 and ($2::text is null or th.bot_id = $2)"),
    'browser_control': _branch(
        "select 'event:' || e.thread_id::text || ':' || e.seq::text as id, e.ts as at, th.bot_id, e.thread_id, e.turn_id, e.payload "
        "from bothub.threads th cross join lateral ("
        + _probe('events', "x.thread_id = th.id and x.kind = 'browser_control'", KEY_EVENT) + ") e "
        "where th.owner_id = $1 and ($2::text is null or th.bot_id = $2)"),
    # schedule_id ищется по строкам уже выбранной страницы (post), а не по всем кандидатам
    'schedule_run': _branch(
        "select 'run:' || t.id::text as id, t.created_at as at, th.bot_id, t.thread_id, t.id as turn_id, t.status, "
        "t.client, th.title as name "
        "from bothub.threads th cross join lateral ("
        + _probe('turns', "x.thread_id = th.id and x.client in ('schedule','hook')", KEY_SCHEDULE_RUN) + ") t "
        "where th.owner_id = $1 and ($2::text is null or th.bot_id = $2)",
        post="(select s.id from bothub.schedules s where s.last_turn_id = p.turn_id limit 1) as schedule_id"),
    'procedure': _branch(
        "select 'procedure:' || r.id::text || ':start' as id, r.created_at as at, p.bot_id, r.thread_id, r.turn_id, "
        "'start'::text as phase, r.status, p.name, p.id as procedure_id, r.id as run_id "
        "from bothub.procedures p cross join lateral ("
        + _probe('procedure_runs', 'x.procedure_id = p.id', KEY_PROCEDURE_START) + ") r "
        "where p.owner_id = $1 and ($2::text is null or p.bot_id = $2)") +
        ' union all ' + _branch(
        "select 'procedure:' || r.id::text || ':end' as id, r.finished_at as at, p.bot_id, r.thread_id, r.turn_id, "
        "'end'::text as phase, r.status, p.name, p.id as procedure_id, r.id as run_id "
        "from bothub.procedures p cross join lateral ("
        + _probe('procedure_runs', 'x.procedure_id = p.id and x.finished_at is not null', KEY_PROCEDURE_END) + ") r "
        "where p.owner_id = $1 and ($2::text is null or p.bot_id = $2)"),
    # memory и activity_log уже начинаются с ключа владельца: memory_proposed_idx и activity_log_owner_at_idx
    'memory': _branch(
        "select 'memory:' || m.id::text as id, m.created_at as at, m.bot_id, m.id as memory_id, m.text, m.status "
        "from bothub.memory m where m.owner_id = $1 and ($2::text is null or m.bot_id = $2) and m.source like 'bot:%'"),
    'log': _branch(
        "select 'log:' || l.id::text as id, l.at, l.bot_id, l.thread_id, l.kind as log_kind, l.code, l.params "
        "from bothub.activity_log l "
        "where l.owner_id = $1 and ($2::text is null or l.bot_id = $2) and l.kind = any($6::text[])"),
}


# ---- пауза триггеров ----

def skip_reason(bot: Mapping, *, provider_status: str | None = None, container_running: bool | None = None,
                mac_online: bool | None = None) -> str | None:
    """Почему расписание или hook этого бота сейчас не должны создавать turn; None: можно.
    provider_status: статус провайдера бота (`ok` годен), None если привязки нет. container_running: идёт ли
    контейнер (None: не проверяем, ядро без лаунчера). mac_online: онлайн ли Mac владельца (для executor=mac)."""
    if bot.get('paused'):
        return 'bot_paused'
    if bot.get('status') == 'no_model' or (bot.get('registry_bound') and bot.get('provider_id') is None):
        return 'provider_unavailable'
    if bot.get('provider_id') is not None and provider_status != 'ok':
        return 'provider_unavailable'
    if bot.get('status') == 'error_starting':
        return 'executor_unavailable'
    if bot.get('executor') == 'mac':
        return None if mac_online else 'executor_unavailable'
    if container_running is False:
        return 'executor_unavailable'
    return None


@dataclass(frozen=True)
class SkipPlan:
    count: int
    paused: bool
    write_event: bool
    probe_at: datetime | None  # когда расписание в паузе проверят снова; None: по своему cron


def plan_skip(count: int, paused: bool, last_event_at: datetime | None, now: datetime) -> SkipPlan:
    count += 1
    paused = paused or count >= SKIP_PAUSE_AFTER
    write = last_event_at is None or now - last_event_at >= SKIP_EVENT_INTERVAL
    return SkipPlan(count, paused, write, now + PROBE_INTERVAL if paused else None)


@dataclass(frozen=True)
class ResumePlan:
    resumed: bool  # был хотя бы один пропуск или пауза: пишем schedule_resumed и обнуляем счётчик
    run_now: bool  # создавать turn в этот проход


def plan_resume(count: int, paused: bool, catch_up: bool) -> ResumePlan:
    """Исполнитель снова доступен. Расписание вне паузы запускается как обычно (его срок как раз настал).
    Расписание в паузе проверялось не по cron: запускается один раз только при catch_up, иначе ждёт своего cron.
    Пропущенные запуски пачкой не догоняются."""
    return ResumePlan(resumed=count > 0 or paused, run_now=(not paused) or catch_up)


def skip_text(reason: str, count: int, paused: bool) -> str:
    cause = {'executor_unavailable': 'компьютер бота недоступен', 'bot_paused': 'бот на паузе',
             'provider_unavailable': 'модель бота недоступна',
             'check_failed': 'проверка исполнителя не удалась'}.get(reason, 'исполнитель недоступен')
    text = f'Запуск пропущен: {cause}. Пропусков подряд: {count}.'
    return text + ' Расписание возобновится само, когда это пройдёт.' if paused else text


RESUME_TEXT = 'Расписание возобновлено: исполнитель снова доступен.'
BOT_PAUSED_TEXT = 'Бот на паузе: сообщение дождётся возобновления.'
