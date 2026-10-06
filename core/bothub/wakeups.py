"""Самопробуждение бота (docs/contracts.md, раздел 17): чистая логика без БД.

Здесь: разбор и проверка срока (`at` или `in_minutes`), пределы (1 минута – 30 дней, не больше 20 активных, prompt до 2000
символов), окно идемпотентности ±1 минута и решение планировщика, что делать с созревшим пробуждением. Ядро только исполняет SQL
и применяет решения, MCP-инструмент `schedule_wakeup` проверяет вход теми же функциями до запроса.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

MIN_DELAY = timedelta(minutes=1)
MAX_DELAY = timedelta(days=30)
MAX_IN_MINUTES = int(MAX_DELAY.total_seconds() // 60)
MAX_ACTIVE = 20
PROMPT_MAX = 2000
REASON_MAX = 200
AT_MAX = 64
DEDUP_WINDOW = timedelta(minutes=1)
# Бот на паузе пропускает пробуждение сразу (пауза это явное решение владельца). Недоступный исполнитель (компьютер, модель,
# сбой проверки) пробуждение ждёт столько после срока: контейнер перезапускается минуту, а не сутки.
GRACE = timedelta(minutes=15)
CODES = ('wakeup_scheduled', 'wakeup_fired', 'wakeup_skipped')
STATUSES = ('active', 'fired', 'skipped')


class WakeupError(ValueError):
    """Отказ с машинным кодом (`code`) и статусом HTTP (`status`). В сообщении только код: значения от клиента не повторяются."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # как остальные даты от клиента: без пояса это UTC
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise WakeupError('at_invalid') from None


def parse_at(value) -> datetime:
    if not isinstance(value, str) or not value.strip() or len(value) > AT_MAX:
        raise WakeupError('at_invalid')
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00').replace('z', '+00:00'))
    except ValueError:
        raise WakeupError('at_invalid') from None
    return _utc(parsed)


def resolve_time(at, in_minutes, now: datetime) -> datetime:
    """Момент пробуждения в UTC. Ровно одно из `at` и `in_minutes`; от now + 1 минута до now + 30 дней включительно."""
    if at is None and in_minutes is None:
        raise WakeupError('time_required')
    if at is not None and in_minutes is not None:
        raise WakeupError('time_conflict')
    if in_minutes is not None:
        if isinstance(in_minutes, bool) or not isinstance(in_minutes, int):
            raise WakeupError('in_minutes_invalid')
        when = now + timedelta(minutes=in_minutes) if 0 < in_minutes <= MAX_IN_MINUTES else None
        if when is None:
            raise WakeupError('too_soon' if in_minutes < 1 else 'too_far')
        return when
    when = parse_at(at)
    if when < now + MIN_DELAY:
        raise WakeupError('too_soon')
    if when > now + MAX_DELAY:
        raise WakeupError('too_far')
    return when


def clean_prompt(prompt) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise WakeupError('prompt_empty')
    if len(prompt) > PROMPT_MAX:
        raise WakeupError('prompt_too_long', 422)
    return prompt


def clean_reason(reason) -> str:
    if reason is None:
        return ''
    if not isinstance(reason, str):
        raise WakeupError('reason_invalid')
    if len(reason) > REASON_MAX:
        raise WakeupError('reason_too_long', 422)
    return reason


def check_active_limit(active: int) -> None:
    if active >= MAX_ACTIVE:
        raise WakeupError('wakeup_limit', 409)


def is_duplicate(existing_at: datetime, existing_prompt: str, when: datetime, prompt: str) -> bool:
    """Повтор вызова (бот ретраит или модель зовёт инструмент дважды): тот же prompt и срок в пределах ±1 минуты."""
    return existing_prompt == prompt and abs(existing_at - when) <= DEDUP_WINDOW


def find_duplicate(rows: Iterable, when: datetime, prompt: str):
    """Первая активная строка (`scheduled_at`, `prompt`), которую считаем тем же пробуждением, или None."""
    for row in rows:
        if is_duplicate(row['scheduled_at'], row['prompt'], when, prompt):
            return row
    return None


def decide(block: str | None, scheduled_at: datetime, now: datetime) -> str:
    """Что делать с созревшим пробуждением. block: причина из trigger_block или None.
    'fire': создать turn. 'skip': пропустить (бот на паузе, либо исполнитель не появился за GRACE). 'wait': оставить active."""
    if block is None:
        return 'fire'
    if block == 'bot_paused':
        return 'skip'
    return 'skip' if now - scheduled_at >= GRACE else 'wait'


def fire_text(prompt: str, reason: str) -> str:
    """Текст turn: сначала кто разбудил и зачем, затем prompt, который бот оставил себе сам."""
    head = 'Самопробуждение: ты сам запланировал этот запуск.'
    if reason:
        head += f' Причина: {reason}'
    return f'{head}\n\n{prompt}'


def scheduled_text(when: datetime) -> str:
    return f'Бот запланировал пробуждение на {when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC.'


def skipped_text(block: str) -> str:
    cause = {'executor_unavailable': 'компьютер бота недоступен', 'bot_paused': 'бот на паузе',
             'provider_unavailable': 'модель бота недоступна',
             'check_failed': 'проверка исполнителя не удалась', 'fire_failed': 'не удалось запустить ход'}.get(block, 'исполнитель недоступен')
    return f'Пробуждение пропущено: {cause}.'
