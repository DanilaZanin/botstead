"""Bot Hub HTTP API and in-process workers."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import struct
import subprocess
import sys
import uuid
import weakref
from typing import Annotated
from urllib.parse import urlsplit
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from croniter import croniter
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError,
                      field_validator, model_validator)
from bothub.builder import launcher_drafter, validate_draft
from bothub import auth
from bothub.db import open_pool
from bothub.launcher_client import launcher_client_from_env
from bothub.launcher_client import (ExecExit, ExecChunk, LauncherError, LauncherNotFound, LauncherTimeout, LauncherUnavailable,
                                    run_exec)
from bothub.gateway import (ADDRESS_CHANGED_DETAIL, ApprovalChangedError, GatewayProvider, UnresolvedHostError,
                            NetworksCache, configured_forbidden_networks, create_gateway_router, inspect_target, normalized_base,
                            own_networks, unapproved_addresses, _validated_target, _resolve_host)
from bothub import secrets as secrets_module
from bothub.secrets import encrypt_secret, decrypt_secret
from bothub.browser_control import (BrowserEventMasker, RFBClientFilter, RFBProtocolError, is_browser_tool,
                                    human_url, mask_browser_args, mask_browser_text, mask_url, transition, url_forbidden)
from bothub import activity, procedures
from bothub import context as ctxlib
from bothub.procedure_runner import PgStore, ProcedureRunner, RunError
from bothub.risk import MAC_READ_TOOLS, MAC_RISKY_TOOLS, decide, forbidden_reason, op_hash, remember_rule
from bothub.runner.base import TurnContext

SCHEMA = 'bothub.'
NOW = timezone.utc
LOGIN_IDLE_TIMEOUT = 600
LOGIN_TOTAL_TIMEOUT = 1800
PROVIDER_CHECK_TIMEOUT = 30
PROVIDER_CHECK_COOLDOWN = 30
PROVIDER_PROBE_PARALLEL = 2           # пробных запросов одновременно на пользователя (POST/PATCH с ключом)
PROVIDER_PROBE_RATE = 10              # пробных запросов в минуту на пользователя
PROVIDER_ADMIN_REQUESTS_PER_HOUR = 5  # заявок администратору (приватный адрес без одобрения) в час на пользователя
PROVIDER_REQUEST_RESOLVE_TIMEOUT = 5  # DNS одной строки в списке заявок администратора
AGY_CHECK_COOLDOWN = 600
PROVIDER_FORCE_COOLDOWN = 5
LAUNCHER_STOP_RETRY_DELAY = 1

# Решение владельца 2026-09-25: мозг ботов только Claude. codex/gemini остаются
# как раннеры (для fake-тестов и на будущее), но API не даёт завести/перевести
# на них бота.
ALLOWED_PROVIDERS = ('claude', 'fake')
PROVIDER_KINDS = {'anthropic_api': 'claude', 'openai_api': 'codex', 'openai_compatible': 'codex', 'google_api': 'gemini'}


def runner_provider(kind: str, cli: str | None) -> str:
    if kind == 'google_api':
        error('invalid', 400, 'google_api CLI routing unavailable')
    return ('gemini' if cli == 'agy' else cli) if kind == 'cli_subscription' else PROVIDER_KINDS[kind]
SUBSCRIPTION_MODELS = {
    'claude': ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001'),
    'codex': ('gpt-6-sol', 'gpt-6-luna', 'gpt-6-astra'),
    'agy': ('gemini-3.8-flash-high', 'gemini-3.8-flash-medium', 'gemini-3.8-flash-low', 'gemini-3.1-pro-high'),
}
AGY_MODEL_ID = re.compile(r'[a-z0-9][a-z0-9.-]{1,63}')
AGY_MODELS_MAX = 50
_ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


def parse_agy_models(text: str) -> list[str]:
    """Вывод `agy models`: строки `id<TAB>название`. Берутся строки с табом, id по AGY_MODEL_ID и непустым названием
    до 80 символов; повторы убираются, максимум AGY_MODELS_MAX. Остальные строки (заголовки, подсказки) пропускаются."""
    ids: list[str] = []
    for line in _ANSI.sub('', text).splitlines():
        model_id, tab, name = line.partition('\t')
        model_id, name = model_id.strip(), name.strip()
        if not tab or not AGY_MODEL_ID.fullmatch(model_id) or not name or len(name) > 80: continue
        if model_id not in ids: ids.append(model_id)
        if len(ids) >= AGY_MODELS_MAX: break
    return ids
DEFAULT_PROVIDER_BASES = {'anthropic_api': 'https://api.anthropic.com', 'openai_api': 'https://api.openai.com',
                          'google_api': 'https://generativelanguage.googleapis.com'}
# Подмена транспорта пробного запроса в тестах; в проде None.
PROBE_TRANSPORT: httpx.AsyncBaseTransport | None = None


class ProbeError(Exception):
    """Пробный запрос провайдера отклонён: code уходит клиенту (422), message пишется в last_error. Секрета в них нет.

    reapproval: одобренный администратором адрес сменился в DNS, провайдеру нужно повторное одобрение."""
    def __init__(self, code: str, message: str, *, reapproval: bool = False):
        super().__init__(message)
        self.code = code
        self.reapproval = reapproval


class OneShotResolver:
    """DNS один раз на запрос: проверка адреса, одобрение и пробный запрос видят один и тот же ответ."""
    def __init__(self):
        self.answers: dict[tuple[str, int], list] = {}

    async def __call__(self, host: str, port: int):
        if (host, port) not in self.answers:
            self.answers[(host, port)] = await _resolve_host(host, port)
        return self.answers[(host, port)]


def private_allow_hosts() -> tuple[str, ...]:
    """Устаревшее глобальное разрешение приватных адресов (PROVIDER_PRIVATE_ALLOW); новый путь: флаг allow_private у провайдера."""
    return tuple(filter(None, os.getenv('PROVIDER_PRIVATE_ALLOW', '').split(',')))


def same_base(new: str, stored: str | None) -> bool:
    """Тот же адрес провайдера с точностью до нормализации (слэш и /v1 в конце не считаются сменой адреса)."""
    try:
        return normalized_base(new) == normalized_base(stored or '')
    except ValueError:
        return new.rstrip('/') == (stored or '')


def secret_tail(secret: str) -> str | None:
    return secret[-4:] if len(secret) >= 12 else None


def canonical_ip(item) -> str:
    """IP из тела запроса в той же форме, что в Target.private: IPv4-mapped сведён к IPv4. ValueError на всё остальное."""
    if not isinstance(item, str) or '%' in item:
        raise ValueError('not an IP address')
    address = ipaddress.ip_address(item)
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


async def fetch_provider_models(kind: str, base_url: str | None, key: str, *, allow_private: bool = False,
                                approved_ips=None, forbidden=(), resolver=None) -> list[str]:
    """Один пробный запрос списка моделей через закреплённый проверенный IP. Бросает ProbeError(code, message).

    Разрешение DNS входит в PROVIDER_CHECK_TIMEOUT. approved_ips: одобренные администратором приватные IP (None: без ограничения).
    base_path адреса (split_base) уже в `pinned`: https://host/api и https://host/api/v1 оба идут на /api/v1/models."""
    path = '/v1beta/models' if kind == 'google_api' else '/v1/models'
    body = bytearray()
    try:
        async with asyncio.timeout(PROVIDER_CHECK_TIMEOUT):
            try:
                origin, pinned, hostname = await _validated_target(base_url or DEFAULT_PROVIDER_BASES[kind], private_allow_hosts(),
                                                                   resolver or _resolve_host, allow_private, approved_ips, forbidden)
            except UnresolvedHostError as exc:
                raise ProbeError('unreachable', str(exc)) from None
            except ApprovalChangedError:
                raise ProbeError('invalid_base_url', ADDRESS_CHANGED_DETAIL, reapproval=True) from None
            except ValueError as exc:
                raise ProbeError('invalid_base_url', str(exc)) from None
            headers = {'host': urlsplit(origin).netloc}
            if kind == 'anthropic_api': headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
            elif kind == 'google_api': headers['x-goog-api-key'] = key
            else: headers['authorization'] = 'Bearer ' + key
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15, transport=PROBE_TRANSPORT) as client:
                request = client.build_request('GET', pinned + path, headers=headers)
                request.extensions['sni_hostname'] = hostname
                response = await client.send(request, stream=True)
                try:
                    status = response.status_code
                    if response.is_redirect or 300 <= status < 400: raise ProbeError('incompatible', 'upstream redirect refused')
                    if status in (401, 403): raise ProbeError('key_rejected', 'provider rejected the key')
                    if status in (408, 429) or status >= 500: raise ProbeError('unreachable', 'provider unavailable')
                    if status >= 400: raise ProbeError('incompatible', 'provider check failed')
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024: raise ProbeError('incompatible', 'model response too large')
                finally:
                    await response.aclose()
    except ProbeError:
        raise
    except (httpx.HTTPError, OSError, TimeoutError):
        raise ProbeError('unreachable', 'provider unreachable') from None
    try:
        payload = json.loads(body)
        entries = payload.get('models' if kind == 'google_api' else 'data', [])
        names = [item.get('name', '').removeprefix('models/') if kind == 'google_api' else item.get('id', '')
                 for item in entries if isinstance(item, dict)]
    except (ValueError, AttributeError, TypeError):
        raise ProbeError('incompatible', 'provider returned an unexpected model list') from None
    if not names: raise ProbeError('incompatible', 'provider returned no models')
    return names

log = logging.getLogger('bothub')
ACTIVE_STATUSES = ('running', 'waiting_approval', 'waiting_mac')
HEALTH_TIMEOUT = 2.0
OUTBOX_BACKOFF_CAP = 3600


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


_LOG_STD = frozenset(logging.LogRecord('', 0, '', 0, '', (), None).__dict__) | {'message', 'asctime'}


class JsonFormatter(logging.Formatter):
    """Одна JSON-строка на запись: ts, level, logger, msg, поля из extra и exc (если есть)."""

    def format(self, record: logging.LogRecord) -> str:
        out = {'ts': datetime.fromtimestamp(record.created, NOW).isoformat(timespec='milliseconds'),
               'level': record.levelname, 'logger': record.name, 'msg': record.getMessage()}
        for key, value in record.__dict__.items():
            if key not in _LOG_STD and not key.startswith('_'):
                out[key] = value
        if record.exc_info:
            out['exc'] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """JSON-строки в stdout, уровень из BOTHUB_LOG_LEVEL (по умолчанию INFO; неизвестный
    уровень тоже даёт INFO). Повторный вызов заменяет свой обработчик, а не плодит дубли."""
    level = logging.getLevelNamesMapping().get(os.getenv('BOTHUB_LOG_LEVEL', 'INFO').strip().upper(), logging.INFO)
    for handler in [h for h in log.handlers if getattr(h, '_bothub_json', False)]:
        log.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._bothub_json = True
    log.addHandler(handler)
    log.setLevel(level)


async def supervise(name: str, step, interval: float, error_delay: float | None = None) -> None:
    """Пункт 7: цикл фонового воркера. Исключение в одном проходе логируется и не убивает
    цикл. step возвращает truthy, если был занят (тогда следующий проход без паузы)."""
    while True:
        try:
            busy = await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('loop_error', extra={'loop': name})
            await asyncio.sleep(interval if error_delay is None else error_delay)
            continue
        await asyncio.sleep(0 if busy else interval)  # sleep(0) отдаёт управление event loop даже занятому циклу


def launcher_failure_detail(exc: Exception) -> tuple[str, str] | None:
    """(код, пояснение) для ошибки лаунчера, которую владелец может исправить сам. None: обычный сбой.
    Бот заморожен, если разморозка при старте ядра не удалась или человек сейчас управляет браузером."""
    if isinstance(exc, LauncherError) and exc.code == 'frozen':
        return ('bot_frozen', 'Процессы бота остановлены: идёт перехват управления браузером или разморозка не '
                              'удалась при запуске ядра. Верните управление боту в окне браузера или пересоздайте бота.')
    return None


def push_status(exc: Exception) -> int | None:
    """HTTP-код ответа push-сервиса из исключения pywebpush (WebPushException.response)."""
    code = getattr(getattr(exc, 'response', None), 'status_code', None)
    return code if isinstance(code, int) else None


def outbox_backoff(attempts: int) -> int:
    return min(5 * 2 ** max(attempts - 1, 0), OUTBOX_BACKOFF_CAP)


def decide_permission(bot: dict, tool: str, args: dict, turn: dict | None = None) -> tuple[str, bool]:
    """Раздел 4 контракта: единая проверка риска и авто-разрешения (логика в bothub.risk.decide).

    risk = classify(tool, args) на сервере (находки 4: сервер больше не доверяет risk от
    бота). pay/delete/login, установка ПО, отправка данных наружу из Bash, не читающие
    действия на Mac и всё, что не удалось классифицировать, не авто-разрешаются никогда.
    Правила auto_allow сравнивают аргументы точно, без glob. mac_full_control авто-разрешает
    только читающие mac-инструменты и shell-команды из белого списка чтения.
    `turn` не используется в самой логике, но принимается для симметрии с
    местами вызова (approvals/mac_call сами проверяют статус/владение turn).
    """
    return decide(bot, tool, args)


def check_provider(provider: str) -> None:
    if provider not in ALLOWED_PROVIDERS:
        error('invalid', 400, 'provider не поддерживается: мозг ботов только claude')


def data(row):
    return jsonable_encoder(dict(row)) if row else None


_SENSITIVE_FIELD = re.compile(r'secret|passw|token|value|key|credential', re.I)


class Unprocessable(ValueError):
    """Предел длины или формат поля: ответ 422 вместо 400 (остальные ошибки схемы остаются 400)."""


def validation_status(exc: RequestValidationError | ValidationError) -> int:
    errors = exc.errors()
    return 422 if errors and all(isinstance((item.get('ctx') or {}).get('error'), Unprocessable) for item in errors) else 400


def validation_detail(exc: RequestValidationError | ValidationError) -> str:
    """Where and why a request is invalid, never what was sent: pydantic puts the offending input into
    the error, and for secret-input that is the password. The place can hold a key of the body (extra=forbid),
    so it is cut and made encodable: a lone surrogate in a key must not break the error response itself."""
    parts = []
    for item in exc.errors():
        loc = tuple(item.get('loc', ()))
        if item.get('type') == 'extra_forbidden':
            loc = loc[:-1]  # имя лишнего ключа пришло от клиента: в ответ его не кладём
        place = '.'.join(str(part) for part in loc)
        place = place[:100].encode('utf-8', 'replace').decode('utf-8').replace('\x00', '?')
        reason = 'invalid value' if _SENSITIVE_FIELD.search(place) else str(item.get('msg', 'invalid'))
        if item.get('type') == 'extra_forbidden':
            reason = 'unknown field'
        parts.append(f'{place}: {reason}' if place else reason)
    return '; '.join(parts) or 'invalid'


def error(code, status=400, detail=''):
    raise HTTPException(status, {'error': code, 'detail': detail or code})


LAUNCHER_SYNC_DELAY_START = 1.0  # стартовая сверка с лаунчером: пауза между попытками растёт вдвое до предела
LAUNCHER_SYNC_DELAY_MAX = 30.0
LAUNCHER_SYNC_ERROR_AFTER = 5  # с этой неудачной попытки сверка логируется как error
LAUNCHER_SYNC_FIRST_WAIT = 3.0  # сколько lifespan ждёт первую попытку сверки: при живом лаунчере маршруты открываются сразу
# Фон после POST /api/bots (контейнер бота, сеть пользователя) ждёт эту паузу, чтобы ответ клиенту ушёл в сокет раньше первого
# вызова лаунчера: `docker network connect` перестраивает правила Docker и рвёт соединения ядра, которые ещё не отданы.
BOT_START_DELAY = 0.3
LIST_ITEM_MAX = 200
LIST_MAX = 200
CLIENT_MAX = 64
TIMEZONE_MAX = 64
DEVICE_MAX = 200
EMAIL_MAX = 254
PASSWORD_MAX = 512
TOKEN_MAX = 512
BODY_MAX = 1024*1024  # общий предел тела запроса (не multipart); у файлов свой путь, у /hooks/{id} свой предел 64 КБ
TEXT_DEPTH_MAX = 64  # глубже вложенность JSON в теле не принимается
RULE_PATTERN_MAX = 500  # auto_allow: pattern правила
RULE_MATCH_KEYS_MAX = 64
RULE_MATCH_VALUE_MAX = 4096
RULE_HASH_MAX = 128  # op_hash: SHA256 в hex это 64
RULE_SCOPE_MAX = 64
BROWSER_ELEMENT_NAME_MAX = 200


def check_list_size(value: list | None, field: str) -> None:
    if value is not None and len(value) > LIST_MAX:
        raise Unprocessable(f'{field} exceeds {LIST_MAX} items')


def check_text(value: str, field: str, *, maximum: int, minimum: int = 0) -> str:
    if len(value) > maximum:
        raise Unprocessable(f'{field} exceeds {maximum} characters')
    if len(value) < minimum:
        raise Unprocessable(f'{field} is shorter than {minimum} characters')
    return value


def check_mcp_allow(value: list[str] | None) -> list[str] | None:
    check_list_size(value, 'mcp_allow')
    for item in value or ():
        check_text(item, 'mcp_allow item', maximum=LIST_ITEM_MAX, minimum=1)
    return value


def check_auto_allow(value: list[dict] | None) -> list[dict] | None:
    check_list_size(value, 'auto_allow')
    for rule in value or ():
        if 'tool' in rule:
            if not isinstance(rule['tool'], str):
                raise ValueError('auto_allow tool must be a string')
            check_text(rule['tool'], 'auto_allow tool', maximum=LIST_ITEM_MAX, minimum=1)
        for field, maximum in (('pattern', RULE_PATTERN_MAX), ('op_hash', RULE_HASH_MAX), ('scope', RULE_SCOPE_MAX)):
            if field in rule:
                if not isinstance(rule[field], str):
                    raise ValueError(f'auto_allow {field} must be a string')
                check_text(rule[field], f'auto_allow {field}', maximum=maximum)
        if 'match' in rule:
            match = rule['match']
            if not isinstance(match, dict):
                raise ValueError('auto_allow match must be an object')
            if len(match) > RULE_MATCH_KEYS_MAX:
                raise Unprocessable(f'auto_allow match exceeds {RULE_MATCH_KEYS_MAX} keys')
            for key, item in match.items():
                check_text(key, 'auto_allow match key', maximum=LIST_ITEM_MAX)
                check_text(item if isinstance(item, str) else canonical(item), 'auto_allow match value', maximum=RULE_MATCH_VALUE_MAX)
    return value


def check_json_text(value):
    """Каждая строка тела (и каждый ключ, на любой глубине) без NUL и кодируется в UTF-8: asyncpg на таком тексте
    отвечает 500. Ответ 422 называет верхнее поле и не содержит значения. Глубже TEXT_DEPTH_MAX 422 тоже (обход без рекурсии)."""
    top = list(value.items()) if isinstance(value, dict) else [('body', value)]
    for name, item in top:
        label = name if isinstance(name, str) and auth.is_clean_text(name) and len(name) <= 64 else 'body'
        stack = [(item, 0), (name, 0)]
        while stack:
            current, depth = stack.pop()
            if isinstance(current, str):
                if not auth.is_clean_text(current):
                    raise Unprocessable(f'{label} contains a NUL or an invalid character')
            elif isinstance(current, (dict, list, tuple)):
                if depth >= TEXT_DEPTH_MAX:
                    raise Unprocessable(f'{label} is nested too deeply')
                children = current.items() if isinstance(current, dict) else ((child,) for child in current)
                for child in children:
                    stack.extend((part, depth + 1) for part in child)
    return value


JsonObject = Annotated[dict, AfterValidator(check_json_text)]  # тело маршрута без своей модели: та же проверка текста


def check_timezone(value: str) -> str:
    check_text(value, 'timezone', maximum=TIMEZONE_MAX, minimum=1)
    try:
        ZoneInfo(value)
    except Exception:  # ZoneInfoNotFoundError, ValueError (путь с «..»), OSError (каталог вместо зоны)
        raise Unprocessable('timezone is not a known time zone') from None
    return value


def parse_email(value) -> str:
    """Адрес из тела публичного маршрута: нормализованный (strip, нижний регистр) или 422. Общая проверка для
    setup, login и приёма приглашения: до 254 символов, один «@» с текстом по обе стороны, без пробелов и управляющих."""
    if not isinstance(value, str):
        error('invalid', 400, 'email')
    email = value.strip().lower()
    local, at, domain = email.partition('@')
    if (not email or len(email) > EMAIL_MAX or not at or not local or not domain or '@' in domain
            or any(char.isspace() or not char.isprintable() for char in email)):
        error('invalid', 422, 'email')
    return email


def check_credential(body: dict, field: str, *, maximum: int = PASSWORD_MAX) -> str:
    """Строковое поле публичного маршрута (пароль, код, токен): не строка 400, длиннее предела или не кодируется в UTF-8 422."""
    value = body.get(field)
    if not isinstance(value, str):
        error('invalid', 400, field)
    if not auth.is_clean_text(value):
        error('invalid', 422, field)
    if len(value) > maximum:
        error('invalid', 422, field)
    return value


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, {'error': 'invalid', 'detail': f'request body exceeds {BODY_MAX // 1024 // 1024} MiB'})


class BodyLimit:
    """Предел тела запроса: по Content-Length сразу, без него по принятым байтам. multipart (файлы) и /gateway/ не считаются."""

    def __init__(self, app, limit: int | None = None):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        if headers.get(b'content-type', b'').lower().startswith(b'multipart/') or scope['path'].startswith('/gateway/'):
            return await self.app(scope, receive, send)  # файлы и прокси шлюза (промпты CLI) со своими пределами
        limit = BODY_MAX if self.limit is None else self.limit
        started = False

        async def refuse():
            await JSONResponse(BodyTooLarge().detail, status_code=413)(scope, receive, send)

        length = headers.get(b'content-length', b'')
        if length.isdigit() and int(length) > limit:
            return await refuse()
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > limit:
                    raise BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            started = started or message['type'] == 'http.response.start'
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if started:
                raise
            await refuse()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def browser_safe_url(value: str | None) -> str | None:
    """Audit address: scheme, host and path only; query, userinfo and fragment can carry passwords or tokens."""
    return mask_url(value) or None


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @model_validator(mode='before')
    @classmethod
    def clean_text(cls, data):
        if isinstance(data, dict):
            check_json_text(data)
        return data


class BotIn(Body):
    id: str | None = Field(default=None, pattern=r'^[a-z0-9-]{1,32}$')
    name: str = Field(max_length=80)
    provider: str
    model: str
    provider_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    role: str = Field(default='', max_length=512)
    instructions: str = Field(default='', max_length=64*1024)
    avatar: str = Field(default='robot', max_length=128)
    executor: str = Field(default='container', max_length=128)
    mac_full_control: StrictBool = False
    auto_allow: list[dict] = Field(default_factory=list)
    mcp_allow: list[str] = Field(default_factory=list)
    budget_daily_tokens: StrictInt = Field(default=200000, ge=0, le=10**12)
    max_turn_seconds: StrictInt = Field(default=1800, ge=30, le=86400)
    auto_compact_percent: StrictInt | None = Field(default=ctxlib.AUTO_COMPACT_DEFAULT, ge=ctxlib.AUTO_COMPACT_MIN, le=ctxlib.AUTO_COMPACT_MAX)  # раздел 15: null выключает
    schedule: dict | None = None  # раздел 9: как в BotDraft, kind всегда 'cron'
    start_container: StrictBool = True  # раздел 9: по умолчанию true в режиме docker
    skip_container: StrictBool = False

    @field_validator('instructions')
    @classmethod
    def instructions_size(cls, value: str) -> str:
        if len(value.encode()) > 64*1024:
            raise ValueError('instructions exceeds 64 KiB')
        return value

    @field_validator('mcp_allow')
    @classmethod
    def mcp_allow_limits(cls, value: list[str]) -> list[str]:
        return check_mcp_allow(value)

    @field_validator('auto_allow')
    @classmethod
    def auto_allow_limits(cls, value: list[dict]) -> list[dict]:
        return check_auto_allow(value)


class BotPatch(Body):
    """Частичный BotIn для PATCH /api/bots/{id} (находка 16): без id, все поля
    опциональны — вместо старого "дырявого" `dict`, который валил 500 на
    несовпадении типов вместо 400."""
    name: str | None = Field(default=None, max_length=80)
    provider: str | None = None
    model: str | None = None
    provider_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    role: str | None = Field(default=None, max_length=512)
    instructions: str | None = Field(default=None, max_length=64*1024)
    avatar: str | None = Field(default=None, max_length=128)
    executor: str | None = Field(default=None, max_length=128)
    mac_full_control: StrictBool | None = None
    auto_allow: list[dict] | None = None
    mcp_allow: list[str] | None = None
    budget_daily_tokens: StrictInt | None = Field(default=None, ge=0, le=10**12)
    max_turn_seconds: StrictInt | None = Field(default=None, ge=30, le=86400)
    auto_compact_percent: StrictInt | None = Field(default=None, ge=ctxlib.AUTO_COMPACT_MIN, le=ctxlib.AUTO_COMPACT_MAX)  # явный null выключает

    @field_validator('instructions')
    @classmethod
    def instructions_size(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode()) > 64*1024:
            raise ValueError('instructions exceeds 64 KiB')
        return value

    @field_validator('mcp_allow')
    @classmethod
    def mcp_allow_limits(cls, value: list[str] | None) -> list[str] | None:
        return check_mcp_allow(value)

    @field_validator('auto_allow')
    @classmethod
    def auto_allow_limits(cls, value: list[dict] | None) -> list[dict] | None:
        return check_auto_allow(value)


class ThreadIn(Body):
    bot_id: str
    title: str = Field(default='', max_length=512)
    kind: str = 'direct'
    dry_run: bool = False


class TurnIn(Body):
    prompt: str = Field(max_length=256*1024)
    client: str = 'api'

    @field_validator('client')
    @classmethod
    def client_size(cls, value: str) -> str:
        return check_text(value, 'client', maximum=CLIENT_MAX)

    @field_validator('prompt')
    @classmethod
    def prompt_size(cls, value: str) -> str:
        if len(value.encode()) > 256*1024:
            raise ValueError('prompt exceeds 256 KiB')
        return value


class CompactIn(Body):
    """POST /api/threads/{id}/compact: тела нет, но если оно пришло, лишних полей быть не должно."""


class ApprovalIn(Body):
    thread_id: uuid.UUID
    turn_id: uuid.UUID | None = None
    risk: str
    title: str = Field(max_length=512)
    tool: str = Field(max_length=512)
    args: dict

    @field_validator('args')
    @classmethod
    def args_size(cls, value: dict) -> dict:
        if len(canonical(value).encode()) > 64*1024:
            raise ValueError('args exceeds 64 KiB')
        return value


class DecisionIn(Body):
    decision: str
    remember: bool = False
    client: str = 'api'

    @field_validator('client')
    @classmethod
    def client_size(cls, value: str) -> str:
        return check_text(value, 'client', maximum=CLIENT_MAX)


def refused_tool_name(tool) -> str:
    """Имя отклонённого инструмента для события compact_failed: одна строка, печатные символы, до 100; без аргументов."""
    name = ''.join(char if char.isprintable() else ' ' for char in str(tool or ''))
    return activity.short(name, 100) or 'unknown'


