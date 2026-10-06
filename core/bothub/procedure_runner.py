"""Воспроизведение процедур (docs/contracts.md, раздел 14, «Воспроизведение»): фоновый исполнитель запусков.

Запуск идёт по снимку шагов (`procedure_runs.steps`) с `next_step`. Один проход по шагу:

1. `procedures.resolve_step`: подстановка параметров и секретов, `url_forbidden` для адреса, риск по итоговой цели;
2. чтение страницы (`dry_run`): адрес, `precondition`, число совпадений цели и её живая подпись; ничего не нажимает;
3. риск по живой подписи (`procedures.risk_with_live`): не `none` значит подтверждение владельца (`approvals`, tool
   `procedure_step`), запуск в `waiting_approval`; ввод секрета (`secret_ref`, секретный параметр) просит подтверждение
   всегда. Подтверждение привязано к origin страницы (`схема://хост[:порт]`, punycode): после одобрения шаг читается ещё раз,
   и если подпись, origin, риск или шаг изменились, подтверждение недействительно и просится новое;
4. действие в браузере бота: `launcher.procedure_step` (Node под uid 1001, данные шага только в stdin). Страница сверяется
   в этом же вызове (`expected`: origin, адрес, роль и имя), несовпадение даёт код `changed` без действия;
5. переход после действия: click, press, select и fill без секрета, после которых вкладка ушла на другую страницу (в том числе
   на другой origin), это успех (`navigated`, `origin_changed` в ответе исполнителя). Ядро проверяет новый адрес на
   `url_forbidden` (`failed`) и идёт к `expect` обычным порядком. Ввод секрета со сменой origin: `waiting_human`,
   `page_changed_after_action`. Ожидание загрузки новой страницы только в чтении: после перехода следующее чтение идёт с
   `SETTLE_AFTER_NAV_MS`, действие само ничего не ждёт;
6. `expect`: повторные чтения, пока не выполнится или не выйдет `timeout_ms`; затем `next_step` растёт.

Остановки запуска: `waiting_approval` (ждёт владельца), `waiting_human` с полем `reason` (не нашлось, не то место, исход
неизвестен только при обрыве связи, таймауте или падении исполнителя, браузером управляет человек), `failed`, `done`, `stopped`. Статус `waiting_model` в этой версии не ставится: бота-
модели в цикле нет, решает владелец (`decide`).

Безопасность. Значения секретов живут только в памяти одного шага и в stdin исполнителя: в `step_log`, `error`, событиях,
approvals, их хэшах и журнале нет ни значений, ни текста страницы (из подписи элемента в approval попадает имя элемента до
200 символов, как `page_label` у живых действий браузера). Исключения пишутся в журнал только классом и местом в коде.

Хранилище (`Store`) отделено от логики: `PgStore` для ядра, в тестах без Postgres стоит память."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import traceback
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Protocol

from bothub import procedures
from bothub.browser_control import mask_browser_text, mask_url, url_forbidden
from bothub.launcher_client import (LauncherBusy, LauncherConflict, LauncherError, LauncherInvalid, LauncherServerError,
                                    LauncherTimeout, LauncherUnavailable)
from bothub.risk import PROCEDURE_TOOL, op_hash
from bothub.secrets import decrypt_secret

log = logging.getLogger('bothub.procedure_runner')

TOOL = PROCEDURE_TOOL  # approvals.tool: не начинается с browser_, поэтому переход takeover его не гасит
ACTIVE = ('queued', 'running', 'waiting_approval', 'waiting_model', 'waiting_human')
PROBE_TIMEOUT = 20.0  # секунд на чтение страницы (launcher: 5..180)
ACTION_TIMEOUT = 45.0  # секунд на действие
SETTLE_MS = 1500  # сколько ждать появления цели при чтении перед шагом
SETTLE_AFTER_NAV_MS = 5000  # то же для чтения после шага с переходом на другую страницу (5000: предел settle_ms исполнителя)
VERIFY_POLL = 0.4  # пауза между чтениями при проверке expect и между повторами шага
EXPECT_TIMEOUT_MS = 5000
RETRY_MAX = 2  # повторы шага после провала expect или неизвестного исхода при safe_to_retry
LAUNCHER_FAILURES_MAX = 5  # подряд недоступный лаунчер: запуск уходит в waiting_human
CRASH_MAX = 3  # неожиданные исключения в проходе: запуск failed с internal_error
WAIT_HOURS = 24  # сколько часов запуск может ждать человека (waiting_human, waiting_approval), потом stopped с wait_expired
WAITING = ('waiting_approval', 'waiting_human')
TITLE_MAX = 400
LABEL_MAX = 80

# Коды исполнителя, при которых действия не было и шаг не пошёл: решает владелец (waiting_human, поле reason).
PAUSE_REASON = {
    'precondition_failed': 'precondition_failed', 'element_not_found': 'element_not_found',
    'element_ambiguous': 'element_ambiguous', 'no_page': 'no_page',
    'cdp_unavailable': 'browser_unavailable', 'runtime_missing': 'browser_unavailable',
    'playwright_missing': 'browser_unavailable', 'changed': 'page_changed',
    'stat_not_actionable': 'element_not_actionable', 'ambiguous': 'page_ambiguous',
}
# page_changed: в вызове действия страница оказалась не той, которую одобрил владелец (origin, адрес, подпись): действия не было.
# element_not_actionable: элемент в момент сверки невидим, недоступен или не редактируется (исполнитель не ждёт): действия не было.
# page_ambiguous: две вкладки с одним адресом, какую одобрил владелец, неизвестно: действия не было.
# page_changed_after_action: ввод секрета выполнен, а вкладка после него на другом origin (код исполнителя `changed_after`): значение
# могло уйти не туда, шаг не повторяется, решает владелец. Обычный клик или нажатие с переходом на другой origin сюда не относятся.
# executor_crashed: исполнитель падал до действия (`internal_error`, phase `before_action`) и повторы кончились: действия не было.
# browser_human: браузером управляет человек (снимается возвратом управления); bot_frozen: лаунчер держит бота замороженным, а в
# базе управляет бот (сбой разморозки): решает владелец.
REASONS = frozenset({*PAUSE_REASON.values(), 'unknown_outcome', 'browser_human', 'bot_frozen', 'launcher_unavailable', 'executor_crashed',
                      'page_changed_after_action'})
REFUSED = {'fatal': 'bot_unavailable', 'invalid': 'executor_error'}  # код ошибки запуска по отказу лаунчера
ACTION_RU = {'navigate': 'открыть страницу', 'click': 'нажать', 'fill': 'ввести текст', 'press': 'нажать клавишу',
             'select': 'выбрать значение', 'wait': 'подождать', 'assert': 'проверить'}
_CODE = re.compile(r'[a-z_]{1,40}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
PHASES = ('before_action', 'after_action')
_VAULT = re.compile(r'vault:([A-Za-z][A-Za-z0-9_-]{0,63})')


class RunError(Exception):
    """Отказ операции над запуском: HTTP-статус, код ошибки и короткое пояснение без значений клиента."""

    def __init__(self, status: int, code: str, detail: str = ''):
        super().__init__(f'{status} {code} {detail}')
        self.status, self.code, self.detail = status, code, detail or code


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _utc().isoformat()


def wait_hours() -> float:
    """Срок ожидания человека из `BOTHUB_PROCEDURE_WAIT_HOURS` (часы, дробные можно). Нет значения, не число, не конечное, не больше
    нуля или больше десяти лет: `WAIT_HOURS`. Читается при каждом проходе, поэтому смена окружения действует без рестарта кода."""
    try:
        hours = float(os.getenv('BOTHUB_PROCEDURE_WAIT_HOURS', '').strip())
    except ValueError:
        return WAIT_HOURS
    return hours if 0 < hours <= 24 * 3650 else WAIT_HOURS  # nan и inf в сравнение не проходят


def _where(exc: BaseException) -> str:
    """Класс исключения и место в коде, без текста: в нём бывают значения."""
    frames = traceback.extract_tb(exc.__traceback__)
    last = f'{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}:{frames[-1].name}' if frames else '?'
    return f'{type(exc).__name__} at {last}'


# --- параметры запуска ---------------------------------------------------------------------------------------------

def check_run_params(declared: list[dict], given) -> tuple[dict, list[str], dict[str, str]]:
    """Значения запуска против описания `params` процедуры: `(значения, имена секретных параметров, {параметр: имя секрета})`.

    Обязательный без значения и без default, неописанный, не того типа, слишком длинный: 400 `invalid`. Секретный параметр
    принимает только `vault:<имя>`; существование секрета проверяет хранилище. Текст ошибки называет параметр процедуры
    (имя задал владелец), значения клиента в нём нет."""
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise RunError(400, 'invalid', 'params: type: an object is expected')
    names = {param['name']: param for param in declared}
    if any(key not in names for key in given):
        raise RunError(400, 'invalid', 'params: unknown_field: unknown parameter')
    values: dict = {}
    secret_params: list[str] = []
    refs: dict[str, str] = {}
    for name, param in names.items():
        value = given.get(name)
        empty = value is None or value == ''
        if empty and param.get('default') not in (None, ''):
            value = param['default']
            empty = False
        if empty:
            if param.get('required', True):
                raise RunError(400, 'invalid', f'params.{name}: required: a value is required')
            continue
        kind = param.get('type', 'string')
        if kind == 'string' and not isinstance(value, str):
            raise RunError(400, 'invalid', f'params.{name}: type: a string is expected')
        if kind == 'number' and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise RunError(400, 'invalid', f'params.{name}: type: a number is expected')
        if kind == 'boolean' and not isinstance(value, bool):
            raise RunError(400, 'invalid', f'params.{name}: type: a boolean is expected')
        if isinstance(value, str) and len(value) > procedures.STRING_MAX:
            raise RunError(400, 'invalid', f'params.{name}: too_long: value is too long')
        if param.get('secret'):
            match = _VAULT.fullmatch(value) if isinstance(value, str) else None
            if not match:
                raise RunError(400, 'invalid', f'params.{name}: invalid: a secret parameter takes only a vault reference')
            secret_params.append(name)
            refs[name] = match.group(1)
        values[name] = value
    return values, secret_params, refs


# --- ответ исполнителя ---------------------------------------------------------------------------------------------

def _tri(value) -> bool:
    """True, False или None именно такими значениями (`1 in (True, False, None)` истинно, число bool не заменяет)."""
    return value is True or value is False or value is None


def clean_result(raw) -> dict | None:
    """Результат исполнителя в проверенном виде или None. Содержимое пишет код в контейнере (браузер мог быть захвачен), поэтому
    берутся только известные поля известных типов и длин, остальное отбрасывается."""
    if not isinstance(raw, dict) or not isinstance(raw.get('ok'), bool) or not _tri(raw.get('acted')):
        return None
    code = raw.get('code')
    if code is not None and not (isinstance(code, str) and _CODE.fullmatch(code)):
        return None
    url = raw.get('url')
    if url is not None and not (isinstance(url, str) and len(url) <= procedures.MATCH_URL_MAX):
        return None
    found = raw.get('found')
    if found is not None:
        if not isinstance(found, dict) or isinstance(found.get('count'), bool) or not isinstance(found.get('count'), int) \
                or not 0 <= found['count'] <= 10000:
            return None
        role, name = found.get('role'), found.get('name')
        if not ((role is None or (isinstance(role, str) and len(role) <= 64))
                and (name is None or (isinstance(name, str) and len(name) <= 200))):
            return None
        found = {'count': found['count'], 'role': role, 'name': name}
    expect = raw.get('expect')
    if expect is not None:
        if not isinstance(expect, dict) or not all(_tri(expect.get(key)) for key in ('visible', 'text')):
            return None
        expect = {'visible': expect.get('visible'), 'text': expect.get('text')}
    pre = raw.get('precondition_visible')
    if not _tri(pre):
        return None
    digest = raw.get('url_sha256')  # SHA-256 полного адреса вкладки (показ в `url` обрезан); старый исполнитель его не шлёт
    if digest is not None and not (isinstance(digest, str) and _SHA256.fullmatch(digest)):
        return None
    phase = raw.get('phase')  # у internal_error: до или после начала действия
    if phase is not None and phase not in PHASES:
        return None
    moved = {}
    for key in ('navigated', 'origin_changed'):  # переход после действия; старый исполнитель полей не шлёт: False
        moved[key] = raw.get(key, False)
        if not isinstance(moved[key], bool):
            return None
    return {'ok': raw['ok'], 'code': code, 'acted': raw['acted'], 'url': url, 'url_sha256': digest, 'precondition_visible': pre,
            'found': found, 'expect': expect, 'phase': phase, **moved}


def _script_condition(cond: dict | None) -> dict | None:
    """Условие для исполнителя: только то, что он вычисляет сам (`visible`, `text`). `url_matches` считает ядро."""
    if not cond:
        return None
    out = {key: cond[key] for key in ('visible', 'text') if cond.get(key)}
    return out or None


def probe_payload(step: dict, *, settle_ms: int = 0, check_expect: bool = False) -> dict:
    """Чтение страницы перед шагом: без значения шага (секрет в такой вызов не кладётся)."""
    payload = {'step': {'action': step['action'], 'target': step.get('target'), 'value': None,
                        'precondition': _script_condition(step.get('precondition')),
                        'expect': _script_condition(step.get('expect'))},
               'settle_ms': settle_ms}
    if check_expect:
        payload['check_expect'] = True
    return payload


def action_payload(step: dict, *, expected: dict | None = None, secret_input: bool = False) -> dict:
    """Действие. `expected`: что исполнитель сверяет в этом же вызове до действия (`expected_page`); `secret_input`: после
    `fill` он ещё раз читает адрес и при смене origin отвечает `changed` (значение секрета могло уйти не туда)."""
    payload = {'step': {'action': step['action'], 'target': step.get('target'), 'value': step.get('value'),
                        'precondition': _script_condition(step.get('precondition')), 'expect': None}, 'settle_ms': 0}
    if expected is not None:
        payload['expected'] = expected
    if secret_input:
        payload['secret_input'] = True
    return payload


def expected_page(url, live: dict | None, url_sha256: str | None = None) -> dict | None:
    """Страница, на которой владелец одобрил шаг: `{origin, url, role, name}` (адрес и подпись из последнего чтения), плюс
    `url_sha256` (хэш полного адреса), если исполнитель его дал: показ адреса обрезан, вкладку исполнитель ищет по хэшу. Без
    адреса сверять нечего (первая навигация в пустом браузере): None."""
    if not url or not isinstance(url, str):
        return None
    labelled = bool(live and live.get('role'))
    page = {'origin': procedures.page_origin(url), 'url': url, 'role': live['role'] if labelled else None,
            'name': live.get('name') if labelled else None}
    if url_sha256:
        page['url_sha256'] = url_sha256
    return page


def verify_payload(expect: dict) -> dict:
    """Чтение только expect (цели шага уже может не быть на странице: после перехода)."""
    return {'step': {'action': 'wait', 'target': None, 'value': '0', 'precondition': None,
                     'expect': _script_condition(expect)}, 'settle_ms': 0, 'check_expect': True}


# --- подтверждение ---------------------------------------------------------------------------------------------------

def _label(text) -> str:
    return procedures.strip_invisible(mask_browser_text(str(text or ''))).replace('\n', ' ').strip()[:LABEL_MAX]


def approval_args(run: dict, index: int, step: dict, live: dict | None, risk: str, flags: list[str], hidden, *,
                  origin: str | None = None, secret: bool = False) -> dict:
    """Аргументы approval: шаг с подставленными параметрами без значений, которые вводят в поля (`fill` и всё, что подставлено
    из секретов), адрес сокращён до `схема://хост/путь`. `origin`: страница, на которой шаг выполняется (`схема://хост[:порт]`,
    punycode): подтверждение привязано к ней. `secret`: в поле вводится секрет из хранилища. По args считаются `args_hash` и
    `op_hash`."""
    target = step.get('target')
    if target and 'url' in target:
        target = {'url': mask_url(target['url'])}
    value = step.get('value')
    if value is not None and (step['action'] == 'fill' or value in hidden):
        value = '[redacted]'
    live_label = None
    if live and live.get('role'):
        live_label = {'role': live['role'], 'name': _label(live.get('name'))}
    args = {'run_id': str(run['id']), 'step_id': step['id'], 'index': index, 'action': step['action'],
            'target': target, 'value': value, 'risk': risk, 'live': live_label, 'origin': origin}
    if secret:
        args['secret_input'] = True
    if flags:
        args['flags'] = flags
    return args


SECRET_PHRASE = ' Значение секрета получит код бота: он сможет его прочитать.'


def args_hash(args: dict) -> str:
    """Хэш args approval (`approvals.args_hash`): sha256 канонического JSON. Origin страницы входит в args, поэтому подтверждение
    для другой страницы даёт другой хэш."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def approval_title(run: dict, index: int, step: dict, live: dict | None, *, origin: str | None = None,
                   secret: bool = False) -> str:
    """Заголовок подтверждения: шаг, живое имя элемента и страница (origin в punycode: подмену букв адреса видно сразу). Для
    ввода секрета добавлена фраза о боте. Обрезается имя элемента, а не origin и не фраза."""
    target = step.get('target') or {}
    name = (live or {}).get('name') or target.get('name')
    if step['action'] == 'navigate':
        name = mask_url(target.get('url', ''))
    label = f' «{_label(name)}»' if name else ''
    action = 'ввести секрет' if secret else ACTION_RU[step['action']]
    head = f'Процедура «{run.get("procedure_name", "")}», шаг {index + 1} из {len(run["steps"])}: {action}'
    tail = f' на {origin}' if origin else ''
    if secret:
        tail += '.' + SECRET_PHRASE
    return (head + label)[:max(0, TITLE_MAX - len(tail))] + tail


