"""Разбор ответа служебного хода с подсказками."""
import json

PROMPT = ('Посмотри последние события ленты и свою память. Предложи до 3 полезных действий владельцу, '
          'ничего не выполняй. Ответь только JSON: {"suggestions":["текст действия"]}.')
MAX_TEXT = 2000
MAX_RESPONSE = 7000
READ_TOOLS = frozenset({'Read', 'Glob', 'Grep', 'WebSearch'})


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def parse_suggestions(response: str) -> list[str]:
    """Строгое тело JSON; неверный ответ целиком игнорируется."""
    if not isinstance(response, str) or len(response) > MAX_RESPONSE:
        return []
    try:
        value = json.loads(response, object_pairs_hook=_unique_object)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, dict) or set(value) != {'suggestions'}:
        return []
    items = value['suggestions']
    if not isinstance(items, list) or len(items) > 3:
        return []
    if any(not isinstance(item, str) or not item.strip() or len(item) > MAX_TEXT or '\x00' in item for item in items):
        return []
    return [item.strip() for item in items]


def read_tool_allowed(tool: str, known_tools) -> bool:
    """Разрешаем только действительно читающую часть READ_ONLY_TOOLS."""
    return tool in READ_TOOLS and tool in known_tools
