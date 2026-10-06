"""Unit tests for bothub.main.decide_permission (находки 2/4/5).

Чистая функция, без БД: risk = classify(tool, args) на сервере, auto_allow
через fnmatch, mac_full_control только для mac_*-инструментов с risk='other',
pay/delete/login никогда не авто-разрешаются.
"""
from bothub.main import decide_permission
from bothub.risk import op_hash


def _bot(**over):
    return {"auto_allow": [], "mac_full_control": False} | over


def _pin(tool, match):
    """Правило как после «запомнить»: точный match плюс op_hash (без него правило с match не действует)."""
    return {"tool": tool, "match": match, "op_hash": op_hash(tool, match)}


def test_pay_delete_login_never_allowed_even_with_matching_auto_allow():
    bot = _bot(auto_allow=[{"tool": "*", "match": {}}])
    for tool in ("checkout", "mac_move_to_trash", "login_user"):
        risk, allowed = decide_permission(bot, tool, {})
        assert risk in ("pay", "delete", "login")
        assert allowed is False


def test_mac_full_control_allows_only_other_risk_mac_tools():
    bot = _bot(mac_full_control=True)
    risk, allowed = decide_permission(bot, "mcp__bothub__mac_screenshot", {})
    assert (risk, allowed) == ("other", True)

    # mac_full_control не обходит риск delete (раздел 4: pay/delete/login всегда pending).
    risk, allowed = decide_permission(bot, "mcp__bothub__mac_move_to_trash", {"path": "/x"})
    assert (risk, allowed) == ("delete", False)


def test_mac_full_control_does_not_affect_non_mac_tools():
    bot = _bot(mac_full_control=True)
    risk, allowed = decide_permission(bot, "read_file", {"path": "/x"})
    assert (risk, allowed) == ("other", False)


def test_auto_allow_tool_fnmatch_and_exact_args():
    # glob остался только в имени инструмента и только для читающих; аргументы сравниваются точно
    bot = _bot(auto_allow=[{"tool": "mcp__bothub__mac_find_*", "match": {"path": "/safe/a"},
                           "op_hash": op_hash("mcp__bothub__mac_find_files", {"path": "/safe/a"})}])
    assert decide_permission(bot, "mcp__bothub__mac_find_files", {"path": "/safe/a"}) == ("other", True)
    assert decide_permission(bot, "mcp__bothub__mac_find_files", {"path": "/safe/b"}) == ("other", False)
    assert decide_permission(bot, "mcp__bothub__mac_read_file", {"path": "/safe/a"}) == ("other", False)
    glob_rule = _bot(auto_allow=[{"tool": "mcp__bothub__mac_find_files", "match": {"path": "/safe/*"}}])
    assert decide_permission(glob_rule, "mcp__bothub__mac_find_files", {"path": "/safe/a"}) == ("other", False)


def test_unknown_tool_needs_exact_pinned_rule():
    bot = _bot(auto_allow=[_pin("read_file", {"path": "/safe/a"})])
    assert decide_permission(bot, "read_file", {"path": "/safe/a"}) == ("other", True)
    assert decide_permission(bot, "read_file", {"path": "/safe/b"}) == ("other", False)
    assert decide_permission(bot, "write_file", {"path": "/safe/a"}) == ("other", False)
    wildcard = _bot(auto_allow=[{"tool": "read_*", "match": {"path": "/safe/a"}, "op_hash": op_hash("read_file", {"path": "/safe/a"})}])
    assert decide_permission(wildcard, "read_file", {"path": "/safe/a"}) == ("other", False)


def test_turn_argument_is_accepted_but_not_required():
    bot = _bot()
    assert decide_permission(bot, "read_file", {}, turn=None) == ("other", False)
    assert decide_permission(bot, "read_file", {}, turn={"status": "running"}) == ("other", False)
