"""Поручения от бота боту (docs/contracts.md, раздел 19): чистая логика без БД.

Здесь: пределы (текст 1..4000 символов, не больше 5 активных поручений на бота-отправителя, глубина 1), текст сообщения
для получателя, обрезка результата и решение, годится ли получатель. Ядро только исполняет SQL и применяет решения,
MCP-инструмент `delegate_to_bot` проверяет вход теми же функциями до запроса.
"""
from __future__ import annotations

TASK_MAX = 4000
MAX_ACTIVE = 5
RESULT_MAX = 8000
CODES = ('delegation_sent', 'delegation_done')
CLIENT = 'delegate'
ACTIVE_STATUSES = ('queued', 'running', 'waiting_approval', 'waiting_mac')


class DelegationError(ValueError):
    """Отказ с машинным кодом (`code`) и статусом HTTP (`status`). В сообщении только код: значения от клиента не повторяются."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def clean_task(task) -> str:
    if not isinstance(task, str) or not task.strip():
        raise DelegationError('task_empty', 422)
    if len(task) > TASK_MAX:
        raise DelegationError('task_too_long', 422)
    return task


def clean_target(bot) -> str:
    if not isinstance(bot, str) or not bot.strip() or len(bot) > 200:
        raise DelegationError('bot_invalid', 422)
    return bot.strip()


def check_not_self(caller_id: str, target_id: str) -> None:
    if caller_id == target_id:
        raise DelegationError('self_delegation', 409)


def check_depth(caller_turn_is_delegated: bool) -> None:
    """Ход, созданный поручением, дальше не поручает: цепочки A -> B -> C не бывает."""
    if caller_turn_is_delegated:
        raise DelegationError('delegation_depth', 409)


def check_active_limit(active: int) -> None:
    if active >= MAX_ACTIVE:
        raise DelegationError('delegation_limit', 409)


def target_block(bot) -> str | None:
    """Почему получатель не годится: 'target_paused', 'target_no_model', 'target_error_starting' или None."""
    if bot['paused']:
        return 'target_paused'
    if bot['status'] == 'no_model' or (bot['registry_bound'] and bot['provider_id'] is None):
        return 'target_no_model'
    if bot['status'] == 'error_starting':
        return 'target_error_starting'
    return None


def message_text(from_name: str, task: str) -> str:
    return f'Поручение от бота {from_name}:\n\n{task}'


def thread_title(from_name: str) -> str:
    return f'Поручения от {from_name}'[:512]


def truncate_result(text: str) -> str:
    return text if len(text) <= RESULT_MAX else text[:RESULT_MAX]


def final_text(events, provider: str | None) -> str:
    """Ответ получателя: текст assistant_msg после последнего вызова инструмента хода (промежуточное «сейчас проверю» не
    берётся). Если после инструментов текста нет, весь текст хода. events: (kind, text) по возрастанию seq."""
    from .context import join_assistant
    tail: list[str] = []
    every: list[str] = []
    for kind, text in events:
        if kind in ('tool_call', 'tool_result'):
            tail = []
        elif kind == 'assistant_msg' and text:
            tail.append(text)
            every.append(text)
    return truncate_result(join_assistant(tail or every, provider))