def client_datetime(value: datetime) -> datetime:
    """Время от клиента в UTC; без пояса считаем UTC, а не местным временем сервера. ValueError, если в UTC оно не
    помещается в календарь (0001-01-01+14:00, 9999-12-31-14:00): иначе драйвер падает OverflowError и клиент видит 500."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=NOW)
    try:
        return value.astimezone(NOW)
    except (OverflowError, ValueError):
        raise ValueError('date out of range') from None


class MemoryIn(Body):
    text: str = Field(max_length=16*1024)
    bot_id: str | None = None
    expires_at: datetime | None = None

    @field_validator('expires_at')
    @classmethod
    def expires_in_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else client_datetime(value)

    @field_validator('text')
    @classmethod
    def text_size(cls, value: str) -> str:
        if len(value.encode()) > 16*1024:
            raise ValueError('text exceeds 16 KiB')
        return value


class UsageIn(Body):
    thread_id: uuid.UUID
    turn_id: uuid.UUID
    provider: str = Field(max_length=64)
    model: str = Field(max_length=128)
    tokens_in: StrictInt = Field(default=0, ge=0, le=10**9)
    tokens_out: StrictInt = Field(default=0, ge=0, le=10**9)


class ScheduleIn(Body):
    bot_id: str
    name: str = Field(max_length=512)
    kind: str
    cron: str | None = None
    timezone: str = 'Europe/Moscow'
    prompt: str = Field(max_length=16*1024)
    enabled: bool = True
    catch_up: StrictBool = False  # раздел 16: один запуск после возобновления вместо пропущенных (только cron)

    @field_validator('timezone')
    @classmethod
    def timezone_known(cls, value: str) -> str:
        return check_timezone(value)


class MacCallIn(Body):
    thread_id: uuid.UUID
    turn_id: uuid.UUID
    tool: str = Field(max_length=512)
    args: dict
    timeout: int = 60

    @field_validator('args')
    @classmethod
    def args_size(cls, value: dict) -> dict:
        if len(canonical(value).encode()) > 64*1024:
            raise ValueError('args exceeds 64 KiB')
        return value


class PushIn(Body):
    endpoint: str = Field(max_length=512)
    keys: dict
    device: str = ''

    @field_validator('device')
    @classmethod
    def device_size(cls, value: str) -> str:
        return check_text(value, 'device', maximum=DEVICE_MAX)

    @field_validator('keys')
    @classmethod
    def keys_size(cls, value: dict) -> dict:
        if any(not isinstance(key,str) or len(key)>512 or not isinstance(item,str) or len(item)>512
               for key,item in value.items()):
            raise ValueError('keys exceeds 512 characters')
        return value


class DraftIn(Body):
    description: str = Field(min_length=10, max_length=4000)


class SecretInputIn(Body):
    value: str = Field(min_length=1, max_length=4096)
    save_as: str | None = Field(default=None, pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')


class BrowserCallIn(Body):
    thread_id: uuid.UUID
    turn_id: uuid.UUID
    authorization_id: uuid.UUID | None = None
    action: str = Field(pattern=r'^(navigate|click|fill|snapshot|screenshot)$')
    target: str = Field(default='', max_length=256)
    url: str | None = Field(default=None, max_length=2048)
    result: str = Field(default='ok', pattern=r'^(ok|error)$')
    role: str | None = Field(default=None, pattern=r'^[A-Za-z][A-Za-z0-9-]{0,63}$')  # роль и имя элемента из snapshot: сырьё для процедур
    name: str | None = None

    @field_validator('role', mode='before')
    @classmethod
    def role_clean(cls, value):
        return procedures.strip_invisible(value) if isinstance(value, str) else value

    @field_validator('name')
    @classmethod
    def name_size(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # События бота не отклоняются из-за подписи: управляющие и невидимые символы (bidi, нулевой ширины) вычищаются,
        # чтобы они не прятали слово «пароль» от классификатора и не подделывали подпись в списке шагов.
        return procedures.strip_invisible(check_text(value, 'name', maximum=BROWSER_ELEMENT_NAME_MAX))


class PauseIn(Body):
    reason: str | None = Field(default=None, max_length=200)


class ProcedureIn(Body):
    name: str
    description: str = ''
    bot_id: str | None = None
    params: list[dict] = []
    steps: list[dict] = []


class ProcedurePatch(Body):
    name: str | None = None
    description: str | None = None
    bot_id: str | None = None
    params: list[dict] | None = None
    steps: list[dict] | None = None
    status: str | None = Field(default=None, pattern=r'^(active|archived)$')


class ProcedureImportIn(Body):
    format: str | None = None
    name: str
    description: str = ''
    params: list[dict] = []
    steps: list[dict] = []


class ProcedureFromTurnIn(Body):
    thread_id: uuid.UUID
    turn_id: uuid.UUID | None = None
    name: str


class ProcedureRunIn(Body):
    bot_id: str | None = None
    thread_id: uuid.UUID | None = None
    params: dict = {}


class ProcedureDecideIn(Body):
    action: str = Field(pattern=r'^(retry|skip|stop)$')


def create_app(runner_factory=None, drafter=None, launcher=None) -> FastAPI:
    if runner_factory is None:
        from bothub.runner import get_runner
        runner_factory = get_runner
    if launcher is None:
        launcher = launcher_client_from_env()
    if drafter is None:
        drafter = lambda description, owner_id: launcher_drafter(description, launcher, owner_id)
    configure_logging()
    # Цели провайдеров, закрытые при любом одобрении: подсети собственных интерфейсов ядра (сети Docker с Postgres и ботами)
    # и PROVIDER_FORBIDDEN_CIDRS. Опечатка в списке останавливает запуск. Сети ядра читаются при каждой проверке адреса
    # с TTL 30 с: лаунчер подключает ядро к сетям ботов bothub-u-* уже после запуска.
    configured_networks = configured_forbidden_networks()
    own_cache = NetworksCache(lambda: own_networks())
    def forbidden_networks():
        return (*configured_networks,*own_cache())
    log.info('provider forbidden networks: %s', ', '.join(map(str, (*configured_networks,*own_networks()))) or 'none')
    app = FastAPI()
    app.add_middleware(BodyLimit)
    app.state.launcher = launcher
    # Стартовая сверка с лаунчером идёт фоновой задачей (lifespan). Пока она не прошла, маршруты, которым нужен лаунчер,
    # отвечают 503 launcher_unavailable: до неё состояние browser_control не сверено с тем, что лаунчер держит замороженным.
    # Без запущенного lifespan (тесты с подменой) флаг истинен; lifespan сбрасывает его, если лаунчер настроен.
    app.state.launcher_ready = True
    app.state.launcher_synced = True  # вся стартовая сверка (не только заморозка) закончилась
    app.state.has_users = False
    app.state.own_networks = own_cache
    app.state.forbidden_networks = forbidden_networks
    def launcher_waiting() -> bool:
        return app.state.launcher is not None and not app.state.launcher_ready

    def prepare_user_network(owner_id) -> None:
        """Сеть нового пользователя и подключение к ней ядра, фоном и до его первого бота: `docker network connect` на время
        рвёт соединения ядра, и в запросе на создание первого бота это обрывало HTTP-ответ. Отказ не ошибка: сеть поднимет
        создание бота, как раньше."""
        if app.state.launcher is None or os.getenv('BOTHUB_RUNNER_EXEC', 'local') != 'docker':
            return
        async def run():
            try:
                if app.state.launcher_ready:
                    await app.state.launcher.ensure_network(str(owner_id))
            except Exception as exc:
                log.warning('user_network_prepare_failed', extra={'owner_id': str(owner_id), 'error': type(exc).__name__})
        task = asyncio.create_task(run())
        background.add(task)  # сильная ссылка: иначе задачу может собрать GC
        task.add_done_callback(background.discard)

    def require_launcher() -> None:
        if app.state.launcher is None or not app.state.launcher_ready:
            error('launcher_unavailable',503)

    async def gateway_binding(bot_id: str, provider_id: str):
        try:
            provider_uuid = uuid.UUID(provider_id)
        except ValueError:
            return None
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow(
                "select p.*, b.max_turn_seconds, u.status as owner_status from bothub.bots b "
                "join bothub.providers p on p.id=b.provider_id and p.owner_id=b.owner_id "
                "join bothub.users u on u.id=b.owner_id where b.id=$1 and p.id=$2", bot_id, provider_uuid)
            if not row or row['kind']=='cli_subscription' or row['status']!='ok' or not row['secret_encrypted']:
                return None
            models = await con.fetch('select name from bothub.models where provider_id=$1 and enabled=true',row['id'])
        return GatewayProvider(str(row['id']),row['kind'],row['base_url'] or DEFAULT_PROVIDER_BASES[row['kind']],
            decrypt_secret(bytes(row['secret_encrypted']),row['id'].bytes).decode(),
            row['owner_status']=='active',[m['name'] for m in models],owner_id=str(row['owner_id']),
            max_turn_seconds=row['max_turn_seconds'],allow_private=row['allow_private'],
            allow_private_ips=tuple(row.get('allow_private_ips') or ()))

    async def gateway_turn_authorize(bot_id, provider_id, turn_id):
        try: turn_uuid=uuid.UUID(turn_id); provider_uuid=uuid.UUID(provider_id)
        except ValueError: return False
        async with app.state.pool.acquire() as con:
            return bool(await con.fetchval("select 1 from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                "join bothub.bots b on b.id=th.bot_id join bothub.users u on u.id=b.owner_id "
                "where t.id=$1 and b.id=$2 and b.provider_id=$3 and u.status='active' "
                "and t.status in ('running','waiting_approval','waiting_mac')",turn_uuid,bot_id,provider_uuid))

    async def gateway_usage(bot_id, provider_id, model, tokens_in, tokens_out, cache_read, cache_write, status, turn_id):
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow("select t.id,t.thread_id,t.turn_type,b.provider,b.model from bothub.turns t "
                "join bothub.threads th on th.id=t.thread_id join bothub.bots b on b.id=th.bot_id "
                "where b.id=$1 and b.provider_id=$2 and t.id=$3",bot_id,uuid.UUID(provider_id),uuid.UUID(turn_id))
            if not row: return
            model_id = await con.fetchval('select id from bothub.models where provider_id=$1 and name=$2',uuid.UUID(provider_id),model)
            await con.execute('insert into bothub.usage(bot_id,thread_id,turn_id,provider,model,tokens_in,tokens_out,provider_id,model_id,tokens_cache_read,tokens_cache_write,turn_type) '
                'values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)',bot_id,row['thread_id'],row['id'],row['provider'],model,
                tokens_in,tokens_out,uuid.UUID(provider_id),model_id,cache_read,cache_write,row['turn_type'])
            if row['turn_type'] != 'compact':  # ход сжатия невидим: расход считается, события в ленте нет
                await append_event(con,row['thread_id'],row['id'],'usage','system',
                    {'provider':row['provider'],'model':model,'tokens_in':tokens_in,'tokens_out':tokens_out,
                     'tokens_cache_read':cache_read,'tokens_cache_write':cache_write,'status':status})
            spent = await budget_spent(con,bot_id)
            budget = await con.fetchval('select budget_daily_tokens from bothub.bots where id=$1',bot_id)
        if spent >= budget:
            await app.state.fail_turn(row['id'],'budget_exceeded','budget_exceeded',
                {'turn_id':str(row['id']),'spent':int(spent),'budget':int(budget)})

    async def provider_address_changed(provider: GatewayProvider):
        """Шлюз отказал: одобренный адрес сменился в DNS. Провайдер ждёт повторного одобрения, флаг и набор не снимаются."""
        try: provider_uuid=uuid.UUID(provider.id)
        except ValueError: return
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.providers set status='pending_admin',last_error=$2,last_check_at=null "
                "where id=$1 and allow_private and status in ('ok','new','error','unchecked')",provider_uuid,ADDRESS_CHANGED_DETAIL)

    app.state.provider_address_changed = provider_address_changed
    gateway_secret = os.getenv('BOTHUB_GATEWAY_TOKEN_SECRET') or auth.new_token()
    app.state.gateway = create_gateway_router(gateway_binding,gateway_usage,gateway_secret,
        allowed_private_hosts=private_allow_hosts(),
        turn_authorize=gateway_turn_authorize,
        forbidden_networks=forbidden_networks,on_address_changed=provider_address_changed)
    app.include_router(app.state.gateway)
    listeners: dict[str, set[asyncio.Queue]] = {}
    pending_mac: dict[str, dict[str, asyncio.Future]] = {}
    running: dict[str, tuple[asyncio.Task, object]] = {}
    stop_retries: dict[tuple[str, str], asyncio.Task] = {}
    stop_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
    background: set[asyncio.Task] = set()
    provider_checks: dict[uuid.UUID, asyncio.Task] = {}
    probe_slots: weakref.WeakValueDictionary[str, asyncio.Semaphore] = weakref.WeakValueDictionary()
    mac_socket: dict[str, WebSocket] = {}
    mac_ready = asyncio.Event()
    app.state.setup_code = auth.new_token()
    app.state.dummy_password_hash = auth.hash_password('invalid-password-for-timing')
    attempts: dict[tuple[str, str], list[float]] = {}
    email_attempts: dict[str, list[float]] = {}
    # Сокет закрывает только его собственный обработчик: остальные кладут в его очередь команду закрытия.
    WS_CLOSE = '__close__'
    active_ws: dict[str, set[asyncio.Queue]] = {}
    active_user_ws: dict[str, set[asyncio.Queue]] = {}
    active_login: dict[tuple[str, str], tuple[asyncio.Queue, str] | None] = {}
    active_screen: dict[str, tuple[asyncio.Queue, str] | None] = {}
    screen_ready: set[str] = set()
    browser_since: dict[str, datetime] = {}
    browser_by: dict[str, str | None] = {}
    browser_locks: dict[str, asyncio.Lock] = {}
    browser_authorizations: dict[uuid.UUID, tuple[str, uuid.UUID, uuid.UUID, float]] = {}

    def close_screen(bot_id: str, code: int = 4410) -> None:
        screen = active_screen.get(bot_id)
        if screen:
            screen[0].put_nowait({WS_CLOSE: code})

    def rate_limit(key: tuple[str, str], *, maximum=8, window=60):
        now = asyncio.get_running_loop().time()
        if len(attempts)>4096:
            for old_key in tuple(attempts):
                if not attempts[old_key] or now-attempts[old_key][-1]>=window:
                    attempts.pop(old_key,None)
            if len(attempts)>4096:
                attempts.pop(next(iter(attempts)))
        recent = [t for t in attempts.get(key, ()) if now-t < window]
        if len(recent) >= maximum:
            error('rate_limited',429)
        recent.append(now)
        attempts[key] = recent

    async def email_delay(email: str):
        now = asyncio.get_running_loop().time()
        recent = [t for t in email_attempts.get(email, ()) if now-t < 60]
        if len(email_attempts) > 4096:
            email_attempts.clear()
        if recent:
            await asyncio.sleep(min(len(recent) * .25, 2))
        recent.append(now)
        email_attempts[email] = recent

    token_equals = auth.token_equals

    async def legacy_enabled(con) -> bool:
        if os.getenv('BOTHUB_LEGACY_AUTH', '').lower() in ('0', 'false', 'off'):
            return False
        return await con.fetchval("select value from bothub.settings where key='legacy_auth_enabled'") is True

    async def setup_admin(con):
        return await con.fetchrow("select u.id,u.role,u.status from bothub.settings s join bothub.users u on u.id::text=(s.value #>> '{}') where s.key='setup_user_id' and u.role='admin' and u.status='active'")

    def ws_token(ws: WebSocket) -> str:
        header = ws.headers.get('authorization')
        if header is not None:
            return header.removeprefix('Bearer ') if header.startswith('Bearer ') else ''
        protocols = [part.strip() for part in ws.headers.get('sec-websocket-protocol', '').split(',')]
        if len(protocols) >= 2 and protocols[0].lower() == 'bearer':
            return protocols[1]
        token = ws.query_params.get('token', '')
        if token:
            log.warning('deprecated_query_token', extra={'path': ws.url.path})
        return token

    def cookie_path() -> str:
        path = os.getenv('BOTHUB_BASE_PATH', '/bots').strip('/')
        return f'/{path}/' if path else '/'

    def set_cookie(response: JSONResponse, token: str):
        response.set_cookie('bothub_session',token,max_age=30*86400,httponly=True,secure=True,samesite='lax',path=cookie_path())

    async def revoke_user(con, user_id):
        hashes = await con.fetch("update bothub.sessions set revoked_at=now() where user_id=$1 and revoked_at is null returning id_hash",user_id)
        for item in hashes:
            for q in tuple(active_ws.get(item['id_hash'],())):
                q.put_nowait({WS_CLOSE: 4401})
        for q in tuple(active_user_ws.get(str(user_id),())):
            q.put_nowait({WS_CLOSE: 4401})

    async def revoke_one_session(con, session_hash: str, user_id):
        revoked = await con.fetchval(
            'update bothub.sessions set revoked_at=now() where id_hash=$1 and user_id=$2 and revoked_at is null returning id_hash',
            session_hash, user_id,
        )
        if revoked:
            for q in tuple(active_ws.get(revoked, ())):
                q.put_nowait({WS_CLOSE: 4401})
        return revoked

    def via_owner_proxy(headers) -> bool:
        # nginx за SSO-прокси ставит X-Bothub-Proxy с общим секретом и Remote-User; контейнеры ботов секрета не знают.
        secret = os.getenv('BOTHUB_PROXY_SECRET', '')
        remote_user = headers.get('remote-user', '')
        if not secret or not remote_user or not token_equals(headers.get('x-bothub-proxy', ''), secret):
            return False
        # Находка 19: если задан BOTHUB_OWNER_USER, Remote-User обязан совпадать -
        # иначе прокси-секрет пускал owner'ом любого Remote-User, который ему передали.
        owner_user = os.getenv('BOTHUB_OWNER_USER')
        return bool(owner_user) and token_equals(remote_user, owner_user)

    async def principal(request: Request, *, owner=False, bot=False, mac=False, admin=False):
        header = request.headers.get('authorization', '')
        token = header.removeprefix('Bearer ') if header.startswith('Bearer ') else ''
        cookie = request.cookies.get('bothub_session', '')
        if not (auth.is_clean_text(token) and auth.is_clean_text(cookie)):
            error('unauthorized', 401)  # NUL в токене или cookie: в базу такая строка не уходит (asyncpg ответил бы 500)
        async with app.state.pool.acquire() as con:
            legacy = await legacy_enabled(con)
            if legacy and (token_equals(token, os.getenv('OWNER_TOKEN')) or (not token and not cookie and via_owner_proxy(request.headers))):
                row = await setup_admin(con)
                who = {'kind':'user','user_id':row['id'],'role':'admin'} if row else None
            elif legacy and token_equals(token, os.getenv('MAC_AGENT_TOKEN')):
                row = await setup_admin(con)
                who = {'kind':'mac','user_id':row['id'],'role':'member'} if row else None
            elif token.startswith('bot:') and os.getenv('BOT_TOKEN_SECRET'):
                parts = token.split(':')
                expected = hmac.new(os.environ['BOT_TOKEN_SECRET'].encode(), parts[1].encode(), hashlib.sha256).hexdigest() if len(parts) == 3 else ''
                row = await con.fetchrow("select b.owner_id from bothub.bots b join bothub.users u on u.id=b.owner_id where b.id=$1 and u.status='active'",parts[1]) if len(parts)==3 and token_equals(parts[2],expected) else None
                who = {'kind':'bot','user_id':row['owner_id'],'role':'member','bot_id':parts[1]} if row and row['owner_id'] else None
            elif token:
                row = await con.fetchrow("select m.user_id from bothub.mac_tokens m join bothub.users u on u.id=m.user_id where m.token_hash=$1 and m.revoked_at is null and u.status='active'",auth.token_hash(token))
                who = {'kind':'mac','user_id':row['user_id'],'role':'member'} if row else None
            elif cookie:
                row = await con.fetchrow("select s.user_id,s.expires_at,s.last_extended_at,u.role,u.status from bothub.sessions s join bothub.users u on u.id=s.user_id where s.id_hash=$1 and s.revoked_at is null",auth.token_hash(cookie))
                if row and row['status']=='active' and row['expires_at']>datetime.now(NOW):
                    who = {'kind':'user','user_id':row['user_id'],'role':row['role']}
                    request.state.csrf_token = auth.csrf_token(cookie,os.getenv('BOT_TOKEN_SECRET',''))
                    if request.method not in ('GET','HEAD','OPTIONS'):
                        origin = request.headers.get('origin') or request.headers.get('referer','')
                        if not auth.same_origin(origin, request.headers.get('host',''), scheme=request.headers.get('x-forwarded-proto',request.url.scheme)) or not token_equals(request.headers.get('x-csrf',''),request.state.csrf_token):
                            error('forbidden',403,'csrf')
                    if auth.should_extend_session(row['last_extended_at'],datetime.now(NOW)):
                        renewed=await con.fetchval("update bothub.sessions set expires_at=now()+interval '30 days',last_extended_at=now() where id_hash=$1 and revoked_at is null and expires_at>now() and last_extended_at<=now()-interval '1 day' returning 1",auth.token_hash(cookie))
                        if renewed: request.state.renew_session = cookie
                else:
                    who = None
            else:
                who = None
        if who is None:
            error('unauthorized', 401)
        if (owner and who['kind'] != 'user') or (not bot and who['kind']=='bot') or (not mac and who['kind']=='mac') or (admin and who['role']!='admin'):
            error('forbidden', 403)
        return who

    async def own_thread(con, thread_id, who):
        row = await con.fetchrow('select * from bothub.threads where id=$1 and owner_id=$2', thread_id,who['user_id'])
        if not row:
            error('not_found', 404)
        if who['kind']=='bot' and row['bot_id'] != who['bot_id']:
            error('forbidden', 403)
        return row

    def publish(event):
        for queue in tuple(listeners.get(event['thread_id'], ())):
            queue.put_nowait(event)

    async def append_event(con, thread_id, turn_id, kind, actor, payload, client=None, fanout=True):
        async with con.transaction():
            seq = await con.fetchval('update bothub.threads set last_seq=last_seq+1 where id=$1 returning last_seq', thread_id)
            row = await con.fetchrow('insert into bothub.events(thread_id,seq,turn_id,kind,actor,client,payload) values($1,$2,$3,$4,$5,$6,$7::jsonb) returning *', thread_id, seq, turn_id, kind, actor, client, canonical(payload))
        event = data(row)
        if fanout:
            publish(event)
        return event

    async def outbox(con, key, payload):
        if payload.get('turn_id'):
            owner_id=await con.fetchval('select th.owner_id from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1',uuid.UUID(payload['turn_id']))
        else:
            owner_id=await con.fetchval('select th.owner_id from bothub.approvals a join bothub.threads th on th.id=a.thread_id where a.id=$1',uuid.UUID(payload['approval_id']))
        await con.execute("insert into bothub.outbox(kind,dedup_key,payload,owner_id) values('push',$1,$2::jsonb,$3) on conflict(dedup_key) do nothing", key, canonical(payload),owner_id)

    async def create_turn(con, thread_id, prompt, client, fanout=True):
        row = await con.fetchrow('insert into bothub.turns(thread_id,prompt,client) values($1,$2,$3) returning *', thread_id, prompt, client)
        await append_event(con, thread_id, row['id'], 'user_msg', 'owner' if client in ('api','iphone','mac') else 'system', {'text': prompt}, client, fanout)
        return data(row)

    async def require_available_bot(con, bot_id):
        bot = await con.fetchrow('select status,provider_id,registry_bound from bothub.bots where id=$1',bot_id)
        if not bot:
            error('not_found',404)
        if bot['status']=='error_starting':
            raise HTTPException(409, {'error':'bot_computer_failed', 'detail':'bot computer failed',
                                      'recreate_url':f'/api/bots/{bot_id}/recreate'})
        if bot['status']=='no_model' or (bot['registry_bound'] and bot['provider_id'] is None):
            error('no_model',409,'bot has no available model')

    async def stop_turn(turn_id, reason=None, *, close_browser_screen=True, compact_failed=None):
        lock = stop_locks.setdefault(str(turn_id), asyncio.Lock())
        async with lock:
            return await _stop_turn(turn_id, reason, close_browser_screen=close_browser_screen, compact_failed=compact_failed)

    async def _stop_turn(turn_id, reason=None, *, close_browser_screen=True, compact_failed=None):
        """compact_failed: дополнительные поля события compact_failed, если останавливается ход сжатия."""
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select t.*, th.bot_id from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1', turn_id)
        if not row or row['status'] in ('done','stopped','error'):
            return data(row)
        if close_browser_screen:
            close_screen(row['bot_id'])
        active = running.get(str(turn_id))
        if active:
            task, runner = active
            try:
                await runner.stop(str(turn_id))
            except LauncherError as exc:
                failure = ''.join(('_' + char.lower()) if char.isupper() else char for char in type(exc).__name__).lstrip('_')
                async with app.state.pool.acquire() as con:
                    await con.execute("update bothub.bots set status='error_starting',stop_retry_exec_id=$2 where id=$1 and status<>'no_model'",row['bot_id'],str(turn_id))
                await fail_turn(turn_id, failure, 'guard', {'reason': failure, 'detail': str(exc)}, stop_runner=False)
                schedule_stop_retry(turn_id, row['bot_id'], row['thread_id'])
                async with app.state.pool.acquire() as con:
                    return data(await con.fetchrow('select * from bothub.turns where id=$1',turn_id))
            if task is not asyncio.current_task():
                task.cancel()
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.turns set status='stopped',finished_at=now(),lease_until=null where id=$1", turn_id)
            await con.execute("update bothub.bots set status='stopped' where id=$1 and status<>'no_model'", row['bot_id'])
            if reason:
                await append_event(con, row['thread_id'], turn_id, 'guard', 'system', {'reason': reason, 'detail': reason})
            if row['turn_type'] == 'compact':
                await con.execute('update bothub.threads set turns_since_compact=0 where id=$1', row['thread_id'])
                await append_event(con, row['thread_id'], turn_id, 'compact_failed', 'system', {'detail': reason or 'stopped'} | (compact_failed or {}))
            else:
                await append_event(con, row['thread_id'], turn_id, 'interrupted', 'system', {'done_steps': [], 'saved': [], 'cancelled': []})
            await append_event(con, row['thread_id'], turn_id, 'status', 'system', {'turn_id': str(turn_id), 'status': 'stopped'})
            await outbox(con, f'turn:{turn_id}', {'turn_id':str(turn_id), 'status':'stopped'})
            result = data(await con.fetchrow('select * from bothub.turns where id=$1', turn_id))
        await recreate_pending_bot(row['bot_id'])
        return result

    async def ping_turn(con, turn_id):
        # Пункт 9: «пинг» раннера - любое обращение его стороны по этому turn (событие раннера,
        # wait/usage/mac_call/approvals от бота). По нему reap_turns отличает упавший раннер.
        if turn_id:
            await con.execute("update bothub.turns set runner_ping_at=now() where id=$1 and status in ('running','waiting_approval','waiting_mac')", turn_id)

    async def retry_stop_exec(exec_id, turn_id, bot_id, thread_id):
        for attempt in range(3):
            try:
                await app.state.launcher.stop_exec(exec_id, bot_id=bot_id)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(LAUNCHER_STOP_RETRY_DELAY)
            else:
                async with app.state.pool.acquire() as con:
                    updated = await con.execute("update bothub.bots set status='stopped',stop_retry_exec_id=null where id=$1 and status='error_starting' and stop_retry_exec_id=$2",bot_id,exec_id)
                if updated.endswith(' 1'):
                    await recreate_pending_bot(bot_id)
                return
        log.error('launcher_stop_retry_failed', extra={'turn_id': str(turn_id), 'bot_id': bot_id})
        async with app.state.pool.acquire() as con:
            await con.execute("update bothub.bots set need_restart=false where id=$1 and status='error_starting' and stop_retry_exec_id=$2",bot_id,exec_id)
            await append_event(con, thread_id, turn_id, 'guard', 'system',
                               {'reason': 'launcher_stop_failed', 'detail': 'process stop retries exhausted'})

    def schedule_stop_retry(turn_id, bot_id, thread_id):
        if app.state.launcher is None:
            return
        exec_id = str(turn_id)
        key = (exec_id, str(turn_id))
        if key in stop_retries:
            return
        retry = asyncio.create_task(retry_stop_exec(exec_id, turn_id, bot_id, thread_id))
        stop_retries[key] = retry
        background.add(retry)
        retry.add_done_callback(lambda task: (background.discard(task), stop_retries.pop(key, None)))

    async def fail_turn(turn_id, reason, kind=None, payload=None, *, stop_runner=True):
        """Закрывает активный turn как неудавшийся (в схеме статус failed = 'error', CHECK
        миграции 001 не расширяем): останавливает раннер, закрывает висящие approval turn'а,
        пишет событие kind (approval_expired, budget_exceeded, guard) и status, освобождая тред
        и бота. Возвращает True, если turn был активен и закрыт."""
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select t.*, th.bot_id from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1', turn_id)
            if not row or row['status'] not in ACTIVE_STATUSES:
                return False
            active = running.get(str(turn_id))
            stop_failed = False
            if active:
                task, runner = active
                if stop_runner:
                    try:
                        await runner.stop(str(turn_id))
                    except Exception:
                        log.exception('runner_stop_failed', extra={'turn_id': str(turn_id)})
                        stop_failed = True
                if task is not asyncio.current_task():
                    task.cancel()
            changed = await con.fetchval("update bothub.turns set status='error',error=$2,finished_at=now(),lease_until=null where id=$1 and status in ('running','waiting_approval','waiting_mac') returning id", turn_id, reason)
            if not changed:
                return False
            await con.execute("update bothub.bots set status=$2,stop_retry_exec_id=$3 where id=$1 and status not in ('no_model','error_starting')", row['bot_id'], 'error_starting' if stop_failed else 'error',str(turn_id) if stop_failed else None)
            for approval in await con.fetch("update bothub.approvals set status='expired',decided_at=now() where turn_id=$1 and status='pending' returning id", turn_id):
                await append_event(con, row['thread_id'], turn_id, 'approval_dec', 'system', {'approval_id': str(approval['id']), 'decision': 'expired', 'remember': False, 'client': 'system'})
            if kind:
                await append_event(con, row['thread_id'], turn_id, kind, 'system', payload or {})
            if row['turn_type'] == 'compact':
                await con.execute('update bothub.threads set turns_since_compact=0 where id=$1', row['thread_id'])
                await append_event(con, row['thread_id'], turn_id, 'compact_failed', 'system', {'detail': str(reason)[:500]})
            await append_event(con, row['thread_id'], turn_id, 'status', 'system', {'turn_id': str(turn_id), 'status': 'error'})
            await outbox(con, f'turn:{turn_id}', {'turn_id': str(turn_id), 'status': 'error'})
            log.warning('turn_failed', extra={'turn_id': str(turn_id), 'thread_id': str(row['thread_id']), 'bot_id': row['bot_id'], 'reason': reason})
        if stop_runner:
            if stop_failed:
                schedule_stop_retry(turn_id,row['bot_id'],row['thread_id'])
            else:
                await recreate_pending_bot(row['bot_id'])
        return True

    async def budget_spent(con, bot_id):
        return await con.fetchval("select coalesce(sum(tokens_in::bigint+tokens_out::bigint+tokens_cache_read::bigint+tokens_cache_write::bigint),0) "
            "from bothub.usage where bot_id=$1 and ts>=date_trunc('day',now())",bot_id)

    async def handle_usage_event(con, bot_id, thread_id, turn_id, provider, model, tokens_in, tokens_out):
        """Пункт 12: учёт расхода по мере прихода usage. Пишет usage и сверяет дневной расход с
        бюджетом: при spent >= budget turn закрывается (событие budget_exceeded с цифрами).
        Возвращает (в пределах бюджета, строка usage)."""
        row = await con.fetchrow("insert into bothub.usage(bot_id,thread_id,turn_id,provider,model,tokens_in,tokens_out,turn_type) values($1,$2,$3,$4,$5,$6,$7,coalesce((select turn_type from bothub.turns where id=$3),'normal')) returning *", bot_id, thread_id, turn_id, provider, model, tokens_in, tokens_out)
        spent = await budget_spent(con,bot_id)
        budget = await con.fetchval('select budget_daily_tokens from bothub.bots where id=$1', bot_id)
        if spent < budget:
            return True, row
        await fail_turn(turn_id, f'budget_exceeded: потрачено {spent} из {budget} токенов за день', 'budget_exceeded',
                        {'turn_id': str(turn_id), 'spent': int(spent), 'budget': int(budget), 'tokens_in': tokens_in, 'tokens_out': tokens_out,
                         'detail': f'Дневной бюджет токенов исчерпан: потрачено {spent} из {budget}'})
        return False, row

    def thread_view(row):
        view = data(row)
        view['context'] = ctxlib.thread_context(view['context_tokens'], view['context_window'], view['context_estimated'],
                                                view['compacted_at'], view['compactions'])
        return view

    async def recent_messages(con, thread_id, exclude_turn_id, provider):
        """Последние сообщения треда (владелец и бот) из событий последних обычных ходов, без текущего хода."""
        rows = await con.fetch(
            "select turn_id,kind,payload->>'text' as text from bothub.events where thread_id=$1 and kind in ('user_msg','assistant_msg') "
            "and turn_id in (select id from bothub.turns where thread_id=$1 and id<>$2 and turn_type='normal' "
            "order by created_at desc,id desc limit $3) order by seq", thread_id, exclude_turn_id, ctxlib.PRELUDE_MESSAGES)
        return ctxlib.messages_from_events([(str(r['turn_id']), r['kind'], r['text']) for r in rows], provider)

    async def create_compact_turn(con, thread_id, client):
        """Ход сжатия (раздел 15): обычный turn с фиксированной инструкцией, без события user_msg."""
        row = await con.fetchrow("insert into bothub.turns(thread_id,prompt,client,turn_type) values($1,$2,$3,'compact') returning *",
                                 thread_id, ctxlib.COMPACT_INSTRUCTION, client)
        return data(row)

    async def refresh_context(con, thread_id, bot, payload):
        """Размер контекста треда по usage хода: цифра раннера, иначе оценка по байтам событий после сжатия."""
        tokens = payload.get('context_tokens')
        estimated = bool(payload.get('context_estimated'))
        if type(tokens) is not int or tokens <= 0:
            base = await con.fetchrow('select context_base_tokens,context_base_seq from bothub.threads where id=$1', thread_id)
            size = await con.fetchval(
                "select coalesce(sum(octet_length(coalesce(payload->>'text',payload->>'summary',payload->>'args',''))),0) "
                "from bothub.events where thread_id=$1 and seq>$2 and kind in ('user_msg','assistant_msg','tool_call','tool_result')",
                thread_id, base['context_base_seq'])
            tokens, estimated = base['context_base_tokens'] + ctxlib.estimate_tokens_from_bytes(size), True
        # окно меньше 1000 токенов (раннер, реестр моделей) считается неизвестным: берём следующий источник
        window = ctxlib.usable_window(payload.get('context_window'))
        if window is None and bot['model_id']:
            window = ctxlib.usable_window(await con.fetchval('select context_window from bothub.models where id=$1', bot['model_id']))
        window = window or ctxlib.default_window(bot['provider'], bot['model'])
        await con.execute('update bothub.threads set context_tokens=$2,context_window=$3,context_estimated=$4 where id=$1',
                          thread_id, tokens, window, estimated)

    async def schedule_auto_compact(con, thread_id):
        """После завершённого обычного хода: счёт ходов и решение об автосжатии. Сжатие встаёт в очередь
        после хода, не посреди него, и claim_turn ставит его перед следующим обычным ходом."""
        thread = await con.fetchrow('update bothub.threads set turns_since_compact=least(turns_since_compact+1,1000) where id=$1 returning *', thread_id)
        threshold = await con.fetchval('select auto_compact_percent from bothub.bots where id=$1', thread['bot_id'])
        pending = await con.fetchval("select exists(select 1 from bothub.turns where thread_id=$1 and turn_type='compact' "
                                     "and status in ('queued','running','waiting_approval','waiting_mac'))", thread_id)
        if thread['status'] == 'active' and thread['cli_session_id'] and ctxlib.should_auto_compact(
                percent=ctxlib.fill_percent(thread['context_tokens'], thread['context_window']), threshold=threshold,
                turns_since_compact=thread['turns_since_compact'], disabled=thread['auto_compact_disabled'], pending=pending):
            await create_compact_turn(con, thread_id, 'system')
            log.info('auto_compact_scheduled', extra={'thread_id': str(thread_id), 'bot_id': thread['bot_id']})

    async def complete_compact(con, thread, bot, row, parts):
        """Успех хода сжатия: сводка в threads.summary, сессия CLI сброшена, счётчики и событие compacted.
        Одна транзакция вместе со статусом done: при сбое тред не меняется. False: ход уже не running."""
        summary = ctxlib.truncate_summary(mask_browser_text(ctxlib.join_assistant(parts, bot['provider'])))
        if not summary:
            raise RuntimeError('compact turn produced no summary')
        auto = row['client'] == 'system'
        events = []
        async with con.transaction():
            if not await con.fetchval("update bothub.turns set status='done',finished_at=now(),lease_until=null where id=$1 and status='running' returning id", row['id']):
                return False
            before = await con.fetchval('select context_tokens from bothub.threads where id=$1 for update', thread['id'])
            after = ctxlib.estimate_tokens(ctxlib.build_prelude(summary, await recent_messages(con, thread['id'], row['id'], bot['provider'])))
            reduction = ctxlib.reduction_percent(before, after)
            compacted = await append_event(con, thread['id'], row['id'], 'compacted', 'system',
                {'tokens_before': before, 'tokens_after': after, 'auto': auto, 'reduction_percent': reduction,
                 'summary_chars': len(summary)}, fanout=False)
            events.append(compacted)
            futile = auto and ctxlib.auto_compact_futile(before, after)
            await con.execute(
                'update bothub.threads set summary=$2,cli_session_id=null,compactions=compactions+1,compacted_at=now(),'
                'context_tokens=$3,context_estimated=true,context_base_tokens=$3,context_base_seq=$4,turns_since_compact=0,'
                'auto_compact_disabled=auto_compact_disabled or $5 where id=$1', thread['id'], summary, after, compacted['seq'], futile)
            if futile:
                events.append(await append_event(con, thread['id'], row['id'], 'auto_compact_disabled', 'system',
                                                 {'reduction_percent': reduction}, fanout=False))
        for event in events:
            publish(event)
        log.info('thread_compacted', extra={'thread_id': str(thread['id']), 'turn_id': str(row['id']), 'auto': auto})
        return True

    async def execute_turn(row):
        turn_id = row['id']
        async with app.state.pool.acquire() as con:
            thread = await con.fetchrow('select * from bothub.threads where id=$1', row['thread_id'])
            bot = await con.fetchrow('select * from bothub.bots where id=$1', thread['bot_id'])
            spent = await budget_spent(con,bot['id'])
            if spent >= bot['budget_daily_tokens']:
                await stop_turn(turn_id, 'budget')
                return
            memories = await con.fetch("select text from bothub.memory where owner_id=$2 and status='active' and (bot_id is null or bot_id=$1) and (expires_at is null or expires_at>now())", bot['id'],bot['owner_id'])
            binding = await con.fetchrow('select kind,cli,status from bothub.providers where id=$1 and owner_id=$2',bot['provider_id'],bot['owner_id']) if bot['provider_id'] else None
            if bot['provider_id'] and (not binding or binding['status']!='ok'):
                raise RuntimeError('provider unavailable')
            compact = row['turn_type'] == 'compact'
            prompt = row['prompt']
            if not compact and thread['summary'] and not thread['cli_session_id']:
                # Раздел 15: первый ход после сжатия начинает новую сессию CLI, сводка и последние сообщения идут в промпт.
                prompt = ctxlib.build_prompt(thread['summary'], await recent_messages(con, thread['id'], turn_id, bot['provider']), prompt)
        if compact and not thread['cli_session_id']:
            await fail_turn(turn_id, 'nothing to compact')
            return
        # Находка 1: -e даёт BOTHUB_TURN_ID/BOTHUB_THREAD_ID процессу в контейнере
        # (сам marker для pkill -f заводит SubprocessRunner, см. runner/subprocess.py).
        docker_mode = os.getenv('BOTHUB_RUNNER_EXEC', 'local') == 'docker'
        exec_env={'BOTHUB_TURN_ID':str(turn_id),'BOTHUB_THREAD_ID':str(thread['id'])} if docker_mode else {}
        bot_data=data(bot)
        if docker_mode and binding and binding['kind']=='cli_subscription':
            result=await run_exec(app.state.launcher,bot['id'],['bash','-c','cp -r "$HOME/.auth/." "$HOME/"'],timeout=30)
            if result.code: raise RuntimeError('subscription credentials unavailable')
        if binding and binding['kind']!='cli_subscription':
            provider_id=str(bot['provider_id'])
            gateway_base=os.getenv('BOTHUB_INTERNAL_URL','http://core:8080').rstrip('/')+'/gateway/'+provider_id
            token=app.state.gateway.issue_token(bot['id'],provider_id,max_turn_seconds=bot['max_turn_seconds'],turn_id=str(turn_id))
            bot_data['_gateway_kind']=binding['kind']; bot_data['_gateway_url']=gateway_base
            exec_env['BOTHUB_GATEWAY_BASE_URL']=gateway_base
            if binding['kind']=='anthropic_api':
                exec_env.update({'ANTHROPIC_BASE_URL':gateway_base,'ANTHROPIC_AUTH_TOKEN':token,'ANTHROPIC_MODEL':bot['model']})
            elif binding['kind'] in ('openai_api','openai_compatible'):
                exec_env['BOTHUB_GATEWAY_TOKEN']=token
            elif binding['kind']=='google_api':
                exec_env.update({'GOOGLE_GEMINI_BASE_URL':gateway_base,'GEMINI_API_KEY':token})
        context = TurnContext(str(turn_id), str(thread['id']), data(bot), prompt, thread['cli_session_id'], thread['dry_run'], '\n'.join(m['text'] for m in memories),
                              launcher=app.state.launcher if docker_mode else None,
                              bot_container_id=bot['id'] if docker_mode else None,
                              exec_env=exec_env, turn_type=row['turn_type'])
        context.bot=bot_data
        runner = runner_factory(bot['provider'])
        task = asyncio.current_task()
        running[str(turn_id)] = (task, runner)
        failures = []
        summary_parts = []
        started = asyncio.get_running_loop().time()
        async def renew():
            # Находка 3: продлеваем lease и пока turn ждёт approval/mac, иначе
            # claim_turn() посчитает его брошенным (lease_until истёк) и второй
            # execute_turn стартует поверх ещё живого.
            while True:
                await asyncio.sleep(30)
                try:
                    async with app.state.pool.acquire() as con:
                        await con.execute("update bothub.turns set lease_until=now()+interval '60 seconds' where id=$1 and status in ('running','waiting_approval','waiting_mac')", turn_id)
                except Exception:
                    # сбой БД не должен молча убить продление lease (turn перезапустится поверх живого)
                    log.exception('lease_renew_failed', extra={'turn_id': str(turn_id)})
        lease_task = asyncio.create_task(renew())
        masker = BrowserEventMasker()

        def repeated_error(event):
            """Три одинаковые ошибки инструмента подряд: ход останавливается (и обычный, и сжатия)."""
            nonlocal failures
            if event.kind != 'tool_result':
                return False
            signature = canonical({k:v for k,v in event.payload.items() if k!='call_id'}) if not event.payload.get('ok') else None
            failures = (failures + [signature])[-3:] if signature else []
            return len(failures) == 3 and len(set(failures)) == 1

        try:
            async with asyncio.timeout(bot['max_turn_seconds']):
                async for event in runner.run(context):
                    if compact and event.kind == 'tool_call':
                        # Раздел 15: ход сжатия только пишет сводку. Любой вызов инструмента (раннер мог не получить запрета:
                        # у codex и agy его нет) немедленно останавливает ход; имя инструмента в событии, аргументов нет.
                        await stop_turn(turn_id, 'tool_call_refused', compact_failed={
                            'reason': 'tool_call_refused', 'tool': refused_tool_name(event.payload.get('tool'))})
                        return
                    if event.kind in ('tool_call', 'tool_result'):
                        event.payload = masker.event(event.kind, event.payload)
                    async with app.state.pool.acquire() as con:
                        await ping_turn(con, turn_id)
                        if compact:
                            # Раздел 15: ход сжатия невидим. Текст ответа это сводка; событий в ленту нет, сессия CLI не
                            # меняется, расход пишется с типом compact. tool_result в ленту не пишется, но считается ниже
                            # в repeat_error.
                            if event.kind == 'assistant_msg':
                                # agy кладёт в final=true только текст ошибки, сводка приходит дельтами
                                if not (event.payload.get('final') and bot['provider'] == 'gemini'):
                                    summary_parts.append(event.payload.get('text') or '')
                            elif event.kind == 'usage' and (not bot['provider_id'] or (binding and binding['kind']=='cli_subscription')):
                                within_budget, _ = await handle_usage_event(con, bot['id'], thread['id'], turn_id, bot['provider'], event.payload.get('model') or bot['model'], event.payload.get('tokens_in', 0), event.payload.get('tokens_out', 0))
                                if not within_budget:
                                    return
                            if repeated_error(event):
                                await stop_turn(turn_id, 'repeat_error')
                                return
                            continue
                        if event.cli_session_id:
                            await con.execute('update bothub.threads set cli_session_id=$2 where id=$1', thread['id'], event.cli_session_id)
                        if event.kind == 'usage':
                            usage = {k: v for k, v in event.payload.items() if not k.startswith('context_')}
                            model = usage.get('model') or bot['model']
                            await append_event(con, thread['id'], turn_id, 'usage', 'system', {**usage, 'model': model, 'seconds': usage.get('seconds') or round(asyncio.get_running_loop().time()-started, 2)})
                            await refresh_context(con, thread['id'], bot, event.payload)
                            # Пункт 12: бюджет проверяется на каждом usage, не только на старте turn.
                            within_budget = True
                            if not bot['provider_id'] or (binding and binding['kind']=='cli_subscription'):
                                within_budget, _ = await handle_usage_event(con, bot['id'], thread['id'], turn_id, bot['provider'], model, usage.get('tokens_in', 0), usage.get('tokens_out', 0))
                            if not within_budget:
                                return
                        else:
                            await append_event(con, thread['id'], turn_id, event.kind, f"bot:{bot['id']}", event.payload, row['client'])
                        if event.kind == 'plan':
                            await con.execute('update bothub.turns set steps=$2 where id=$1', turn_id, len(event.payload.get('steps', [])))
                    if repeated_error(event):
                        await stop_turn(turn_id, 'repeat_error')
                        return
            async with app.state.pool.acquire() as con:
                if compact:
                    changed = await complete_compact(con, thread, bot, row, summary_parts)
                else:
                    changed = await con.fetchval("update bothub.turns set status='done',finished_at=now(),lease_until=null where id=$1 and status='running' returning id", turn_id)
                if changed:
                    await con.execute("update bothub.bots set status='idle' where id=$1 and status<>'no_model'", bot['id'])
                    await append_event(con, thread['id'], turn_id, 'status', 'system', {'turn_id': str(turn_id), 'status':'done'})
                    if not compact:
                        await outbox(con, f'turn:{turn_id}', {'turn_id':str(turn_id), 'status':'done'})
                        try:
                            await schedule_auto_compact(con, thread['id'])
                        except Exception:
                            log.exception('auto_compact_failed', extra={'turn_id': str(turn_id)})
                    log.info('turn_done', extra={'turn_id': str(turn_id), 'bot_id': bot['id']})
                elif await con.fetchval('select status from bothub.turns where id=$1', turn_id) in ('waiting_approval', 'waiting_mac'):
                    # Пункт 9: раннер завершился, пока turn ждал approval/mac: ответа уже некому
                    # ждать, а тред остался бы заблокированным.
                    await fail_turn(turn_id, 'раннер завершился во время ожидания', 'guard', {'reason': 'runner_exited', 'detail': 'раннер завершился, пока turn ждал approval или Mac'})
        except TimeoutError:
            await stop_turn(turn_id, 'timeout')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception('turn_error', extra={'turn_id': str(turn_id), 'bot_id': bot['id']})
            # Пункт 9: закрываем turn и в waiting_approval/waiting_mac (раньше только в running).
            if known := launcher_failure_detail(exc):
                await fail_turn(turn_id, known[0], 'guard', {'reason': known[0], 'detail': known[1]})
            else:
                await fail_turn(turn_id, str(exc))
        finally:
            lease_task.cancel()
            running.pop(str(turn_id), None)

    async def run_turn(row):
        # Исключение вне try в execute_turn (например, упала БД на старте) не должно оставлять
        # turn в running до истечения lease: логируем и закрываем.
        try:
            await execute_turn(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('turn_crashed', extra={'turn_id': str(row['id'])})
            with suppress(Exception):
                await fail_turn(row['id'], 'внутренняя ошибка ядра')
        finally:
            async with app.state.pool.acquire() as con:
                bot_id = await con.fetchval('select bot_id from bothub.threads where id=$1',row['thread_id'])
            if bot_id:
                await recreate_pending_bot(bot_id)

    async def reap_turns():
        """Пункты 9 и 10, вызывается из claim_turn перед захватом.
        1) approval turn'а истёк (status expired, expires_at прошёл или старше APPROVAL_TIMEOUT
        минут, по умолчанию 60): turn завершается с событием approval_expired и не
        перезапускается через claim_turn.
        2) turn ждёт approval/Mac, а раннер не пинговал BOTHUB_RUNNER_TIMEOUT минут (по
        умолчанию 5): раннер упал, turn закрывается, тред разблокируется."""
        approval_timeout = env_float('APPROVAL_TIMEOUT', 60)
        runner_timeout = env_float('BOTHUB_RUNNER_TIMEOUT', 5)
        async with app.state.pool.acquire() as con:
            expired = await con.fetch(
                "select distinct on (t.id) t.id, a.id as approval_id, a.tool from bothub.turns t join bothub.approvals a on a.turn_id=t.id "
                "where t.status in ('running','waiting_approval','waiting_mac') and (a.status='expired' or "
                "(a.status='pending' and (a.expires_at<=now() or a.created_at < now() - $1::float8 * interval '1 minute'))) "
                "order by t.id, a.created_at", approval_timeout)
            stale = await con.fetch(
                "select id from bothub.turns where status in ('waiting_approval','waiting_mac') "
                "and coalesce(runner_ping_at, started_at, created_at) < now() - $1::float8 * interval '1 minute'", runner_timeout)
        for row in expired:
            await fail_turn(row['id'], 'approval_expired', 'approval_expired', {'turn_id': str(row['id']), 'approval_id': str(row['approval_id']), 'tool': row['tool'], 'detail': 'approval истёк, turn завершён'})
        for row in stale:
            await fail_turn(row['id'], 'раннер не отвечает', 'guard', {'reason': 'runner_lost', 'detail': f'раннер не пинговал {runner_timeout:g} мин'})

    async def slot_available(con) -> bool:
        """Пункт 11: лимит параллельных turn на ядро. Одна проверка для claim_turn и для возврата
        turn из waiting_approval/waiting_mac в running (resume_turn)."""
        return await con.fetchval("select count(*) from bothub.turns where status='running'") < int(env_float('BOTHUB_MAX_PARALLEL_TURNS', 4))

    async def resume_turn(con, turn_id, from_status) -> bool:
        """waiting_approval/waiting_mac -> running под тем же лимитом и той же блокировкой, что claim_turn.
        True: turn больше не ждёт (вернулся в running или уже не в from_status). False: слота нет,
        turn остаётся в from_status, а ответ раннеру (approval в /wait, вызов mac_call) придерживается,
        пока слот не освободится. Статус не меняем на queued: раннер turn'а жив, и claim_turn
        запустил бы второй execute_turn поверх него."""
        async with con.transaction():
            await con.execute("select pg_advisory_xact_lock(hashtext('bothub-claim-turn'))")
            if not await slot_available(con):
                return not await con.fetchval('select 1 from bothub.turns where id=$1 and status=$2', turn_id, from_status)
            row = await con.fetchrow("update bothub.turns set status='running',lease_until=now()+interval '60 seconds' where id=$1 and status=$2 returning thread_id", turn_id, from_status)
            if row:
                # Находка 3: свежий lease_until при возврате в running - иначе claim_turn()
                # считает turn брошенным и второй execute_turn стартует поверх ещё живого раннера.
                await con.execute("update bothub.bots set status='running' where id=(select bot_id from bothub.threads where id=$1) and status<>'no_model'", row['thread_id'])
        if row:
            await append_event(con, row['thread_id'], turn_id, 'status', 'system', {'turn_id': str(turn_id), 'status': 'running'})
        return True

    async def claim_turn():
        await reap_turns()
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                # Ревью Gemini: без общей блокировки два воркера могли взять turn'ы одного треда
                # до коммита статуса. ponytail: одна глобальная блокировка на захват, дёшево при одном ядре.
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub-claim-turn'))")
                # Пункт 11: лимит параллельных turn на ядро. Новые (queued) ждут свободного слота,
                # перехват брошенного running слот не занимает (turn уже посчитан).
                slot_free = await slot_available(con)
                # Находка 7 + пункт 11: не больше одного активного turn на бота (это включает и
                # тред: тред принадлежит одному боту). Занятый бот не блокирует очередь: берём
                # следующий turn другого бота, а turn занятого остаётся queued.
                row = await con.fetchrow(
                    "select t.* from bothub.turns t join bothub.threads th on th.id=t.thread_id join bothub.users u on u.id=th.owner_id join bothub.bots b on b.id=th.bot_id "
                    "where ((t.status='queued' and $1 and not b.paused) or (t.status='running' and t.lease_until<now())) "
                    "and u.status='active' and b.status not in ('starting','error_starting','no_model') "
                    "and (b.provider_id is not null or b.registry_bound=false) "
                    "and (not b.need_restart or t.status='running') "
                    "and not exists (select 1 from bothub.approvals a where a.turn_id=t.id and "
                    "(a.status='expired' or (a.status='pending' and "
                    "(a.expires_at<=now() or a.created_at<now()-$2::float8*interval '1 minute')))) "
                    "and not exists (select 1 from bothub.turns o join bothub.threads oth on oth.id=o.thread_id "
                    "where oth.bot_id=th.bot_id and o.id<>t.id and o.status in ('running','waiting_approval','waiting_mac')) "
                    # Процедура и turn не делят браузер бота: пока у бота есть неконечный запуск процедуры, turn ждёт (queued)
                    "and not exists (select 1 from bothub.procedure_runs pr where pr.bot_id=th.bot_id and pr.status in "
                    "('queued','running','waiting_approval','waiting_model','waiting_human')) "
                    "order by (t.turn_type='compact') desc,t.created_at for update of t skip locked limit 1", slot_free, env_float('APPROVAL_TIMEOUT', 60))
                if row:
                    await con.execute("update bothub.turns set status='running',started_at=coalesce(started_at,now()),lease_until=now()+interval '60 seconds',runner_ping_at=now() where id=$1", row['id'])
                    await con.execute('update bothub.bots set status=$2 where id=(select bot_id from bothub.threads where id=$1)', row['thread_id'], 'running')
            if row:
                await append_event(con, row['thread_id'], row['id'], 'status', 'system', {'turn_id':str(row['id']), 'status':'running'})
                log.info('turn_claimed', extra={'turn_id': str(row['id']), 'thread_id': str(row['thread_id'])})
            return row

    async def worker_step():
        row = await app.state.claim_turn()
        if not row:
            return False
        task = asyncio.create_task(run_turn(row))
        background.add(task)  # сильная ссылка: иначе задачу может собрать GC
        task.add_done_callback(background.discard)
        return True

    async def worker():
        await supervise('worker', worker_step, env_float('BOTHUB_WORKER_INTERVAL', .2), env_float('BOTHUB_LOOP_ERROR_DELAY', 1))

    def next_run(cron, tz, now=None):
        local = (now or datetime.now(NOW)).astimezone(ZoneInfo(tz))
        return croniter(cron, local).get_next(datetime).astimezone(NOW)

    async def run_schedule(con, schedule, fanout=True):
        await require_available_bot(con,schedule['bot_id'])
        if schedule['last_turn_id']:
            thread_id = await con.fetchval("select th.id from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and th.status='active' and th.owner_id=$2 and th.bot_id=$3", schedule['last_turn_id'],schedule['owner_id'],schedule['bot_id'])
        else:
            thread_id = None
        if not thread_id:
            thread_id = await con.fetchval("insert into bothub.threads(bot_id,kind,title,owner_id) values($1,'routine',$2,$3) returning id", schedule['bot_id'], schedule['name'],schedule['owner_id'])
        turn = await create_turn(con, thread_id, schedule['prompt'], 'schedule', fanout=fanout)
        await con.execute('update bothub.schedules set last_turn_id=$2 where id=$1', schedule['id'], uuid.UUID(turn['id']))
        return turn

    # --- Пауза триггеров (раздел 16): расписание и hook не создают turn, пока исполнитель бота недоступен.
    async def log_activity(con, owner_id, bot_id, kind, code, params, thread_id=None):
        await con.execute('insert into bothub.activity_log(owner_id,bot_id,thread_id,kind,code,params) values($1,$2,$3,$4,$5,$6::jsonb)',
                          owner_id, bot_id, thread_id, kind, code, canonical(params))

    async def mac_online(con, owner_id) -> bool:
        if str(owner_id) not in mac_socket:
            return False
        row = await con.fetchrow('select state,last_seen from bothub.mac_status where owner_id=$1 order by id limit 1', owner_id)
        return bool(row and row['state'] == 'online' and row['last_seen'] and row['last_seen'] >= datetime.now(NOW) - timedelta(seconds=90))

    async def trigger_block(con, bot_id):
        """Причина, по которой триггер бота сейчас не должен создавать turn (activity.SKIP_REASONS), или None.
        Дешёвые причины (пауза, провайдер) проверяются раньше, чем Mac и лаунчер: до них доходит только годный бот."""
        bot = await con.fetchrow('select * from bothub.bots where id=$1', bot_id)
        if not bot:
            return None  # дальше run_schedule/hook ответят своей ошибкой
        provider_status = await con.fetchval('select status from bothub.providers where id=$1 and owner_id=$2', bot['provider_id'], bot['owner_id']) if bot['provider_id'] else None
        reason = activity.skip_reason(bot, provider_status=provider_status, container_running=True, mac_online=True)
        if reason:
            return reason
        if bot['executor'] == 'mac':
            return activity.skip_reason(bot, provider_status=provider_status, mac_online=await mac_online(con, bot['owner_id']))
        if os.getenv('BOTHUB_RUNNER_EXEC', 'local') == 'docker' and app.state.launcher is not None:
            running_now = False
            if app.state.launcher_ready:
                try:
                    running_now = bool((await app.state.launcher.status(bot_id)).running)
                except (LauncherError, OSError, asyncio.TimeoutError):
                    running_now = False
            return activity.skip_reason(bot, provider_status=provider_status, container_running=running_now)
        return None

    app.state.trigger_block = trigger_block

    async def schedule_thread(con, schedule):
        if not schedule['last_turn_id']:
            return None
        return await con.fetchval("select th.id from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and th.status='active' and th.owner_id=$2 and th.bot_id=$3", schedule['last_turn_id'], schedule['owner_id'], schedule['bot_id'])

    async def record_skip(con, schedule, reason, published):
        """Пропущенный запуск: счётчик и причина в расписании; событие в ленту и в тред расписания не чаще раза в час.
        Расписание, где пропусков подряд стало 5, помечается paused_by_unavailable. Новые события тредов кладёт в published."""
        now = datetime.now(NOW)
        plan = activity.plan_skip(schedule['skipped_count'], schedule['paused_by_unavailable'], schedule['last_skip_event_at'], now)
        await con.execute('update bothub.schedules set skipped_count=$2,last_skipped_at=$3,last_skip_reason=$4,paused_by_unavailable=$5,'
                          'last_skip_event_at=case when $6 then $3 else last_skip_event_at end where id=$1',
                          schedule['id'], plan.count, now, reason, plan.paused, plan.write_event)
        if plan.write_event:
            thread_id = await schedule_thread(con, schedule)
            await log_activity(con, schedule['owner_id'], schedule['bot_id'], 'schedule', 'schedule_skipped',
                               {'reason': reason, 'count': plan.count, 'paused': plan.paused, 'schedule_id': str(schedule['id']), 'name': schedule['name']}, thread_id)
            if thread_id:
                published.append(await append_event(con, thread_id, None, 'system', 'system',
                                                    {'text': activity.skip_text(reason, plan.count, plan.paused), 'code': 'schedule_skipped', 'reason': reason, 'count': plan.count}, fanout=False))
        return plan

    async def record_resume(con, schedule, published):
        await con.execute('update bothub.schedules set skipped_count=0,paused_by_unavailable=false where id=$1', schedule['id'])
        thread_id = await schedule_thread(con, schedule)
        await log_activity(con, schedule['owner_id'], schedule['bot_id'], 'schedule', 'schedule_resumed',
                           {'schedule_id': str(schedule['id']), 'name': schedule['name'], 'count': schedule['skipped_count']}, thread_id)
        if thread_id:
            published.append(await append_event(con, thread_id, None, 'system', 'system', {'text': activity.RESUME_TEXT, 'code': 'schedule_resumed'}, fanout=False))

    async def run_due_schedules():
        async with app.state.pool.acquire() as con:
            # Доступность исполнителей считаем до блокировок строк: лаунчер и Mac могут отвечать долго.
            blocked = {}
            for row in await con.fetch("select distinct s.bot_id from bothub.schedules s join bothub.users u on u.id=s.owner_id where s.enabled and s.kind='cron' and s.next_run_at<=now() and u.status='active'"):
                try:
                    blocked[row['bot_id']] = await trigger_block(con, row['bot_id'])
                except Exception:
                    # Сбой проверки не должен зацикливать расписание: без записи в blocked его next_run_at оставался в прошлом и каждые
                    # 30 секунд проход снова бросал исключение. Срабатывание считается пропуском check_failed (раздел 16), расписание
                    # сдвигается на следующий срок.
                    log.exception('trigger_block_failed', extra={'bot_id': row['bot_id']})
                    blocked[row['bot_id']] = 'check_failed'
            async with con.transaction():
                # Бот с неконечным запуском процедуры (в том числе ждущим человека до BOTHUB_PROCEDURE_WAIT_HOURS) расписание откладывает:
                # next_run_at остаётся в прошлом, расписание срабатывает одним turn'ом на первом проходе после освобождения бота.
                # Без этого за часы ожидания очередь копила бы по turn'у на каждый срок cron (claim_turn их всё равно не берёт).
                due = await con.fetch("select s.* from bothub.schedules s join bothub.users u on u.id=s.owner_id join bothub.bots b on b.id=s.bot_id where s.enabled and s.kind='cron' and s.next_run_at<=now() and u.status='active' and not exists (select 1 from bothub.procedure_runs pr where pr.bot_id=s.bot_id and pr.status in ('queued','running','waiting_approval','waiting_model','waiting_human')) for update of s skip locked")
                turns = []
                published = []
                for schedule in due:
                    if schedule['bot_id'] not in blocked:
                        continue  # бот появился после сверки: следующий проход
                    turn, events = None, []
                    try:
                        # savepoint: битое расписание (например, плохой cron) не откатывает остальные
                        async with con.transaction():
                            reason = blocked[schedule['bot_id']]
                            next_at = next_run(schedule['cron'], schedule['timezone'])
                            if reason:
                                plan = await record_skip(con, schedule, reason, events)
                                if plan.probe_at:
                                    next_at = max(next_at, plan.probe_at)  # в паузе исполнителя проверяем не чаще раза в 15 минут
                            else:
                                resume = activity.plan_resume(schedule['skipped_count'], schedule['paused_by_unavailable'], schedule['catch_up'])
                                if resume.resumed:
                                    await record_resume(con, schedule, events)
                                if resume.run_now:
                                    turn = await run_schedule(con, schedule, fanout=False)
                            await con.execute('update bothub.schedules set next_run_at=$2 where id=$1', schedule['id'], next_at)
                    except Exception:
                        log.exception('schedule_failed', extra={'schedule_id': str(schedule['id']), 'bot_id': schedule['bot_id']})
                        continue
                    published.extend(events)
                    if turn:
                        turns.append(turn)
            for turn in turns:
                event = await con.fetchrow("select * from bothub.events where turn_id=$1 and kind='user_msg' order by seq desc limit 1",uuid.UUID(turn['id']))
                publish(data(event))
            for event in published:
                publish(event)

    app.state.run_due_schedules = run_due_schedules

    async def scheduler():
        await supervise('scheduler', lambda: app.state.run_due_schedules(), env_float('BOTHUB_SCHEDULER_INTERVAL', 30), env_float('BOTHUB_LOOP_ERROR_DELAY', 30))

    async def expire_approvals():
        async with app.state.pool.acquire() as con:
            rows = await con.fetch("update bothub.approvals set status='expired',decided_at=now() where status='pending' and expires_at<=now() returning *")
            for row in rows:
                await append_event(con, row['thread_id'], row['turn_id'], 'approval_dec', 'system', {'approval_id':str(row['id']), 'decision':'expired', 'remember':False, 'client':'system'})
        for row in rows:
            await fail_for_expired_approval(row)

    async def fail_for_expired_approval(row):
        # Пункт 10: истёкший approval не возвращает turn в running (иначе после истечения lease
        # claim_turn запускал его заново): turn закрывается событием approval_expired.
        if row['turn_id']:
            await fail_turn(row['turn_id'], 'approval_expired', 'approval_expired', {'turn_id': str(row['turn_id']), 'approval_id': str(row['id']), 'tool': row['tool'], 'detail': 'approval истёк, turn завершён'})

    async def recover_stale_turns():
        # Находка 6: рестарт ядра посреди turn'а - лишённые обновлений lease turn'ы
        # (running/waiting_*) помечаем ошибкой, а не даём claim_turn() запустить их
        # заново поверх (возможно) ещё живого процесса в контейнере бота.
        async with app.state.pool.acquire() as con:
            stale = await con.fetch(
                "select t.id,t.thread_id,th.bot_id from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                "where t.status in ('running','waiting_approval','waiting_mac') "
                "and (t.lease_until is null or t.lease_until<now())")
            for row in stale:
                # Процесс останавливаем до записи в БД: если лаунчер недоступен, turn остаётся stale и повторный вызов
                # (фоновая сверка при старте повторяется) остановит его снова, а не пропустит как уже закрытый.
                if os.getenv('BOTHUB_RUNNER_EXEC', 'local') == 'docker':
                    try:
                        await app.state.launcher.stop_exec(str(row['id']), bot_id=row['bot_id'])
                    except LauncherNotFound:
                        pass  # контейнера или процесса уже нет
                    except (LauncherUnavailable, LauncherTimeout):
                        raise
                    except LauncherError:
                        log.warning('stale_turn_stop_failed', extra={'turn_id': str(row['id']), 'bot_id': row['bot_id']})
                # Три записи одного turn одной транзакцией: отмена посередине не оставит turn закрытым без события
                # или бота без статуса. Событие уходит подписчикам после коммита.
                async with con.transaction():
                    await con.execute("update bothub.turns set status='error',error=$2,finished_at=now(),lease_until=null where id=$1", row['id'], 'прервано рестартом ядра')
                    await con.execute("update bothub.bots set status='error' where id=$1 and status<>'no_model'", row['bot_id'])
                    event = await append_event(con, row['thread_id'], row['id'], 'status', 'system', {'turn_id': str(row['id']), 'status': 'error'}, fanout=False)
                publish(event)
        for bot_id in {row['bot_id'] for row in stale}:
            await recreate_pending_bot(bot_id)

    async def send_push(sub, body):
        from pywebpush import webpush
        await asyncio.to_thread(webpush, {'endpoint': sub['endpoint'], 'keys': sub['keys']}, body, vapid_private_key=os.environ['VAPID_PRIVATE'], vapid_claims={'sub': os.environ['VAPID_SUBJECT']}, timeout=10)

    async def deliver_row(row):
        """Пункт 6: доставка записи outbox по каждой подписке отдельно (outbox_deliveries).
        Запись о попытке (status='sending') создаётся ДО сетевой отправки: сбой БД после отправки не
        вызывает повтора, а endpoint в 'sending' (оборванная или чужая попытка) повторно не шлём.
        Сбой одной подписки не повторяет отправку остальным, 404/410 удаляют подписку. Соединение из
        пула берётся на один запрос, на время сети оно свободно; от параллельного прохода запись
        защищает аренда next_attempt_at (см. deliver_outbox)."""
        if not all(os.getenv(k) for k in ('VAPID_PUBLIC', 'VAPID_PRIVATE', 'VAPID_SUBJECT')):
            async with app.state.pool.acquire() as con:
                await con.execute('update bothub.outbox set sent_at=now() where id=$1', row['id'])
            return
        async with app.state.pool.acquire() as con:
            subs = await con.fetch('select * from bothub.push_subscriptions where owner_id=(select owner_id from bothub.outbox where id=$1) order by created_at, endpoint',row['id'])
        body = canonical(row['payload'])
        failed = False
        for sub in subs:
            async with app.state.pool.acquire() as con:
                # брать на отправку можно новый endpoint и 'failed'; sent, gone и sending пропускаем
                claimed = await con.fetchval(
                    "insert into bothub.outbox_deliveries(outbox_id,endpoint,status) values($1,$2,'sending') "
                    "on conflict(outbox_id,endpoint) do update set status='sending',updated_at=now() "
                    "where bothub.outbox_deliveries.status='failed' returning 1", row['id'], sub['endpoint'])
            if not claimed:
                continue
            try:
                await app.state.send_push(sub, body)
                status, error_text = 'sent', None
            except Exception as exc:
                code = push_status(exc)
                if code in (404, 410):
                    status, error_text = 'gone', f'HTTP {code}'
                else:
                    status, error_text, failed = 'failed', (f'HTTP {code}: {exc}' if code else str(exc))[:300], True
                    log.warning('push_failed', extra={'outbox_id': row['id'], 'endpoint_host': sub['endpoint'].split('/')[2] if sub['endpoint'].count('/') >= 2 else '', 'status_code': code})
            async with app.state.pool.acquire() as con:
                if status == 'gone':
                    await con.execute('delete from bothub.push_subscriptions where endpoint=$1', sub['endpoint'])
                await con.execute(
                    "update bothub.outbox_deliveries set status=$3,attempts=attempts+$4,last_error=$5,updated_at=now() where outbox_id=$1 and endpoint=$2",
                    row['id'], sub['endpoint'], status, 1 if status == 'failed' else 0, error_text)
        async with app.state.pool.acquire() as con:
            if not failed:
                await con.execute('update bothub.outbox set sent_at=now() where id=$1', row['id'])
                return
            attempts = row['attempts'] + 1
            if attempts >= int(env_float('BOTHUB_OUTBOX_MAX_ATTEMPTS', 10)):
                log.error('outbox_gave_up', extra={'outbox_id': row['id'], 'attempts': attempts})
                await con.execute('update bothub.outbox set attempts=$2,failed_at=now() where id=$1', row['id'], attempts)
            else:
                await con.execute("update bothub.outbox set attempts=$2,next_attempt_at=now()+$3::float8 * interval '1 second' where id=$1", row['id'], attempts, float(outbox_backoff(attempts)))

    async def deliver_outbox():
        await expire_approvals()
        async with app.state.pool.acquire() as con:
            ids = await con.fetch('select id from bothub.outbox where sent_at is null and failed_at is null and next_attempt_at<=now() order by id limit 20')
        lease = env_float('BOTHUB_OUTBOX_LEASE', 120)
        for item in ids:
            try:
                async with app.state.pool.acquire() as con:
                    # Аренда записи одним UPDATE: параллельный проход её не получит, пока не истечёт
                    # lease или запись не обработана. Блокировку строки на время сети не держим.
                    row = await con.fetchrow(
                        "update bothub.outbox set next_attempt_at=now()+$2::float8 * interval '1 second' "
                        "where id=$1 and sent_at is null and failed_at is null and next_attempt_at<=now() returning *", item['id'], lease)
                if row:
                    await deliver_row(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('outbox_row_failed', extra={'outbox_id': item['id']})
                with suppress(Exception):
                    async with app.state.pool.acquire() as con:
                        await con.execute("update bothub.outbox set attempts=attempts+1,next_attempt_at=now()+interval '60 seconds' where id=$1", item['id'])

    async def outbox_sender():
        await supervise('outbox_sender', lambda: app.state.deliver_outbox(), env_float('BOTHUB_OUTBOX_INTERVAL', 5))

    app.state.worker = worker
    app.state.scheduler = scheduler
    app.state.outbox_sender = outbox_sender
    app.state.deliver_outbox = deliver_outbox
    app.state.send_push = send_push
    app.state.stop_turn = stop_turn
    app.state.run_turn = run_turn
    app.state.running = running
    app.state.fail_turn = fail_turn
    app.state.reap_turns = reap_turns
    app.state.claim_turn = claim_turn
    app.state.resume_turn = resume_turn
    app.state.recover_stale_turns = recover_stale_turns

    async def reconcile_launcher_state():
        """Часть старта, которой нужен лаунчер: все контейнерные боты вне human размораживаются (лаунчер хранит метки
        заморозки между перезапусками, а у экрана после перезапуска ядра никто не сидит). Идемпотентна. Недоступный
        лаунчер (LauncherUnavailable, LauncherTimeout) поднимается наружу: сверку повторят."""
        if os.getenv('BOTHUB_RUNNER_EXEC', 'local') == 'docker':
            await app.state.launcher.list_bots()
        async with app.state.pool.acquire() as con:
            container_rows = await con.fetch("select id from bothub.bots where executor='container'")
        for container_row in container_rows:
            bot_id = container_row['id']
            try:
                async with app.state.pool.acquire() as con:
                    async with con.transaction():
                        state = await con.fetchval('select browser_control from bothub.bots where id=$1 for update',
                                                   bot_id)
                        if state != 'human':
                            # A bot under a human keeps the clean Chromium; every other bot gets its own Chromium
                            # (CDP) back first. The thaw does not depend on how that went: a stopped container
                            # cannot switch modes, yet it must lose the freeze marker, or its recreated container
                            # refuses every exec. The launcher itself refuses to thaw a bot whose container still
                            # shows the human's browser, so the thaw cannot reach a human.
                            try:
                                await app.state.launcher.browser_mode(bot_id,'bot')
                            except LauncherNotFound:
                                pass  # no container: the thaw below answers the same
                            except (LauncherUnavailable, LauncherTimeout):
                                raise
                            except Exception as exc:
                                log.warning('browser_startup_mode_failed',
                                            extra={'bot_id': bot_id, 'error': f'{type(exc).__name__}: {exc}'[:300]})
                            await app.state.launcher.unfreeze_bot(bot_id)
            except LauncherNotFound:
                pass  # no container yet: nothing to unfreeze
            except (LauncherUnavailable, LauncherTimeout):
                raise
            except Exception as exc:
                # The core must start. The bot stays frozen and its next exec fails with a clear reason
                # (launcher_failure_detail); the owner can recreate the bot.
                log.warning('browser_startup_unfreeze_failed',
                            extra={'bot_id': bot_id, 'error': f'{type(exc).__name__}: {exc}'[:300]})

    bot_starts: set[str] = set()  # боты, чей запуск идёт сейчас: второй запуск того же бота параллельно не стартует

    async def start_new_bot(bot_id: str, *, delay: float = 0.0):
        """Запуск нового бота после ответа на POST /api/bots: сеть пользователя, контейнер и браузер у лаунчера, затем статус
        starting -> idle или error_starting. Статус пишется только из starting: удаление или пересоздание за это время не
        затирается. Не бросает: итог в статусе бота и в журнале (bot_started, bot_start_failed). Нет лаунчера: starting
        остаётся в базе, его доведёт стартовая сверка (recover_after_start)."""
        if bot_id in bot_starts:
            return
        bot_starts.add(bot_id)
        try:
            if delay:
                await asyncio.sleep(delay)
            if app.state.launcher is None or not app.state.launcher_ready:
                return
            async with app.state.pool.acquire() as con:
                bot = await con.fetchrow("select id,owner_id from bothub.bots where id=$1 and status='starting'", bot_id)
            if not bot:
                return
            status = 'idle'
            try:
                await app.state.launcher.create_bot(bot_id, str(bot['owner_id']))
            except Exception as exc:
                status = 'error_starting'
                log.warning('bot_start_failed', extra={'bot_id': bot_id, 'error': f'{type(exc).__name__}: {exc}'[:300]})
            exists = True
            for attempt in range(3):  # сбой базы не должен оставить бота в starting до рестарта ядра
                try:
                    async with app.state.pool.acquire() as con:
                        updated = await con.execute("update bothub.bots set status=$2 where id=$1 and status='starting'", bot_id, status)
                        exists = updated == 'UPDATE 1' or bool(await con.fetchval('select 1 from bothub.bots where id=$1', bot_id))
                    break
                except Exception as exc:
                    log.warning('bot_start_status_write_failed',
                                extra={'bot_id': bot_id, 'attempt': attempt + 1, 'error': f'{type(exc).__name__}: {exc}'[:300]})
                    if attempt == 2:
                        return
                    await asyncio.sleep(1.0)
            if not exists:
                if status == 'idle':  # бота удалили, пока лаунчер создавал контейнер: контейнер без записи не оставляем
                    with suppress(Exception):
                        await app.state.launcher.remove_bot(bot_id)
            elif status == 'idle':
                log.info('bot_started', extra={'bot_id': bot_id})
        finally:
            bot_starts.discard(bot_id)

    app.state.start_new_bot = start_new_bot

    def spawn_bot_start(bot_id: str):
        task = asyncio.create_task(start_new_bot(bot_id, delay=BOT_START_DELAY))
        background.add(task)  # сильная ссылка: иначе задачу может собрать GC
        task.add_done_callback(background.discard)

    async def recover_after_start():
        """Остальной старт, который трогает контейнеры: stale turn'ы, боты с need_restart и боты в starting (ядро остановилось
        до конца их запуска). Идемпотентна."""
        if not app.state.has_users:
            return
        await recover_stale_turns()
        async with app.state.pool.acquire() as con:
            pending = await con.fetch('select id from bothub.bots where need_restart')
            starting = await con.fetch("select id from bothub.bots where status='starting'")
        for bot in pending:
            await recreate_pending_bot(bot['id'])
        for bot in starting:
            await start_new_bot(bot['id'])

    started_workers: dict[str, asyncio.Task] = {}
    recovered = {'done': True}

    def start_workers():
        """worker, scheduler и outbox_sender, каждый один раз. Worker гоняет turn'ы в контейнерах, поэтому при
        настроенном лаунчере он ждёт конца стартовой сверки (stale turn'ы к этому времени закрыты); scheduler и
        outbox_sender от лаунчера не зависят."""
        if not app.state.has_users:
            return
        loops = [('worker', app.state.worker), ('scheduler', app.state.scheduler), ('outbox_sender', app.state.outbox_sender)]
        if app.state.launcher is not None:
            loops.append(('procedure_runner', app.state.procedure_runner_loop))  # шаги идут в браузере бота через лаунчер
        for name, fn in loops:
            if name in started_workers or (name in ('worker', 'procedure_runner') and not recovered['done']):
                continue
            task = asyncio.create_task(fn())
            started_workers[name] = task
            app.state.worker_tasks.append(task)

    async def launcher_startup_sync(first_pass: asyncio.Event):
        """Фоновая стартовая сверка с лаунчером: повторы с растущей паузой (до LAUNCHER_SYNC_DELAY_MAX), без предела
        числа попыток; отменяется вместе с приложением. Сначала сверка заморозки, после неё маршруты лаунчера
        открываются (launcher_ready), затем восстановление turn'ов и запуск worker."""
        delay = LAUNCHER_SYNC_DELAY_START
        attempt = 0
        reconciled = False
        while True:
            attempt += 1
            try:
                if not reconciled:
                    await reconcile_launcher_state()
                    reconciled = True
                    app.state.launcher_ready = True
                    log.info('launcher_startup_sync_ready', extra={'attempt': attempt})
                await recover_after_start()
                recovered['done'] = True
                app.state.launcher_synced = True
                start_workers()
                first_pass.set()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Одна запись на попытку: с LAUNCHER_SYNC_ERROR_AFTER-й попытки уровень error и трассировка, чтобы
                # сверка, которая не проходит, не пряталась среди предупреждений.
                loud = attempt >= LAUNCHER_SYNC_ERROR_AFTER
                log.log(logging.ERROR if loud else logging.WARNING, 'launcher_startup_sync_failed',
                        extra={'attempt': attempt, 'retry_in': delay, 'error': f'{type(exc).__name__}: {exc}'[:300]},
                        exc_info=exc if loud else None)
            first_pass.set()
            await asyncio.sleep(delay)
            delay = min(delay * 2, LAUNCHER_SYNC_DELAY_MAX)

    def check_secret_keys():
        """Ключи шифрования API-ключей провайдеров проверяются на старте: иначе ошибка формата всплывает только 500-й
        при добавлении провайдера. Пустое значение допустимо (провайдеры с ключом недоступны), кривое: ядро не стартует."""
        raw = os.getenv('BOTHUB_SECRET_KEYS', '').strip()
        if not raw:
            log.warning('secret_keys_missing', extra={'hint': 'BOTHUB_SECRET_KEYS empty: providers with an API key cannot be added'})
            return
        try:
            secrets_module._keys(None)
        except ValueError as exc:
            log.error('secret_keys_invalid', extra={'error': str(exc), 'hint': 'BOTHUB_SECRET_KEYS=1:<base64 of 32 random bytes>'})
            raise RuntimeError(f'BOTHUB_SECRET_KEYS invalid: {exc}') from None

    @asynccontextmanager
    async def lifespan(app):
        check_secret_keys()
        if os.getenv('BOTHUB_RUNNER_EXEC', 'local') == 'docker':
            if app.state.launcher is None:
                raise RuntimeError('Docker mode requires launcher configuration')
            if not os.getenv('BOTHUB_GATEWAY_TOKEN_SECRET'):
                raise RuntimeError('Docker mode requires BOTHUB_GATEWAY_TOKEN_SECRET')
        # Ядро стартует и при недоступном лаунчере: всё, что от него зависит, делает launcher_startup_sync.
        app.state.launcher_ready = app.state.launcher is None
        app.state.launcher_synced = app.state.launcher is None
        recovered['done'] = app.state.launcher is None
        started_workers.clear()
        app.state.pool = await open_pool()
        await app.state.gateway.startup()
        async with app.state.pool.acquire() as con:
            # An old human session may have changed the page while core was down.
            browser_rows = await con.fetch("select id from bothub.bots where executor='container' and provider='claude'")
            browser_ids = [browser_row['id'] for browser_row in browser_rows]
            await con.execute("update bothub.bots set browser_control='returning' where id=any($1::text[])", browser_ids)
            await con.execute("update bothub.bots set browser_control='bot' "
                              "where browser_control<>'bot' and not (id=any($1::text[]))", browser_ids)
            await con.execute("update bothub.approvals set status='expired',decided_at=now() "
                              "where status in ('pending','approved') and "
                              "(starts_with(tool,'browser_') or starts_with(tool,'mcp__bothub__browser') "
                              "or starts_with(tool,'mcp__playwright__'))")
            for browser_row in browser_rows:
                browser_since[browser_row['id']] = datetime.now(NOW)
                browser_by[browser_row['id']] = None
            for encrypted in await con.fetch('select id,secret_encrypted from bothub.providers where secret_encrypted is not null'):
                decrypt_secret(bytes(encrypted['secret_encrypted']),encrypted['id'].bytes)
            has_users = bool(await con.fetchval('select 1 from bothub.users limit 1'))
            if not has_users:
                log.warning('setup_code', extra={'code': app.state.setup_code})
        app.state.has_users = has_users
        app.state.worker_tasks = []
        sync_task = None
        if app.state.launcher is None:
            await recover_after_start()
            start_workers()
        else:
            # Scheduler и outbox не трогают контейнеры и стартуют сразу; worker start_workers пропускает, пока
            # сверка не закончилась (recovered), его запустит launcher_startup_sync.
            start_workers()
            # Фоновая задача, но первую попытку ждём (не дольше LAUNCHER_SYNC_FIRST_WAIT): при живом лаунчере
            # сверка кончается до первого запроса, при мёртвом (отказ приходит сразу) ядро всё равно стартует.
            first_pass = asyncio.Event()
            sync_task = asyncio.create_task(launcher_startup_sync(first_pass))
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(first_pass.wait(), LAUNCHER_SYNC_FIRST_WAIT)
        tasks = app.state.worker_tasks
        try:
            yield
        finally:
            if sync_task is not None:
                sync_task.cancel()
            for task in tasks:
                task.cancel()
            for task, _ in running.values():
                task.cancel()
            for task in tuple(background):
                task.cancel()
            await asyncio.gather(*([sync_task] if sync_task else []), *tasks, *background,
                                 *(t for t, _ in running.values()), return_exceptions=True)
            await app.state.pool.close()
            await app.state.gateway.shutdown()
            if app.state.launcher is not None:
                await app.state.launcher.aclose()

    app.router.lifespan_context = lifespan

    @app.middleware('http')
    async def renew_cookie(request: Request, call_next):
        response=await call_next(request)
        token=getattr(request.state,'renew_session',None)
        if token:
            set_cookie(response,token)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if isinstance(exc.detail, dict) and 'error' in exc.detail:
            return JSONResponse(exc.detail, status_code=exc.status_code)
        return JSONResponse({'error':'invalid', 'detail':str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'error':'invalid', 'detail':validation_detail(exc)}, status_code=validation_status(exc))

    @app.exception_handler(asyncpg.PostgresError)
    async def database_error(request, exc):
        if isinstance(exc, asyncpg.ForeignKeyViolationError):
            code, status = 'not_found', 404
        elif isinstance(exc, asyncpg.UniqueViolationError):
            code, status = 'conflict', 409
        elif isinstance(exc, (asyncpg.CheckViolationError, asyncpg.NotNullViolationError, asyncpg.InvalidTextRepresentationError,
                              asyncpg.CharacterNotInRepertoireError, asyncpg.UntranslatableCharacterError)):  # NUL и подобное, дошедшее до базы
            code, status = 'invalid', 400
        else:
            code, status = 'invalid', 500
        return JSONResponse({'error':code,'detail':code},status_code=status)

    @app.get('/api/health')
    async def health():
        # Пункт 13: быстрая проверка БД (select 1 с таймаутом); подробности ошибки наружу не отдаём.
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT):
                async with app.state.pool.acquire() as con:
                    await con.fetchval('select 1')
        except Exception as exc:
            log.error('health_db_failed', extra={'error': repr(exc)})
            return JSONResponse({'status':'error','detail':'db_unavailable'}, status_code=503)
        # launcher: ok только когда стартовая сверка закончена целиком (или лаунчер не настроен); syncing: заморозка
        # сверена, маршруты открыты, остальное (stale turn'ы) ещё повторяется; unavailable: заморозка не сверена.
        # Статус и код ответа от него не зависят.
        launcher_state = 'ok' if app.state.launcher_synced else 'unavailable' if launcher_waiting() else 'syncing'
        return {'status':'ok','ok':True,'version':'0.1.0','launcher':launcher_state}

    @app.post('/api/setup', status_code=201)
    async def setup(body: JsonObject, request: Request):
        check_credential(body,'password')
        parse_email(body.get('email'))
        async with app.state.pool.acquire() as con:
            if await con.fetchval('select 1 from bothub.users limit 1'): error('conflict',409)
        supplied = request.headers.get('x-setup-code','')
        bearer = request.headers.get('authorization','').removeprefix('Bearer ')
        by_owner = token_equals(bearer, os.getenv('OWNER_TOKEN'))
        allowed = by_owner or bool(supplied and app.state.setup_code and token_equals(supplied, app.state.setup_code))
        if not allowed: error('unauthorized',401)
        email = parse_email(body.get('email'))
        try: digest = auth.hash_password(body.get('password',''))
        except (ValueError,TypeError): error('invalid')
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub-setup'))")
                if await con.fetchval('select 1 from bothub.users limit 1'): error('conflict',409)
                user = await con.fetchrow("insert into bothub.users(email,password_hash,role) values($1,$2,'admin') returning id,email,role,status,created_at",email,digest)
                await con.execute("insert into bothub.settings(key,value) values('setup_user_id',to_jsonb($1::text)),('legacy_auth_enabled',to_jsonb($2::boolean)) on conflict(key) do update set value=excluded.value",str(user['id']),by_owner)
                for table in ('bots','threads','memory','schedules','files','push_subscriptions','outbox','mac_status'):
                    await con.execute(f'update bothub.{table} set owner_id=$1 where owner_id is null',user['id'])
                    await con.execute(f'alter table bothub.{table} alter column owner_id set not null')
        app.state.setup_code = None
        app.state.has_users = True
        if not launcher_waiting():
            await recover_stale_turns()  # первых пользователей не было: stale turn'ов нет, вызов не трогает лаунчер
        start_workers()  # с лаунчером worker запустит стартовая сверка, если она ещё идёт
        prepare_user_network(user['id'])
        return data(user)

    @app.get('/api/setup/status')
    async def setup_status():
        async with app.state.pool.acquire() as con:
            exists = await con.fetchval('select 1 from bothub.users limit 1')
        return {'needs_setup': not bool(exists)}

    @app.post('/api/auth/login')
    async def login(body: JsonObject, request: Request):
        password = check_credential(body,'password')
        email = parse_email(body.get('email'))
        ip = str(request.client.host if request.client else 'unknown')
        rate_limit((email, ip))
        await email_delay(email)
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select id,email,password_hash,role,status from bothub.users where email=$1',email)
            digest = row['password_hash'] if row else app.state.dummy_password_hash
            valid = auth.verify_password(digest,password if isinstance(password,str) else '')
            if not row or not valid or row['status']!='active':
                error('unauthorized',401,'invalid_credentials')
            token = auth.new_token()
            await con.execute("insert into bothub.sessions(id_hash,user_id,expires_at,user_agent) values($1,$2,now()+interval '30 days',$3)",auth.token_hash(token),row['id'],request.headers.get('user-agent','')[:512].replace('\x00',''))
        response = JSONResponse({'id':str(row['id']),'email':row['email'],'role':row['role'],'csrf_token':auth.csrf_token(token,os.getenv('BOT_TOKEN_SECRET',''))})
        set_cookie(response,token)
        return response

    @app.get('/api/auth/me')
    async def me(request: Request):
        who = await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select id,email,role,status,created_at from bothub.users where id=$1',who['user_id'])
            setup_id = await con.fetchval("select value #>> '{}' from bothub.settings where key='setup_user_id'")
            legacy = await legacy_enabled(con) if setup_id == str(who['user_id']) else False
        return data(row) | {'csrf_token':getattr(request.state,'csrf_token',None), 'legacy_auth':legacy}

    @app.post('/api/auth/logout')
    async def logout(request: Request):
        who = await principal(request,owner=True)
        request.state.renew_session=None
        cookie = request.cookies.get('bothub_session')
        if cookie:
            async with app.state.pool.acquire() as con:
                await revoke_one_session(con, auth.token_hash(cookie), who['user_id'])
        response = JSONResponse({'ok':True})
        response.delete_cookie('bothub_session',path=cookie_path())
        return response

    @app.post('/api/auth/password')
    async def password(body: JsonObject, request: Request):
        who = await principal(request,owner=True)
        request.state.renew_session=None
        old_password = body.get('old_password','')
        if isinstance(old_password,str): check_credential(body,'old_password')
        try: digest = auth.hash_password(check_credential(body,'new_password'))
        except (ValueError,TypeError): error('invalid')
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                old = await con.fetchval('select password_hash from bothub.users where id=$1 for update',who['user_id'])
                if not auth.verify_password(old,old_password): error('unauthorized',401)
                await con.execute('update bothub.users set password_hash=$2 where id=$1',who['user_id'],digest)
                await revoke_user(con,who['user_id'])
                await con.execute('update bothub.mac_tokens set revoked_at=now() where user_id=$1',who['user_id'])
        socket=mac_socket.get(str(who['user_id']))
        if socket:
            with suppress(Exception): await socket.close(code=4401)
        response=JSONResponse({'ok':True})
        response.delete_cookie('bothub_session',path=cookie_path())
        return response

    @app.post('/api/invites',status_code=201)
    async def add_invite(body: JsonObject, request: Request):
        who = await principal(request,admin=True)
        role=body.get('role','member')
        if role not in ('admin','member'): error('invalid')
        try: expiry=auth.invite_expiry(datetime.now(NOW),int(body.get('days',7)))
        except (TypeError,ValueError,OverflowError): error('invalid')
        token=auth.new_token()
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('insert into bothub.invites(token_hash,created_by,role,expires_at) values($1,$2,$3,$4) returning *',auth.token_hash(token),who['user_id'],role,expiry)
        return data(row) | {'token':token}

    @app.get('/api/invites')
    async def invites(request: Request):
        await principal(request,admin=True)
        async with app.state.pool.acquire() as con:
            return [data(r) for r in await con.fetch('select * from bothub.invites order by expires_at desc')]

    @app.delete('/api/invites/{id}')
    async def revoke_invite(id: str, request: Request):
        await principal(request,admin=True)
        if not auth.is_clean_text(id): error('not_found',404)
        async with app.state.pool.acquire() as con:
            row=await con.fetchval('delete from bothub.invites where token_hash=$1 and used_at is null returning token_hash',id)
        if not row: error('not_found',404)
        return {'ok':True}

    @app.get('/api/invites/check')
    async def check_invite(token: str):
        if len(token)>TOKEN_MAX or not auth.is_clean_text(token): return {'valid':False}  # такой токен выдать не могли
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('select i.role,i.expires_at,u.email as invited_by from bothub.invites i join bothub.users u on u.id=i.created_by where i.token_hash=$1 and i.used_at is null and i.expires_at>now()',auth.token_hash(token))
        return {'valid':bool(row), **(data(row) if row else {})}

    @app.post('/api/invites/accept',status_code=201)
    async def accept_invite(body: dict):  # без JsonObject: текст токена с NUL или суррогатом даёт 410, как у несуществующего
        token=body.get('token','')
        if not isinstance(token,str) or len(token)>TOKEN_MAX or not auth.is_clean_text(token): error('invite_invalid',410)
        email=parse_email(body.get('email'))
        try: digest=auth.hash_password(check_credential(body,'password'))
        except (TypeError,ValueError,OverflowError): error('invalid')
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                invite=await con.fetchrow('select * from bothub.invites where token_hash=$1 for update',auth.token_hash(token))
                if not invite: error('invite_invalid',410)
                if invite['used_at']: error('invite_used',410)
                if invite['expires_at']<=datetime.now(NOW): error('invite_expired',410)
                user=await con.fetchrow('insert into bothub.users(email,password_hash,role) values($1,$2,$3) returning id,email,role,status,created_at',email,digest,invite['role'])
                await con.execute('update bothub.invites set used_by=$2,used_at=now() where token_hash=$1',invite['token_hash'],user['id'])
        prepare_user_network(user['id'])
        return data(user)

    @app.get('/api/users')
    async def users(request: Request):
        await principal(request,admin=True)
        async with app.state.pool.acquire() as con:
            return [data(r) for r in await con.fetch('select u.id,u.email,u.role,u.status,u.created_at,(select count(*) from bothub.bots b where b.owner_id=u.id) as bots_count,(select max(created_at) from bothub.sessions s where s.user_id=u.id) as last_seen_at from bothub.users u order by u.created_at')]

    @app.patch('/api/users/{id}')
    async def edit_user(id: uuid.UUID, body: JsonObject, request: Request):
        await principal(request,admin=True)
        if not body or set(body)-{'role','disabled'} or body.get('role','member') not in ('admin','member') or not isinstance(body.get('disabled',False),bool): error('invalid')
        to_stop = []
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub-admin'))")
                row=await con.fetchrow('select * from bothub.users where id=$1 for update',id)
                if not row: error('not_found',404)
                role=body.get('role',row['role'])
                status='disabled' if body.get('disabled',row['status']=='disabled') else 'active'
                if row['role']=='admin' and row['status']=='active' and (role!='admin' or status!='active'):
                    if await con.fetchval("select count(*) from bothub.users where role='admin' and status='active'")<=1: error('conflict',409,'last_admin')
                updated=await con.fetchrow('update bothub.users set role=$2,status=$3 where id=$1 returning id,email,role,status,created_at',id,role,status)
                if status=='disabled' and row['status']!='disabled':
                    to_stop = [item['id'] for item in await con.fetch("select t.id from bothub.turns t join bothub.threads th on th.id=t.thread_id where th.owner_id=$1 and t.status in ('running','waiting_approval','waiting_mac')",id)]
                    paused = await con.fetch("update bothub.schedules set enabled=false,paused_by_disabled=true where owner_id=$1 and enabled returning id,last_turn_id,bot_id,name",id)
                    for schedule in paused:
                        thread_id = await con.fetchval('select th.id from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and th.owner_id=$2 and th.bot_id=$3',schedule['last_turn_id'],id,schedule['bot_id']) if schedule['last_turn_id'] else None
                        event_turn_id = schedule['last_turn_id'] if thread_id else None
                        if not thread_id:
                            thread_id = await con.fetchval('select id from bothub.threads where owner_id=$1 and bot_id=$2 order by created_at desc limit 1',id,schedule['bot_id'])
                        if not thread_id:
                            thread_id = await con.fetchval("insert into bothub.threads(bot_id,kind,title,owner_id) values($1,'routine',$2,$3) returning id",schedule['bot_id'],schedule['name'],id)
                        await append_event(con,thread_id,event_turn_id,'schedule_paused','system',{'schedule_id':str(schedule['id']),'reason':'user_disabled','detail':'Пользователь отключён'})
                    await revoke_user(con,id)
                    socket=mac_socket.get(str(id))
                    if socket:
                        with suppress(Exception): await socket.close(code=4401)
                elif status=='active' and row['status']=='disabled':
                    await con.execute("update bothub.schedules set enabled=true,paused_by_disabled=false where owner_id=$1 and paused_by_disabled",id)
        for turn_id in to_stop:
            try:
                await stop_turn(turn_id, 'user_disabled')
            except Exception:
                log.exception('disabled_user_turn_stop_failed', extra={'turn_id': str(turn_id)})
        return data(updated)

    @app.get('/api/sessions')
    async def sessions(request: Request):
        who=await principal(request,owner=True)
        current = auth.token_hash(request.cookies['bothub_session']) if request.cookies.get('bothub_session') else None
        async with app.state.pool.acquire() as con:
            return [data(r) | {'current':r['id_hash']==current} for r in await con.fetch('select id_hash,created_at,expires_at,revoked_at,user_agent from bothub.sessions where user_id=$1 order by created_at desc',who['user_id'])]

    @app.delete('/api/sessions/{id}')
    async def revoke_session(id: str, request: Request):
        who=await principal(request,owner=True)
        if not auth.is_clean_text(id): error('not_found',404)
        async with app.state.pool.acquire() as con:
            row=await revoke_one_session(con,id,who['user_id'])
        if not row: error('not_found',404)
        response = JSONResponse({'ok':True})
        if request.cookies.get('bothub_session') and auth.token_hash(request.cookies['bothub_session'])==id:
            request.state.renew_session=None
            response.delete_cookie('bothub_session',path=cookie_path())
        return response

    @app.post('/api/mac/token',status_code=201)
    async def create_mac_token(request: Request):
        who=await principal(request,owner=True)
        token=auth.new_token()
        async with app.state.pool.acquire() as con:
            await con.execute('insert into bothub.mac_tokens(token_hash,user_id) values($1,$2) on conflict(user_id) do update set token_hash=$1,created_at=now(),revoked_at=null',auth.token_hash(token),who['user_id'])
        socket=mac_socket.get(str(who['user_id']))
        if socket:
            with suppress(Exception): await socket.close(code=4401)
        return {'token':token}

    @app.delete('/api/mac/token')
    async def revoke_mac_token(request: Request):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            await con.execute('update bothub.mac_tokens set revoked_at=now() where user_id=$1',who['user_id'])
        socket=mac_socket.get(str(who['user_id']))
        if socket:
            with suppress(Exception): await socket.close(code=4401)
        return {'ok':True}

    def public_provider(row):
        item = dict(row)
        item.pop('allow_private_ips', None)  # внутренние адреса видит только администратор (заявки и ответ allow-private)
        has_secret = bool(item.pop('secret_encrypted', None))
        return {**jsonable_encoder(item), 'has_secret': has_secret}

    async def sync_models(con, provider_id, names):
        names = tuple(dict.fromkeys(str(n) for n in names if isinstance(n,str) and n))[:500]
        for name in names:
            await con.execute('insert into bothub.models(provider_id,name) values($1,$2) '
                'on conflict(provider_id,name) do update set enabled=not models.manually_disabled',provider_id,name)
        await con.execute('update bothub.models set enabled=false where provider_id=$1 and not (name=any($2::text[]))',provider_id,list(names))

    async def check_provider_record(provider_id, owner_id, force=False):
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select * from bothub.providers where id=$1 and owner_id=$2',provider_id,owner_id)
            if not row: error('not_found',404)
        if row['status'] in ('disabled','pending_admin'):  # pending_admin: приватный адрес без одобрения админа не опрашиваем
            return public_provider(row)
        cooldown = AGY_CHECK_COOLDOWN if row['cli']=='agy' else PROVIDER_CHECK_COOLDOWN
        if row['status']=='error':
            cooldown = min(cooldown, 30)
        if force: cooldown = min(cooldown, PROVIDER_FORCE_COOLDOWN)  # force обходит кэш, но не чаще раза в несколько секунд
        if row['last_check_at'] and datetime.now(NOW)-row['last_check_at']<timedelta(seconds=cooldown):
            return public_provider(row)
        task = provider_checks.get(provider_id)
        if task is None:
            task = asyncio.create_task(_check_provider_record(provider_id, owner_id))
            provider_checks[provider_id] = task
            task.add_done_callback(lambda done: provider_checks.pop(provider_id, None) if provider_checks.get(provider_id) is done else None)
        return await asyncio.shield(task)

    @asynccontextmanager
    async def probe_slot(owner: str):
        """Один пробный запрос к API провайдера: не больше PROVIDER_PROBE_PARALLEL одновременно и PROVIDER_PROBE_RATE в минуту
        на пользователя. Общий для POST и PATCH с ключом и для /check; лишним 429 `rate_limited`, ничего не записано."""
        slot=probe_slots.get(owner)
        if slot is None: slot=probe_slots[owner]=asyncio.Semaphore(PROVIDER_PROBE_PARALLEL)
        if slot.locked(): error('rate_limited',429,'too many provider checks in progress')
        rate_limit((owner,'provider_probe'),maximum=PROVIDER_PROBE_RATE,window=60)
        async with slot:
            yield

    async def _check_provider_record(provider_id, owner_id):
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('select * from bothub.providers where id=$1 and owner_id=$2',provider_id,owner_id)
            if not row: error('not_found',404)
        names=[]
        try:
            if row['kind']=='cli_subscription':
                if app.state.launcher is None or not app.state.launcher_ready: raise RuntimeError('launcher unavailable')
                async with asyncio.timeout(PROVIDER_CHECK_TIMEOUT):
                    await app.state.launcher.create_login_container(str(owner_id))
                    sid=await app.state.launcher.open_login_session(str(owner_id),command=row['cli']+'_status')
                    code=None
                    stdout=bytearray()
                    try:
                        async for frame in app.state.launcher.login_output(sid):
                            if isinstance(frame,ExecExit): code=frame.code
                            elif isinstance(frame,ExecChunk) and frame.stream=='stdout' and len(stdout)<1024*1024: stdout+=frame.data
                    finally:
                        close_task=asyncio.create_task(app.state.launcher.close_login_session(sid))
                        try:
                            await asyncio.shield(close_task)
                        except asyncio.CancelledError:
                            await close_task
                            raise
                if code != 0: raise RuntimeError('subscription not authenticated')
                names=(row['cli']=='agy' and parse_agy_models(bytes(stdout).decode('utf-8','replace'))) or list(SUBSCRIPTION_MODELS[row['cli']])
            else:
                key=decrypt_secret(bytes(row['secret_encrypted']),row['id'].bytes).decode()
                async with probe_slot(str(owner_id)):
                    names=await fetch_provider_models(row['kind'],row['base_url'],key,allow_private=row['allow_private'],
                        approved_ips=row.get('allow_private_ips') or (),forbidden=forbidden_networks)
            status,last_error='ok',None
        except HTTPException:
            raise  # 429 по лимиту проб: проверки не было, статус провайдера не меняем
        except ProbeError as exc:
            # Одобренный адрес сменился: флаг не снимаем, провайдер ждёт повторного одобрения администратором.
            status,last_error=('pending_admin' if exc.reapproval else 'error'),str(exc)[:300]
        except Exception as exc:
            status,last_error='error',(str(exc) or exc.__class__.__name__)[:300]
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                current = await con.fetchrow('select * from bothub.providers where id=$1 and owner_id=$2 for update',provider_id,owner_id)
                if not current: error('not_found',404)
                if any(current[key] != row[key] for key in ('kind','cli','base_url','secret_encrypted','status','allow_private','allow_private_ips')):
                    return public_provider(current)
                await con.execute('update bothub.providers set status=$2,last_check_at=now(),last_error=$3 where id=$1 and owner_id=$4',provider_id,status,last_error,owner_id)
                if status=='ok': await sync_models(con,provider_id,names)
            return public_provider(await con.fetchrow('select * from bothub.providers where id=$1 and owner_id=$2',provider_id,owner_id))

    async def recreate_pending_bot(bot_id):
        if app.state.launcher is None or not app.state.launcher_ready:
            return  # need_restart остаётся в БД: стартовая сверка пересоздаст бота, когда лаунчер ответит
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                bot = await con.fetchrow("select id,owner_id from bothub.bots where id=$1 and need_restart and status not in ('error_starting','no_model') for update",bot_id)
                if not bot or await con.fetchval(
                    "select 1 from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                    "where th.bot_id=$1 and t.status in ('running','waiting_approval','waiting_mac') limit 1",bot_id):
                    return
                try:
                    existing = await app.state.launcher.status(bot_id)
                    if existing.exists:
                        if existing.owner_id != str(bot['owner_id']):
                            raise RuntimeError('container owner mismatch')
                        await app.state.launcher.recreate_bot(bot_id)
                    else:
                        await app.state.launcher.create_bot(bot_id,str(bot['owner_id']))
                except Exception:
                    log.exception('bot_recreate_failed',extra={'bot_id':bot_id,'recreate_url':f'/api/bots/{bot_id}/recreate'})
                    await con.execute("update bothub.bots set need_restart=false,status='error_starting' where id=$1",bot_id)
                    return
                await con.execute("update bothub.bots set need_restart=false,status='idle' where id=$1",bot_id)

    app.state.recreate_pending_bot = recreate_pending_bot

    async def refresh_subscription_bots(provider_id, owner_id):
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub-claim-turn'))")
                bots = await con.fetch('update bothub.bots set need_restart=true where provider_id=$1 and owner_id=$2 returning id',provider_id,owner_id)
        for bot in bots:
            await recreate_pending_bot(bot['id'])

    @app.get('/api/providers')
    async def providers(request: Request):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            return [public_provider(r) for r in await con.fetch('select * from bothub.providers where owner_id=$1 order by created_at',who['user_id'])]

    async def admit_address(base, resolver, *, allow_private, approved_ips):
        """Адрес провайдера до пробного запроса: (Target, 'ready' | 'pending_admin'); отказ: 422 с кодом.

        approved_ips=None при allow_private: администратор одобряет этим же запросом то, во что адрес разрешается сейчас."""
        try:
            target=await inspect_target(base,private_allow_hosts(),resolver,forbidden_networks)
        except UnresolvedHostError as exc:
            error('unreachable',422,str(exc))
        except ValueError as exc:
            error('invalid_base_url',422,str(exc))
        if not unapproved_addresses(target,allow_private,approved_ips): return target,'ready'
        if allow_private: error('invalid_base_url',422,ADDRESS_CHANGED_DETAIL)  # флаг есть, но IP вне одобренного набора
        return target,'pending_admin'

    async def verify_secret(kind, base, secret, *, user_id, allow_private, approved_ips, force):
        """Адрес и ключ до записи, пробный запрос ровно один. Возвращает (base_url, status, имена моделей | None, одобренные IP).

        pending_admin: приватный адрес без одобрения, запрос не уходит (SSRF). force пропускает только пробный запрос.
        approved_ips=None: флаг ставится этим запросом (админ), набор берётся из ответа DNS. DNS, проверка адреса и проба
        делят один ответ DNS и один PROVIDER_CHECK_TIMEOUT. Пробы: не больше PROVIDER_PROBE_PARALLEL одновременно и
        PROVIDER_PROBE_RATE в минуту на пользователя; заявки администратору: PROVIDER_ADMIN_REQUESTS_PER_HOUR в час."""
        resolver,origin,ips,owner=OneShotResolver(),base,list(approved_ips or ()),str(user_id)
        try:
            async with asyncio.timeout(PROVIDER_CHECK_TIMEOUT):
                if base:
                    target,state=await admit_address(base,resolver,allow_private=allow_private,approved_ips=approved_ips)
                    origin=target.origin
                    if approved_ips is None: ips=list(target.private) if allow_private else []
                    if state=='pending_admin':
                        rate_limit((owner,'provider_admin_request'),maximum=PROVIDER_ADMIN_REQUESTS_PER_HOUR,window=3600)
                        return origin,'pending_admin',None,[]
                if force: return origin,'unchecked',None,ips
                try:
                    async with probe_slot(owner):
                        names=await fetch_provider_models(kind,origin,secret,allow_private=allow_private,approved_ips=ips,
                                                          forbidden=forbidden_networks,resolver=resolver)
                except ProbeError as exc:
                    error(exc.code,422,str(exc))
                return origin,'ok',names,ips
        except TimeoutError:
            error('unreachable',422,'provider check timed out')

    def admin_provider_view(row, email=None):
        view={'id':str(row['id']),'name':row['name'],'base_url':row['base_url'],'created_at':row['created_at'].isoformat()}
        return view if email is None else {**view,'email':email}

    @app.post('/api/providers',status_code=201)
    async def create_provider(body: JsonObject,request: Request):
        who=await principal(request,owner=True)
        if set(body)-{'kind','cli','name','base_url','secret','force','allow_private'}: error('invalid')
        if 'allow_private' in body and who['role']!='admin': error('forbidden',403,'allow_private is set by an administrator only')
        kind,cli,name=body.get('kind'),body.get('cli'),body.get('name')
        if kind not in (*PROVIDER_KINDS,'cli_subscription') or not isinstance(name,str) or not name.strip() or len(name)>80: error('invalid')
        if any(key in body and type(body[key]) is not bool for key in ('force','allow_private')): error('invalid')
        if kind=='cli_subscription':
            if not isinstance(cli,str) or cli not in SUBSCRIPTION_MODELS or body.get('secret') or body.get('base_url') or body.get('force') or body.get('allow_private'): error('invalid')
        elif cli or not isinstance(body.get('secret'),str) or not body['secret'] or len(body['secret'].encode())>8192: error('invalid')
        base=body.get('base_url')
        if base is not None and (not isinstance(base,str) or len(base)>2048): error('invalid')
        if kind=='openai_compatible' and not base: error('invalid')
        provider_id=uuid.uuid4()
        if kind=='cli_subscription':
            async with app.state.pool.acquire() as con:
                await con.execute('insert into bothub.providers(id,owner_id,kind,cli,name) values($1,$2,$3,$4,$5)',provider_id,who['user_id'],kind,cli,name.strip())
            return await check_provider_record(provider_id,who['user_id'])
        allow=bool(body.get('allow_private'))
        base,status,names,ips=await verify_secret(kind,base,body['secret'],user_id=who['user_id'],allow_private=allow,
                                                  approved_ips=None if allow else [],force=bool(body.get('force')))
        encrypted=encrypt_secret(body['secret'].encode(),provider_id.bytes)
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                row=await con.fetchrow('insert into bothub.providers(id,owner_id,kind,name,base_url,secret_encrypted,secret_tail,allow_private,allow_private_ips,status,last_check_at) '
                    "values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,case when $10='ok' then now() end) returning *",
                    provider_id,who['user_id'],kind,name.strip(),base or None,encrypted,secret_tail(body['secret']),allow,ips,status)
                if names is not None: await sync_models(con,provider_id,names)
        return public_provider(row)

    @app.patch('/api/providers/{id}')
    async def patch_provider(id: uuid.UUID,body: JsonObject,request: Request):
        who=await principal(request,owner=True)
        if not body or set(body)-{'name','base_url','secret','status','force','allow_private'}: error('invalid')
        if 'allow_private' in body and who['role']!='admin': error('forbidden',403,'allow_private is set by an administrator only')
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('select * from bothub.providers where id=$1 and owner_id=$2',id,who['user_id'])
        if not row: error('not_found',404)
        if 'name' in body and (not isinstance(body['name'],str) or not body['name'].strip() or len(body['name'])>80): error('invalid')
        if 'secret' in body and (row['kind']=='cli_subscription' or not isinstance(body['secret'],str) or not body['secret'] or len(body['secret'].encode())>8192): error('invalid')
        if any(key in body and type(body[key]) is not bool for key in ('force','allow_private')): error('invalid')
        if ('force' in body and 'secret' not in body) or ('allow_private' in body and row['kind']=='cli_subscription'): error('invalid')
        if 'base_url' in body:
            if row['kind']=='cli_subscription': error('invalid')
            if 'secret' not in body: error('invalid',400,'secret required when changing base_url')
            if not isinstance(body['base_url'],str) or len(body['base_url'])>2048: error('invalid')
            if not body['base_url'].strip(): error('invalid_base_url',400,'base_url must not be empty')
        if 'status' in body and body['status']!='disabled': error('invalid')
        values={k:body[k] for k in ('name','status') if k in body}
        names=None
        if 'secret' in body:
            # Смена адреса сбрасывает флаг; явный allow_private администратора относится к новому адресу и этим же запросом
            # привязывается к его IP. Без явного флага проверка идёт по уже одобренному набору.
            changed='base_url' in body and not same_base(body['base_url'],row['base_url'])
            allow=body['allow_private'] if 'allow_private' in body else (False if changed else row['allow_private'])
            approved=None if allow and 'allow_private' in body else (list(row['allow_private_ips'] or ()) if allow else [])
            base=body['base_url'] if 'base_url' in body else row['base_url']
            origin,status,names,ips=await verify_secret(row['kind'],base,body['secret'],user_id=who['user_id'],allow_private=allow,
                                                        approved_ips=approved,force=bool(body.get('force')))
            values.update(secret_encrypted=encrypt_secret(body['secret'].encode(),id.bytes),secret_tail=secret_tail(body['secret']),
                          allow_private=allow,allow_private_ips=ips,last_error=None)
            if 'base_url' in body: values['base_url']=origin
            if row['status']!='disabled' and 'status' not in body:
                values.update(status=status,last_check_at=datetime.now(NOW) if status=='ok' else None)
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                current=await con.fetchrow('select * from bothub.providers where id=$1 and owner_id=$2 for update',id,who['user_id'])
                if not current: error('not_found',404)
                if any(current[key]!=row[key] for key in ('secret_encrypted','base_url','allow_private','allow_private_ips','kind')): error('conflict',409,'provider changed during the check')
                if values:
                    sets=','.join(f'{k}=${i+3}' for i,k in enumerate(values))
                    current=await con.fetchrow(f'update bothub.providers set {sets} where id=$1 and owner_id=$2 returning *',id,who['user_id'],*values.values())
                if names is not None: await sync_models(con,id,names)
        if 'allow_private' in body and 'secret' not in body and (
                body['allow_private']!=row['allow_private'] or (body['allow_private'] and row['status']=='pending_admin')):
            await apply_allow_private(current,body['allow_private'])
            async with app.state.pool.acquire() as con:
                current=await con.fetchrow('select * from bothub.providers where id=$1',id)
        return public_provider(current)

    async def lookup_target(base):
        """Один вопрос DNS в пределах PROVIDER_CHECK_TIMEOUT; Target или ошибка 422 с кодом."""
        try:
            async with asyncio.timeout(PROVIDER_CHECK_TIMEOUT):
                return await inspect_target(base,private_allow_hosts(),_resolve_host,forbidden_networks)
        except UnresolvedHostError as exc:
            error('unreachable',422,str(exc))
        except TimeoutError:
            error('unreachable',422,'provider hostname lookup timed out')
        except ValueError as exc:
            error('invalid_base_url',422,str(exc))

    async def apply_allow_private(row, allow, expect_ips=None):
        """Ставит флаг, набор одобренных IP и пересчитывает статус: pending_admin и new.

        allow: набор берётся из DNS сейчас (expect_ips, если задан, должен совпасть: 409), имя без ответа DNS одобрить нельзя.
        Не allow: набор очищается, приватный адрес возвращается в pending_admin. Одобрение сразу проверяется пробным запросом."""
        status,ips,last_error=row['status'],[],row['last_error']
        checkable=row['kind']!='cli_subscription' and row['base_url']
        if allow:
            if checkable:
                ips=list((await lookup_target(row['base_url'])).private)
                if expect_ips is not None and set(expect_ips)!=set(ips): error('conflict',409,'resolved addresses changed')
                if status=='pending_admin': status='new'
                last_error=None
        elif checkable and status!='disabled':
            try:
                async with asyncio.timeout(PROVIDER_CHECK_TIMEOUT):
                    target=await inspect_target(row['base_url'],private_allow_hosts(),_resolve_host,forbidden_networks)
            except (ValueError,TimeoutError):
                pass
            else:
                if unapproved_addresses(target,False,None): status='pending_admin'
        async with app.state.pool.acquire() as con:
            updated=await con.fetchrow("update bothub.providers set allow_private=$2,status=$3,allow_private_ips=$6,last_error=$7,"
                "last_check_at=case when $3 is distinct from status then null else last_check_at end "
                "where id=$1 and base_url is not distinct from $4 and secret_encrypted is not distinct from $5 returning id",
                row['id'],allow,status,row['base_url'],row['secret_encrypted'],ips,last_error)
        if not updated: error('conflict',409,'provider changed')
        if row['status']=='pending_admin' and status=='new':
            try:
                await check_provider_record(row['id'],row['owner_id'])
            except HTTPException as exc:
                if exc.status_code!=429: raise  # слоты проб владельца заняты: одобрение записано, провайдер остаётся new до /check

    @app.patch('/api/providers/{id}/allow-private')
    async def provider_allow_private(id: uuid.UUID,body: JsonObject,request: Request):
        await principal(request,admin=True)
        if not body or set(body)-{'allow','base_url','ips'} or type(body.get('allow')) is not bool: error('invalid')
        if 'base_url' in body and not isinstance(body['base_url'],str): error('invalid')
        expect=None
        if 'ips' in body:
            if not body['allow'] or not isinstance(body['ips'],list) or len(body['ips'])>64: error('invalid',400,'ips must be a list of up to 64 addresses and only comes with allow: true')
            try: expect={canonical_ip(item) for item in body['ips']}
            except ValueError: error('invalid',400,'ips must contain IP addresses')
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('select p.*,u.email from bothub.providers p join bothub.users u on u.id=p.owner_id where p.id=$1',id)
        if not row: error('not_found',404)
        if row['kind']=='cli_subscription': error('invalid')
        # base_url в теле фиксирует адрес, который администратор видел в списке: участник мог сменить его после.
        if body['allow'] and 'base_url' not in body: error('invalid',400,'base_url is required to approve: approve the address you saw')
        # Одобряется набор, который администратор видел (resolved_ips в списке заявок), а не то, во что имя разрешится в момент PATCH.
        if body['allow'] and not expect: error('invalid',400,'ips is required to approve: approve the addresses you saw')
        if 'base_url' in body and body['base_url']!=row['base_url']: error('conflict',409,'base_url changed')
        await apply_allow_private(row,body['allow'],expect)
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('select p.*,u.email from bothub.providers p join bothub.users u on u.id=p.owner_id where p.id=$1',id)
        return {**admin_provider_view(row,row['email']),'status':row['status'],'allow_private':row['allow_private'],
                'allow_private_ips':list(row['allow_private_ips'] or ())}

    @app.get('/api/admin/provider-requests')
    async def provider_requests(request: Request):
        await principal(request,admin=True)
        async with app.state.pool.acquire() as con:
            rows=await con.fetch("select p.id,p.name,p.base_url,p.created_at,p.allow_private,p.allow_private_ips,u.email from bothub.providers p "
                "join bothub.users u on u.id=p.owner_id where p.status='pending_admin' order by p.created_at,p.id")

        async def resolve(row):
            try:
                async with asyncio.timeout(PROVIDER_REQUEST_RESOLVE_TIMEOUT):
                    target=await inspect_target(row['base_url'],private_allow_hosts(),_resolve_host,forbidden_networks)
            except (ValueError,TimeoutError) as exc:
                return {'resolved_ips':[],'resolve_error':str(exc) or 'provider hostname lookup timed out'}
            return {'resolved_ips':list(target.private)}

        # Администратор видит, во что адрес разрешается сейчас, и какие IP уже одобрены: «одобрить» значит одобрить эти IP.
        resolved=await asyncio.gather(*(resolve(r) for r in rows))
        return [{**admin_provider_view(r,r['email']),'allow_private':r['allow_private'],'approved_ips':list(r['allow_private_ips'] or ()),
                 'reapproval':bool(r['allow_private']),**extra} for r,extra in zip(rows,resolved)]

    @app.delete('/api/providers/{id}')
    async def delete_provider(id: uuid.UUID,request: Request):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub-claim-turn'))")
                await con.execute("update bothub.bots set status='no_model',need_restart=false where provider_id=$1 and owner_id=$2",id,who['user_id'])
                deleted=await con.fetchval('delete from bothub.providers where id=$1 and owner_id=$2 returning id',id,who['user_id'])
        if not deleted: error('not_found',404)
        return {'ok':True}

    @app.post('/api/providers/{id}/check')
    async def provider_check(id: uuid.UUID,request: Request):
        who=await principal(request,owner=True)
        checked=await check_provider_record(id,who['user_id'],force=request.query_params.get('force')=='1')
        if checked['status']=='pending_admin' and checked['allow_private']:
            error('invalid_base_url',422,checked['last_error'] or ADDRESS_CHANGED_DETAIL)  # одобренный адрес сменился
        return checked

    @app.get('/api/models')
    async def models(request: Request, include_disabled: bool = False):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            return [data(r) for r in await con.fetch('select m.* from bothub.models m join bothub.providers p on p.id=m.provider_id where p.owner_id=$1 and ($2 or m.enabled=true) order by p.name,m.name',who['user_id'],include_disabled)]

    @app.post('/api/models/refresh')
    async def refresh_models(request: Request):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            ids=[r['id'] for r in await con.fetch('select id from bothub.providers where owner_id=$1',who['user_id'])]
        async def refresh_one(provider_id):
            try:
                return await check_provider_record(provider_id,who['user_id'])
            except HTTPException as exc:
                if exc.status_code!=429: raise
                async with app.state.pool.acquire() as con:  # слоты проб заняты: провайдер без новой проверки, как есть
                    row=await con.fetchrow('select * from bothub.providers where id=$1 and owner_id=$2',provider_id,who['user_id'])
                if not row: error('not_found',404)
                return public_provider(row)
        return await asyncio.gather(*(refresh_one(provider_id) for provider_id in ids))

    @app.patch('/api/models/{id}')
    async def patch_model(id: uuid.UUID,body: JsonObject,request: Request):
        who=await principal(request,owner=True)
        if not body or set(body)-{'enabled','display_name'} or ('enabled' in body and type(body['enabled']) is not bool) or ('display_name' in body and not isinstance(body['display_name'],str)): error('invalid')
        if 'enabled' in body: body['manually_disabled']=not body['enabled']
        vals=list(body.values()); sets=','.join(f'{k}=${i+3}' for i,k in enumerate(body))
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow(f'update bothub.models m set {sets} from bothub.providers p where m.id=$1 and m.provider_id=p.id and p.owner_id=$2 returning m.*',id,who['user_id'],*vals)
        if not row: error('not_found',404)
        return data(row)

    @app.websocket('/api/providers/{id}/login')
    async def provider_login(ws: WebSocket,id: uuid.UUID):
        # та же проверка Origin, что у /api/ws: за TLS-прокси без X-Forwarded-Proto сокет приходит как ws://
        origin=ws.headers.get('origin','')
        scheme=ws.headers.get('x-forwarded-proto') or ('https' if ws.url.scheme=='wss' else 'http')
        if not (auth.same_origin(origin,ws.headers.get('host',''),scheme=scheme) or
                not ws.headers.get('x-forwarded-proto') and ws.url.scheme=='ws' and
                auth.same_origin(origin,ws.headers.get('host',''),scheme='https')):
            await ws.close(code=4401); return
        cookie=ws.cookies.get('bothub_session','')
        session_hash=auth.token_hash(cookie) if cookie else ''
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow("select p.*,u.status from bothub.providers p join bothub.sessions s on s.user_id=p.owner_id "
                "join bothub.users u on u.id=p.owner_id where p.id=$1 and s.id_hash=$2 and s.revoked_at is null "
                "and s.expires_at>now() and u.status='active'",id,session_hash)
        if not row or row['kind']!='cli_subscription' or app.state.launcher is None:
            await ws.close(code=4404); return
        if launcher_waiting():
            await ws.close(code=1013,reason='launcher_unavailable'); return
        owner_id=str(row['owner_id']); key=(owner_id,row['cli'])
        if key in active_login:
            await ws.close(code=4409); return
        active_login[key]=None
        try:
            await app.state.launcher.create_login_container(owner_id)
            sid=await app.state.launcher.open_login_session(owner_id,command=row['cli'])
        except Exception:
            active_login.pop(key,None)
            await ws.close(code=1011); return
        except asyncio.CancelledError:
            active_login.pop(key,None)
            raise
        queue=asyncio.Queue()
        active_login[key]=(queue,sid)
        active_ws.setdefault(session_hash,set()).add(queue)
        active_user_ws.setdefault(owner_id,set()).add(queue)
        log.info('provider_login_start',extra={'provider_id':str(id),'user_id':owner_id})
        try:
            await ws.accept()
        except BaseException:
            cleanup_task=asyncio.create_task(app.state.launcher.close_login_session(sid))
            try:
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
            finally:
                active_login.pop(key,None)
                active_ws.get(session_hash,set()).discard(queue)
                active_user_ws.get(owner_id,set()).discard(queue)
            raise
        started=last_input=asyncio.get_running_loop().time()
        sent_window=0; window_started=started; exit_code=None
        incoming=asyncio.create_task(ws.receive())
        output_stream=app.state.launcher.login_output(sid)
        output=asyncio.create_task(anext(output_stream))
        revoked=asyncio.create_task(queue.get())
        try:
            while True:
                now=asyncio.get_running_loop().time()
                if now-started>LOGIN_TOTAL_TIMEOUT or now-last_input>LOGIN_IDLE_TIMEOUT:
                    await ws.close(code=1001); break
                done,_=await asyncio.wait((incoming,output,revoked),timeout=5,return_when=asyncio.FIRST_COMPLETED)
                if revoked in done:
                    await ws.close(code=revoked.result()[WS_CLOSE]); break
                message = incoming.result() if incoming in done else None
                payload = {}
                if message is not None:
                    if message['type']=='websocket.disconnect': break
                    if message.get('text') is not None:
                        try: payload=json.loads(message['text'])
                        except ValueError: pass
                        if not isinstance(payload,dict): payload={}
                        if payload.get('t')=='close': break
                async with app.state.pool.acquire() as con:
                    valid=await con.fetchval("select 1 from bothub.sessions s join bothub.users u on u.id=s.user_id where s.id_hash=$1 and s.revoked_at is null and s.expires_at>now() and u.status='active'",session_hash)
                if not valid:
                    await ws.close(code=4401); break
                if output in done:
                    try: frame=output.result()
                    except StopAsyncIteration: break
                    if isinstance(frame,ExecExit):
                        exit_code=frame.code
                        await ws.send_json({'t':'exit','code':exit_code})
                        break
                    if isinstance(frame,ExecChunk):
                        if now-window_started>=1: sent_window=0; window_started=now
                        sent_window+=len(frame.data)
                        if sent_window>1024*1024:
                            await ws.close(code=1009); break
                        await ws.send_bytes(frame.data)
                    output=asyncio.create_task(anext(output_stream))
                if incoming in done:
                    last_input=now
                    if message.get('bytes') is not None:
                        if len(message['bytes'])>65536: await ws.close(code=1009); break
                        await app.state.launcher.login_input(sid,message['bytes'])
                    elif message.get('text') is not None:
                        if payload.get('t')=='resize' and type(payload.get('cols')) is int and type(payload.get('rows')) is int and 20<=payload['cols']<=300 and 5<=payload['rows']<=100:
                            await app.state.launcher.login_resize(sid,payload['cols'],payload['rows'])
                        else: await ws.close(code=1003); break
                    else: await ws.close(code=1003); break
                    incoming=asyncio.create_task(ws.receive())
        except WebSocketDisconnect:
            pass
        finally:
            for task in (incoming,output,revoked): task.cancel()
            async def cleanup():
                await asyncio.gather(incoming,output,revoked,return_exceptions=True)
                try:
                    await app.state.launcher.close_login_session(sid)
                except Exception:
                    log.exception('provider_login_close_failed',extra={'provider_id':str(id)})
                finally:
                    if active_login.get(key)==(queue,sid): active_login.pop(key,None)
                    active_ws.get(session_hash,set()).discard(queue)
                    active_user_ws.get(owner_id,set()).discard(queue)
                    log.info('provider_login_end',extra={'provider_id':str(id),'user_id':owner_id,'code':exit_code})
            cleanup_task=asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise
        if exit_code is not None:
            async with app.state.pool.acquire() as con:
                await con.execute('update bothub.providers set last_check_at=null where id=$1 and owner_id=$2',id,row['owner_id'])
            checked = await check_provider_record(id,row['owner_id'])
            if checked['status']=='ok':
                await refresh_subscription_bots(id,row['owner_id'])

    @app.get('/api/bots')
    async def bots(request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            return [data(r) | {'recreate_url':f"/api/bots/{r['id']}/recreate" if r['status']=='error_starting' else None}
                    for r in await con.fetch('select * from bothub.bots where owner_id=$1 order by created_at',who['user_id'])]

    async def own_browser_bot(con, bot_id: str, who):
        row = await con.fetchrow('select * from bothub.bots where id=$1 and owner_id=$2', bot_id, who['user_id'])
        if not row:
            error('not_found', 404)
        return row

    async def browser_transition(bot_id: str, who, action: str, reason: str):
        async with browser_locks.setdefault(bot_id,asyncio.Lock()):
            return await _browser_transition_unlocked(bot_id,who,action,reason)

    async def takeover_url(con, bot_id: str) -> str:
        """Address the human gets in the clean Chromium: the bot's first page tab, else the last address the bot
        navigated to (already reduced to scheme://host/path in the event), else about:blank. The host is opened and
        reported as punycode; a forbidden address, or one with hidden or direction-changing characters, becomes
        about:blank. An unusable address never blocks the takeover."""
        url = None
        try:
            tab = await app.state.launcher.browser_tab(bot_id)
            if isinstance(tab.get('url'), str):
                url = tab['url']
        except LauncherError:
            pass
        if not url:
            url = await con.fetchval("select e.payload->>'url' from bothub.events e "
                                     "join bothub.threads th on th.id=e.thread_id "
                                     "where th.bot_id=$1 and e.kind='browser_step' and e.payload->>'action'='navigate' "
                                     "and e.payload->>'url' is not null order by e.ts desc,e.seq desc limit 1",bot_id)
        return human_url(url) or 'about:blank'

    async def _browser_transition_unlocked(bot_id: str, who, action: str, reason: str):
        retry_return = False
        freeze_attempted = False
        freeze_failed = False
        mode_attempted = False
        mode_failed = False
        opened_url = None
        async def reconcile_freeze():
            try:
                async with app.state.pool.acquire() as con:
                    async with con.transaction():
                        state = await con.fetchval('select browser_control from bothub.bots '
                                                   'where id=$1 and owner_id=$2 for update',bot_id,who['user_id'])
                        if state in ('bot','returning'):
                            # The bot's own Chromium (CDP) must be back before the bot runs again; if it cannot be
                            # restored the bot stays frozen.
                            if mode_attempted:
                                try:
                                    await app.state.launcher.browser_mode(bot_id,'bot')
                                except Exception:
                                    log.exception('browser_mode_reconcile_failed', extra={'bot_id': bot_id})
                                    return
                            await app.state.launcher.unfreeze_bot(bot_id)
            except Exception:
                log.exception('browser_freeze_reconcile_failed', extra={'bot_id': bot_id})
        async def run_reconcile():
            task = asyncio.create_task(reconcile_freeze())
            background.add(task)
            task.add_done_callback(background.discard)
            await asyncio.shield(task)
        try:
            async with app.state.pool.acquire() as con:
                async with con.transaction():
                    row = await con.fetchrow('select * from bothub.bots where id=$1 and owner_id=$2 for update',bot_id,who['user_id'])
                    if not row:
                        error('not_found',404)
                    if launcher_waiting():
                        # После проверки владельца и существования: чужому бота не видно, а до стартовой сверки
                        # состояние browser_control не сверено с лаунчером, перехват и возврат ждут её.
                        error('launcher_unavailable',503)
                    retry_return = action == 'return' and row['browser_control'] == 'returning'
                    if retry_return:
                        next_state = 'returning'
                    else:
                        try:
                            next_state = transition(row['browser_control'], action)
                        except ValueError:
                            error('invalid_transition',409)
                    if action == 'takeover':
                        if app.state.launcher is None:
                            error('launcher_unavailable',503)
                        # The address is read before the freeze: afterwards the bot's page is no longer reachable.
                        opened_url = await takeover_url(con,bot_id)
                        try:
                            freeze_attempted = True
                            await app.state.launcher.freeze_bot(bot_id)
                        except LauncherError:
                            # The bot may still run: the human gets no input path (state stays), the thread gets the event.
                            freeze_failed = True
                            next_state = row['browser_control']
                        if not freeze_failed:
                            try:
                                mode_attempted = True
                                await app.state.launcher.browser_mode(bot_id,'human',url=opened_url)
                            except LauncherError:
                                # No clean Chromium for the human: the bot gets its browser and its processes back.
                                mode_failed = True
                                next_state = row['browser_control']
                    elif action == 'return':
                        if app.state.launcher is None:
                            error('launcher_unavailable',503)
                    if not retry_return:
                        if not (freeze_failed or mode_failed):
                            await con.execute('update bothub.bots set browser_control=$2 where id=$1',bot_id,next_state)
                            if action in ('takeover','return'):
                                await con.execute("update bothub.approvals set status='expired',decided_at=now() "
                                                  "where bot_id=$1 and status in ('pending','approved') and "
                                                  "(starts_with(tool,'browser_') or starts_with(tool,'mcp__bothub__browser') "
                                                  "or starts_with(tool,'mcp__playwright__'))",bot_id)
                        thread = await con.fetchrow('select id from bothub.threads where bot_id=$1 order by created_at desc limit 1',bot_id)
                        if not thread:
                            thread = await con.fetchrow("insert into bothub.threads(bot_id,owner_id,title) values($1,$2,'Browser') returning id",
                                                        bot_id,who['user_id'])
                        payload = {'from':row['browser_control'],'to':next_state,'by':str(who['user_id']),
                                   'reason':'freeze_failed' if freeze_failed else 'browser_mode_failed' if mode_failed else reason}
                        if action == 'takeover' and not (freeze_failed or mode_failed):
                            payload['url'] = browser_safe_url(opened_url)
                        await append_event(con,thread['id'],None,'browser_control','owner' if who['kind']=='user' else f'bot:{bot_id}',payload)
        except (Exception, asyncio.CancelledError):
            if freeze_attempted and app.state.launcher is not None:
                await run_reconcile()
            raise
        if freeze_failed or mode_failed:
            await run_reconcile()
            if freeze_failed:
                error('freeze_failed',502,'bot processes could not be frozen, control stays with the bot')
            error('browser_mode_failed',502,'clean browser could not be started, control stays with the bot')
        if not retry_return:
            browser_since[bot_id] = datetime.now(NOW)
            browser_by[bot_id] = str(who['user_id'])
            for old_id, authorization in tuple(browser_authorizations.items()):
                if authorization[0]==bot_id:
                    browser_authorizations.pop(old_id,None)
        if action == 'return':
            # `returning` is already stored and gives the human no input path. Order matters: the human's Chromium goes
            # away and the bot's CDP Chromium returns before the bot runs again. A failure leaves the state `returning`:
            # a repeated return runs both calls again and finishes the job, while `returning -> human` needs a new
            # takeover, which freezes the bot first.
            try:
                await app.state.launcher.browser_mode(bot_id,'bot')
                await app.state.launcher.unfreeze_bot(bot_id)
            except LauncherError:
                error('launcher_failed',502)
        if retry_return:
            return {'state':'returning','since':browser_since.setdefault(bot_id,row['created_at']),
                    'by':browser_by.get(bot_id)}
        result = {'state':next_state,'since':browser_since[bot_id],'by':browser_by[bot_id]}
        if action == 'takeover':
            result['url'] = opened_url
        return result

    @app.get('/api/bots/{id}/browser')
    async def browser_state(id: str, request: Request):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            row=await own_browser_bot(con,id,who)
        return {'state':row['browser_control'],'since':browser_since.setdefault(id,row['created_at']),
                'by':browser_by.get(id)}

    @app.post('/api/bots/{id}/browser/takeover')
    async def browser_takeover(id: str, request: Request):
        who=await principal(request,owner=True)
        async with browser_locks.setdefault(id,asyncio.Lock()):
            result=await _browser_transition_unlocked(id,who,'takeover','takeover')
            try:
                async with app.state.pool.acquire() as con:
                    turns=await con.fetch("select t.id from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                                          "where th.bot_id=$1 and t.status in ('running','waiting_approval','waiting_mac')",id)
                for turn in turns:
                    stopped=await stop_turn(turn['id'],'browser_takeover',close_browser_screen=False)
                    if stopped and stopped.get('status') not in ('stopped','done'):
                        error('turn_stop_failed',502)
            except Exception:
                error('turn_stop_failed',502)
        return result

    @app.post('/api/bots/{id}/browser/return')
    async def browser_return(id: str, request: Request):
        who=await principal(request,owner=True)
        return await browser_transition(id,who,'return','return')

    @app.post('/api/bots/{id}/browser/secret-input')
    async def browser_secret_input(id: str, body: SecretInputIn, request: Request):
        who=await principal(request,owner=True)
        async with browser_locks.setdefault(id,asyncio.Lock()):
            async with app.state.pool.acquire() as con:
                row=await own_browser_bot(con,id,who)
                if launcher_waiting():
                    error('launcher_unavailable',503)  # после 404 чужому и разбора тела
                if row['browser_control']!='human':
                    error('human_required',409)
            screen=active_screen.get(id)
            if not screen or id not in screen_ready:
                error('screen_required',409)
            # RFB KeyEvent uses X11 keysyms; Unicode uses the UCS-4 keysym range.
            if any(not char.isprintable() or 0xD800 <= ord(char) <= 0xDFFF for char in body.value):
                error('invalid',400,'secret-input contains control characters')
            sid=screen[1]
            events=bytearray()
            for char in body.value:
                codepoint=ord(char)
                key=codepoint if codepoint<256 else 0x01000000 | codepoint
                events.extend(struct.pack('!BBHI',4,1,0,key)+struct.pack('!BBHI',4,0,0,key))
            await app.state.launcher.screen_input(sid,bytes(events))
            if body.save_as:
                secret_id=uuid.uuid4()
                ciphertext=encrypt_secret(body.value.encode(),secret_id.bytes)
                async with app.state.pool.acquire() as con:
                    await con.execute('insert into bothub.secrets(id,owner_id,bot_id,name,value_encrypted) '
                                      'values($1,$2,$3,$4,$5) on conflict(owner_id,bot_id,name) '
                                      'do update set id=excluded.id,value_encrypted=excluded.value_encrypted',
                                      secret_id,who['user_id'],id,body.save_as,ciphertext)
        return {'ok':True}

    async def browser_bot_call(body: BrowserCallIn, request: Request):
        who=await principal(request,bot=True)
        if who['kind']!='bot':
            error('forbidden',403)
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow("select b.browser_control,b.provider,b.executor from bothub.turns t "
                                   "join bothub.threads th on th.id=t.thread_id "
                                   "join bothub.bots b on b.id=th.bot_id where t.id=$1 and th.id=$2 "
                                   "and th.bot_id=$3 and t.status in ('running','waiting_approval','waiting_mac')",
                                   body.turn_id,body.thread_id,who['bot_id'])
        if not row:
            error('not_found',404)
        if row['provider'] not in ('claude','fake') or row['executor']!='container':
            error('browser_unavailable',409)
        state=row['browser_control']
        if body.action=='navigate' and (reason:=url_forbidden(body.url)):
            error('url_forbidden',403,reason)
        if state=='human':
            error('human_in_control',409)
        # После возврата от человека бот сначала смотрит страницу (snapshot) или открывает новую (navigate):
        # клик и ввод вслепую по старой странице не разрешаются.
        if state=='returning' and body.action not in ('snapshot','navigate'):
            error('browser_stale',409)
        require_launcher()  # ни разрешения на шаг, ни доступа к браузеру до стартовой сверки состояния с лаунчером
        try:
            await app.state.launcher.ensure_browser(who['bot_id'])
        except LauncherError:
            error('launcher_failed',502)
        return who,state

    @app.post('/api/browser/authorize')
    async def browser_authorize(body: BrowserCallIn, request: Request):
        who,state=await browser_bot_call(body,request)
        if state=='returning':
            now=asyncio.get_running_loop().time()
            for old_id, authorization in tuple(browser_authorizations.items()):
                if authorization[3]<now:
                    browser_authorizations.pop(old_id,None)
            authorization_id=uuid.uuid4()
            browser_authorizations[authorization_id]=(who['bot_id'],body.thread_id,body.turn_id,
                                                       now+60)
            return {'ok':True,'authorization_id':str(authorization_id)}
        return {'ok':True}

    @app.post('/api/browser/step')
    async def browser_step(body: BrowserCallIn, request: Request):
        who,state=await browser_bot_call(body,request)
        if state=='returning':
            expected=browser_authorizations.pop(body.authorization_id,None)
            if expected is None or expected[:3] != (who['bot_id'],body.thread_id,body.turn_id) or expected[3]<asyncio.get_running_loop().time():
                error('browser_authorization_required',409)
        async with app.state.pool.acquire() as con:
            # Never persist fill values or arbitrary tool output, which can contain passwords. Role and name of the
            # element are not secret (the label of a field, not what is typed into it) and feed the procedures.
            name=mask_browser_text(body.name.strip()) if body.name is not None else None
            payload={'action':body.action,'target':'[redacted]' if body.action=='fill' else body.target,
                     'url':browser_safe_url(body.url) if body.action=='navigate' else None,
                     'value':'[redacted]' if body.action=='fill' else None,'result':body.result,
                     'role':body.role if body.action in ('click','fill') else None,
                     'name':name if body.action in ('click','fill') else None}
            if body.action=='fill':
                payload['secret']=procedures.secret_field(body.role,name)
            await append_event(con,body.thread_id,body.turn_id,'browser_step',f"bot:{who['bot_id']}",payload)
        if state=='returning' and body.action in ('snapshot','navigate') and body.result=='ok':
            await browser_transition(who['bot_id'],who,'snapshot','snapshot')
        return {'ok':True}

    @app.post('/api/bots/draft')
    async def draft_bot(body: DraftIn, request: Request):
        # Раздел 9: черновик от модели (drafter) - в проде docker exec claude в
        # контейнере bot-*, в тестах/dev - drafter из create_app(). Любая ошибка
        # оттуда (не JSON, CLI упал, таймаут) - 502 builder_failed.
        who = await principal(request, owner=True)
        if launcher_waiting(): error('launcher_unavailable',503)
        try:
            try:
                raw = await drafter(body.description, str(who['user_id']))
            except TypeError:
                raw = await drafter(body.description)
        except Exception:
            error('builder_failed', 502)
        if not isinstance(raw, dict):
            error('builder_failed', 502)
        async with app.state.pool.acquire() as con:
            existing = {row['id'] for row in await con.fetch('select id from bothub.bots where owner_id=$1',who['user_id'])}
        return validate_draft(raw, existing)

    @app.post('/api/bots')
    async def add_bot(body: BotIn, request: Request):
        who = await principal(request, owner=True)
        if not body.provider_id:
            check_provider(body.provider)
        elif not body.model_id: error('invalid')
        schedule = body.schedule
        if schedule:
            for field, limit in (('name',512),('prompt',16*1024)):
                value = schedule.get(field)
                if value is not None and (not isinstance(value,str) or len(value)>limit):
                    error('invalid',400,field)
            cron = schedule.get('cron')
            tz = schedule.get('timezone') or 'Europe/Moscow'
            if not isinstance(tz, str):
                error('invalid', 400, 'timezone')
            try:
                check_timezone(tz)
            except Unprocessable as exc:
                error('invalid', 422, str(exc))
            if not isinstance(schedule.get('prompt'), str) or not schedule['prompt'].strip():
                error('invalid')
            try:
                next_at = next_run(cron, tz)
            except Exception:
                error('invalid')
        fields = body.model_dump(exclude={'schedule', 'start_container', 'skip_container'})
        fields['owner_id'] = who['user_id']
        if body.provider_id:
            fields['registry_bound'] = True
        async with app.state.pool.acquire() as con:
            if body.id:
                # Повтор запроса, пока бот запускается (ответ потерялся по дороге): тот же бот, не дубль и не второй запуск.
                # Только свой бот и только в starting; чужой или готовый id молча игнорируется, как и раньше.
                replay = await con.fetchrow("select * from bothub.bots where id=$1 and owner_id=$2 and status='starting'",body.id,who['user_id'])
                if replay:
                    return {**data(replay), 'container': 'starting', 'recreate_url': None}
            if not body.provider_id:
                has_registry = await con.fetchval('select exists(select 1 from bothub.providers where owner_id=$1)',who['user_id'])
                # Бот без модели создаётся только в режиме совместимости (установка, обновлённая через OWNER_TOKEN) и пока реестр владельца пуст.
                if body.provider != 'fake' and (has_registry or not await legacy_enabled(con)):  # fake: тестовый раннер без модели
                    error('invalid',400,'provider_id required')
            if body.provider_id:
                binding=await con.fetchrow('select p.kind,p.cli,p.status,m.name from bothub.providers p join bothub.models m on m.provider_id=p.id and m.id=$3 and m.enabled=true where p.id=$1 and p.owner_id=$2',body.provider_id,who['user_id'],body.model_id)
                if not binding or binding['status']!='ok': error('invalid',400,'provider/model unavailable')
                fields['provider']=runner_provider(binding['kind'],binding['cli'])
                fields['model']=binding['name']
            starts_container = body.start_container and not body.skip_container and os.getenv('BOTHUB_RUNNER_EXEC', 'local') == 'docker'
            if starts_container and launcher_waiting():
                error('launcher_unavailable',503)  # после проверки полей и привязки провайдера, до записи бота
            if starts_container:
                fields['status'] = 'starting'  # контейнер создаётся фоном после ответа (start_new_bot)
            for _ in range(5):
                fields['id'] = body.id if os.getenv('BOTHUB_TEST_LEGACY_IDS')=='1' and body.id else auth.new_bot_id(body.name)
                try:
                    keys = list(fields)
                    vals = [canonical(fields[k]) if k in ('auto_allow','mcp_allow') else fields[k] for k in keys]
                    sql = 'insert into bothub.bots ('+','.join(keys)+') values ('+','.join(f'${i+1}::jsonb' if k in ('auto_allow','mcp_allow') else f'${i+1}' for i,k in enumerate(keys))+') returning *'
                    bot = data(await con.fetchrow(sql, *vals))
                except asyncpg.UniqueViolationError:
                    if os.getenv('BOTHUB_TEST_LEGACY_IDS')=='1' and body.id: error('conflict',409)
                    continue
                break
            else: error('conflict',409)
            if schedule:
                await con.execute(
                    "insert into bothub.schedules(bot_id,name,kind,cron,timezone,prompt,enabled,next_run_at,owner_id) "
                    "values($1,$2,'cron',$3,$4,$5,true,$6,$7)",
                    bot['id'], schedule.get('name') or bot['name'], cron, tz, schedule.get('prompt', ''), next_at,who['user_id'])
            if not starts_container:
                return {**bot, 'container': 'skipped', 'recreate_url': None}
        # Ответ уходит сразу (201, id итоговый, status starting): сеть, контейнер и браузер создаёт фон, статус станет idle или
        # error_starting. Первый бот пользователя создаёт сеть и подключает к ней ядро; Docker на этом рвёт соединения.
        spawn_bot_start(bot['id'])
        return JSONResponse(jsonable_encoder({**bot, 'container': 'starting', 'recreate_url': None}), status_code=201)

    @app.patch('/api/bots/{id}')
    async def edit_bot(id: str, request: Request, body: JsonObject):
        # Находка 16: частичная pydantic-модель вместо голого dict - несовпадение
        # типов (например, mac_full_control="да") раньше валило 500 из asyncpg,
        # а не 400 invalid.
        who = await principal(request, owner=True)
        try:
            patch = BotPatch.model_validate(body)
        except ValidationError as exc:
            error('invalid', validation_status(exc), validation_detail(exc))  # текст самой ValidationError несёт значения полей
        fields = patch.model_dump(exclude_unset=True)
        if not fields:
            error('invalid')
        if 'provider' in fields:
            if fields.get('provider_id') is None: check_provider(fields['provider'])
        async with app.state.pool.acquire() as con:
            old=await con.fetchrow('select * from bothub.bots where id=$1 and owner_id=$2',id,who['user_id'])
            if not old: error('not_found',404)
            if old['provider_id'] and ('provider' in fields or 'model' in fields) and 'provider_id' not in fields and 'model_id' not in fields:
                error('invalid',400,'change provider_id and model_id together')
            if 'provider_id' in fields or 'model_id' in fields:
                provider_id=fields.get('provider_id',old['provider_id'])
                model_id=fields.get('model_id',old['model_id'])
                if bool(provider_id)!=bool(model_id): error('invalid')
                if provider_id:
                    binding=await con.fetchrow('select p.kind,p.cli,p.status,m.name from bothub.providers p join bothub.models m on m.provider_id=p.id and m.id=$3 and m.enabled=true where p.id=$1 and p.owner_id=$2',provider_id,who['user_id'],model_id)
                    if not binding or binding['status']!='ok': error('invalid',400,'provider/model unavailable')
                    fields['provider']=runner_provider(binding['kind'],binding['cli'])
                    fields['model']=binding['name']
                    fields['registry_bound']=True
                else:
                    if old['provider_id']:
                        error('invalid',400,'bound bot cannot be unbound')
                    check_provider(fields.get('provider',old['provider']))
                if provider_id!=old['provider_id'] or model_id!=old['model_id']:
                    fields['provider_id']=provider_id; fields['model_id']=model_id
                    if provider_id and old['status']=='no_model':
                        fields['status']='idle'
            binding_change = any(key in fields and fields[key] != old[key]
                                 for key in ('provider_id','model_id','provider','model'))
            vals = [canonical(v) if k in ('auto_allow','mcp_allow') else v for k,v in fields.items()]
            sets = ','.join(f'{k}=${i+2}'+('::jsonb' if k in ('auto_allow','mcp_allow') else '') for i,k in enumerate(fields))
            async with con.transaction():
                if binding_change:
                    # Serialize with claim_turn/resume_turn so a new active turn cannot race this check.
                    await con.execute("select pg_advisory_xact_lock(hashtext('bothub-claim-turn'))")
                    active = await con.fetchval(
                        "select exists(select 1 from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                        "where th.bot_id=$1 and th.owner_id=$2 "
                        "and t.status in ('running','waiting_approval','waiting_mac'))", id, who['user_id'])
                    if active: error('conflict',409,'bot has an active turn')
                row = await con.fetchrow(f'update bothub.bots set {sets} where id=$1 and owner_id=${len(vals)+2} returning *', id, *vals,who['user_id'])
                if row and (row['provider_id']!=old['provider_id'] or row['model_id']!=old['model_id']):
                    await con.execute('update bothub.threads set cli_session_id=null,context_tokens=null,context_window=null,context_base_tokens=0,context_base_seq=last_seq where bot_id=$1 and owner_id=$2',id,who['user_id'])
            if not row: error('not_found',404)
            return data(row)

    @app.delete('/api/bots/{id}')
    async def delete_bot(id: str, request: Request):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                row=await con.fetchrow('select id from bothub.bots where id=$1 and owner_id=$2 for update',id,who['user_id'])
                if not row: error('not_found',404)
                related=await con.fetchval(
                    'select exists(select 1 from bothub.threads where bot_id=$1) '
                    'or exists(select 1 from bothub.approvals where bot_id=$1) '
                    'or exists(select 1 from bothub.memory where bot_id=$1) '
                    'or exists(select 1 from bothub.usage where bot_id=$1) '
                    'or exists(select 1 from bothub.schedules where bot_id=$1)',id)
                if related: error('conflict',409,'bot has related records')
                if launcher_waiting(): error('launcher_unavailable',503)
                close_screen(id)  # the container goes away: no stream to a removed computer, not even until the timer
                if app.state.launcher is not None:
                    try:
                        removed=await app.state.launcher.remove_bot(id)
                    except Exception:
                        error('launcher_failed',502)
                try:
                    await con.execute('delete from bothub.bots where id=$1 and owner_id=$2',id,who['user_id'])
                except asyncpg.ForeignKeyViolationError:
                    error('conflict',409,'bot has related records')
        return {'ok':True}

    @app.post('/api/bots/{id}/recreate')
    async def recreate_bot(id: str, request: Request):
        who=await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub-claim-turn'))")
                row=await con.fetchrow('select * from bothub.bots where id=$1 and owner_id=$2 for update',id,who['user_id'])
                if not row: error('not_found',404)
                active=await con.fetchval("select exists(select 1 from bothub.turns t join bothub.threads th on th.id=t.thread_id where th.bot_id=$1 and t.status in ('running','waiting_approval','waiting_mac'))",id)
                if active: error('conflict',409,'завершите или остановите активный turn')
                require_launcher()
                close_screen(id)  # the old container is replaced: the stream and the typed input must not outlive it
                try:
                    existing=await app.state.launcher.status(id)
                    if existing.exists and existing.owner_id != str(row['owner_id']):
                        error('conflict',409,'container owner mismatch')
                    status=await (app.state.launcher.recreate_bot(id) if existing.exists else app.state.launcher.create_bot(id,str(row['owner_id'])))
                except HTTPException:
                    raise
                except Exception:
                    error('launcher_failed',502)
                await con.execute("update bothub.bots set status=case when status='no_model' or (registry_bound and provider_id is null) then 'no_model' else 'idle' end,need_restart=false,stop_retry_exec_id=null where id=$1 and owner_id=$2",id,who['user_id'])
        return {'ok':True,'container':status.container}

    # --- Пауза бота и лента активности (раздел 16) ---
    async def bot_pause_view(con, bot_id):
        bot = await con.fetchrow('select id,paused,paused_at,paused_reason from bothub.bots where id=$1', bot_id)
        running = await con.fetchval("select t.id from bothub.turns t join bothub.threads th on th.id=t.thread_id "
                                     "where th.bot_id=$1 and t.status in ('running','waiting_approval','waiting_mac') "
                                     "order by t.created_at desc limit 1", bot_id)
        return jsonable_encoder({'bot_id': bot['id'], 'paused': bot['paused'], 'paused_at': bot['paused_at'],
                                 'paused_reason': bot['paused_reason'], 'running_turn': running})

    async def set_bot_paused(con, who, bot_id, paused: bool, reason: str | None, *, by_all=False) -> bool:
        """Меняет паузу бота владельца; True, если состояние сменилось (тогда событие в ленту). Повтор ничего не пишет.
        Возобновление возвращает запускам процедур бота время паузы в срок ожидания человека (раздел 16)."""
        async with con.transaction():
            since = None if paused else await con.fetchval('select paused_at from bothub.bots where id=$1 and owner_id=$2 and paused for update', bot_id, who['user_id'])
            row = await con.fetchrow('update bothub.bots set paused=$3,paused_at=case when $3 then now() else null end,paused_reason=case when $3 then $4 else null end '
                                     'where id=$1 and owner_id=$2 and paused is distinct from $3 returning id', bot_id, who['user_id'], paused, reason)
            if row:
                await log_activity(con, who['user_id'], bot_id, 'pause', 'bot_paused' if paused else 'bot_resumed',
                                   ({'reason': reason} if paused and reason else {}) | ({'by_all': True} if by_all else {}))
                if since is not None:
                    await procedure_store.thaw_waits(con, bot_id, since)
            return bool(row)

    def pause_reason(body):
        return ' '.join(body.reason.split()) or None if body and body.reason else None

    @app.post('/api/bots/pause-all')
    async def pause_all(request: Request, body: PauseIn | None = None):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            ids = [row['id'] for row in await con.fetch('select id from bothub.bots where owner_id=$1 order by created_at,id', who['user_id'])]
            for bot_id in ids:
                await set_bot_paused(con, who, bot_id, True, pause_reason(body), by_all=True)
            return {'bots': [await bot_pause_view(con, bot_id) for bot_id in ids]}

    @app.post('/api/bots/resume-all')
    async def resume_all(request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            ids = [row['id'] for row in await con.fetch('select id from bothub.bots where owner_id=$1 order by created_at,id', who['user_id'])]
            for bot_id in ids:
                await set_bot_paused(con, who, bot_id, False, None, by_all=True)
            return {'bots': [await bot_pause_view(con, bot_id) for bot_id in ids]}

    @app.post('/api/bots/{id}/pause')
    async def pause_bot(id: str, request: Request, body: PauseIn | None = None):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            await own_browser_bot(con, id, who)
            await set_bot_paused(con, who, id, True, pause_reason(body))
            return await bot_pause_view(con, id)

    @app.post('/api/bots/{id}/resume')
    async def resume_bot(id: str, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            await own_browser_bot(con, id, who)
            await set_bot_paused(con, who, id, False, None)
            return await bot_pause_view(con, id)

    @app.get('/api/activity')
    async def activity_feed(request: Request, bot_id: str | None = None, kind: str | None = None,
                            before: str | None = None, limit: str | None = None):
        who = await principal(request, owner=True)
        try:
            kinds = activity.parse_kinds(kind)
            size = activity.parse_limit(limit)
            cursor = activity.decode_cursor(before) if before is not None else None
        except activity.ActivityError as exc:
            error('invalid', 422, str(exc))
        if bot_id is not None and len(bot_id) > 64:
            error('invalid', 422, 'bot_id')
        args = (who['user_id'], bot_id, cursor[0] if cursor else None, cursor[1] if cursor else None, size + 1)
        groups = []
        async with app.state.pool.acquire() as con:
            for source in activity.sources_for(kinds):
                extra = (activity.log_kinds_for(kinds),) if source == 'log' else ()
                groups.append([activity.build_item(source, row) for row in await con.fetch(activity.SQL[source], *args, *extra)])
        page, next_cursor = activity.merge_page(groups, size, cursor)
        return activity.finish_page(page, next_cursor)

    @app.get('/api/threads')
    async def threads(request: Request, bot_id: str | None = None, status: str | None = None):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            return [thread_view(r) for r in await con.fetch('select * from bothub.threads where owner_id=$3 and ($1::text is null or bot_id=$1) and ($2::text is null or status=$2) order by last_seq desc,created_at desc', bot_id,status,who['user_id'])]

    @app.get('/api/threads/{id}')
    async def get_thread(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            return thread_view(await own_thread(con, id, who))

    @app.post('/api/threads')
    async def add_thread(body: ThreadIn, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            try:
                row=await con.fetchrow('insert into bothub.threads(bot_id,title,kind,dry_run,owner_id) select id,$2,$3,$4,owner_id from bothub.bots where id=$1 and owner_id=$5 returning *', body.bot_id,body.title,body.kind,body.dry_run,who['user_id'])
                if not row: error('not_found',404)
                return data(row)
            except asyncpg.ForeignKeyViolationError: error('not_found',404)

    @app.patch('/api/threads/{id}')
    async def edit_thread(id: uuid.UUID, request: Request, body: JsonObject):
        who = await principal(request, owner=True)
        if not body or set(body)-{'status','dry_run','title'} or body.get('status','archived') != 'archived': error('invalid')
        if 'title' in body and (not isinstance(body['title'],str) or len(body['title'])>512): error('invalid',400,'title')
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('update bothub.threads set status=coalesce($2,status),dry_run=coalesce($3,dry_run),title=coalesce($4,title) where id=$1 and owner_id=$5 returning *', id,body.get('status'),body.get('dry_run'),body.get('title'),who['user_id'])
            if not row: error('not_found',404)
            return data(row)

    @app.post('/api/threads/{id}/turns')
    async def add_turn(id: uuid.UUID, body: TurnIn, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            thread = await own_thread(con,id,who)
            if thread['status'] != 'active': error('conflict',409)
            await require_available_bot(con,thread['bot_id'])
            turn = await create_turn(con,id,body.prompt,body.client)
            if await con.fetchval('select paused from bothub.bots where id=$1',thread['bot_id']):
                # Раздел 16: сообщение принято и ждёт возобновления; одно системное событие на turn
                await append_event(con,id,uuid.UUID(turn['id']),'system','system',{'text':activity.BOT_PAUSED_TEXT,'code':'bot_paused'})
            return turn

    @app.post('/api/threads/{id}/compact')
    async def compact_thread(id: uuid.UUID, request: Request, body: CompactIn | None = None):
        """Раздел 15: ручное сжатие. Ход сжатия невидим и идёт через очередь, как обычный."""
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                # Та же блокировка, что у claim_turn и recreate: новый ход не проскочит между проверкой и вставкой.
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub-claim-turn'))")
                thread = await own_thread(con, id, who)
                if thread['status'] != 'active': error('conflict', 409, 'thread is archived')
                await require_available_bot(con, thread['bot_id'])
                if await con.fetchval("select exists(select 1 from bothub.turns where thread_id=$1 and status in ('queued','running','waiting_approval','waiting_mac'))", id):
                    error('conflict', 409, 'thread has an active turn')
                if not thread['cli_session_id']:
                    error('conflict', 409, 'nothing to compact')
                return await create_compact_turn(con, id, 'api')

    @app.post('/api/turns/{id}/stop')
    async def stop(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            if not await con.fetchval('select 1 from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and th.owner_id=$2',id,who['user_id']): error('not_found',404)
        result = await stop_turn(id)
        if not result: error('not_found',404)
        return result

    @app.get('/api/threads/{id}/events')
    async def events(id: uuid.UUID, request: Request, since: int = 0):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            await own_thread(con,id,who)
            return [data(r) for r in await con.fetch('select e.* from bothub.events e join bothub.threads th on th.id=e.thread_id where e.thread_id=$1 and e.seq>$2 and th.owner_id=$3 order by e.seq limit 500',id,since,who['user_id'])]

    @app.websocket('/api/bots/{id}/screen')
    async def browser_screen(ws: WebSocket, id: str):
        origin=ws.headers.get('origin','')
        scheme=ws.headers.get('x-forwarded-proto') or ('https' if ws.url.scheme=='wss' else 'http')
        if not (auth.same_origin(origin,ws.headers.get('host',''),scheme=scheme) or
                not ws.headers.get('x-forwarded-proto') and ws.url.scheme=='ws' and
                auth.same_origin(origin,ws.headers.get('host',''),scheme='https')):
            await ws.close(code=4401); return
        cookie=ws.cookies.get('bothub_session','')
        if not cookie:
            await ws.close(code=4401); return
        session_hash=auth.token_hash(cookie)
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow("select b.owner_id from bothub.bots b join bothub.sessions s on s.user_id=b.owner_id "
                                   "join bothub.users u on u.id=b.owner_id where b.id=$1 and s.id_hash=$2 "
                                   "and s.revoked_at is null and s.expires_at>now() and u.status='active'",id,session_hash)
        if not row:
            await ws.close(code=4404); return
        if id in active_screen:
            if 'websocket.http.response' in ws.scope.get('extensions', {}):
                await ws.send_denial_response(JSONResponse({'error':'screen_session_conflict'},status_code=409))
            else:
                await ws.close(code=4409,reason='screen_session_conflict')
            return
        if app.state.launcher is None or not app.state.launcher_ready:
            await ws.close(code=1013,reason='launcher_unavailable'); return
        active_screen[id]=None
        sid=None
        queue=asyncio.Queue()
        try:
            try:
                await app.state.launcher.ensure_browser(id)
                sid=await app.state.launcher.open_screen_session(id,str(row['owner_id']))
            except LauncherError:
                await ws.close(code=1013,reason='launcher_open_failed'); return
            active_screen[id]=(queue,sid)
            active_ws.setdefault(session_hash,set()).add(queue)
            active_user_ws.setdefault(str(row['owner_id']),set()).add(queue)
            await ws.accept()
            parser=RFBClientFilter()
            incoming=asyncio.create_task(ws.receive())
            output_iter=app.state.launcher.screen_output(sid)
            output=asyncio.create_task(anext(output_iter))
            revoked=asyncio.create_task(queue.get())
            next_session_check=asyncio.get_running_loop().time()+5
            try:
                while True:
                    done,_=await asyncio.wait((incoming,output,revoked),
                                              timeout=max(0,next_session_check-asyncio.get_running_loop().time()),
                                              return_when=asyncio.FIRST_COMPLETED)
                    if revoked in done:
                        await ws.close(code=revoked.result()[WS_CLOSE]); break
                    if incoming in done:
                        msg=incoming.result()
                        if msg['type']=='websocket.disconnect': break
                        chunk=msg.get('bytes')
                        if chunk is None or len(chunk)>65536:
                            await ws.close(code=1003); break
                        async with browser_locks.setdefault(id,asyncio.Lock()):
                            async with app.state.pool.acquire() as con:
                                state=await con.fetchval('select browser_control from bothub.bots where id=$1 and owner_id=$2',id,row['owner_id'])
                            if state is None:
                                await ws.close(code=4410); break
                            try:
                                allowed=parser.feed(chunk,allow_input=state=='human')
                            except RFBProtocolError:
                                await ws.close(code=1003); break
                            if parser.framebuffer_requested:
                                screen_ready.add(id)
                            for pos in range(0,len(allowed),65536):
                                await app.state.launcher.screen_input(sid,allowed[pos:pos+65536])
                        incoming=asyncio.create_task(ws.receive())
                    if output in done:
                        try:
                            chunk=output.result()
                        except StopAsyncIteration:
                            await ws.close(code=4410); break
                        await ws.send_bytes(chunk)
                        output=asyncio.create_task(anext(output_iter))
                    if asyncio.get_running_loop().time()>=next_session_check:
                        async with app.state.pool.acquire() as con:
                            valid=await con.fetchval("select 1 from bothub.sessions s join bothub.users u on u.id=s.user_id "
                                                     "join bothub.bots b on b.owner_id=u.id where s.id_hash=$1 and b.id=$2 "
                                                     "and s.revoked_at is null and s.expires_at>now() and u.status='active'",session_hash,id)
                        if not valid:
                            await ws.close(code=4401); break
                        next_session_check=asyncio.get_running_loop().time()+5
            finally:
                for task in (incoming,output,revoked):
                    task.cancel()
                await asyncio.gather(incoming,output,revoked,return_exceptions=True)
                await output_iter.aclose()
        except (WebSocketDisconnect, LauncherError):
            pass
        finally:
            if sid:
                with suppress(LauncherError):
                    await app.state.launcher.close_screen_session(sid)
            if active_screen.get(id) in (None,(queue,sid)):
                active_screen.pop(id,None)
            screen_ready.discard(id)
            active_ws.get(session_hash,set()).discard(queue)
            active_user_ws.get(str(row['owner_id']),set()).discard(queue)

    @app.websocket('/api/ws')
    async def ws_events(ws: WebSocket, thread_id: uuid.UUID, since: int = 0):
        token = ws_token(ws)
        origin=ws.headers.get('origin','')
        scheme=ws.headers.get('x-forwarded-proto') or ('https' if ws.url.scheme=='wss' else 'http')
        if not (auth.same_origin(origin,ws.headers.get('host',''),scheme=scheme) or
                not ws.headers.get('x-forwarded-proto') and ws.url.scheme=='ws' and
                auth.same_origin(origin,ws.headers.get('host',''),scheme='https')):
            await ws.close(code=4401); return
        cookie=ws.cookies.get('bothub_session','')
        session_hash=auth.token_hash(cookie) if cookie else None
        async with app.state.pool.acquire() as con:
            if session_hash:
                user=await con.fetchrow("select u.id from bothub.sessions s join bothub.users u on u.id=s.user_id where s.id_hash=$1 and s.revoked_at is null and s.expires_at>now() and u.status='active'",session_hash)
            elif await legacy_enabled(con) and (token_equals(token,os.getenv('OWNER_TOKEN')) or (not token and via_owner_proxy(ws.headers))):
                user=await setup_admin(con)
            else:
                user=None
        if not user:
            await ws.close(code=4401); return
        queue = asyncio.Queue()
        listeners.setdefault(str(thread_id),set()).add(queue)
        if session_hash: active_ws.setdefault(session_hash,set()).add(queue)
        active_user_ws.setdefault(str(user['id']),set()).add(queue)
        try:
            async with app.state.pool.acquire() as con:
                if not await con.fetchval('select 1 from bothub.threads where id=$1 and owner_id=$2',thread_id,user['id']):
                    await ws.close(code=4404); return
                await ws.accept()
                tail = await con.fetch('select e.* from bothub.events e join bothub.threads th on th.id=e.thread_id where e.thread_id=$1 and e.seq>$2 and th.owner_id=$3 order by e.seq limit 500',thread_id,since,user['id'])
            last = since
            for row in tail:
                await ws.send_json(data(row)); last = row['seq']
            incoming = asyncio.create_task(ws.receive())
            next_auth_check=asyncio.get_running_loop().time()+5
            try:
                while True:
                    next_event = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait((incoming, next_event), timeout=max(0,next_auth_check-asyncio.get_running_loop().time()), return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        next_event.cancel()
                        await asyncio.gather(next_event,return_exceptions=True)
                    if asyncio.get_running_loop().time()>=next_auth_check:
                        async with app.state.pool.acquire() as con:
                            if session_hash:
                                valid=await con.fetchval("select 1 from bothub.sessions s join bothub.users u on u.id=s.user_id where s.id_hash=$1 and s.revoked_at is null and s.expires_at>now() and u.status='active'",session_hash)
                            else:
                                valid=await con.fetchval("select 1 from bothub.users where id=$1 and role='admin' and status='active'",user['id'])
                        if not valid:
                            if not next_event.done(): next_event.cancel()
                            await ws.close(code=4401); break
                        next_auth_check=asyncio.get_running_loop().time()+5
                    if incoming in done:
                        message = incoming.result()
                        if not next_event.done():
                            next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)
                        if message['type'] == 'websocket.disconnect':
                            break
                        incoming = asyncio.create_task(ws.receive())
                    if next_event in done:
                        event = next_event.result()
                        if WS_CLOSE in event:
                            await ws.close(code=event[WS_CLOSE]); break
                        if event['seq'] > last:
                            await ws.send_json(event); last = event['seq']
            finally:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        finally:
            listeners[str(thread_id)].discard(queue)
            if session_hash: active_ws.get(session_hash,set()).discard(queue)
            active_user_ws.get(str(user['id']),set()).discard(queue)

    @app.get('/api/approvals')
    async def approvals(request: Request, status: str = 'pending'):
        who = await principal(request, owner=True)
        await expire_approvals()
        async with app.state.pool.acquire() as con:
            return [data(r) for r in await con.fetch('select a.* from bothub.approvals a join bothub.threads th on th.id=a.thread_id where a.status=$1 and th.owner_id=$2 order by a.created_at desc',status,who['user_id'])]

    @app.post('/api/approvals')
    async def add_approval(body: ApprovalIn, request: Request):
        who = await principal(request, bot=True)
        if not who['kind']=='bot': error('forbidden',403)
        async with app.state.pool.acquire() as con:
            thread = await own_thread(con,body.thread_id,who)
            turn_type = None
            if body.turn_id:
                turn_type = await con.fetchval('select t.turn_type from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and t.thread_id=$2 and th.owner_id=$3',body.turn_id,body.thread_id,who['user_id'])
                if turn_type is None: error('not_found',404)
            if turn_type == 'compact':
                # Раздел 15: ход сжатия инструментов не вызывает, значит и подтверждений не просит: ход останавливается,
                # подтверждение не создаётся (ни владельцу, ни в ленту).
                await stop_turn(body.turn_id, 'tool_call_refused', compact_failed={'reason': 'tool_call_refused', 'tool': refused_tool_name(body.tool)})
                error('conflict', 409, 'tool_call_refused')
            bot = await con.fetchrow('select * from bothub.bots where id=$1 and owner_id=$2',thread['bot_id'],who['user_id'])
            # Находки 2/4/5: risk из тела бота игнорируется - сервер считает его
            # сам через decide_permission (bothub.risk.classify + mac_full_control).
            risk, allowed = decide_permission(dict(bot), body.tool, body.args)
            forbidden = forbidden_reason(body.tool, body.args)
            status = 'rejected' if forbidden else 'approved' if allowed else 'pending'
            # typed text and URL secrets are neither stored nor hashed (a hash of a short password is guessable)
            stored_args = mask_browser_args(body.args) if is_browser_tool(body.tool) else body.args
            digest = hashlib.sha256(canonical(stored_args).encode()).hexdigest()
            ttl_minutes = int(os.getenv('APPROVAL_TTL_MINUTES', '60'))  # находка 18
            expires_at = datetime.now(NOW) + timedelta(minutes=ttl_minutes)
            await ping_turn(con,body.turn_id)
            row = await con.fetchrow("insert into bothub.approvals(thread_id,turn_id,bot_id,risk,title,tool,args,args_hash,op_hash,status,expires_at) values($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11) returning *",body.thread_id,body.turn_id,bot['id'],risk,body.title,body.tool,canonical(stored_args),digest,op_hash(body.tool,stored_args),status,expires_at)
            if forbidden:
                await append_event(con,body.thread_id,body.turn_id,'approval_dec','system',{'approval_id':str(row['id']),'decision':'rejected','remember':False,'client':'system','reason':forbidden})
            if status == 'pending':
                if body.turn_id:
                    await con.execute("update bothub.turns set status='waiting_approval' where id=$1 and status='running'",body.turn_id)
                    await con.execute("update bothub.bots set status='waiting' where id=$1 and status<>'no_model'",bot['id'])
                    await append_event(con,body.thread_id,body.turn_id,'status','system',{'turn_id':str(body.turn_id),'status':'waiting_approval'})
                await append_event(con,body.thread_id,body.turn_id,'approval_req',f"bot:{who['bot_id']}",{'approval_id':str(row['id']),'risk':risk,'title':body.title,'tool':body.tool,'expires_at':row['expires_at'].isoformat()})
                await outbox(con,f'approval:{row["id"]}',{'approval_id':str(row['id'])})
            return data(row)

    @app.post('/api/approvals/{id}/decide')
    async def decide(id: uuid.UUID, body: DecisionIn, request: Request):
        who = await principal(request, owner=True)
        if body.decision not in ('approve','reject'): error('invalid')
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow('select a.* from bothub.approvals a join bothub.threads th on th.id=a.thread_id where a.id=$1 and th.owner_id=$2 for update of a',id,who['user_id'])
                if not row: error('not_found',404)
                if row['status'] != 'pending': error('conflict',409)
                if row['expires_at'] <= datetime.now(NOW):
                    await con.execute("update bothub.approvals set status='expired',decided_at=now() where id=$1",id)
                    expired = True
                else:
                    expired = False
                if expired:
                    updated = await con.fetchrow('select * from bothub.approvals where id=$1',id)
                else:
                    # Правило запоминается с точным сравнением всех аргументов; pay/delete/login,
                    # установка ПО, отправка наружу и всё непонятное не запоминаются никогда.
                    # Правило несёт хэш операции (tool + все args) и не срабатывает на других аргументах
                    rule = remember_rule(row['tool'], row['args']) if body.remember and body.decision == 'approve' else None
                    remember = rule is not None
                    updated = await con.fetchrow('update bothub.approvals set status=$2,remember=$3,decided_at=now(),decided_from=$4 where id=$1 returning *',id,'approved' if body.decision=='approve' else 'rejected',remember,body.client)
                    if rule:
                        await con.execute('update bothub.bots set auto_allow=auto_allow || $2::jsonb where id=$1',row['bot_id'],canonical([rule]))
            await append_event(con,row['thread_id'],row['turn_id'],'approval_dec','owner' if not expired else 'system',{'approval_id':str(id),'decision':body.decision if not expired else 'expired','remember':remember if not expired else False,'client':body.client if not expired else 'system'})
            if expired:
                await fail_for_expired_approval(row)  # пункт 10: истёкший approval закрывает turn, а не возобновляет
            elif row['turn_id']:
                # Пункт 5: возврат в running проходит ту же проверку лимита, что claim_turn. Нет слота:
                # turn остаётся в waiting_approval, а /wait придерживает ответ раннеру (см. wait_approval).
                await resume_turn(con, row['turn_id'], 'waiting_approval')
            return data(updated)

    @app.get('/api/approvals/{id}/wait')
    async def wait_approval(id: uuid.UUID, request: Request, timeout: int = 25):
        who = await principal(request,bot=True)
        if not who['kind']=='bot': error('forbidden',403)
        deadline = asyncio.get_running_loop().time()+min(max(timeout,0),30)
        while True:
            async with app.state.pool.acquire() as con:
                row = await con.fetchrow('select a.* from bothub.approvals a join bothub.bots b on b.id=a.bot_id join bothub.threads th on th.id=a.thread_id where a.id=$1 and b.owner_id=$2 and th.owner_id=$2',id,who['user_id'])
                if not row: error('not_found',404)
                if row['bot_id'] != who['bot_id']: error('not_found',404)
                await ping_turn(con,row['turn_id'])  # пункт 9: бот опрашивает approval, значит раннер жив
                if row['status']=='pending' and row['expires_at'] <= datetime.now(NOW):
                    if await con.fetchval("update bothub.approvals set status='expired',decided_at=now() where id=$1 and status='pending' returning id",id):
                        await append_event(con,row['thread_id'],row['turn_id'],'approval_dec','system',{'approval_id':str(id),'decision':'expired','remember':False,'client':'system'})
                        await fail_for_expired_approval(row)  # пункт 10
                    row = await con.fetchrow('select a.* from bothub.approvals a join bothub.bots b on b.id=a.bot_id join bothub.threads th on th.id=a.thread_id where a.id=$1 and b.owner_id=$2 and th.owner_id=$2',id,who['user_id'])
                late = asyncio.get_running_loop().time()>=deadline
                if row['status']=='pending':
                    if late: return data(row)
                elif not row['turn_id'] or await resume_turn(con,row['turn_id'],'waiting_approval'):
                    return data(row)
                elif late:
                    # Пункт 5: решение принято, но слота под turn нет. Раннер ждёт, пока слот освободится:
                    # его цикл опроса повторяет /wait, пока статус pending.
                    return data(row) | {'status':'pending','queued':True}
            await asyncio.sleep(.2)

    @app.get('/api/memory')
    async def memory(request: Request, bot_id: str | None = None, status: str | None = None, q: str | None = None):
        who = await principal(request,bot=True)
        escaped_q = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') if q is not None else None
        async with app.state.pool.acquire() as con:
            if who['kind']=='bot':
                return [data(r) for r in await con.fetch("select * from bothub.memory where owner_id=$2 and status='active' and (bot_id is null or bot_id=$1) and (expires_at is null or expires_at>now()) and ($3::text is null or text ilike ('%' || $3 || '%') escape '\\') order by created_at desc, id desc",who['bot_id'],who['user_id'],escaped_q)]
            return [data(r) for r in await con.fetch("select * from bothub.memory where owner_id=$3 and ($1::text is null or bot_id=$1) and ($2::text is null or status=$2) and ($4::text is null or text ilike ('%' || $4 || '%') escape '\\') order by created_at desc, id desc",bot_id,status,who['user_id'],escaped_q)]

    @app.post('/api/memory')
    async def add_memory(body: MemoryIn, request: Request):
        who = await principal(request,bot=True)
        if who['kind']=='bot' and body.bot_id not in (None,who['bot_id']): error('not_found',404)
        async with app.state.pool.acquire() as con:
            if body.bot_id and not await con.fetchval('select 1 from bothub.bots where id=$1 and owner_id=$2',body.bot_id,who['user_id']): error('not_found',404)
            actor = f"bot:{who['bot_id']}" if who['kind']=='bot' else 'owner'
            return data(await con.fetchrow('insert into bothub.memory(text,bot_id,source,status,expires_at,owner_id) values($1,$2,$3,$4,$5,$6) returning *',body.text,body.bot_id or (who['bot_id'] if who['kind']=='bot' else None),actor,'proposed' if who['kind']=='bot' else 'active',body.expires_at,who['user_id']))

    @app.patch('/api/memory/{id}')
    async def edit_memory(id: uuid.UUID, request: Request, body: JsonObject):
        who = await principal(request,owner=True)
        if not body or set(body)-{'text','status','bot_id','expires_at'}: error('invalid')
        if 'text' in body and (not isinstance(body['text'],str) or not body['text'].strip() or not auth.is_clean_text(body['text']) or len(body['text'].encode())>16*1024): error('invalid',400,'text')
        if 'status' in body and body['status'] not in ('proposed','active','archived'): error('invalid',400,'status')
        if 'bot_id' in body and body['bot_id'] is not None and (not isinstance(body['bot_id'],str) or not body['bot_id']): error('invalid',400,'bot_id')
        expires_at = None
        if 'expires_at' in body:
            exp = body['expires_at']
            if exp is not None:
                if not isinstance(exp, str): error('invalid',400,'expires_at')
                try: expires_at = client_datetime(datetime.fromisoformat(exp.replace('Z', '+00:00')))
                except (ValueError, TypeError): error('invalid',400,'expires_at')
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                if 'bot_id' in body and body['bot_id']:
                    if not await con.fetchval('select 1 from bothub.bots where id=$1 and owner_id=$2',body['bot_id'],who['user_id']): error('not_found',404)
                # for update: два параллельных PATCH не затирают поля друг друга значениями из устаревшего чтения
                prior = await con.fetchrow('select * from bothub.memory where id=$1 and owner_id=$2 for update',id,who['user_id'])
                if not prior: error('not_found',404)
                new_text = body.get('text', prior['text'])
                new_status = body.get('status', prior['status'])
                new_bot_id = body['bot_id'] if 'bot_id' in body else prior['bot_id']
                new_expires = expires_at if 'expires_at' in body else prior['expires_at']
                version_inc = 1 if ('text' in body and new_text != prior['text']) else 0
                row = await con.fetchrow(
                    'update bothub.memory set text=$2,status=$3,bot_id=$4,expires_at=$5,version=version+$6 where id=$1 and owner_id=$7 returning *',
                    id, new_text, new_status, new_bot_id, new_expires, version_inc, who['user_id'])
                return data(row)

    @app.delete('/api/memory/{id}')
    async def delete_memory(id: uuid.UUID, request: Request):
        who = await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            deleted = await con.fetchval('delete from bothub.memory where id=$1 and owner_id=$2 returning id',id,who['user_id'])
            if not deleted: error('not_found',404)
            return {'ok':True}

    @app.get('/api/usage/summary')
    async def usage_summary(request: Request):
        who = await principal(request,owner=True)
        raw_days = request.query_params.get('days')
        if raw_days is None:
            days = 7
        else:
            try:
                days = int(raw_days)
                if days < 1 or days > 90 or str(days) != raw_days.strip(): error('invalid',422,'days')
            except (ValueError,TypeError):
                error('invalid',422,'days')
        async with app.state.pool.acquire() as con:
            bot_rows = await con.fetch(
                "select b.id bot_id,b.budget_daily_tokens budget,"
                "coalesce(sum(u.tokens_in::bigint+u.tokens_out::bigint+u.tokens_cache_read::bigint+u.tokens_cache_write::bigint) filter(where u.ts>=date_trunc('day',now())),0)::bigint tokens_today,"
                "coalesce(sum(u.tokens_in::bigint+u.tokens_out::bigint+u.tokens_cache_read::bigint+u.tokens_cache_write::bigint) filter(where u.ts>=date_trunc('day',now()-($2::int-1)*interval '1 day')),0)::bigint tokens_period,"
                "max(u.ts) last_activity "
                "from bothub.bots b left join bothub.usage u on u.bot_id=b.id where b.owner_id=$1 group by b.id,b.budget_daily_tokens order by b.created_at,b.id",
                who['user_id'],days)
            model_rows = await con.fetch(
                "select u.provider,u.model,"
                "coalesce(sum(u.tokens_in::bigint),0)::bigint tokens_in,"
                "coalesce(sum(u.tokens_out::bigint),0)::bigint tokens_out,"
                "coalesce(sum(u.tokens_cache_read::bigint),0)::bigint tokens_cache_read,"
                "coalesce(sum(u.tokens_cache_write::bigint),0)::bigint tokens_cache_write,"
                "coalesce(sum(u.tokens_in::bigint+u.tokens_out::bigint+u.tokens_cache_read::bigint+u.tokens_cache_write::bigint),0)::bigint total_tokens,"
                "count(distinct coalesce(u.turn_id::text,'u:'||u.id::text))::int turns "
                "from bothub.usage u join bothub.bots b on b.id=u.bot_id "
                "where b.owner_id=$1 and u.ts>=date_trunc('day',now()-($2::int-1)*interval '1 day') "
                "group by u.provider,u.model order by total_tokens desc,u.provider,u.model",
                who['user_id'],days)
            daily_rows = await con.fetch(
                "select to_char(d,'YYYY-MM-DD') date,"
                "coalesce(sum(x.tokens_in::bigint+x.tokens_out::bigint+x.tokens_cache_read::bigint+x.tokens_cache_write::bigint),0)::bigint total_tokens "
                "from generate_series(date_trunc('day',now()-($2::int-1)*interval '1 day'),date_trunc('day',now()),interval '1 day') d "
                "left join (select u.ts,u.tokens_in,u.tokens_out,u.tokens_cache_read,u.tokens_cache_write "
                "from bothub.usage u join bothub.bots b on b.id=u.bot_id "
                "where b.owner_id=$1 and u.ts>=date_trunc('day',now()-($2::int-1)*interval '1 day')) x on date_trunc('day',x.ts)=d "
                "group by d order by d",
                who['user_id'],days)
            providers = await con.fetch('select distinct provider from bothub.bots where owner_id=$1 order by provider',who['user_id'])
            total_tokens = sum(r['total_tokens'] for r in daily_rows)
            return {
                'days': days,
                'total_tokens': total_tokens,
                'bots': [data(r) for r in bot_rows],
                'models': [data(r) for r in model_rows],
                'daily': [data(r) for r in daily_rows],
                'providers': [{'provider':r['provider'],'pct_week':None,'reset_at':None} for r in providers]
            }

    @app.post('/api/usage')
    async def add_usage(body: UsageIn, request: Request):
        who = await principal(request,bot=True)
        if not who['kind']=='bot': error('forbidden',403)
        async with app.state.pool.acquire() as con:
            await own_thread(con,body.thread_id,who)
            if body.turn_id and not await con.fetchval('select 1 from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and t.thread_id=$2 and th.owner_id=$3',body.turn_id,body.thread_id,who['user_id']): error('not_found',404)
            await ping_turn(con,body.turn_id)
            # Пункт 12: бюджет проверяется на каждом usage; превышение закрывает turn (budget_exceeded)
            _, row = await handle_usage_event(con,who['bot_id'],body.thread_id,body.turn_id,body.provider,body.model,body.tokens_in,body.tokens_out)
            return data(row)

    @app.get('/api/schedules')
    async def schedules(request: Request):
        who = await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            return [data(r) for r in await con.fetch('select * from bothub.schedules where owner_id=$1 order by created_at',who['user_id'])]

    @app.post('/api/schedules')
    async def add_schedule(body: ScheduleIn, request: Request):
        who = await principal(request,owner=True)
        if body.kind not in ('cron','hook','mac_folder') or (body.kind=='cron' and not body.cron): error('invalid')
        try: next_at = next_run(body.cron,body.timezone) if body.kind=='cron' else None
        except (ValueError,KeyError): error('invalid')
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('insert into bothub.schedules(bot_id,name,kind,cron,timezone,prompt,hook_token,enabled,next_run_at,owner_id,catch_up) select id,$2,$3,$4,$5,$6,$7,$8,$9,owner_id,$11 from bothub.bots where id=$1 and owner_id=$10 returning *',body.bot_id,body.name,body.kind,body.cron,body.timezone,body.prompt,secrets.token_urlsafe(32) if body.kind=='hook' else None,body.enabled,next_at,who['user_id'],body.catch_up)
            if not row: error('not_found',404)
            return data(row)

    @app.patch('/api/schedules/{id}')
    async def edit_schedule(id: uuid.UUID, request: Request, body: JsonObject):
        who = await principal(request,owner=True)
        if not body or set(body)-{'enabled','cron','prompt','catch_up'}: error('invalid')
        if 'catch_up' in body and not isinstance(body['catch_up'],bool): error('invalid',400,'catch_up')
        if 'prompt' in body and (not isinstance(body['prompt'],str) or len(body['prompt'])>512): error('invalid',400,'prompt')
        async with app.state.pool.acquire() as con:
            prior = await con.fetchrow('select * from bothub.schedules where id=$1 and owner_id=$2',id,who['user_id'])
            if not prior: error('not_found',404)
            cron = body.get('cron',prior['cron'])
            try: next_at = next_run(cron,prior['timezone']) if prior['kind']=='cron' else None
            except (ValueError,KeyError): error('invalid')
            return data(await con.fetchrow('update bothub.schedules set enabled=coalesce($2,enabled),cron=coalesce($3,cron),prompt=coalesce($4,prompt),next_run_at=$5,catch_up=coalesce($7,catch_up) where id=$1 and owner_id=$6 returning *',id,body.get('enabled'),body.get('cron'),body.get('prompt'),next_at,who['user_id'],body.get('catch_up')))

    @app.post('/api/schedules/{id}/run')
    async def run_now(id: uuid.UUID, request: Request):
        who = await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            schedule = await con.fetchrow('select * from bothub.schedules where id=$1 and owner_id=$2',id,who['user_id'])
            if not schedule: error('not_found',404)
            if await con.fetchval('select paused from bothub.bots where id=$1',schedule['bot_id']): error('bot_paused',409)
            return await run_schedule(con,schedule)

    # --- Процедуры (раздел 14): CRUD, из действий бота, импорт и экспорт, чтение запусков. Воспроизведение: следующая часть.
    PROCEDURE_STATUSES = ('draft', 'active', 'archived')
    PROCEDURE_RUNS_PER_HOUR = 20  # запусков процедур в час на пользователя: страницы реальные, ошибка не должна множиться
    PROCEDURE_ACTIVE_RUNS = ('queued', 'running', 'waiting_approval', 'waiting_model', 'waiting_human')
    FROM_TURN_EVENTS_MAX = 5000

    def procedure_checked(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except procedures.ProcedureError as exc:
            error('invalid', 422, str(exc))

    def procedure_view(row, last_run):
        """Процедура для ответа: у каждого шага `computed_risk` (нижняя граница выбора риска), `last_run` последний запуск
        или null. Ни то ни другое в базе не хранится и в экспорт не попадает."""
        item = data(row)
        item['steps'] = jsonable_encoder(procedures.with_computed_risk(row['steps']))
        item['last_run'] = None if not last_run else jsonable_encoder(
            {key: last_run[key] for key in ('id', 'status', 'started_at', 'finished_at', 'error')})
        return item

    async def procedure_views(con, rows):
        rows = list(rows)
        last = {}
        if rows:
            for run in await con.fetch('select distinct on (procedure_id) procedure_id, id, status, started_at, finished_at, error '
                                       'from bothub.procedure_runs where procedure_id = any($1::uuid[]) '
                                       'order by procedure_id, created_at desc, id desc', [row['id'] for row in rows]):
                last[run['procedure_id']] = run
        return [procedure_view(row, last.get(row['id'])) for row in rows]

    async def own_procedure(con, id, who, *, lock=False):
        row = await con.fetchrow('select * from bothub.procedures where id=$1 and owner_id=$2' + (' for update' if lock else ''),
                                 id, who['user_id'])
        if not row:
            error('not_found', 404)
        return row

    async def own_procedure_run(con, id, who):
        row = await con.fetchrow('select r.* from bothub.procedure_runs r join bothub.procedures p on p.id=r.procedure_id '
                                 'where r.id=$1 and p.owner_id=$2', id, who['user_id'])
        if not row:
            error('not_found', 404)
        return row

    async def own_procedure_bot(con, bot_id, who):
        if bot_id is not None and not await con.fetchval('select 1 from bothub.bots where id=$1 and owner_id=$2', bot_id, who['user_id']):
            error('not_found', 404)

    async def insert_procedure(con, who, bot_id, name, description, params, steps, source):
        try:
            return procedure_view(await con.fetchrow(
                'insert into bothub.procedures(owner_id,bot_id,name,description,params,steps,source,status) '
                'values($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8) returning *',
                who['user_id'], bot_id, name, description, canonical(params), canonical(steps), source,
                procedures.status_for(steps)), None)
        except asyncpg.UniqueViolationError:
            error('conflict', 409, 'name is already used')

    @app.get('/api/procedures')
    async def list_procedures(request: Request, bot_id: str | None = None, status: str | None = None):
        who = await principal(request, owner=True)
        if status is not None and status not in PROCEDURE_STATUSES:
            error('invalid', 400, 'status')
        async with app.state.pool.acquire() as con:
            return await procedure_views(con, await con.fetch(
                'select * from bothub.procedures where owner_id=$1 and ($2::text is null or bot_id=$2) '
                'and ($3::text is null or status=$3) order by updated_at desc, created_at desc', who['user_id'], bot_id, status))

    @app.post('/api/procedures', status_code=201)
    async def create_procedure(body: ProcedureIn, request: Request):
        who = await principal(request, owner=True)
        name = procedure_checked(procedures.check_name, body.name)
        description = procedure_checked(procedures.check_description, body.description)
        params, steps = procedure_checked(procedures.normalize_procedure, body.params, body.steps)
        async with app.state.pool.acquire() as con:
            await own_procedure_bot(con, body.bot_id, who)
            return await insert_procedure(con, who, body.bot_id, name, description, params, steps, 'human')

    @app.post('/api/procedures/from-turn', status_code=201)
    async def procedure_from_turn(body: ProcedureFromTurnIn, request: Request):
        who = await principal(request, owner=True)
        name = procedure_checked(procedures.check_name, body.name)
        async with app.state.pool.acquire() as con:
            thread = await own_thread(con, body.thread_id, who)
            if body.turn_id is not None and not await con.fetchval(
                    'select 1 from bothub.turns where id=$1 and thread_id=$2', body.turn_id, body.thread_id):
                error('not_found', 404)
            rows = await con.fetch("select payload from bothub.events where thread_id=$1 and kind='browser_step' "
                                   "and ($2::uuid is null or turn_id=$2) order by seq limit $3",
                                   body.thread_id, body.turn_id, FROM_TURN_EVENTS_MAX + 1)
            if len(rows) > FROM_TURN_EVENTS_MAX:
                error('invalid', 422, 'steps: too_many: too many recorded events, pass turn_id')
            steps = procedure_checked(procedures.events_to_steps, [row['payload'] for row in rows])
            if not steps:
                error('invalid', 422, 'steps: empty: no recorded browser steps')
            params, steps = procedure_checked(procedures.normalize_procedure, [], steps, strict_risk=False)
            return await insert_procedure(con, who, thread['bot_id'], name, '', params, steps, 'bot')

    @app.post('/api/procedures/import', status_code=201)
    async def import_procedure(body: ProcedureImportIn, request: Request):
        who = await principal(request, owner=True)
        doc = procedure_checked(procedures.parse_import, body.model_dump(exclude_unset=True))
        params, steps = procedure_checked(procedures.normalize_procedure, doc['params'], doc['steps'], strict_risk=False)
        async with app.state.pool.acquire() as con:
            return await insert_procedure(con, who, None, doc['name'], doc['description'], params, steps, 'import')

    @app.get('/api/procedures/{id}')
    async def get_procedure(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            return (await procedure_views(con, [await own_procedure(con, id, who)]))[0]

    @app.get('/api/procedures/{id}/export')
    async def export_procedure(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            return procedures.export_document(await own_procedure(con, id, who))

    @app.patch('/api/procedures/{id}')
    async def edit_procedure(id: uuid.UUID, body: ProcedurePatch, request: Request):
        who = await principal(request, owner=True)
        sent = body.model_fields_set
        if not sent:
            error('invalid', 400, 'nothing to change')
        if any(field in sent and getattr(body, field) is None for field in ('name', 'description', 'params', 'steps', 'status')):
            error('invalid', 400, 'null is not allowed')
        name = procedure_checked(procedures.check_name, body.name) if 'name' in sent else None
        description = procedure_checked(procedures.check_description, body.description) if 'description' in sent else None
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                row = await own_procedure(con, id, who, lock=True)
                if 'bot_id' in sent:
                    await own_procedure_bot(con, body.bot_id, who)
                params, steps = row['params'], row['steps']
                if 'params' in sent or 'steps' in sent:
                    params, steps = procedure_checked(procedures.normalize_procedure,
                                                      body.params if 'params' in sent else row['params'],
                                                      body.steps if 'steps' in sent else row['steps'])
                changed = canonical(params) != canonical(row['params']) or canonical(steps) != canonical(row['steps'])
                status = row['status']
                if body.status == 'active' and procedures.status_for(steps) == 'draft':
                    error('invalid', 422, 'status: steps_need_values: steps still need values')
                if body.status is not None:
                    status = body.status
                elif status != 'archived':
                    status = procedures.status_for(steps)
                try:
                    updated = await con.fetchrow(
                        'update bothub.procedures set name=$2,description=$3,bot_id=$4,params=$5::jsonb,steps=$6::jsonb,status=$7,'
                        'version=version+$8,updated_at=now() where id=$1 returning *',
                        id, name if name is not None else row['name'],
                        description if description is not None else row['description'],
                        body.bot_id if 'bot_id' in sent else row['bot_id'], canonical(params), canonical(steps), status,
                        1 if changed else 0)
                except asyncpg.UniqueViolationError:
                    error('conflict', 409, 'name is already used')
            return (await procedure_views(con, [updated]))[0]

    @app.delete('/api/procedures/{id}')
    async def delete_procedure(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            async with con.transaction():
                await own_procedure(con, id, who, lock=True)
                if await con.fetchval('select exists(select 1 from bothub.procedure_runs where procedure_id=$1 and status=any($2::text[]))',
                                      id, list(PROCEDURE_ACTIVE_RUNS)):
                    error('conflict', 409, 'procedure has unfinished runs')
                await con.execute('delete from bothub.procedures where id=$1', id)  # запуски уходят каскадом
        return {'ok': True}

    @app.get('/api/procedures/{id}/runs')
    async def procedure_runs(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            await own_procedure(con, id, who)
            return [data(r) for r in await con.fetch('select * from bothub.procedure_runs where procedure_id=$1 '
                                                     'order by created_at desc, id desc limit 100', id)]

    @app.get('/api/procedure-runs/{id}')
    async def get_procedure_run(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            return data(await own_procedure_run(con, id, who))

    # --- Воспроизведение (раздел 14): исполнитель в procedure_runner.py, маршруты тонкие ---
    async def release_browser(bot_id, owner_id):
        """Человек вернул управление, исполнитель только что прочитал страницу шага: `returning -> bot` (раздел 13). Сбой не
        останавливает запуск: состояние остаётся `returning`, следующий проход попробует снова."""
        try:
            await browser_transition(bot_id, {'kind': 'bot', 'bot_id': bot_id, 'user_id': owner_id}, 'snapshot', 'procedure_run')
        except Exception as exc:
            log.warning('procedure_release_browser_failed', extra={'bot_id': bot_id, 'error': type(exc).__name__})

    procedure_store = PgStore(lambda: app.state.pool, SimpleNamespace(append_event=append_event, outbox=outbox))
    procedure_runner = ProcedureRunner(
        procedure_store, lambda: app.state.launcher if app.state.launcher_ready else None, release_browser=release_browser)
    app.state.procedure_runner = procedure_runner
    runner_state = {'recovered': False}

    async def procedure_runner_step():
        runner = app.state.procedure_runner
        if not runner_state['recovered']:  # шаги с запущенным действием до рестарта: повтор или решение владельца
            await runner.recover()
            runner_state['recovered'] = True
        return await runner.tick()

    async def procedure_runner_loop():
        try:
            await supervise('procedure_runner', procedure_runner_step, env_float('BOTHUB_PROCEDURE_INTERVAL', 1),
                            env_float('BOTHUB_LOOP_ERROR_DELAY', 1))
        finally:
            await app.state.procedure_runner.shutdown()

    app.state.procedure_runner_loop = procedure_runner_loop

    @app.post('/api/procedures/{id}/run', status_code=201)
    async def run_procedure(id: uuid.UUID, body: ProcedureRunIn, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            row = await own_procedure(con, id, who)
        if row['status'] != 'active':
            error('not_runnable', 409, row['status'])
        # раздел 16: на паузе бота запуск процедуры отклоняется (409 bot_paused). Проверка внутри транзакции создания запуска
        # (PgStore.create_run, под блокировкой строки бота), здесь её нет: пауза между проверкой и вставкой не проскочит.
        if body.bot_id or row['bot_id']:
            require_launcher()  # запуск без бота (no_bot) контейнера не требует
        rate_limit((str(who['user_id']), 'procedure_run'), maximum=PROCEDURE_RUNS_PER_HOUR, window=3600)
        try:
            run = await app.state.procedure_runner.create_run(who['user_id'], id, body.bot_id, body.thread_id, body.params)
        except RunError as exc:
            error(exc.code, exc.status, exc.detail)
        return data(run)

    @app.post('/api/procedure-runs/{id}/stop')
    async def stop_procedure_run(id: uuid.UUID, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            await own_procedure_run(con, id, who)
        try:
            return data(await app.state.procedure_runner.stop_run(who['user_id'], id))
        except RunError as exc:
            error(exc.code, exc.status, exc.detail)

    @app.post('/api/procedure-runs/{id}/decide')
    async def decide_procedure_run(id: uuid.UUID, body: ProcedureDecideIn, request: Request):
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            await own_procedure_run(con, id, who)
        try:
            return data(await app.state.procedure_runner.decide_run(who['user_id'], id, body.action))
        except RunError as exc:
            error(exc.code, exc.status, exc.detail)

    @app.get('/api/secrets')
    async def list_secrets(request: Request):
        """Имена сохранённых секретов пользователя для выбора `vault:<имя>`: значений и шифртекста в ответе нет."""
        who = await principal(request, owner=True)
        async with app.state.pool.acquire() as con:
            rows = await con.fetch('select name, bot_id from bothub.secrets where owner_id=$1 order by bot_id nulls first, name',
                                   who['user_id'])
        return [{'name': row['name'], 'bot_id': row['bot_id']} for row in rows]

    @app.post('/hooks/{id}',status_code=202)
    async def hook(id: uuid.UUID, request: Request, token: str = ''):
        header_token = request.headers.get('x-hook-token')
        if header_token is None and token:
            log.warning('deprecated_query_token', extra={'path':request.url.path})
        token = header_token if header_token is not None else token
        raw = await request.body()
        if len(raw)>65536: error('invalid',413)
        try:
            text = raw.decode()  # utf-16/32 пропустил бы json.loads(bytes), а разбор ниже ждёт utf-8
            payload = json.loads(text)
        except ValueError: error('invalid')
        async with app.state.pool.acquire() as con:
            schedule = await con.fetchrow("select s.* from bothub.schedules s join bothub.users u on u.id=s.owner_id where s.id=$1 and s.kind='hook' and s.enabled and u.status='active'",id)
            if not schedule: error('not_found',404)
            if not token_equals(token,schedule['hook_token']): error('forbidden',403)
            # Раздел 16: ответ всегда 202 {"status":"accepted"}, без id turn: по форме и телу ответа отправитель не узнаёт, создан ли turn
            # или запуск пропущен (пауза, исполнитель, провайдер). Проверка и постановка в очередь (или запись пропуска) идут до ответа
            # в обоих случаях, поэтому и по времени ответа разница небольшая. Сбой самой проверки считается пропуском check_failed.
            try:
                reason = await trigger_block(con,schedule['bot_id'])
            except Exception:
                log.exception('trigger_block_failed',extra={'bot_id':schedule['bot_id'],'schedule_id':str(id)})
                reason = 'check_failed'
            if not reason:
                try:
                    await require_available_bot(con,schedule['bot_id'])
                except HTTPException as exc:  # бот сломался между проверкой и постановкой: тоже пропуск, не 409 отправителю
                    if exc.status_code != 409: raise
                    reason = 'provider_unavailable' if (exc.detail or {}).get('error') == 'no_model' else 'executor_unavailable'
            if reason:
                async with con.transaction():
                    locked = await con.fetchrow('select * from bothub.schedules where id=$1 for update',id)
                    events = []
                    await record_skip(con,locked,reason,events)
                for event in events: publish(event)
                return {'status':'accepted'}
            thread_id = await con.fetchval("insert into bothub.threads(bot_id,kind,title,owner_id) values($1,'routine',$2,$3) returning id",schedule['bot_id'],schedule['name'],schedule['owner_id'])
            prompt = schedule['prompt'] + '\n\nДанные события:\n```json\n' + text + '\n```'
            turn = await create_turn(con,thread_id,prompt,'hook')
            await con.execute('update bothub.schedules set last_turn_id=$2 where id=$1 and owner_id=$3',id,uuid.UUID(turn['id']),schedule['owner_id'])
            if schedule['skipped_count'] or schedule['paused_by_unavailable']:
                events = []
                async with con.transaction():
                    await record_resume(con,schedule,events)
                for event in events: publish(event)
            return {'status':'accepted'}

    @app.post('/api/files')
    async def upload_file(request: Request, file: UploadFile = File(...), thread_id: uuid.UUID = Form(...), origin: str = Form('upload')):
        who = await principal(request,bot=True,mac=True)
        async with app.state.pool.acquire() as con:
            await own_thread(con,thread_id,who)
            file_id = uuid.uuid4()
            root = Path(os.getenv('FILES_DIR','/data/files'))
            root.mkdir(parents=True,exist_ok=True)
            path = root/str(file_id)
            size = 0
            try:
                with path.open('wb') as output:
                    while chunk := await file.read(1024*1024):
                        size += len(chunk); output.write(chunk)
                row = await con.fetchrow('insert into bothub.files(id,thread_id,name,origin,size,mime,storage_path,owner_id) values($1,$2,$3,$4,$5,$6,$7,$8) returning *',file_id,thread_id,file.filename or 'file',origin,size,file.content_type or mimetypes.guess_type(file.filename or '')[0] or 'application/octet-stream',str(path),who['user_id'])
            except Exception:
                path.unlink(missing_ok=True); raise
            await append_event(con,thread_id,None,'file',f"bot:{who['bot_id']}" if who['kind']=='bot' else who['kind'],{'file_id':str(file_id),'name':row['name'],'size':size,'mime':row['mime'],'origin':origin})
            return data(row)

    @app.get('/api/files/{id}')
    async def download_file(id: uuid.UUID, request: Request):
        who = await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select * from bothub.files where id=$1 and owner_id=$2',id,who['user_id'])
            if not row: error('not_found',404)
            return FileResponse(row['storage_path'],media_type=row['mime'],filename=row['name'])

    @app.get('/api/mac/status')
    async def mac_status(request: Request):
        who = await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            row = await con.fetchrow('select * from bothub.mac_status where owner_id=$1 order by id limit 1',who['user_id'])
            if not row: return {'state':'offline','last_seen':None,'info':{}}
            result = data(row)
            if str(who['user_id']) not in mac_socket or not row['last_seen'] or row['last_seen']<datetime.now(NOW)-timedelta(seconds=90): result['state']='offline'
            return {k:result[k] for k in ('state','last_seen','info')}

    @app.websocket('/agent/mac')
    async def mac_agent(ws: WebSocket):
        token = ws_token(ws)
        origin=ws.headers.get('origin','')
        scheme=ws.headers.get('x-forwarded-proto') or ('https' if ws.url.scheme=='wss' else 'http')
        if origin and not (auth.same_origin(origin,ws.headers.get('host',''),scheme=scheme) or
                           not ws.headers.get('x-forwarded-proto') and ws.url.scheme=='ws' and
                           auth.same_origin(origin,ws.headers.get('host',''),scheme='https')):
            await ws.close(code=4401); return
        async with app.state.pool.acquire() as con:
            if await legacy_enabled(con) and token_equals(token,os.getenv('MAC_AGENT_TOKEN')):
                first=await setup_admin(con)
                user_id=first['id'] if first else None
            else:
                user_id=await con.fetchval("select m.user_id from bothub.mac_tokens m join bothub.users u on u.id=m.user_id where m.token_hash=$1 and m.revoked_at is null and u.status='active'",auth.token_hash(token))
        if not user_id:
            await ws.close(code=4401); return
        key=str(user_id)
        previous=mac_socket.get(key)
        if previous:
            with suppress(Exception): await previous.close(code=4401)
        await ws.accept()
        mac_socket[key]=ws; mac_ready.set()
        try:
            while True:
                msg=await ws.receive_json()
                async with app.state.pool.acquire() as con:
                    if msg.get('type')=='hello':
                        async with con.transaction():
                            await con.execute('select pg_advisory_xact_lock(hashtext($1))',key)
                            if not await con.fetchval('select 1 from bothub.mac_status where owner_id=$1',user_id):
                                await con.execute("insert into bothub.mac_status(state,last_seen,info,owner_id) values('online',now(),$1::jsonb,$2)",canonical(msg),user_id)
                            await con.execute("update bothub.mac_status set state='online',last_seen=now(),info=$2::jsonb where id=(select id from bothub.mac_status where owner_id=$1 order by id limit 1)",user_id,canonical(msg))
                    elif msg.get('type')=='heartbeat':
                        await con.execute("update bothub.mac_status set state=$2,last_seen=now(),info=info || $3::jsonb where id=(select id from bothub.mac_status where owner_id=$1 order by id limit 1)",user_id,'locked' if msg.get('locked') else 'online',canonical(msg))
                    elif msg.get('type')=='result':
                        future=pending_mac.get(key,{}).pop(msg.get('id'),None)
                        if future and not future.done(): future.set_result(msg)
        except WebSocketDisconnect:
            pass
        finally:
            if mac_socket.get(key) is ws:
                mac_socket.pop(key,None)
                if not mac_socket: mac_ready.clear()
                async with app.state.pool.acquire() as con:
                    await con.execute("update bothub.mac_status set state='offline' where owner_id=$1",user_id)
                for future in pending_mac.get(key,{}).values():
                    if not future.done(): future.set_exception(ConnectionError('Mac disconnected'))

    def mac_wait_timeout(timeout) -> float:
        # delegate шлёт до 1800+60 с, поэтому потолок 1900.
        return min(max(timeout or 60, 1), 1900)

    @app.post('/api/mac/call')
    async def mac_call(body: MacCallIn, request: Request):
        who=await principal(request,bot=True)
        if not who['kind']=='bot': error('forbidden',403)
        approval_id=None
        waited=False  # turn сейчас в waiting_mac (или уйдёт туда ниже): возврат в running идёт через resume_turn
        async def release_approval():
            if approval_id:
                async with app.state.pool.acquire() as con:
                    await con.execute('update bothub.approvals set used_at=null where id=$1',approval_id)
        async with app.state.pool.acquire() as con:
            thread = await own_thread(con,body.thread_id,who)
            turn = await con.fetchrow('select t.* from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1 and t.thread_id=$2 and th.owner_id=$3',body.turn_id,body.thread_id,who['user_id'])
            # Находка 2: turn обязан существовать, принадлежать треду и быть в
            # статусе running/waiting_mac - раньше проверялось только владение.
            if not turn: error('not_found',404)
            if turn['status'] not in ('running', 'waiting_mac'): error('forbidden',403)
            waited = turn['status']=='waiting_mac'
            bot = await con.fetchrow('select * from bothub.bots where id=$1 and owner_id=$2',thread['bot_id'],who['user_id'])
            await ping_turn(con,body.turn_id)
            # Имя для auto_allow/риска - как видит инструмент Claude (раздел 4/5).
            mcp_tool = f"mcp__bothub__mac_{body.tool}"
            if body.tool != 'delegate' and bot['executor'] != 'mac' and not bot['mac_full_control']:
                error('forbidden',403)
            if body.tool in MAC_RISKY_TOOLS or body.tool == 'delegate':
                digest = hashlib.sha256(canonical(body.args).encode()).hexdigest()
                # Пункт 6: одобрение действует только на ту же операцию (op_hash = tool + args);
                # строки без op_hash (до миграции 003) сверяются по tool и args_hash, как раньше.
                # Пункт 7: одобрение расходуется одним UPDATE (used_at), повтор требует нового. Одобрение
                # с «запомнить» не расходуется: правило и так разрешает ту же операцию.
                approval_id = await con.fetchval(
                    "update bothub.approvals set used_at=case when remember then used_at else now() end "
                    "where id=(select id from bothub.approvals where turn_id=$1 and status='approved' and used_at is null and expires_at>now() "
                    "and (op_hash=$2 or (op_hash is null and tool=$3 and args_hash=$4)) order by remember, created_at limit 1 for update skip locked) "
                    "returning id",body.turn_id,op_hash(mcp_tool,body.args),mcp_tool,digest)
                if not approval_id:
                    _, allowed = decide_permission(dict(bot), mcp_tool, body.args, dict(turn))
                    if not allowed: error('forbidden',403,'approval required')
            elif body.tool in MAC_READ_TOOLS:
                if not bot['mac_full_control']:
                    _, allowed = decide_permission(dict(bot), mcp_tool, body.args, dict(turn))
                    if not allowed: error('forbidden',403)
            else:
                error('invalid', 400, f'unknown mac tool: {body.tool}')
            state=await con.fetchval("select state from bothub.mac_status where owner_id=$1 and last_seen>now()-interval '90 seconds' order by id limit 1",who['user_id'])
        socket=mac_socket.get(str(who['user_id']))
        if not socket or state!='online':
            wol=os.getenv('MAC_WOL_CMD')
            if wol:
                waited=True
                async with app.state.pool.acquire() as con:
                    await con.execute("update bothub.turns set status='waiting_mac' where id=$1 and status='running'",body.turn_id)
                    await append_event(con,body.thread_id,body.turn_id,'status','system',{'turn_id':str(body.turn_id),'status':'waiting_mac'})
                await asyncio.to_thread(subprocess.run,wol,shell=True,timeout=10,check=False)
                deadline=asyncio.get_running_loop().time()+30
                while asyncio.get_running_loop().time()<deadline:
                    async with app.state.pool.acquire() as con:
                        state=await con.fetchval("select state from bothub.mac_status where owner_id=$1 and last_seen>now()-interval '90 seconds' order by id limit 1",who['user_id'])
                    socket=mac_socket.get(str(who['user_id']))
                    if socket and state=='online': break
                    await asyncio.sleep(.2)
            if not socket or state!='online':
                await release_approval()  # вызов до Mac не дошёл: одобрение не потрачено, раннер повторит
                if waited:
                    async with app.state.pool.acquire() as con:
                        await resume_turn(con,body.turn_id,'waiting_mac')  # как и раньше: после неудачного WOL turn снова running (под лимитом)
                return JSONResponse({'error':'mac_unavailable','detail':'Mac agent offline','state':state or 'offline'},status_code=409)
        # Пункт 5: из waiting_mac (в том числе после WOL) в running только под лимитом параллельных turn.
        # Слота нет: turn остаётся в waiting_mac, раннер повторит вызов (409, как при недоступном Mac).
        if waited:
            async with app.state.pool.acquire() as con:
                if not await resume_turn(con,body.turn_id,'waiting_mac'):
                    await release_approval()
                    return JSONResponse({'error':'queued','detail':'лимит параллельных turn, ждём свободный слот','state':state or 'offline'},status_code=409)
        call_id=str(uuid.uuid4())
        future=asyncio.get_running_loop().create_future()
        pending_mac.setdefault(str(who['user_id']),{})[call_id]=future
        try:
            try:
                await socket.send_json({'type':'call','id':call_id,'tool':body.tool,'args':body.args,'timeout':body.timeout})
            except ConnectionError:
                await release_approval()  # не отправлено: одобрение не потрачено
                raise
            return await asyncio.wait_for(future, mac_wait_timeout(body.timeout))
        except (asyncio.TimeoutError,ConnectionError):
            return JSONResponse({'error':'mac_unavailable','detail':'Mac agent unavailable','state':'offline'},status_code=409)
        finally:
            pending_mac.get(str(who['user_id']),{}).pop(call_id,None)

    @app.post('/api/push/subscribe')
    async def subscribe(body: PushIn, request: Request):
        who = await principal(request,owner=True)
        async with app.state.pool.acquire() as con:
            row=await con.fetchrow('insert into bothub.push_subscriptions(endpoint,keys,device,owner_id) values($1,$2::jsonb,$3,$4) on conflict(endpoint) do update set keys=$2::jsonb,device=$3 where bothub.push_subscriptions.owner_id=$4 returning *',body.endpoint,canonical(body.keys),body.device,who['user_id'])
            if not row: error('not_found',404)
            return data(row)

    # Ядро отдаёт PWA: смонтировано последним, чтобы /api, /agent, /hooks матчились раньше.
    pwa_dir = Path(os.getenv('PWA_DIR') or (Path(__file__).resolve().parents[2] / 'pwa'))
    if pwa_dir.is_dir():
        app.mount('/', StaticFiles(directory=pwa_dir, html=True), name='pwa')
        app.routes[-1].path = '/'  # Starlette normalizes the root mount path to ''.

    return app