def _same_live(approved: dict | None, live: dict | None, origin: str | None = None) -> bool:
    """Страница, которую одобрил владелец, совпадает с живой: роль и имя элемента и origin страницы не изменились."""
    left = (approved or {}).get('live') or {}
    right = {'role': (live or {}).get('role'), 'name': _label((live or {}).get('name'))} if live and live.get('role') else {}
    return left == right and (approved or {}).get('origin') == origin


# --- хранилище ---------------------------------------------------------------------------------------------------------

class Store(Protocol):
    async def create_run(self, owner_id, procedure_id, bot_id, thread_id, params) -> dict: ...
    async def stop_run(self, owner_id, run_id) -> dict: ...
    async def decide_run(self, owner_id, run_id, action: str) -> dict: ...
    async def active_runs(self) -> list[dict]: ...
    async def get_run(self, run_id) -> dict | None: ...
    async def get_bot(self, bot_id) -> dict | None: ...
    async def load_secrets(self, owner_id, bot_id, names: list[str]) -> dict[str, str | None]: ...
    async def bot_has_turn(self, bot_id) -> bool: ...
    async def bot_is_paused(self, bot_id) -> bool: ...
    async def hold(self, run_id, held: bool) -> bool: ...
    async def thaw_waits(self, con, bot_id, paused_at: datetime) -> None: ...
    async def begin(self, run_id) -> bool: ...
    async def set_in_flight(self, run_id, flag: bool) -> bool: ...
    async def bump_attempt(self, run_id) -> int | None: ...
    async def pause(self, run_id, reason: str | None, *, status: str = 'waiting_human', approval_id=None) -> bool: ...
    async def resume(self, run_id, from_status: str, *, keep_approval: bool = False) -> bool: ...
    async def advance(self, run_id, index: int, entry: dict, *, last: bool) -> bool: ...
    async def finish(self, run_id) -> bool: ...
    async def fail(self, run_id, code: str, entry: dict | None = None) -> bool: ...
    async def get_approval(self, approval_id) -> dict | None: ...
    async def create_approval(self, run: dict, risk: str, title: str, args: dict) -> dict: ...
    async def consume_approval(self, approval_id) -> bool: ...
    async def expire_approval(self, approval_id) -> None: ...
    async def recover(self) -> None: ...
    async def expire_wait(self, run_id, cutoff: datetime) -> dict | None: ...
    async def note(self, run: dict, text: str) -> None: ...


