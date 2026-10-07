"""Подсказки: строгий JSON и список разрешённых инструментов без БД."""
import pytest

from bothub import suggestions
from bothub.risk import READ_ONLY_TOOLS

pytestmark = pytest.mark.pure


def test_valid_suggestions_and_limits():
    assert suggestions.parse_suggestions('{"suggestions":[" проверить отчёт ","ответить клиенту"]}') == [
        'проверить отчёт', 'ответить клиенту']
    assert suggestions.parse_suggestions('{"suggestions":[]}') == []
    assert suggestions.parse_suggestions('{"suggestions":["a","b","c","d"]}') == []


@pytest.mark.parametrize('response', [
    'текст {"suggestions":["a"]}', '{"suggestions":["a"]} мусор',
    '{"suggestions":["a"],"suggestions":["b"]}',
    '{"suggestions":[{"text":"a"}]}', '{"suggestions":[" "]}',
    '{"suggestions":["a"],"extra":1}', '{"suggestions":"a"}',
    '{"suggestions":["a\\u0000b"]}',
])
def test_invalid_output_is_ignored(response):
    assert suggestions.parse_suggestions(response) == []


def test_oversized_output_is_ignored():
    assert suggestions.parse_suggestions(' ' * 7001 + '{"suggestions":[]}') == []


def test_only_safe_subset_of_risk_read_only_tools_is_allowed():
    for tool in ('Read', 'Glob', 'Grep', 'WebSearch'):
        assert suggestions.read_tool_allowed(tool, READ_ONLY_TOOLS)
    for tool in ('Write', 'Edit', 'Bash', 'TodoWrite', 'mcp__bothub__remember',
                 'mcp__bothub__schedule_wakeup', 'mcp__bothub__attach_file', 'mcp__other__read'):
        assert not suggestions.read_tool_allowed(tool, READ_ONLY_TOOLS)
