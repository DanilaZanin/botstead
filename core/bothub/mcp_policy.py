"""Список разрешённых MCP-инструментов бота (bots.mcp_allow). Контракт: docs/contracts.md, раздел 4.

Пустой список: у бота только собственный сервер `bothub`. Чужой MCP-инструмент проходит, если его имя подходит
под элемент списка; прохождение списка не авто-разрешает вызов, одобрение владельца работает как раньше.

Имя инструмента канонически `mcp__<сервер>__<инструмент>` (так его видит Claude CLI); у codex в событиях
`<сервер>.<инструмент>`, оно приводится к тому же виду. Элемент списка: точное имя, glob (`mcp__github__*`),
`<сервер>.<инструмент>` или голое имя сервера (весь сервер).

Закрыто по умолчанию: имя, которое похоже на MCP, но не разобралось (`mcp__github`, `mcp_tool_call`, `a.b c`),
считается чужим и не проходит ни по какому списку. Свой сервер только ровно `bothub`: `bothub_`, `Bothub`,
`bothub_x` и имя `mcp__bothub___x` (разбирается как сервер `bothub_`) чужие.
"""
import re
from fnmatch import fnmatchcase

NOT_ALLOWED = 'mcp_not_allowed'
OWN_SERVER = 'bothub'

_PREFIX = 'mcp__'
_BARE_MCP_TYPE = 'mcp_tool_call'  # так codex называет вызов, если в событии нет server и tool
_DOTTED_RE = re.compile(r'^([\w-]+)\.([\w.-]+)$')
_BARE_SERVER_RE = re.compile(r'^[\w-]+$')


UNSUPPORTED = 'mcp_allow_unsupported'


class McpAllowUnsupported(RuntimeError):
    """У бота непустой mcp_allow, а раннер его не применяет (agy): ход не запускается, причина mcp_allow_unsupported."""

    def __init__(self, provider: str):
        super().__init__(f'{UNSUPPORTED}: {provider}')
        self.provider = provider


class McpNotAllowed(RuntimeError):
    """Раннер увидел вызов чужого MCP-инструмента вне списка: ход останавливается с причиной mcp_not_allowed."""

    def __init__(self, tool: str):
        super().__init__(f'{NOT_ALLOWED}: {tool}')
        self.tool = tool


def _split(tool: str) -> tuple[str, str] | None:
    """(сервер, инструмент) из `mcp__сервер__инструмент` или None. Граница ставится после первого `__`, а лишние
    `_` отходят серверу: `mcp__bothub___x` это сервер `bothub_`, а не свой `bothub` с инструментом `_x`."""
    rest = tool[len(_PREFIX):]
    cut = rest.find('__')
    if cut < 1:
        return None
    while rest[cut + 2:cut + 3] == '_':
        cut += 1
    server, name = rest[:cut], rest[cut + 2:]
    return (server, name) if name else None


def looks_like_mcp(tool) -> bool:
    """Имя похоже на MCP-инструмент: префикс `mcp__`, голый `mcp_tool_call` или вид `сервер.инструмент` codex.
    У встроенных инструментов CLI (Bash, Read, WebSearch, command_execution) нет ни точки, ни этого префикса."""
    return isinstance(tool, str) and (tool.startswith(_PREFIX) or tool == _BARE_MCP_TYPE or '.' in tool)


def canonical_tool(tool) -> str | None:
    """`mcp__сервер__инструмент` или None: не MCP-инструмент либо имя не разобралось (см. looks_like_mcp)."""
    if not isinstance(tool, str):
        return None
    if tool.startswith(_PREFIX):
        parts = _split(tool)
        return f'{_PREFIX}{parts[0]}__{parts[1]}' if parts else None
    dotted = _DOTTED_RE.match(tool)
    return f'{_PREFIX}{dotted[1]}__{dotted[2]}' if dotted else None


def server_of(tool) -> str | None:
    name = canonical_tool(tool)
    return _split(name)[0] if name else None


def is_foreign_mcp(tool) -> bool:
    """MCP-инструмент не из сервера bothub: на него действует mcp_allow. Неразобранное MCP-подобное имя тоже чужое."""
    if not looks_like_mcp(tool):
        return False
    name = canonical_tool(tool)
    if not name:
        return True
    server, own_tool = _split(name)
    # Свой только `mcp__bothub__<инструмент>` без `__` внутри: иначе сервер с именем `bothub__gh` (codex такие
    # принимает) выдавал бы свои инструменты за встроенные (`mcp__bothub__gh__delete_repo`).
    return server != OWN_SERVER or '__' in own_tool


def _matchers(item) -> list[tuple[str, str] | str]:
    """Элемент списка как пара масок (сервер, инструмент) или, для странных масок вроде `*`, как маска всего имени.
    Сервер и инструмент сравниваются по отдельности: маска по склеенной строке `mcp__bothub__*` подошла бы к
    `mcp__bothub___x` (сервер `bothub_`)."""
    if not isinstance(item, str) or not item.strip():
        return []
    item = item.strip()
    if item.startswith(_PREFIX):
        return [_split(item) or (item[len(_PREFIX):], '*')]  # `mcp__git*` без `__`: все серверы по маске
    dotted = _DOTTED_RE.match(item)
    if dotted:
        return [(dotted[1], dotted[2])]
    if _BARE_SERVER_RE.match(item):
        return [(item, '*')]
    return [item]


def mcp_allowed(allow, tool) -> bool:
    """False для чужого MCP-инструмента вне списка и для неразобранного MCP-подобного имени. Свои и не MCP-инструменты проходят."""
    if not is_foreign_mcp(tool):
        return True
    name = canonical_tool(tool)
    if name is None:
        return False  # не разобралось: ни один список это имя не открывает
    server, tool_name = _split(name)
    for item in (allow if isinstance(allow, (list, tuple)) else ()):
        for matcher in _matchers(item):
            if isinstance(matcher, tuple):
                if fnmatchcase(server, matcher[0]) and fnmatchcase(tool_name, matcher[1]):
                    return True
            elif fnmatchcase(name, matcher):
                return True
    return False