class PgStore:
    """Хранилище в Postgres. `pool` вызывается при каждом обращении (пул создаётся в lifespan), `hooks` даёт запись событий и
    push из `main.py` (`append_event(con, thread_id, turn_id, kind, actor, payload)` и `outbox(con, key, payload)`)."""

    def __init__(self, pool, hooks, *, approval_ttl_minutes=None):
        self.pool, self.hooks = pool, hooks
        self.approval_ttl_minutes = approval_ttl_minutes

    # --- операции владельца ---

    async def create_run(self, owner_id, procedure_id, bot_id, thread_id, params):
        async with self.pool().acquire() as con:
            async with con.transaction():
                proc = await con.fetchrow('select * from bothub.procedures where id=$1 and owner_id=$2 for share',
                                          procedure_id, owner_id)
                if not proc:
                    raise RunError(404, 'not_found')
                if proc['status'] != 'active':
                    raise RunError(409, 'not_runnable', proc['status'])
                values, secret_params, refs = check_run_params(proc['params'], params)
                bot_id = bot_id or proc['bot_id']
                steps = json.dumps(proc['steps'])
                if not bot_id:
                    return dict(await con.fetchrow(
                        "insert into bothub.procedure_runs(procedure_id,procedure_version,params,steps,secret_params,status,error,finished_at) "
                        "values($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,'failed','no_bot',now()) returning *",
                        procedure_id, proc['version'], json.dumps(values), steps, json.dumps(secret_params)))
                # for update: два запуска одного бота одновременно проходят проверку по очереди (у бота один браузер)
                bot = await con.fetchrow('select id, executor, paused from bothub.bots where id=$1 and owner_id=$2 for update', bot_id, owner_id)
                if not bot:
                    raise RunError(400, 'invalid', 'bot_id: invalid: bot was not found')
                if bot['paused']:  # пауза бота (раздел 16): проверка под той же блокировкой, что пауза, поэтому не проскакивает
                    raise RunError(409, 'bot_paused', 'bot_paused')
                if bot['executor'] != 'container':
                    raise RunError(400, 'invalid', 'bot_id: not_allowed: the bot has no container browser')
                for name, vault in refs.items():
                    if not await con.fetchval('select 1 from bothub.secrets where owner_id=$1 and name=$2 and (bot_id=$3 or bot_id is null)',
                                              owner_id, vault, bot_id):
                        raise RunError(400, 'invalid', f'params.{name}: secret_not_found: secret was not found')
                if thread_id is not None:
                    if not await con.fetchval('select 1 from bothub.threads where id=$1 and bot_id=$2 and owner_id=$3',
                                              thread_id, bot_id, owner_id):
                        raise RunError(400, 'invalid', 'thread_id: invalid: thread was not found')
                # Одна проверка тела и ресурсов выше (400), затем занятость браузера: у бота один неконечный запуск (409).
                if await con.fetchval("select 1 from bothub.procedure_runs where bot_id=$1 and status in "
                                      "('queued','running','waiting_approval','waiting_model','waiting_human') limit 1", bot_id):
                    raise RunError(409, 'conflict', 'the bot already has an active run')
                if thread_id is None:
                    thread_id = await con.fetchval("insert into bothub.threads(bot_id,kind,title,owner_id) values($1,'routine',$2,$3) returning id",
                                                   bot_id, proc['name'], owner_id)
                run = dict(await con.fetchrow(
                    "insert into bothub.procedure_runs(procedure_id,procedure_version,bot_id,thread_id,params,steps,secret_params,status) "
                    "values($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,'queued') returning *",
                    procedure_id, proc['version'], bot_id, thread_id, json.dumps(values), steps, json.dumps(secret_params)))
        await self.note({**run, 'procedure_name': proc['name']}, f'Запущена процедура «{proc["name"]}».')
        return run

    async def _owned_run(self, con, owner_id, run_id):
        run = await con.fetchrow('select r.* from bothub.procedure_runs r join bothub.procedures p on p.id=r.procedure_id '
                                 'where r.id=$1 and p.owner_id=$2 for update of r', run_id, owner_id)
        if not run:
            raise RunError(404, 'not_found')
        return run

    async def _close_approval(self, con, approval_id):
        if approval_id:
            await con.execute("update bothub.approvals set status='expired', decided_at=now() "
                              "where id=$1 and status in ('pending','approved') and used_at is null", approval_id)

    async def stop_run(self, owner_id, run_id):
        async with self.pool().acquire() as con:
            async with con.transaction():
                run = await self._owned_run(con, owner_id, run_id)
                if run['status'] not in ACTIVE:
                    raise RunError(409, 'conflict', 'run is already finished')
                await self._close_approval(con, run['approval_id'])
                entry = None
                if run['in_flight'] and run['next_step'] < len(run['steps']):  # действие шло, исполнитель остановлен: исход неизвестен
                    entry = json.dumps([{'step_id': run['steps'][run['next_step']]['id'], 'status': 'failed', 'at': _stamp(),
                                         'duration_ms': 0, 'error': 'unknown_outcome'}])
                row = dict(await con.fetchrow("update bothub.procedure_runs set status='stopped', finished_at=now(), in_flight=false, "
                                              "reason=null, step_log=case when $2::jsonb is null then step_log else step_log||$2::jsonb end "
                                              "where id=$1 returning *", run_id, entry))
        return row

    async def decide_run(self, owner_id, run_id, action):
        async with self.pool().acquire() as con:
            async with con.transaction():
                run = await self._owned_run(con, owner_id, run_id)
                if run['status'] != 'waiting_human':
                    raise RunError(409, 'conflict', 'run is not waiting for a decision')
                if action == 'stop':
                    return dict(await con.fetchrow("update bothub.procedure_runs set status='stopped', finished_at=now(), "
                                                   "in_flight=false, reason=null where id=$1 returning *", run_id))
                if action == 'retry':
                    return dict(await con.fetchrow("update bothub.procedure_runs set status='running', reason=null, attempt=0, "
                                                   "in_flight=false, approval_id=null where id=$1 returning *", run_id))
                steps, index = run['steps'], run['next_step']
                entry = {'step_id': steps[index]['id'] if index < len(steps) else None, 'status': 'skipped', 'at': _stamp(), 'duration_ms': 0}
                last = index + 1 >= len(steps)
                return dict(await con.fetchrow(
                    "update bothub.procedure_runs set step_log=step_log||$2::jsonb, next_step=next_step+1, attempt=0, in_flight=false, "
                    "approval_id=null, reason=null, status=case when $3 then 'done' else 'running' end, "
                    "finished_at=case when $3 then now() else finished_at end where id=$1 returning *",
                    run_id, json.dumps([entry]), last))

    # --- чтение ---

    async def active_runs(self):
        async with self.pool().acquire() as con:
            rows = await con.fetch("select r.id, r.status, r.bot_id, r.reason, r.approval_id, r.waiting_since, "
                                   "coalesce(b.paused, false) as bot_paused from bothub.procedure_runs r "
                                   "left join bothub.bots b on b.id = r.bot_id "
                                   "where r.status in ('queued','running','waiting_approval','waiting_human') order by r.created_at, r.id")
        return [dict(row) for row in rows]

    async def get_run(self, run_id):
        async with self.pool().acquire() as con:
            row = await con.fetchrow('select r.*, p.owner_id, p.name as procedure_name from bothub.procedure_runs r '
                                     'join bothub.procedures p on p.id=r.procedure_id where r.id=$1', run_id)
        return dict(row) if row else None

    async def get_bot(self, bot_id):
        async with self.pool().acquire() as con:
            row = await con.fetchrow('select id, owner_id, executor, browser_control from bothub.bots where id=$1', bot_id)
        return dict(row) if row else None

    async def load_secrets(self, owner_id, bot_id, names):
        if not names:
            return {}
        async with self.pool().acquire() as con:
            rows = await con.fetch('select id, bot_id, name, value_encrypted from bothub.secrets '
                                   'where owner_id=$1 and name=any($3::text[]) and (bot_id=$2 or bot_id is null)', owner_id, bot_id, names)
        found: dict[str, str | None] = {}
        for row in sorted(rows, key=lambda item: item['bot_id'] is None):  # секрет бота раньше общего
            if row['name'] in found:
                continue
            try:
                found[row['name']] = decrypt_secret(bytes(row['value_encrypted']), row['id'].bytes).decode()
            except Exception as exc:
                log.warning('procedure_secret_unreadable', extra={'secret_id': str(row['id']), 'error': type(exc).__name__})
                found[row['name']] = None  # не расшифровался: тоже «не найден», общий секрет его не заменяет
        return found

    # --- переходы запуска (каждый условен по статусу: остановка владельцем не затирается) ---

    async def _one(self, query, *args):
        async with self.pool().acquire() as con:
            return await con.fetchrow(query, *args)

    async def bot_has_turn(self, bot_id):
        """У бота идёт turn (модель действует в том же браузере): шаг процедуры не начинается."""
        return bool(await self._one("select 1 as busy from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                                    "where th.bot_id=$1 and t.status in ('running','waiting_approval','waiting_mac') limit 1", bot_id))

    async def bot_is_paused(self, bot_id):
        return bool(await self._one('select 1 as paused from bothub.bots where id=$1 and paused', bot_id))

    async def hold(self, run_id, held):
        """Запуск бота на паузе остаётся в своём статусе (`queued` или `running`), причина ожидания `reason = 'bot_paused'`;
        после возобновления причина снимается. Чужую причину (её у `queued` и `running` не бывает) не трогает."""
        return bool(await self._one("update bothub.procedure_runs set reason = case when $2::boolean then 'bot_paused' else null end "
                                    "where id=$1 and status in ('queued','running') and "
                                    "(($2::boolean and reason is distinct from 'bot_paused') or (not $2::boolean and reason = 'bot_paused')) "
                                    "returning id", run_id, held))

    async def thaw_waits(self, con, bot_id, paused_at):
        """Бота возобновили: ожидания человека его запусков получают обратно время паузы (срок `wait_hours` на паузе не тикает).
        Для ожидания, начавшегося до паузы, вычитается вся пауза, для начавшегося во время неё только часть после начала. Вызывается
        в той же транзакции, что снятие паузы (`con`)."""
        await con.execute("update bothub.procedure_runs set waiting_since = waiting_since + (now() - greatest(waiting_since, $2::timestamptz)) "
                          "where bot_id=$1 and status in ('waiting_approval','waiting_human') and waiting_since is not null", bot_id, paused_at)

    async def begin(self, run_id):
        # бот на паузе (раздел 16): запуск не начинается, даже если проход цикла уже выбрал его до паузы
        return bool(await self._one("update bothub.procedure_runs r set status='running', started_at=coalesce(r.started_at, now()) "
                                    "where r.id=$1 and r.status='queued' and not exists "
                                    "(select 1 from bothub.bots b where b.id=r.bot_id and b.paused) returning r.id", run_id))

    async def set_in_flight(self, run_id, flag):
        return bool(await self._one("update bothub.procedure_runs set in_flight=$2 where id=$1 and status='running' returning id",
                                    run_id, flag))

    async def bump_attempt(self, run_id):
        row = await self._one("update bothub.procedure_runs set attempt=attempt+1, in_flight=false "
                              "where id=$1 and status='running' returning attempt", run_id)
        return row['attempt'] if row else None

    async def pause(self, run_id, reason, *, status='waiting_human', approval_id=None):
        return bool(await self._one("update bothub.procedure_runs set status=$2, reason=$3, approval_id=$4, in_flight=false, "
                                    "waiting_since=now() where id=$1 and status='running' returning id", run_id, status, reason, approval_id))

    async def resume(self, run_id, from_status, *, keep_approval=False):
        return bool(await self._one("update bothub.procedure_runs set status='running', reason=null, "
                                    "approval_id=case when $3 then approval_id else null end where id=$1 and status=$2 returning id",
                                    run_id, from_status, keep_approval))

    async def advance(self, run_id, index, entry, *, last):
        return bool(await self._one(
            "update bothub.procedure_runs set step_log=step_log||$3::jsonb, next_step=$2+1, attempt=0, in_flight=false, "
            "approval_id=null, reason=null, status=case when $4 then 'done' else status end, "
            "finished_at=case when $4 then now() else finished_at end "
            "where id=$1 and status='running' and next_step=$2 returning id", run_id, index, json.dumps([entry]), last))

    async def finish(self, run_id):
        return bool(await self._one("update bothub.procedure_runs set status='done', finished_at=now(), in_flight=false "
                                    "where id=$1 and status='running' returning id", run_id))

    async def fail(self, run_id, code, entry=None):
        async with self.pool().acquire() as con:
            async with con.transaction():
                row = await con.fetchrow("update bothub.procedure_runs set status='failed', error=$2, finished_at=now(), in_flight=false, "
                                         "reason=null where id=$1 and status in ('queued','running','waiting_approval','waiting_human') "
                                         "returning approval_id", run_id, code)
                if not row:
                    return False
                if entry is not None:
                    await con.execute('update bothub.procedure_runs set step_log=step_log||$2::jsonb where id=$1', run_id, json.dumps([entry]))
                await self._close_approval(con, row['approval_id'])
        return True

    async def recover(self):
        """После рестарта ядра: шаг с запущенным действием и неизвестным исходом. `safe_to_retry`: повторится с next_step, остальные
        в waiting_human (reason unknown_outcome)."""
        async with self.pool().acquire() as con:
            async with con.transaction():
                await con.execute("update bothub.procedure_runs set in_flight=false where status='running' and in_flight and "
                                  "coalesce((steps -> next_step ->> 'safe_to_retry')::boolean, false) and "
                                  "(steps -> next_step ->> 'secret_ref') is null")  # ввод секрета повторять нельзя
                await con.execute("update bothub.procedure_runs set status='waiting_human', reason='unknown_outcome', in_flight=false, "
                                  "waiting_since=now() where status='running' and in_flight")

    async def expire_wait(self, run_id, cutoff):
        """Запуск ждёт человека (`waiting_approval`, `waiting_human`) с момента раньше `cutoff`: `stopped` с `error='wait_expired'`,
        `in_flight` снят, подтверждение закрыто. Условный переход: решение владельца, остановка или возврат браузера, пришедшие
        раньше, не затираются. Ответ: строка запуска с прежними `status`, `reason` в полях `was_status`, `was_reason`; нет перехода: None."""
        async with self.pool().acquire() as con:
            async with con.transaction():
                run = await con.fetchrow("select r.*, p.name as procedure_name from bothub.procedure_runs r "
                                         "join bothub.procedures p on p.id=r.procedure_id where r.id=$1 and r.status in "
                                         "('waiting_approval','waiting_human') and r.waiting_since < $2 for update of r", run_id, cutoff)
                if not run:
                    return None
                row = dict(await con.fetchrow("update bothub.procedure_runs set status='stopped', error='wait_expired', finished_at=now(), "
                                              "in_flight=false, reason=null where id=$1 returning *", run_id))
                await self._close_approval(con, run['approval_id'])
        return {**row, 'procedure_name': run['procedure_name'], 'was_status': run['status'], 'was_reason': run['reason']}

    # --- подтверждения и события ---

    async def get_approval(self, approval_id):
        row = await self._one('select * from bothub.approvals where id=$1', approval_id)
        return dict(row) if row else None

    async def create_approval(self, run, risk, title, args):
        ttl = self.approval_ttl_minutes if self.approval_ttl_minutes is not None else int(os.getenv('APPROVAL_TTL_MINUTES', '60'))
        async with self.pool().acquire() as con:
            async with con.transaction():
                row = dict(await con.fetchrow(
                    "insert into bothub.approvals(thread_id,turn_id,bot_id,risk,title,tool,args,args_hash,op_hash,status,expires_at) "
                    "values($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,'pending',$10) returning *",
                    run['thread_id'], run['turn_id'], run['bot_id'], risk, title, TOOL, json.dumps(args, sort_keys=True, ensure_ascii=False),
                    args_hash(args), op_hash(TOOL, args), _utc() + timedelta(minutes=ttl)))
                await self.hooks.append_event(con, run['thread_id'], run['turn_id'], 'approval_req', f"bot:{run['bot_id']}",
                                              {'approval_id': str(row['id']), 'risk': risk, 'title': title, 'tool': TOOL,
                                               'expires_at': row['expires_at'].isoformat()})
                await self.hooks.outbox(con, f'approval:{row["id"]}', {'approval_id': str(row['id'])})
        return row

    async def consume_approval(self, approval_id):
        return bool(await self._one("update bothub.approvals set used_at=now() where id=$1 and status='approved' and used_at is null "
                                    "and expires_at>now() returning id", approval_id))

    async def expire_approval(self, approval_id):
        async with self.pool().acquire() as con:
            await self._close_approval(con, approval_id)

    async def note(self, run, text):
        """Короткая запись в тред запуска: PWA по событию треда обновляет экран запуска. Сбой записи запуск не останавливает."""
        if not run.get('thread_id'):
            return
        try:
            async with self.pool().acquire() as con:
                await self.hooks.append_event(con, run['thread_id'], run.get('turn_id'), 'system', 'system',
                                              {'text': text, 'run_id': str(run['id'])})
        except Exception as exc:
            log.warning('procedure_note_failed', extra={'run_id': str(run['id']), 'error': _where(exc)})


# --- исполнитель ---------------------------------------------------------------------------------------------------------

class ProcedureRunner:
    """Фоновый исполнитель. `tick()` один проход цикла: берёт готовые запуски (по одной задаче на бота), проверяет ответы владельца
    по подтверждениям и возврат браузера. Маршруты зовут `create_run`, `stop_run`, `decide_run`."""

    def __init__(self, store: Store, launcher, *, release_browser=None, clock=time.monotonic, sleep=asyncio.sleep,
                 now=_utc):
        self.store = store
        self._launcher = launcher  # () -> клиент лаунчера или None (нет или ещё не сверен)
        self._release = release_browser  # async (bot_id, owner_id): браузер returning -> bot после чтения страницы
        self.clock, self.sleep, self.now = clock, sleep, now
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._bots: dict[uuid.UUID, str | None] = {}
        self._failures: dict[uuid.UUID, int] = {}
        self._crashes: dict[uuid.UUID, int] = {}
        self._settle: dict[uuid.UUID, int] = {}  # запуск -> settle_ms первого чтения следующего шага (после перехода)

    # --- маршруты ---

    async def create_run(self, owner_id, procedure_id, bot_id, thread_id, params):
        return await self.store.create_run(owner_id, procedure_id, bot_id, thread_id, params)

    async def stop_run(self, owner_id, run_id):
        before = await self.store.get_run(run_id)
        run = await self.store.stop_run(owner_id, run_id)
        task = self._tasks.get(run_id)
        flying = (task is not None and not task.done()) or bool(before and before.get('in_flight'))
        if flying and run.get('bot_id'):  # шаг в полёте: процесс исполнителя в контейнере (uid 1001) завершает лаунчер
            launcher = self._launcher()
            if launcher is not None:
                with suppress(LauncherError):
                    await launcher.procedure_step_cancel(run['bot_id'], self._exec_id(run_id))
        await self.store.note(run, 'Запуск остановлен.')
        return run

    async def decide_run(self, owner_id, run_id, action):
        run = await self.store.decide_run(owner_id, run_id, action)
        await self.store.note(run, {'retry': 'Шаг повторяется по решению владельца.', 'skip': 'Шаг пропущен по решению владельца.',
                                    'stop': 'Запуск остановлен.'}[action])
        return run

    # --- цикл ---

    async def recover(self):
        await self.store.recover()

    async def tick(self) -> bool:
        busy = False
        taken = {bot for rid, bot in self._bots.items() if rid in self._tasks}
        for run in await self.store.active_runs():
            rid = run['id']
            if rid in self._tasks:
                continue
            status = run['status']
            if status in WAITING and await self._expire_wait(run):
                busy = True
                continue
            if status in ('queued', 'running'):
                if run.get('bot_paused'):  # бот на паузе: шаги не начинаются, запуск ждёт в своём статусе с причиной bot_paused
                    if run.get('reason') != 'bot_paused':
                        busy = await self.store.hold(rid, True) or busy
                    continue
                if run['bot_id'] in taken:
                    continue
                if run.get('reason') == 'bot_paused':  # возобновили: причина снимается, запуск идёт дальше
                    await self.store.hold(rid, False)
                taken.add(run['bot_id'])
                self._spawn(run)
                busy = True
            elif status == 'waiting_approval':
                busy = await self._poll_approval(run) or busy
            elif status == 'waiting_human' and run.get('reason') == 'browser_human':
                busy = await self._poll_browser(run) or busy
        return busy

    def _spawn(self, run):
        rid = run['id']
        task = asyncio.create_task(self._advance(rid))
        self._tasks[rid], self._bots[rid] = task, run['bot_id']

        def done(_task, rid=rid):
            self._tasks.pop(rid, None)
            self._bots.pop(rid, None)

        task.add_done_callback(done)

    async def shutdown(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _exec_id(run_id) -> str:
        return f'proc-{run_id.hex}'

    async def _expire_wait(self, run) -> bool:
        """Запуск ждёт человека дольше `wait_hours()`: `stopped` с `wait_expired`. Бот освобождается: turn'ы и расписания бота, которые
        ждали этот запуск, идут дальше (worker и scheduler смотрят на неконечные запуски). Бот, замороженный лаунчером при управлении
        ботом в базе (`bot_frozen`), размораживается; браузер, которым управляет человек (`browser_human`), не отбирается.
        True: запуск обработан (остановлен или состояние уже сменилось), остальные ветки прохода пропускаются."""
        since = run.get('waiting_since')
        if since is None or run.get('bot_paused'):  # на паузе бота срок ожидания не тикает: возобновление вернёт время паузы
            return False
        hours = wait_hours()
        cutoff = self.now() - timedelta(hours=hours)
        if since >= cutoff:
            return False
        stopped = await self.store.expire_wait(run['id'], cutoff)
        if stopped is None:  # владелец или цикл успели раньше: запуск уже не ждёт
            return True
        self._failures.pop(run['id'], None)
        self._crashes.pop(run['id'], None)
        self._settle.pop(run['id'], None)
        if stopped.get('was_reason') == 'bot_frozen' and stopped.get('bot_id'):
            launcher = self._launcher()
            if launcher is not None:
                with suppress(LauncherError):  # сбой разморозки остановку не отменяет: стартовая сверка и владелец снимут заморозку
                    await launcher.unfreeze_bot(stopped['bot_id'])
        await self.store.note(stopped, f'Запуск {stopped["id"]}: процедура «{stopped["procedure_name"]}» остановлена на шаге '
                                       f'{stopped["next_step"] + 1}, ожидание человека дольше {hours:g} ч (wait_expired). '
                                       'Бот свободен для сообщений и расписаний.')
        return True

    async def _poll_browser(self, run) -> bool:
        """Запуск ждал человека у браузера: управление вернулось (`bot` или `returning`), шаг идёт снова с повторной проверкой."""
        bot = await self.store.get_bot(run['bot_id']) if run['bot_id'] else None
        if bot is None:
            await self.store.fail(run['id'], 'no_bot')
            return True
        if bot['browser_control'] in ('bot', 'returning'):
            return await self.store.resume(run['id'], 'waiting_human')
        return False

    async def _poll_approval(self, run) -> bool:
        approval = await self.store.get_approval(run['approval_id']) if run.get('approval_id') else None
        if approval is None:  # подтверждения нет (удалено): шаг заново просит его
            return await self.store.resume(run['id'], 'waiting_approval')
        status = approval['status']
        if status == 'pending':
            if approval['expires_at'] <= self.now():
                await self.store.expire_approval(approval['id'])
                await self._fail_by_id(run['id'], 'approval_expired')
                return True
            return False
        if status == 'approved':
            return await self.store.resume(run['id'], 'waiting_approval', keep_approval=True)
        await self._fail_by_id(run['id'], 'approval_rejected' if status == 'rejected' else 'approval_expired')
        return True

    async def _fail_by_id(self, run_id, code):
        run = await self.store.get_run(run_id)
        if run is None:
            return
        index = run['next_step']
        step = run['steps'][index] if index < len(run['steps']) else None
        entry = {'step_id': step['id'], 'status': 'failed', 'at': _stamp(), 'duration_ms': 0, 'error': code} if step else None
        if await self.store.fail(run_id, code, entry):
            await self.store.note(run, f'Процедура «{run["procedure_name"]}» остановилась на шаге {index + 1}: {code}.')

    async def _advance(self, run_id):
        try:
            while await self._step_once(run_id):
                pass
            self._crashes.pop(run_id, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            crashes = self._crashes[run_id] = self._crashes.get(run_id, 0) + 1
            log.error('procedure_run_crashed', extra={'run_id': str(run_id), 'crashes': crashes, 'error': _where(exc)})
            if crashes >= CRASH_MAX:
                with suppress(Exception):
                    await self.store.fail(run_id, 'internal_error')

    # --- один проход по шагу ---

    async def _launcher_call(self, run, bot_id, payload: dict, *, dry_run: bool, timeout: float):
        """`(вид, результат)`. Вид: ok, frozen (бот заморожен: перехват человеком или сбой разморозки), transient (лаунчер или
        контейнер недоступны, исход действия неизвестен), lost (исполнитель не вернул разбираемый результат), invalid (лаунчер
        отверг вызов: наша ошибка), fatal (бота или контейнера нет, он не принадлежит лаунчеру)."""
        launcher = self._launcher()
        if launcher is None:
            return 'transient', None
        try:
            answer = await launcher.procedure_step(bot_id, json.dumps(payload, ensure_ascii=False), dry_run, timeout,
                                                   exec_id=self._exec_id(run['id']))
        except LauncherError as exc:
            if exc.code == 'frozen':
                return 'frozen', None
            if isinstance(exc, (LauncherUnavailable, LauncherTimeout, LauncherBusy, LauncherServerError, LauncherConflict)):
                return 'transient', None  # Conflict: контейнер остановлен или шаг этого бота ещё не закончился
            if isinstance(exc, LauncherInvalid):
                log.warning('procedure_launcher_invalid', extra={'run_id': str(run['id']), 'code': exc.code})
                return 'invalid', None
            log.warning('procedure_launcher_refused', extra={'run_id': str(run['id']), 'code': exc.code})
            return 'fatal', None
        result = clean_result(answer.get('result'))
        return ('ok', result) if result is not None else ('lost', None)

    def _action_timeout(self, step) -> float:
        if step['action'] == 'wait' and step.get('value'):
            return min(180.0, max(ACTION_TIMEOUT, int(step['value']) / 1000 + 15))
        return ACTION_TIMEOUT

    async def _pause(self, run, index, reason) -> bool:
        if await self.store.pause(run['id'], reason):
            await self.store.note(run, f'Процедура «{run["procedure_name"]}» ждёт вашего решения на шаге {index + 1}: {reason}.')
        return False

    async def _fail(self, run, index, code, step, started) -> bool:
        entry = {'step_id': step['id'], 'status': 'failed', 'at': _stamp(), 'duration_ms': int((self.clock() - started) * 1000),
                 'error': code}
        self._settle.pop(run['id'], None)
        if await self.store.fail(run['id'], code, entry):
            await self.store.note(run, f'Процедура «{run["procedure_name"]}» остановилась на шаге {index + 1}: {code}.')
        return False

    async def _retry_or(self, run, step, give_up) -> bool:
        """Повтор шага (`safe_to_retry`, до RETRY_MAX раз) или `give_up()`."""
        if step.get('safe_to_retry'):
            attempts = await self.store.bump_attempt(run['id'])
            if attempts is None:
                return False  # запуск остановлен
            if attempts <= RETRY_MAX:
                await self.sleep(VERIFY_POLL)
                return True
        return await give_up()

    async def _crashed(self, run, index) -> bool:
        """Исполнитель упал до действия (`internal_error`, действия не было): шаг повторяется сам до RETRY_MAX раз, независимо от
        `safe_to_retry` (ничего не сделано), потом решает владелец (`executor_crashed`, повтор по `decide`)."""
        attempts = await self.store.bump_attempt(run['id'])
        if attempts is None:
            return False  # запуск остановлен
        if attempts <= RETRY_MAX:
            await self.sleep(VERIFY_POLL)
            return True
        return await self._pause(run, index, 'executor_crashed')

    async def _transient(self, run, index, reason='launcher_unavailable') -> bool:
        count = self._failures[run['id']] = self._failures.get(run['id'], 0) + 1
        if count >= LAUNCHER_FAILURES_MAX:
            self._failures.pop(run['id'], None)
            return await self._pause(run, index, reason)
        return False  # следующий проход цикла попробует снова

    async def _step_once(self, run_id) -> bool:
        """Один шаг запуска. True: пройден или повторяется, идти дальше сразу; False: запуск остановился или ждёт."""
        run = await self.store.get_run(run_id)
        if run is None or run['status'] not in ('queued', 'running'):
            return False
        steps, index = run['steps'], run['next_step']
        if run['status'] == 'running' and run['in_flight'] and index < len(steps):
            # Признак ставится до отправки действия и снимается вместе с next_step и step_log. Увидеть его включённым значит, что
            # действие шло, а исход не записан (сбой между ними): повторять нельзя, решает владелец.
            return await self._pause(run, index, 'unknown_outcome')
        if run['bot_id'] and await self.store.bot_is_paused(run['bot_id']):
            # Бот на паузе (раздел 16): перед каждым шагом, в том числе перед началом запуска. Идущий шаг доведён до конца,
            # следующий не начинается; запуск остаётся в своём статусе с причиной bot_paused и идёт дальше после возобновления.
            await self.store.hold(run_id, True)
            return False
        if run['bot_id'] and await self.store.bot_has_turn(run['bot_id']):
            return False  # браузер занят turn'ом бота (модель и процедура мешали бы друг другу): шаг начнётся позже
        if run['status'] == 'queued':
            if await self.store.begin(run_id):
                await self.store.note(run, f'Процедура «{run["procedure_name"]}» выполняется.')
                return True
            return False
        if index >= len(steps):  # шагов нет (процедура без шагов): запуск сразу выполнен
            if await self.store.finish(run_id):
                await self.store.note(run, f'Процедура «{run["procedure_name"]}» выполнена.')
            return False
        step = steps[index]
        started = self.clock()
        bot = await self.store.get_bot(run['bot_id']) if run['bot_id'] else None
        if bot is None:
            return await self._fail(run, index, 'no_bot', step, started)
        if bot['executor'] != 'container' or not run.get('thread_id'):
            return await self._fail(run, index, 'bot_unavailable' if bot['executor'] != 'container' else 'no_thread', step, started)
        if bot['browser_control'] == 'human':
            return await self._pause(run, index, 'browser_human')

        # 1. подстановка параметров и секретов
        try:
            resolved, hidden = await self._resolve(run, bot, step, index)
        except procedures.ProcedureError as exc:
            return await self._fail(run, index, exc.code, step, started)
        if resolved['action'] in ('fill', 'select', 'press') and not resolved.get('value'):
            return await self._fail(run, index, 'step_incomplete', step, started)
        secret = resolved['action'] == 'fill' and resolved.get('value') in hidden  # значение пришло из хранилища
        retryable = dict(step, safe_to_retry=False) if secret else step  # ввод секрета автоматически не повторяется

        # 2. чтение страницы
        kind, probe = await self._launcher_call(run, bot['id'], probe_payload(resolved, settle_ms=self._settle.get(run_id, SETTLE_MS)),
                                                dry_run=True, timeout=PROBE_TIMEOUT)
        if kind == 'frozen':  # перехват мог ещё не записаться в базу: сначала повторы, потом решает владелец
            return await self._transient(run, index, 'bot_frozen')
        if kind in ('fatal', 'invalid'):
            return await self._fail(run, index, REFUSED[kind], step, started)
        if kind != 'ok':
            return await self._transient(run, index)
        self._failures.pop(run_id, None)
        pre = resolved.get('precondition') or {}
        if pre.get('url_matches'):
            matched, _timed_out = await procedures.match_url(pre['url_matches'], probe['url'] or '')
            if not matched:
                return await self._pause(run, index, 'precondition_failed')
        if not probe['ok']:
            if probe['code'] in PAUSE_REASON:
                return await self._pause(run, index, PAUSE_REASON[probe['code']])
            if probe['code'] == 'url_forbidden':
                return await self._fail(run, index, 'url_forbidden', step, started)
            if probe['code'] == 'internal_error':
                return await self._crashed(run, index)  # чтение ничего не делает: повтор безопасен
            return await self._fail(run, index, 'executor_error', step, started)
        if bot['browser_control'] == 'returning' and self._release is not None:
            # Человек вернул управление; страницу только что прочитал исполнитель (условия шага проверены), это и есть перечитывание.
            await self._release(bot['id'], bot['owner_id'])

        # 3. риск по живой подписи и подтверждение
        live = probe['found']
        expected = expected_page(probe['url'], live, probe.get('url_sha256'))
        if expected is None and resolved['action'] != 'navigate':
            return await self._pause(run, index, 'no_page')
        origin = expected['origin'] if expected else None
        if secret and origin is None:
            return await self._pause(run, index, 'precondition_failed')  # секрет не вводят на странице без адреса http(s)
        if resolved['action'] == 'fill' and not hidden and live and procedures.secret_field(live['role'], live['name']):
            return await self._fail(run, index, 'secret_required', step, started)  # литерал в поле, которое оказалось секретным
        risk, flags = procedures.risk_with_live(resolved, live['role'] if live else None, live['name'] if live else None)
        if secret:  # ввод секрета подтверждается всегда: код бота в том же браузере может прочитать введённое
            risk, flags = procedures.raise_risk(risk, flags, 'login')
        if risk != 'none':
            gate = await self._gate(run, index, resolved, hidden, live, risk, flags, started, step, origin=origin, secret=secret)
            if gate is not None:
                return gate

        # 4. действие (страницу исполнитель сверяет сам, в этом же вызове)
        if not await self.store.set_in_flight(run_id, True):
            return False
        kind, result = await self._launcher_call(run, bot['id'], action_payload(resolved, expected=expected, secret_input=secret),
                                                 dry_run=False, timeout=self._action_timeout(resolved))
        if kind == 'frozen':  # лаунчер отказал до запуска исполнителя: действия не было
            await self.store.set_in_flight(run_id, False)
            return await self._pause(run, index, 'bot_frozen')
        if kind in ('fatal', 'invalid'):
            await self.store.set_in_flight(run_id, False)  # лаунчер отказал до запуска исполнителя
            return await self._fail(run, index, REFUSED[kind], step, started)
        if kind in ('transient', 'lost'):
            return await self._unknown(run, index, retryable)
        if not result['ok']:
            code = result['code']
            if result['acted'] is False:
                await self.store.set_in_flight(run_id, False)
                if code in PAUSE_REASON:
                    return await self._pause(run, index, PAUSE_REASON[code])
                if code == 'assert_failed':
                    return await self._retry_or(run, retryable, lambda: self._fail(run, index, 'assert_failed', step, started))
                if code == 'internal_error':
                    return await self._crashed(run, index)  # сбой до действия: ничего не сделано, повтор безопасен и для секрета
                return await self._fail(run, index, 'executor_error', step, started)
            if code == 'changed_after':  # ввод секрета выполнен, а страница на другом origin: повторять нельзя, решает владелец
                return await self._pause(run, index, 'page_changed_after_action')
            return await self._unknown(run, index, retryable)  # action_failed, timeout, internal_error после начала действия
        # in_flight остаётся включённым до advance: сбой между действием и записью результата даёт unknown_outcome, не повтор
        navigated = result['navigated'] or result['origin_changed']
        if navigated:
            # Переход после действия (в том числе на другой origin) это обычный результат клика, нажатия, выбора и ввода без секрета.
            if secret and result['origin_changed']:  # значение секрета могло уйти не туда (так отвечает и исполнитель: changed_after)
                return await self._pause(run, index, 'page_changed_after_action')
            if result['url'] and url_forbidden(result['url']):  # новый адрес проходит ту же проверку, что адрес navigate
                return await self._fail(run, index, 'url_forbidden', step, started)

        # 5. expect
        verdict = await self._verify(run, bot['id'], resolved)
        if verdict == 'unknown':
            return await self._unknown(run, index, retryable)
        if verdict == 'failed':
            return await self._retry_or(run, retryable, lambda: self._fail(run, index, 'expect_failed', step, started))
        entry = {'step_id': step['id'], 'status': 'ok', 'at': _stamp(), 'duration_ms': int((self.clock() - started) * 1000)}
        last = index + 1 >= len(steps)
        if not await self.store.advance(run_id, index, entry, last=last):
            return False
        self._settle.pop(run_id, None)
        if navigated and not last:
            self._settle[run_id] = SETTLE_AFTER_NAV_MS  # следующее чтение ждёт загрузки новой страницы
        if last:
            await self.store.note(run, f'Процедура «{run["procedure_name"]}» выполнена.')
            return False
        return True

    async def _resolve(self, run, bot, step, index):
        names: set[str] = set()
        match = _VAULT.fullmatch(step.get('secret_ref') or '')
        if match:
            names.add(match.group(1))
        for name in run.get('secret_params') or ():
            ref = _VAULT.fullmatch(str((run['params'] or {}).get(name) or ''))
            if ref:
                names.add(ref.group(1))
        secrets = await self.store.load_secrets(run['owner_id'], bot['id'], sorted(names))
        return procedures.resolve_step(step, run['params'] or {}, secrets.get, secret_params=frozenset(run.get('secret_params') or ()),
                                       path=f'steps[{index}]')

    async def _gate(self, run, index, resolved, hidden, live, risk, flags, started, step, *, origin=None, secret=False):
        """Подтверждение владельца для шага с риском. None: одобрено и израсходовано, действие можно выполнять. Иначе значение,
        которое `_step_once` возвращает: запуск ждёт подтверждения или провалился (False)."""
        approval = await self.store.get_approval(run['approval_id']) if run.get('approval_id') else None
        if approval is not None:
            status = approval['status']
            if status == 'rejected':
                return await self._fail(run, index, 'approval_rejected', step, started)
            if status == 'expired':
                return await self._fail(run, index, 'approval_expired', step, started)
            fresh = approval['expires_at'] > self.now()
            if status == 'pending':
                if not fresh:
                    await self.store.expire_approval(approval['id'])
                    return await self._fail(run, index, 'approval_expired', step, started)
                await self.store.pause(run['id'], None, status='waiting_approval', approval_id=approval['id'])
                return False
            if status == 'approved' and approval.get('used_at') is None and fresh:
                args = approval['args']
                if args.get('step_id') == resolved['id'] and args.get('risk') == risk and _same_live(args, live, origin) \
                        and bool(args.get('secret_input')) == secret:
                    if await self.store.consume_approval(approval['id']):
                        return None
                else:  # страница или шаг изменились с момента одобрения: прежнее подтверждение недействительно
                    await self.store.expire_approval(approval['id'])
            # одобрено и уже израсходовано (повтор шага) или просрочено: нужно новое
        created = await self.store.create_approval(
            run, risk, approval_title(run, index, resolved, live, origin=origin, secret=secret),
            approval_args(run, index, resolved, live, risk, flags, hidden, origin=origin, secret=secret))
        if await self.store.pause(run['id'], None, status='waiting_approval', approval_id=created['id']):
            await self.store.note(run, f'Процедура «{run["procedure_name"]}» ждёт подтверждения на шаге {index + 1}.')
        return False

    async def _unknown(self, run, index, step) -> bool:
        """Исход действия неизвестен (обрыв, таймаут, сбой исполнителя): `safe_to_retry` повторяется сам, остальное решает человек."""
        return await self._retry_or(run, step, lambda: self._pause(run, index, 'unknown_outcome'))

    async def _verify(self, run, bot_id, step) -> str:
        """`ok`, `failed` (expect не выполнился за timeout_ms) или `unknown` (лаунчер не отвечал, проверить не удалось)."""
        expect = step.get('expect')
        if not expect or not any(expect.get(key) for key in ('url_matches', 'visible', 'text')):
            return 'ok'
        deadline = self.clock() + (expect.get('timeout_ms') or EXPECT_TIMEOUT_MS) / 1000
        payload = verify_payload(expect)
        last_ok = False
        while True:
            kind, result = await self._launcher_call(run, bot_id, payload, dry_run=True, timeout=PROBE_TIMEOUT)
            last_ok = kind == 'ok' and result['ok']
            if last_ok:
                satisfied = True
                if expect.get('url_matches'):
                    matched, _ = await procedures.match_url(expect['url_matches'], result['url'] or '')
                    satisfied = matched
                if expect.get('visible'):
                    satisfied = satisfied and (result['expect'] or {}).get('visible') is True
                if expect.get('text'):
                    satisfied = satisfied and (result['expect'] or {}).get('text') is True
                if satisfied:
                    return 'ok'
            if self.clock() >= deadline:
                return 'failed' if last_ok else 'unknown'
            await self.sleep(VERIFY_POLL)
